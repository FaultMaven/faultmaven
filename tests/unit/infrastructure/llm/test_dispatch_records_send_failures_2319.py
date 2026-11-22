"""The dispatch records a failed send for the judged call bound around it (#2319).

``send_to_model`` is the one writer: when ``_generate`` raises, or is cancelled
while it runs, it records the send's key (the provider's label and the model
the send targets, ``label/model``) and the exception for whatever collector is
bound (``turn_budget.collect_model_failures``) and re-raises unchanged. The
binder reduces what it collected to spent deadlines per key
(``llm_error_handler.deadlines_spent_into``, ``spent_a_deadline``) and keeps no
exception. Every test here observes through a binding; none patches the writer.
"""

import asyncio

import pytest

from faultmaven.core.investigation.llm_error_handler import (
    deadlines_spent_into,
    spent_a_deadline,
)
from faultmaven.core.investigation.turn_budget import collect_model_failures
from faultmaven.exceptions import (
    ExternalCallTimeout,
    LLMErrorCategory,
    LLMException,
    RawModelContentRefused,
)
from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from tests.model_content import vetted
from tests.unit.architecture.test_model_dispatch_lexical_1998 import dispatch_names

pytestmark = [pytest.mark.unit, pytest.mark.llm]


class _Hangs:
    LABEL = "hangs"

    async def _generate(self, **_):
        await asyncio.sleep(3600)


class _Fails:
    LABEL = "fails"

    def __init__(self, error):
        self.error = error

    async def _generate(self, **_):
        raise self.error


async def test_a_send_cut_inside_wait_for_is_visible_to_the_binder():
    """The router's ceiling is a ``wait_for``: the send runs in its child task,
    on a copy of the context that holds the binder's one dict."""
    failures = {}
    with collect_model_failures(failures):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(send_to_model(_Hangs(), prompt=vetted("p")), 0.01)

    assert list(failures) == ["hangs"]
    assert [type(e) for e in failures["hangs"]] == [asyncio.CancelledError]


async def test_a_failure_is_recorded_with_its_key_and_re_raised_unchanged():
    error = LLMException("upstream 503", status_code=503)
    failures = {}
    with collect_model_failures(failures):
        with pytest.raises(LLMException) as raised:
            await send_to_model(_Fails(error), prompt=vetted("p"))

    assert raised.value is error
    assert failures == {"fails": [error]}


async def test_a_send_from_a_task_started_before_the_binding_records_nothing():
    """The out-of-band triage is a task the turn starts before any judged call
    binds a collector, and it sends while one is bound in the parent. Its task
    runs on the context it was created with, so the binding never sees its
    send: it records nowhere."""
    error = LLMException("upstream 503", status_code=503)
    go = asyncio.Event()

    async def triage():
        await go.wait()
        await send_to_model(_Fails(error), prompt=vetted("p"))

    task = asyncio.create_task(triage())
    failures = {}
    with collect_model_failures(failures):
        go.set()
        with pytest.raises(LLMException) as raised:
            await task

    assert raised.value is error
    assert failures == {}


class _Configured:
    """An adapter as the registry builds one: its default model on its
    ``config``."""

    LABEL = "gemini"

    def __init__(self, default_model, error):
        self.config = ProviderConfig(name="gemini", default_model=default_model)
        self.error = error

    @property
    def provider_name(self):  # pragma: no cover - must never be read
        raise AssertionError("the dispatch read a property")

    async def _generate(self, **_):
        raise self.error


@pytest.mark.parametrize(
    "default_model,sent_model,key",
    [
        ("gemini-flash", None, "gemini/gemini-flash"),
        ("gemini-flash", "gemini-flash", "gemini/gemini-flash"),
        ("gemini-flash", "gemini-pro", "gemini/gemini-pro"),
        (None, None, "gemini"),
    ],
    ids=["default", "named-default", "named-other", "label-only"],
)
async def test_a_failure_is_keyed_by_the_provider_configuration(
    default_model, sent_model, key
):
    """The label and the model the send targets: the ``model`` it names, else
    the instance's configured default, read without running a property. A
    routed send (no model) and a send naming the default share a key; one
    vendor on another model is another key."""
    error = LLMException("timed out", status_code=504)
    failures = {}
    model = {} if sent_model is None else {"model": sent_model}
    with collect_model_failures(failures):
        with pytest.raises(LLMException):
            await send_to_model(
                _Configured(default_model, error), prompt=vetted("p"), **model
            )

    assert failures == {key: [error]}


async def test_a_refused_call_records_nothing():
    """Refused before any ``_generate``: nothing was sent, so nothing failed."""
    failures = {}
    with collect_model_failures(failures):
        with pytest.raises(RawModelContentRefused):
            await send_to_model(_Fails(RuntimeError("unreached")), prompt="raw")

    assert failures == {}


def test_the_writer_is_a_name_the_lexical_guard_holds():
    """``record_model_failure`` is one of the names ``send_to_model`` reads,
    so rebinding it outside ``dispatch.py`` is flagged (#1998 A5)."""
    assert "record_model_failure" in dispatch_names()


@pytest.mark.parametrize(
    "failure,spent",
    [
        (asyncio.CancelledError(), True),
        (
            ExternalCallTimeout(
                "timed out", service="LLM_Providers", operation="x", timeout=30
            ),
            True,
        ),
        (LLMException("upstream request timed out", status_code=504), True),
        (LLMException("origin timeout", status_code=524), True),
        (
            LLMException(
                "upstream timed out: context length exceeded", status_code=504
            ),
            False,
        ),
        (
            LLMException(
                "cut", status_code=504, category=LLMErrorCategory.OUTPUT_TRUNCATION
            ),
            False,
        ),
        (LLMException("upstream 503", status_code=503), False),
        (LLMException("rate limited", status_code=429), False),
    ],
    ids=[
        "cancelled",
        "router-timeout",
        "504",
        "524",
        "overflow-504",
        "truncation-504",
        "503",
        "429",
    ],
)
def test_what_spends_a_deadline(failure, spent):
    """A cancellation, or a deadline-shaped failure that is not a verdict on
    the request (#2319 amendment 1)."""
    assert spent_a_deadline(failure) is spent


async def test_the_binder_counts_spent_deadlines_and_keeps_no_exception():
    counts = {}
    with pytest.raises(TimeoutError):
        with deadlines_spent_into(counts):
            await asyncio.gather(
                *(
                    _swallow(send_to_model(_Fails(error), prompt=vetted("p")))
                    for error in (
                        LLMException("timed out", status_code=504),
                        LLMException("upstream 503", status_code=503),
                    )
                )
            )
            await asyncio.wait_for(send_to_model(_Hangs(), prompt=vetted("p")), 0.01)

    assert counts == {"fails": 1, "hangs": 1}
    assert all(type(value) is int for value in counts.values())


async def _swallow(send):
    try:
        await send
    except LLMException:
        pass
