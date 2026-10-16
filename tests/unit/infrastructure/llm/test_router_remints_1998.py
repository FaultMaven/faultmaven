"""A routed call's minted content reaches the provider still minted (#1998, A2).

``LLMRouter.route`` runs its own ``SANITIZE_PII`` pass over the prompt and the
messages. ``DataSanitizer`` returns plain ``str``/``dict`` values, so before
``sanitizer_pass`` re-minted them, every routed call would have reached the
model dispatch as raw content under ``SANITIZE_PII=true``, minted by its
caller or not. These tests drive the real router over a real registry and the
real dispatch — only the provider's ``_generate`` is a double — under both
settings of the flag, and read the dispatch's own verdict: no refusal, no
metric, and the values the dispatch was handed.

And the router's own check (A6): ``route()`` runs the dispatch's validator at
entry on the caller's values, without unwrapping them, so a raw call is
refused before the ``SANITIZE_PII`` pass sees its content, the cache answers
it or a falsy value is dropped (#1998 P3), and a minted one still passes. The
router itself is never a ``_generate`` target.
"""

from __future__ import annotations

import socket
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from faultmaven.exceptions import RawModelContentRefused
from faultmaven.infrastructure.llm import dispatch
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
from tests.model_content import forged, refusal_pattern, vetted, vetted_messages
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
    """What the dispatch was handed, and what was refused.

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


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_a_minted_prompt_reaches_the_provider_minted(
    router, provider, boundary, sanitize_pii
):
    seen, metric = boundary
    with sanitize_pii_pinned(sanitize_pii):
        await router.route(prompt=vetted(f"host {IP} is down"), max_tokens=50)

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
    with sanitize_pii_pinned(sanitize_pii):
        await router.route(messages=_minted_messages(), max_tokens=50)

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


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_raw_reasoning_artifacts_are_refused(
    router, provider, boundary, sanitize_pii
):
    """I1's other half: the pass mints nothing. Raw metadata is refused at
    ``route()``'s entry, before the pass."""
    seen, metric = boundary
    messages = _minted_messages()
    messages[2]["provider_metadata"] = {"assistant_content": [{"text": "raw"}]}
    with (
        sanitize_pii_pinned(sanitize_pii),
        pytest.raises(
            RawModelContentRefused,
            match=refusal_pattern("router", "messages.provider_metadata raw"),
        ),
    ):
        await router.route(messages=messages, max_tokens=50)
    assert {c.kwargs["argument"] for c in metric.labels.call_args_list} == {
        "messages.provider_metadata"
    }
    assert not provider.sent


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
    router, provider, sanitizer_calls, case, sanitize_pii
):
    with (
        sanitize_pii_pinned(sanitize_pii),
        pytest.raises(RawModelContentRefused) as raised,
    ):
        await router.route(max_tokens=50, **_UNMINTED[case])
    assert str(raised.value).startswith("router:"), "refused past route()'s entry"
    assert not sanitizer_calls, "the SANITIZE_PII pass saw content route() refuses"
    assert not provider.sent
    assert IP not in str(raised.value)


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_refusal_still_routes_minted_content(router, provider, sanitize_pii):
    """The entry check does not unwrap: a minted call passes it, the re-mint,
    and the dispatch, in both postures."""
    with sanitize_pii_pinned(sanitize_pii):
        await router.route(prompt=vetted(f"host {IP} is down"), max_tokens=50)
        await router.route(messages=_minted_messages(), max_tokens=50)
    assert len(provider.sent) == 2


# ---------------------------------------------------------------------------
# #2023 review: what the dispatch never sees, a refusal's cost, attribution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_a_dropped_raw_prompt_is_refused_at_entry(
    router, provider, boundary, sanitize_pii
):
    """I5: the router drops a falsy prompt before the registry, so no dispatch
    sees it. ``route(prompt="", messages=minted)`` is refused at entry."""
    seen, metric = boundary
    with (
        sanitize_pii_pinned(sanitize_pii),
        pytest.raises(
            RawModelContentRefused, match=refusal_pattern("router", "prompt raw")
        ),
    ):
        await router.route(prompt="", messages=_minted_messages(), max_tokens=50)
    [label] = [c.kwargs for c in metric.labels.call_args_list]
    assert (label["target"], label["argument"]) == ("router", "prompt")
    assert not provider.sent


async def test_a_raw_call_is_refused_whatever_the_cache_holds(router, provider):
    """I5: the cache answers a minted call it has seen; the same text raw is
    refused at entry, before the cache is consulted."""
    with sanitize_pii_pinned(False):
        await router.route(prompt=vetted("cache me"), model=MODEL, max_tokens=50)
        with pytest.raises(RawModelContentRefused, match="prompt raw"):
            await router.route(prompt="cache me", model=MODEL, max_tokens=50)
    assert len(provider.sent) == 1


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_a_minted_routed_call_is_refused_nowhere(
    router, provider, boundary, sanitize_pii
):
    """No false refusal on the new paths: minted content, with and without a
    cache hit, and an empty message list."""
    seen, metric = boundary
    with sanitize_pii_pinned(sanitize_pii):
        await router.route(prompt=vetted("cache me"), model=MODEL, max_tokens=50)
        await router.route(prompt=vetted("cache me"), model=MODEL, max_tokens=50)
        await router.route(prompt=vetted("x"), messages=[], max_tokens=50)
    metric.labels.assert_not_called()


def _two_provider_router(monkeypatch, first, second):
    from faultmaven.infrastructure.llm.router import LLMRouter

    registry = ProviderRegistry(settings=None)
    registry._providers = {"first": first, "second": second}
    registry._fallback_chain = ["first", "second"]
    registry._provider_states = {
        "first": ProviderState(name="first"),
        "second": ProviderState(name="second"),
    }
    registry._initialized = True
    monkeypatch.setattr(
        "faultmaven.infrastructure.llm.router.get_registry", lambda: registry
    )
    monkeypatch.setattr(
        "faultmaven.infrastructure.llm.providers.registry.record_provider_call",
        lambda *a, **k: None,
    )
    router = LLMRouter()
    router.sanitizer = regex_only_sanitizer()
    return router


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
@pytest.mark.parametrize("through", ["route", "seam"])
async def test_a_raw_call_reaches_no_provider_of_a_fallback_chain(
    monkeypatch, through, sanitize_pii
):
    """A raw routed call is refused once, by the seam or at the router's entry,
    before the registry's fallback chain reaches any provider."""
    from faultmaven.infrastructure.llm.dispatch import route_model_call
    from tests.model_content import FakeProvider

    first = FakeProvider([RuntimeError("first provider down")], name="first")
    second = FakeProvider(name="second")
    router = _two_provider_router(monkeypatch, first, second)
    with (
        sanitize_pii_pinned(sanitize_pii),
        patch.object(dispatch, "llm_model_content_violations") as metric,
        pytest.raises(
            RawModelContentRefused, match=refusal_pattern("router", "prompt raw")
        ),
    ):
        if through == "route":
            await router.route(prompt="raw text", max_tokens=5)
        else:
            await route_model_call(router, prompt="raw text", max_tokens=5)

    assert (first.calls, second.calls) == ([], [])
    assert metric.labels.call_count == 1


async def test_a_router_whose_registry_is_a_double_still_refuses(monkeypatch):
    """A test that replaces the registry never reaches a dispatch: the router's
    entry refuses its raw call before the double is called."""
    from unittest.mock import AsyncMock, MagicMock

    from faultmaven.infrastructure.llm.router import LLMRouter

    registry = MagicMock()
    registry.route_request = AsyncMock(
        return_value=LLMResponse(
            content="ok",
            confidence=1.0,
            provider="double",
            model=MODEL,
            tokens_used=1,
            response_time_ms=1,
        )
    )
    monkeypatch.setattr(
        "faultmaven.infrastructure.llm.router.get_registry", lambda: registry
    )
    router = LLMRouter()
    monkeypatch.setattr(router, "_resolve_timeout", lambda: None)
    with (
        sanitize_pii_pinned(False),
        pytest.raises(
            RawModelContentRefused, match=refusal_pattern("router", "prompt raw")
        ),
    ):
        await router.route(prompt="raw text", max_tokens=5)
    registry.route_request.assert_not_called()


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
        with pytest.raises(RawModelContentRefused):
            await registry.route_request(prompt="raw 10.9.8.7", max_tokens=5)
        with pytest.raises(RawModelContentRefused):
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
            with pytest.raises(RawModelContentRefused):
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


async def test_a_route_override_cannot_step_around_the_entry_check(router, provider):
    """O4: a subclass's ``route`` that only delegates (the cloud's tenant
    router) reaches core's entry check, which refuses a raw call."""
    from faultmaven.infrastructure.llm.router import LLMRouter

    class _Tenant(LLMRouter):
        async def route(self, *args, **kwargs):
            return await super().route(*args, **kwargs)

    tenant = _Tenant.__new__(_Tenant)
    tenant.__dict__.update(router.__dict__)
    with (
        sanitize_pii_pinned(False),
        pytest.raises(
            RawModelContentRefused, match=refusal_pattern("router", "prompt raw")
        ),
    ):
        await tenant.route(prompt="raw text", max_tokens=50)
    assert not provider.sent


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_dropped_falsy_messages_are_refused_at_entry(
    router, provider, boundary, sanitize_pii
):
    """η: ``messages=()`` is falsy, so the router would drop it before the
    registry and no dispatch would see the malformed list: the entry check
    refuses it."""
    seen, metric = boundary
    with (
        sanitize_pii_pinned(sanitize_pii),
        pytest.raises(
            RawModelContentRefused,
            match=refusal_pattern("router", "messages bad_shape"),
        ),
    ):
        await router.route(prompt=vetted("ok"), messages=(), max_tokens=50)
    [label] = [c.kwargs for c in metric.labels.call_args_list]
    assert (label["target"], label["argument"], label["kind"]) == (
        "router",
        "messages",
        "bad_shape",
    )
    assert not provider.sent


async def test_a_refusal_books_no_request_error_and_no_sla_failure(
    router, provider, monkeypatch
):
    """D3: a refusal raised inside ``call_external`` (content minted at entry,
    raw after a pass that lost provenance) is not a failed provider request:
    no ``llm_requests{status="error"}``, no SLA failure, no "All providers
    failed" line."""
    from faultmaven.infrastructure.llm import router as router_module

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
                with pytest.raises(RawModelContentRefused):
                    await router.route(prompt=vetted("x"), max_tokens=5)
        for _ in range(6):  # and at route()'s entry
            with pytest.raises(RawModelContentRefused):
                await router.route(prompt="raw", max_tokens=5)
    assert not [
        c for c in requests.labels.call_args_list if c.kwargs.get("status") == "error"
    ]
    assert not [k for _, k in sla if k.get("success") is False]
    assert not [c for c in logged.call_args_list if "All providers failed" in str(c)]
    assert not provider.sent
