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

import socket
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
from faultmaven.infrastructure.security.model_content import plain, provenance
from tests.model_content import forged, vetted, vetted_messages
from tests.utils import regex_only_sanitizer, sanitize_pii_pinned, sockets_refused

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


@pytest.fixture(autouse=True)
def _no_network():
    """No connection leaves this file's tests, by any path (#1998): on a
    MonkeyPatch of its own, so a test's monkeypatch.undo() cannot lift it."""
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


#: The reasoning artifacts a tool loop echoes (#2023 review, I1). No address in
#: them: a pass that changed one would break Anthropic's signature.
_METADATA = {
    "assistant_content": [
        {"type": "thinking", "thinking": "consider it", "signature": "c2lnbmF0dXJl"},
        {"type": "tool_use", "id": "call_1", "name": "search_file", "input": {}},
    ]
}


def _metadata_strings(value: Any) -> List[Any]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in [k, *_metadata_strings(v)]]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _metadata_strings(v)]
    return []


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
                        # Gemini 2.5's per-call reasoning signature.
                        "provider_metadata": {"thought_signature": "CikBjz1rX"},
                    }
                ],
                # Anthropic's thinking blocks, echoed in place of the content
                # (I1: the SANITIZE_PII pass must keep them minted).
                "provider_metadata": _METADATA,
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
    # I1: the reasoning artifacts, on the message and on the call, are still
    # minted model output after the pass — every leaf and every key.
    metadata = _metadata_strings(messages[2]["provider_metadata"]) + _metadata_strings(
        messages[2]["tool_calls"][0]["provider_metadata"]
    )
    assert metadata and all(
        (provenance(value) or ("?",))[0] == "model_output" for value in metadata
    )
    if sanitize_pii:
        assert all(provenance(m["content"])[1] is True for m in messages)
    # The artifacts reach the transport as they came, plain.
    assert provider.sent[0]["messages"][2]["provider_metadata"] == _METADATA
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


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_raw_reasoning_artifacts_are_still_reported(
    router, boundary, sanitize_pii
):
    """I1's other half: the pass keeps the provenance it was handed, and mints
    none. Raw metadata — unchanged by the sanitizer — is still reported."""
    seen, metric = boundary
    messages = _minted_messages()
    messages[2]["provider_metadata"] = {"assistant_content": [{"text": "raw"}]}
    with sanitize_pii_pinned(sanitize_pii), pytest.warns(RawModelContentWarning):
        await router.route(messages=messages, max_tokens=50)
    assert {c.kwargs["argument"] for c in metric.labels.call_args_list} == {
        "messages.provider_metadata"
    }


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


# ---------------------------------------------------------------------------
# #2023 review: what the dispatch never sees, a refusal's cost, attribution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_a_dropped_raw_prompt_is_reported_once_in_warn_mode(
    router, provider, boundary, sanitize_pii
):
    """I5: the router drops a falsy prompt before the registry, so no dispatch
    sees it. ``route(prompt="", messages=minted)`` is what the engine's routed
    tool loop sends; refusal refuses it at entry, and warn mode reports it —
    here, once, naming the caller."""
    import inspect

    seen, metric = boundary
    with sanitize_pii_pinned(sanitize_pii), pytest.warns(RawModelContentWarning) as rec:
        line = inspect.currentframe().f_lineno + 1
        await router.route(prompt="", messages=_minted_messages(), max_tokens=50)
    [warning] = _raw_warnings(rec)
    assert "prompt raw" in str(warning.message)
    assert (warning.filename, warning.lineno) == (__file__, line)
    [label] = [c.kwargs for c in metric.labels.call_args_list]
    assert (label["provider"], label["argument"]) == ("router", "prompt")
    assert provider.sent, "the call was still sent (warn mode)"


async def test_a_cache_hit_on_raw_content_is_reported_in_warn_mode(router, provider):
    """I5: a call answered from the cache reaches no dispatch; the router
    reports what it would have refused."""
    with sanitize_pii_pinned(False), pytest.warns(RawModelContentWarning) as rec:
        await router.route(prompt="raw text", model=MODEL, max_tokens=50)
        await router.route(prompt="raw text", model=MODEL, max_tokens=50)
    assert len(provider.sent) == 1, "the second call is answered from the cache"
    assert len(_raw_warnings(rec)) == 2, "once by the dispatch, once on the cache hit"


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_a_minted_routed_call_is_reported_nowhere(router, provider, sanitize_pii):
    """No false report from the new paths: minted content, with and without a
    cache hit, and an empty message list, report nothing."""
    with (
        sanitize_pii_pinned(sanitize_pii),
        warnings.catch_warnings(record=True) as recorded,
    ):
        warnings.simplefilter("always")
        await router.route(prompt=vetted("cache me"), model=MODEL, max_tokens=50)
        await router.route(prompt=vetted("cache me"), model=MODEL, max_tokens=50)
        await router.route(prompt=vetted("x"), messages=[], max_tokens=50)
    assert not _raw_warnings(recorded)


async def test_refusals_leave_provider_health_and_the_breaker_untouched(
    router, provider, monkeypatch
):
    """O1: a refusal is the request's fault. Five refused calls — at the
    dispatch, inside the registry's fallback loop, and at the router's entry —
    record no provider failure and open no breaker, and the next minted call is
    routed."""
    from faultmaven.infrastructure.llm.providers.registry import (
        ProviderRegistry,
        ProviderState,
    )
    from tests.model_content import FakeProvider

    monkeypatch.setattr(dispatch, "REFUSE_RAW_MODEL_CONTENT", True)
    second = FakeProvider(name="second")
    registry = ProviderRegistry(settings=None)
    registry._providers = {PROVIDER: provider, "second": second}
    registry._fallback_chain = [PROVIDER, "second"]
    registry._provider_states = {
        PROVIDER: ProviderState(name=PROVIDER),
        "second": ProviderState(name="second"),
    }
    registry._initialized = True
    for _ in range(5):
        with pytest.raises(dispatch.RawModelContentRefused):
            await registry.route_request(prompt="raw 10.9.8.7", max_tokens=5)
        with pytest.raises(dispatch.RawModelContentRefused):
            await router.route(prompt="raw 10.9.8.7", max_tokens=5)
    states = {
        n: (s.health.value, s.consecutive_failures)
        for n, s in registry._provider_states.items()
    }
    assert all(failures == 0 for _, failures in states.values()), states
    assert registry._get_routing_order(), "a refusal emptied the routing order"
    assert not second.calls, "a refused call was retried on the next provider"

    # A refusal that happens INSIDE call_external — content minted at entry,
    # raw after the pass (a re-mint that lost provenance) — is not counted
    # against the router's breaker either (``retryable`` is False).
    async def losing_pass(value, sanitizer):
        return plain(value)

    with pytest.MonkeyPatch.context() as scoped:
        scoped.setattr(
            "faultmaven.infrastructure.llm.router.sanitizer_pass", losing_pass
        )
        scoped.setattr(
            "faultmaven.infrastructure.llm.router.get_registry", lambda: registry
        )
        for _ in range(5):
            with pytest.raises(dispatch.RawModelContentRefused):
                await router.route(prompt=vetted("x"), max_tokens=5)
    states = {
        n: (s.health.value, s.consecutive_failures)
        for n, s in registry._provider_states.items()
    }
    assert all(failures == 0 for _, failures in states.values()), states
    assert router.circuit_breaker is not None
    assert router.circuit_breaker.failure_count == 0, "a refusal tripped the breaker"
    assert not second.calls
    await router.route(prompt=vetted("ok"), max_tokens=5)
    assert provider.sent


async def test_a_route_override_is_not_the_send_site(router, provider):
    """O4: a subclass's ``route`` that only delegates (the cloud's tenant
    router) is a hop, never the caller: the warning names the line that called
    the override."""
    import inspect

    from faultmaven.infrastructure.llm.router import LLMRouter

    class _Tenant(LLMRouter):
        async def route(self, *args, **kwargs):
            return await super().route(*args, **kwargs)  # never the send site

    tenant = _Tenant.__new__(_Tenant)
    tenant.__dict__.update(router.__dict__)
    with sanitize_pii_pinned(False), pytest.warns(RawModelContentWarning) as rec:
        line = inspect.currentframe().f_lineno + 1
        await tenant.route(prompt="raw text", max_tokens=50)
    [warning] = _raw_warnings(rec)
    assert (warning.filename, warning.lineno) == (__file__, line)


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_dropped_falsy_messages_are_reported_once_in_warn_mode(
    router, provider, boundary, sanitize_pii, monkeypatch
):
    """η: ``messages=()`` is falsy, so the router drops it before the registry;
    no dispatch sees the malformed list. Warn mode reports it here, refusal
    refuses it at entry."""
    seen, metric = boundary
    with sanitize_pii_pinned(sanitize_pii), pytest.warns(RawModelContentWarning) as rec:
        await router.route(prompt=vetted("ok"), messages=(), max_tokens=50)
    [warning] = _raw_warnings(rec)
    assert "messages bad_shape" in str(warning.message)
    [label] = [c.kwargs for c in metric.labels.call_args_list]
    assert (label["provider"], label["argument"], label["kind"]) == (
        "router",
        "messages",
        "bad_shape",
    )
    monkeypatch.setattr(dispatch, "REFUSE_RAW_MODEL_CONTENT", True)
    with (
        sanitize_pii_pinned(sanitize_pii),
        pytest.raises(
            dispatch.RawModelContentRefused, match="^router: .*messages bad_shape"
        ),
    ):
        await router.route(prompt=vetted("ok"), messages=(), max_tokens=50)


async def test_a_refusal_books_no_request_error_and_no_sla_failure(
    router, provider, monkeypatch
):
    """D3: a refusal raised inside ``call_external`` (content minted at entry,
    raw after a pass that lost provenance) is not a failed provider request:
    no ``llm_requests{status="error"}``, no SLA failure, no "All providers
    failed" line."""
    from faultmaven.infrastructure.llm import router as router_module

    monkeypatch.setattr(dispatch, "REFUSE_RAW_MODEL_CONTENT", True)

    async def losing_pass(value, sanitizer):
        return plain(value)

    sla = []
    with (
        patch.object(router_module, "llm_requests") as requests,
        patch.object(
            router_module.sla_tracker,
            "record_request_metrics",
            side_effect=lambda *a, **k: sla.append((a, k)),
        ),
        patch.object(router.logger, "error") as logged,
    ):
        with pytest.MonkeyPatch.context() as scoped:
            scoped.setattr(
                "faultmaven.infrastructure.llm.router.sanitizer_pass", losing_pass
            )
            for _ in range(6):
                with pytest.raises(dispatch.RawModelContentRefused):
                    await router.route(prompt=vetted("x"), max_tokens=5)
        for _ in range(6):  # and at route()'s entry
            with pytest.raises(dispatch.RawModelContentRefused):
                await router.route(prompt="raw", max_tokens=5)
    assert not [
        c for c in requests.labels.call_args_list if c.kwargs.get("status") == "error"
    ]
    assert not [k for _, k in sla if k.get("success") is False]
    assert not [c for c in logged.call_args_list if "All providers failed" in str(c)]
    assert not provider.sent
