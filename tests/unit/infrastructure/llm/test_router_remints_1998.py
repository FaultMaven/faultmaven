"""A routed call's minted content reaches the provider still minted (#1998, A2).

``LLMRouter.route`` runs its own ``SANITIZE_PII`` pass over the prompt and the
messages. ``DataSanitizer`` returns plain ``str``/``dict`` values, so before
``sanitizer_pass`` re-minted them, every routed call would have reached the
model dispatch as raw content under ``SANITIZE_PII=true``, minted by its
caller or not. These tests drive the real router over a real registry and the
real dispatch — only the provider's ``_generate`` is a double — under both
settings of the flag, and read the dispatch's own verdict: no
``RawModelContentWarning``, no metric, and the values the dispatch was handed.

They also pin the other half of the pass: raw content is still sanitized (no
regression in what reaches a provider), and is still reported raw — once.

And the router's own check (A6): ``route()`` runs the dispatch's validator at
entry on the caller's values, without unwrapping them, so under refusal a raw
call stops before the ``SANITIZE_PII`` pass sees its content, and a minted one
still passes. The router itself is never a ``_generate`` target.
"""

from __future__ import annotations

import sys
import warnings
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from faultmaven.infrastructure.llm import dispatch
from faultmaven.infrastructure.llm.dispatch import SEND_SITE, RawModelContentWarning
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
from faultmaven.infrastructure.security.model_content import provenance
from tests.model_content import forged, vetted, vetted_messages
from tests.utils import regex_only_sanitizer, sanitize_pii_pinned

pytestmark = [pytest.mark.unit, pytest.mark.llm, pytest.mark.security]

PROVIDER = "stubprovider"
MODEL = "stub-model"
IP = "10.1.2.3"


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
def boundary(monkeypatch):
    """What the dispatch was handed, and what it reported.

    The spy replaces the dispatch module's own name, which ``send_to_model``
    reads; ``route()``'s entry check holds its own binding and is not seen.
    """
    seen: List[Dict[str, Any]] = []
    check = dispatch.model_call_violations

    def spy(prompt, messages, kwargs):
        seen.append(
            {"prompt": prompt, **({"messages": messages} if messages else {}), **kwargs}
        )
        return check(prompt, messages, kwargs)

    monkeypatch.setattr(dispatch, "model_call_violations", spy)
    with patch.object(dispatch, "llm_model_content_violations") as metric:
        yield seen, metric


def _minted_messages() -> List[Dict[str, Any]]:
    return vetted_messages(
        [
            {"role": "system", "content": "fixed instructions"},
            {"role": "user", "content": f"host {IP} is down"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search_file",
                            "arguments": f'{{"q": "{IP}"}}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "search_file",
                "content": f"{IP} refused",
            },
        ]
    )


def _raw_warnings(recorded) -> list:
    return [w for w in recorded if issubclass(w.category, RawModelContentWarning)]


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_a_minted_prompt_reaches_the_provider_minted(
    router, provider, boundary, sanitize_pii
):
    seen, metric = boundary
    with (
        sanitize_pii_pinned(sanitize_pii),
        warnings.catch_warnings(record=True) as recorded,
    ):
        warnings.simplefilter("always")
        await router.route(prompt=vetted(f"host {IP} is down"), max_tokens=50)

    assert not _raw_warnings(recorded)
    metric.labels.assert_not_called()
    [handed] = seen
    assert provenance(handed["prompt"]) is not None
    sent = provider.sent[0]["prompt"]
    assert type(sent) is str
    assert (IP in sent) is (not sanitize_pii)


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_minted_messages_reach_the_provider_minted(
    router, provider, boundary, sanitize_pii
):
    seen, metric = boundary
    with (
        sanitize_pii_pinned(sanitize_pii),
        warnings.catch_warnings(record=True) as recorded,
    ):
        warnings.simplefilter("always")
        await router.route(messages=_minted_messages(), max_tokens=50)

    assert not _raw_warnings(recorded)
    metric.labels.assert_not_called()
    [handed] = seen
    messages = handed["messages"]
    assert all(provenance(m["content"]) is not None for m in messages)
    arguments = messages[2]["tool_calls"][0]["function"]["arguments"]
    assert provenance(arguments)[0] == "model_output"
    if sanitize_pii:
        assert all(provenance(m["content"])[1] is True for m in messages)
    # The transport sees plain values, redacted or not per the flag.
    for message in provider.sent[0]["messages"]:
        assert type(message["content"]) is str
        assert (IP in message["content"]) is (
            not sanitize_pii and message["role"] in {"user", "tool"}
        )


def test_the_router_sends_through_route_alone():
    """No ``generate`` (A6): ``route()`` is the router's API. And no
    ``_generate``: the dispatch unwraps before it calls one, so a router behind
    it would see every minted routed call as raw."""
    from faultmaven.infrastructure.llm.router import LLMRouter
    from faultmaven.models.interfaces import ILLMProvider

    assert not hasattr(LLMRouter, "generate")
    assert not hasattr(LLMRouter, "_generate")
    assert not hasattr(ILLMProvider, "generate")
    assert "route" in ILLMProvider.__abstractmethods__


async def test_raw_content_is_still_sanitized_and_still_reported(
    router, provider, boundary
):
    seen, metric = boundary
    with sanitize_pii_pinned(True), pytest.warns(RawModelContentWarning):
        await router.route(
            messages=[{"role": "user", "content": f"host {IP} is down"}],
            max_tokens=50,
        )
    assert IP not in provider.sent[0]["messages"][0]["content"]
    [call] = metric.labels.call_args_list
    assert call.kwargs["argument"] == "messages.content"
    assert call.kwargs["kind"] == "raw"


async def test_what_the_router_keeps_is_plain_text(router, provider):
    """The cache, the telemetry and the response's fields never hold a ModelText."""
    with sanitize_pii_pinned(False):
        response = await router.route(
            prompt=vetted("cache me"), model=MODEL, max_tokens=50
        )
        assert type(response.sanitized_prompt) is str
        again = await router.route(
            prompt=vetted("cache me"), model=MODEL, max_tokens=50
        )
        assert len(provider.sent) == 1, "the second call is answered from the cache"
        assert type(again.sanitized_prompt) is str
        response = await router.route(messages=_minted_messages(), max_tokens=50)
    assert all(type(m["content"]) is str for m in response.sanitized_messages)


async def test_raw_telemetry_fields_are_plain_text(router, monkeypatch):
    """With raw-prompt logging on, ``raw_prompt``/``raw_messages`` are plain ``str``."""
    monkeypatch.setattr(router.settings.observability, "opik_log_raw_prompts", True)
    with sanitize_pii_pinned(False):
        first = await router.route(
            prompt=vetted("cache me"), model=MODEL, max_tokens=50
        )
        cached = await router.route(
            prompt=vetted("cache me"), model=MODEL, max_tokens=50
        )
        routed = await router.route(messages=_minted_messages(), max_tokens=50)
    assert type(first.raw_prompt) is str
    assert type(cached.raw_prompt) is str
    assert all(type(m["content"]) is str for m in routed.raw_messages)
    arguments = routed.raw_messages[2]["tool_calls"][0]["function"]["arguments"]
    assert type(arguments) is str


async def test_a_routed_call_names_the_line_that_called_the_router(router):
    """The registry reaches the boundary in a task of its own; the warning
    still names the caller of ``route``, from what the router recorded."""
    import inspect

    with sanitize_pii_pinned(False), pytest.warns(RawModelContentWarning) as recorded:
        line = inspect.currentframe().f_lineno + 1
        await router.route(prompt="raw text", max_tokens=50)
    [warning] = _raw_warnings(recorded)
    assert (warning.filename, warning.lineno) == (__file__, line)


async def test_a_send_its_caller_put_in_a_task_falls_back_to_the_dispatch(router):
    """The stated limit: ``asyncio.wait_for(router.route(...))`` on 3.11 runs
    ``route`` as a task's root, so no frame reaches the caller; the warning
    names the registry's dispatch, ``send_to_model``'s direct caller."""
    import asyncio

    with sanitize_pii_pinned(False), pytest.warns(RawModelContentWarning) as recorded:
        await asyncio.wait_for(router.route(prompt="raw text", max_tokens=50), 5)
    [warning] = _raw_warnings(recorded)
    if sys.version_info >= (3, 12):  # wait_for creates no task from 3.12
        assert warning.filename == __file__
    else:
        assert warning.filename.endswith(
            "faultmaven/infrastructure/llm/providers/registry.py"
        )


async def test_the_send_site_is_reset_when_route_returns(router):
    """The router's record lives for the call, not in the caller's context."""
    assert SEND_SITE.get() is None
    with sanitize_pii_pinned(False), pytest.warns(RawModelContentWarning):
        await router.route(prompt=vetted("x"), max_tokens=10)
        await router.route(prompt="raw", max_tokens=10)
    assert SEND_SITE.get() is None, "route() left its send site in the caller's context"


async def test_a_registry_awaited_inline_still_names_the_caller(router, monkeypatch):
    """With no timeout, call_external awaits the registry in the caller's own
    task: base_client.py is transport, never the send site."""
    import inspect

    monkeypatch.setattr(router, "_resolve_timeout", lambda: None)
    with sanitize_pii_pinned(False), pytest.warns(RawModelContentWarning) as recorded:
        line = inspect.currentframe().f_lineno + 1
        await router.route(prompt="raw text", max_tokens=50)
    [warning] = _raw_warnings(recorded)
    assert (warning.filename, warning.lineno) == (__file__, line)


# ---------------------------------------------------------------------------
# A6: route() checks at entry, without unwrapping, before the SANITIZE_PII pass
# ---------------------------------------------------------------------------

#: name -> route() keywords whose content the entry check refuses.
_UNMINTED = {
    "raw_prompt": {"prompt": f"host {IP} is down"},
    "raw_messages": {"messages": [{"role": "user", "content": f"host {IP}"}]},
    "forged_prompt": {"prompt": forged("x")},
    "unknown_message_key": {
        "messages": [{"role": "user", "content": vetted("x"), "case_id": "c"}]
    },
}


@pytest.fixture
def sanitizer_calls(router, monkeypatch):
    """Every value the router's SANITIZE_PII pass was asked to sanitize."""
    calls: List[Any] = []
    real = router.sanitizer.asanitize

    async def spy(value, *args, **kwargs):
        calls.append(value)
        return await real(value, *args, **kwargs)

    monkeypatch.setattr(router.sanitizer, "asanitize", spy)
    return calls


@pytest.mark.parametrize("case", sorted(_UNMINTED))
@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_refusal_stops_a_routed_call_before_the_sanitize_pass(
    router, provider, sanitizer_calls, monkeypatch, case, sanitize_pii
):
    monkeypatch.setattr(dispatch, "REFUSE_RAW_MODEL_CONTENT", True)
    with sanitize_pii_pinned(sanitize_pii), pytest.raises(TypeError) as raised:
        await router.route(max_tokens=50, **_UNMINTED[case])
    assert str(raised.value).startswith("router:"), "refused past route()'s entry"
    assert not sanitizer_calls, "the SANITIZE_PII pass saw content route() refuses"
    assert not provider.sent
    assert IP not in str(raised.value)


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_refusal_still_routes_minted_content(
    router, provider, monkeypatch, sanitize_pii
):
    """The entry check does not unwrap: a minted call passes it, the re-mint,
    and the dispatch, with refusal on, in both postures."""
    monkeypatch.setattr(dispatch, "REFUSE_RAW_MODEL_CONTENT", True)
    with sanitize_pii_pinned(sanitize_pii):
        await router.route(prompt=vetted(f"host {IP} is down"), max_tokens=50)
        await router.route(messages=_minted_messages(), max_tokens=50)
    assert len(provider.sent) == 2
