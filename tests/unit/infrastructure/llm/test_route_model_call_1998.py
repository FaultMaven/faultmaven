"""The routed seam, ``dispatch.route_model_call`` (#1998 P2).

Production code reaches a router only through the seam (rule S of
``test_model_dispatch_lexical_1998.py``). It checks the call's content before
any router runs, so a test whose router is a double still exercises the
check, at the line it drives (#2023 review, O3: 464 raw sends from 11 send
sites never met it while tests replaced the router).

Pinned here, over the real router, registry and dispatch (only the provider's
``_generate`` is a double) and over a router double:

* A1 — the seam is a plain function returning the coroutine, so the report
  names the CALLER's line, under ``asyncio.wait_for`` too (an ``async def``
  seam is the root of the task ``wait_for`` makes, and its report named the
  dispatch);
* A2 — a raw send is reported ONCE: the router's own reports (a dropped falsy
  value, a cache hit) and the dispatch report only what the seam did not.
  Refusal is never subtracted;
* A3 — the seam checks content only: the router's own knobs (``case_id``,
  ``bypass_cache``) are ``route()``'s closed signature, not provider keywords.
"""

from __future__ import annotations

import asyncio
import sys
import warnings
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.infrastructure.llm import dispatch
from faultmaven.infrastructure.llm.dispatch import (
    REPORTED_UPSTREAM,
    SEND_SITE,
    RawModelContentRefused,
    RawModelContentWarning,
    route_model_call,
    send_to_model,
)
from faultmaven.infrastructure.llm.providers.base import (
    BaseLLMProvider,
    LLMResponse,
    ProviderConfig,
    StopReason,
)
from faultmaven.infrastructure.llm.providers.registry import (
    ProviderRegistry,
    ProviderState,
)
from tests.model_content import fake_response, vetted, vetted_messages
from tests.utils import regex_only_sanitizer, sanitize_pii_pinned, sockets_refused

pytestmark = [pytest.mark.unit, pytest.mark.llm, pytest.mark.security]

PROVIDER = "stubprovider"
MODEL = "stub-model"
THIS_FILE = __file__


class _RecordingProvider(BaseLLMProvider):
    """A provider whose transport records what the dispatch handed it."""

    def __init__(self) -> None:
        super().__init__(
            ProviderConfig(
                name=PROVIDER, api_key="k", models=[MODEL], default_model=MODEL
            )
        )
        self.sent: List[Dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return PROVIDER

    async def _generate(self, prompt, model=None, max_tokens=1000, **kwargs):
        self.sent.append({"prompt": prompt, **kwargs})
        return LLMResponse(
            content="ok",
            confidence=0.99,
            provider=PROVIDER,
            model=MODEL,
            tokens_used=2,
            response_time_ms=1,
            stop_reason=StopReason.STOP,
        )

    def is_available(self) -> bool:
        return True

    def get_supported_models(self) -> List[str]:
        return [MODEL]


@pytest.fixture(autouse=True)
def _no_network():
    with sockets_refused():
        yield


@pytest.fixture
def provider() -> _RecordingProvider:
    return _RecordingProvider()


@pytest.fixture
def router(monkeypatch, provider):
    """The real router over a real registry holding the recording provider."""
    registry = ProviderRegistry(settings=None)
    registry._providers = {PROVIDER: provider}
    registry._fallback_chain = [PROVIDER]
    registry._provider_states = {PROVIDER: ProviderState(name=PROVIDER)}
    registry._initialized = True
    monkeypatch.setattr(
        "faultmaven.infrastructure.llm.router.get_registry", lambda: registry
    )
    monkeypatch.setattr(
        "faultmaven.infrastructure.llm.providers.registry.record_provider_call",
        lambda *a, **k: None,
    )
    from faultmaven.infrastructure.llm.router import LLMRouter

    router = LLMRouter()
    router.sanitizer = regex_only_sanitizer()
    return router


@pytest.fixture
def metric():
    with patch.object(dispatch, "llm_model_content_violations") as metric:
        yield metric


def _reports(recorded) -> List[tuple]:
    """``(file, line, message)`` of every RawModelContentWarning recorded."""
    return [
        (w.filename, w.lineno, str(w.message))
        for w in recorded
        if issubclass(w.category, RawModelContentWarning)
    ]


def _here() -> int:
    """The caller's NEXT line: where the call under test is written."""
    return sys._getframe(1).f_lineno + 1


# ---------------------------------------------------------------------------
# A1: the report names the caller's line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_a_raw_send_is_reported_once_at_the_line_that_made_it(
    router, provider, metric, sanitize_pii
):
    with (
        sanitize_pii_pinned(sanitize_pii),
        warnings.catch_warnings(record=True) as recorded,
    ):
        warnings.simplefilter("always")
        line = _here()
        await route_model_call(router, prompt="raw 10.1.2.3", max_tokens=5)
    assert _reports(recorded) == [
        (
            THIS_FILE,
            line,
            "model-bound content not minted by the redaction module: prompt raw",
        )
    ]
    [call] = metric.labels.call_args_list
    assert (call.kwargs["argument"], call.kwargs["kind"]) == ("prompt", "raw")
    assert len(provider.sent) == 1


async def test_under_wait_for_the_report_names_the_caller(router):
    """A1: ``wait_for`` runs the coroutine as the root of a task; the site was
    read before it, from the caller's frame."""
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        line = _here()
        await asyncio.wait_for(route_model_call(router, prompt="raw", max_tokens=5), 5)
    [(filename, lineno, _)] = _reports(recorded)
    assert (filename, lineno) == (THIS_FILE, line)


async def test_a_router_double_still_meets_the_check():
    """The point of the seam (O3): a test that replaces the router exercises
    the check, and the double receives the call's values unchanged."""
    double = AsyncMock()
    double.route = AsyncMock(return_value=fake_response())
    messages = [{"role": "user", "content": "raw"}]
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        line = _here()
        await route_model_call(double, messages=messages, max_tokens=5)
    [(filename, lineno, message)] = _reports(recorded)
    assert (filename, lineno) == (THIS_FILE, line)
    assert message.endswith("messages.content raw")
    double.route.assert_awaited_once_with(messages=messages, max_tokens=5)


async def test_minted_content_through_a_double_reports_nothing():
    double = AsyncMock()
    double.route = AsyncMock(return_value=fake_response())
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        await route_model_call(
            double,
            messages=vetted_messages([{"role": "user", "content": "minted"}]),
        )
        await route_model_call(double, prompt=vetted("minted"), model="m")
    assert _reports(recorded) == []


async def test_prompt_reaches_the_router_only_when_passed():
    """Omitting ``prompt`` omits it; ``prompt=None`` passes ``None``."""
    double = AsyncMock()
    double.route = AsyncMock(return_value=fake_response())
    messages = vetted_messages([{"role": "user", "content": "m"}])
    await route_model_call(double, messages=messages)
    assert "prompt" not in double.route.await_args.kwargs
    await route_model_call(double, prompt=None, messages=messages)
    assert double.route.await_args.kwargs["prompt"] is None


async def test_the_window_closes_with_the_call(router):
    """``SEND_SITE`` and ``REPORTED_UPSTREAM`` are set for the call only."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RawModelContentWarning)
        await route_model_call(router, prompt="raw", max_tokens=5)
    assert SEND_SITE.get() is None
    assert REPORTED_UPSTREAM.get() == frozenset()


# ---------------------------------------------------------------------------
# A2: reported once; refusal never subtracted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_raw_messages_are_reported_once_through_the_dispatch(
    router, metric, sanitize_pii
):
    """The dispatch sees the same raw messages after the router's pass, and
    reports nothing the seam already did."""
    with (
        sanitize_pii_pinned(sanitize_pii),
        warnings.catch_warnings(record=True) as recorded,
    ):
        warnings.simplefilter("always")
        await route_model_call(
            router,
            messages=[{"role": "user", "content": "host 10.1.2.3 is down"}],
            max_tokens=5,
        )
    assert len(_reports(recorded)) == 1
    assert metric.labels.call_count == 1


async def test_a_dropped_falsy_prompt_is_reported_once(router, metric):
    """``route()`` drops ``prompt=""`` before the registry and reports it; the
    seam already did."""
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        await route_model_call(
            router,
            prompt="",
            messages=vetted_messages([{"role": "user", "content": "m"}]),
            max_tokens=5,
        )
    [(_, _, message)] = _reports(recorded)
    assert message.endswith(": prompt raw")
    assert metric.labels.call_count == 1


async def test_a_cache_hit_is_reported_once(router, provider, metric):
    """A call the router answers from its cache reaches no dispatch; the
    router's own report of it is subtracted too."""
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        await route_model_call(router, prompt="cache me", model=MODEL, max_tokens=5)
        await route_model_call(router, prompt="cache me", model=MODEL, max_tokens=5)
    assert len(provider.sent) == 1, "the second call is answered from the cache"
    assert len(_reports(recorded)) == 2, "one report per call, not per layer"
    assert metric.labels.call_count == 2


async def test_a_new_violation_downstream_is_still_reported(provider):
    """Only the pairs the seam reported are subtracted: a different kind the
    dispatch finds is reported."""
    token = REPORTED_UPSTREAM.set(frozenset({("prompt", "raw")}))
    try:
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            await send_to_model(
                provider,
                prompt="raw",
                messages=[{"role": "user", "content": "raw too"}],
            )
    finally:
        REPORTED_UPSTREAM.reset(token)
    [(_, _, message)] = _reports(recorded)
    assert message.endswith(": messages.content raw")


async def test_refusal_is_refused_at_the_seam_before_any_router(monkeypatch):
    monkeypatch.setattr(dispatch, "REFUSE_RAW_MODEL_CONTENT", True)
    double = AsyncMock()
    with pytest.raises(RawModelContentRefused, match="prompt raw"):
        await route_model_call(double, prompt="raw")
    double.route.assert_not_called()


async def test_refusal_is_never_subtracted(monkeypatch, provider):
    """A raw call the seam reported is still refused downstream: the dispatch
    reads the reported pairs for its REPORT only."""
    monkeypatch.setattr(dispatch, "REFUSE_RAW_MODEL_CONTENT", True)
    token = REPORTED_UPSTREAM.set(frozenset({("prompt", "raw")}))
    try:
        with pytest.raises(RawModelContentRefused, match="prompt raw"):
            await send_to_model(provider, prompt="raw")
    finally:
        REPORTED_UPSTREAM.reset(token)
    assert provider.sent == []


# ---------------------------------------------------------------------------
# A3: content only
# ---------------------------------------------------------------------------


async def test_the_router_knobs_are_not_provider_keywords(router):
    """``case_id`` and ``bypass_cache`` are ``route()``'s own: the seam checks
    content, and a minted call carrying them reports nothing."""
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        await route_model_call(
            router,
            prompt=vetted("minted"),
            model=MODEL,
            max_tokens=5,
            case_id="case-1",
            bypass_cache=True,
        )
    assert _reports(recorded) == []
