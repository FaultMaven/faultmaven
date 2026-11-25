"""A call is fitted to the provider that serves it (#2836, #2983).

The router shapes a call and picks its ceiling before the registry picks the
provider. The dispatch fits a ``response_format`` to the receiving provider's
declared capability, Fireworks refuses the tools its denylisted models cannot
carry, and the router's outer ceiling covers every provider the walk may try,
each attempt bounded by its own client.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.config.settings import get_settings, reset_settings
from faultmaven.core.investigation.turn_budget import (
    bind_turn_deadline,
    clamp_to_turn_budget,
)
from faultmaven.exceptions import CallShapeUnsupported, LLMException
from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ProviderConfig
from faultmaven.infrastructure.llm.providers.fireworks_provider import (
    FireworksProvider,
)
from faultmaven.infrastructure.llm.providers.registry import (
    ProviderRegistry,
    ProviderState,
    reset_registry,
)
from faultmaven.infrastructure.llm.router import LLMRouter, resolve_request_timeout
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability as Cap,
)
from tests.model_content import vetted

pytestmark = [pytest.mark.unit, pytest.mark.llm]

STRICT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {"name": "Answer", "strict": True, "schema": {"type": "object"}},
}
JSON_OBJECT = {"type": "json_object"}
TOOLS = [{"type": "function", "function": {"name": "search_file", "parameters": {}}}]


def _response(provider="stub"):
    return LLMResponse(
        content="ok",
        confidence=1.0,
        provider=provider,
        model="m",
        tokens_used=1,
        response_time_ms=1,
    )


class _Stub:
    """A provider declaring ``capability``, answering or failing each send."""

    LABEL = "stub"

    def __init__(self, capability, timeout=30, failure=None):
        self.capability = capability
        self.config = SimpleNamespace(timeout=timeout, default_model="m")
        self.asked = []
        self._generate = AsyncMock(
            side_effect=failure, return_value=_response(type(self).LABEL)
        )

    def get_structured_output_capability(self, model=None):
        self.asked.append(model)
        return self.capability


def _stub(label, capability, **kwargs):
    return type(f"Stub_{label}", (_Stub,), {"LABEL": label})(capability, **kwargs)


def _sent_format(stub):
    return stub._generate.await_args.kwargs.get("response_format", "<absent>")


# --- the dispatch fits the response_format to the provider -------------------


@pytest.mark.parametrize(
    "capability,sent,expected",
    [
        (Cap.BEST_EFFORT, STRICT_SCHEMA, JSON_OBJECT),
        (Cap.FUNCTION_CALLING, STRICT_SCHEMA, "<absent>"),
        (Cap.NONE, STRICT_SCHEMA, "<absent>"),
        (Cap.FUNCTION_CALLING, JSON_OBJECT, "<absent>"),
        (Cap.NONE, JSON_OBJECT, "<absent>"),
        # Shaped for its own provider: unchanged.
        (Cap.STRICT, STRICT_SCHEMA, STRICT_SCHEMA),
        (Cap.BEST_EFFORT, JSON_OBJECT, JSON_OBJECT),
        (Cap.STRICT, JSON_OBJECT, JSON_OBJECT),
    ],
    ids=lambda v: getattr(v, "name", None) or (v if isinstance(v, str) else None),
)
async def test_the_response_format_reaches_the_provider_fitted(
    capability, sent, expected
):
    stub = _stub("p", capability)
    await send_to_model(stub, prompt=vetted("q"), model="m-1", response_format=sent)

    assert _sent_format(stub) == expected
    if expected != sent:
        assert stub.asked == ["m-1"], "the call's own model decides"


async def test_a_call_with_no_response_format_asks_no_capability():
    stub = _stub("p", Cap.NONE)
    await send_to_model(stub, prompt=vetted("q"), tools=TOOLS)

    assert stub.asked == []
    assert stub._generate.await_args.kwargs["tools"] == TOOLS


# --- Fireworks refuses tools on a denylisted model ---------------------------


def _fireworks(model):
    return FireworksProvider(
        ProviderConfig(
            name="fireworks",
            api_key="test-key-not-real",
            base_url="https://api.fireworks.ai/inference/v1",
            models=[model],
            default_model=model,
        )
    )


_DENYLISTED = next(iter(FireworksProvider._TOOL_CALLING_DENYLIST))
_OPENAI_REPLY = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    "usage": {"total_tokens": 2},
}


async def test_fireworks_refuses_tools_its_model_cannot_carry(mock_aiohttp_session):
    session = mock_aiohttp_session(_OPENAI_REPLY)
    with (
        patch("aiohttp.ClientSession", return_value=session),
        pytest.raises(CallShapeUnsupported),
    ):
        await send_to_model(_fireworks(_DENYLISTED), prompt=vetted("q"), tools=TOOLS)

    session.post.assert_not_called()


async def test_fireworks_sends_tools_on_a_capable_model(mock_aiohttp_session):
    session = mock_aiohttp_session(_OPENAI_REPLY)
    model = "accounts/fireworks/models/deepseek-v3"
    with patch("aiohttp.ClientSession", return_value=session):
        await send_to_model(_fireworks(model), prompt=vetted("q"), tools=TOOLS)

    assert session.post.call_args.kwargs["json"]["tools"] == TOOLS


# --- through the registry ---------------------------------------------------


def _registry(*providers):
    registry = ProviderRegistry(settings=None)
    registry._providers = {p.LABEL: p for p in providers}
    registry._fallback_chain = [p.LABEL for p in providers]
    registry._provider_states = {
        p.LABEL: ProviderState(name=p.LABEL) for p in providers
    }
    registry._initialized = True
    return registry


@pytest.fixture(autouse=True)
def _no_metering():
    with patch(
        "faultmaven.infrastructure.llm.providers.registry.record_provider_call",
        lambda *a, **k: None,
    ):
        yield


async def test_a_best_effort_fallback_receives_json_object():
    primary = _stub(
        "primary", Cap.STRICT, failure=LLMException("down", status_code=503)
    )
    fallback = _stub("fallback", Cap.BEST_EFFORT)
    registry = _registry(primary, fallback)

    await registry.route_request(prompt=vetted("q"), response_format=STRICT_SCHEMA)

    assert _sent_format(primary) == STRICT_SCHEMA
    assert _sent_format(fallback) == JSON_OBJECT


async def test_a_refused_tool_shape_is_skipped_without_a_health_failure(
    mock_aiohttp_session,
):
    fireworks = _fireworks(_DENYLISTED)
    fallback = _stub("fallback", Cap.STRICT)
    registry = _registry(fireworks, fallback)
    session = mock_aiohttp_session(_OPENAI_REPLY)

    with patch("aiohttp.ClientSession", return_value=session):
        await registry.route_request(prompt=vetted("q"), tools=TOOLS)

    session.post.assert_not_called()
    assert fallback._generate.await_args.kwargs["tools"] == TOOLS
    assert registry._provider_states["fireworks"].consecutive_failures == 0


# --- the walk's ceiling (#2983) ----------------------------------------------


def _router_over(monkeypatch, registry):
    monkeypatch.setattr(
        "faultmaven.infrastructure.llm.router.get_registry", lambda: registry
    )
    router = LLMRouter()
    seen = []
    real = router.call_external

    async def recording(*args, **kwargs):
        seen.append(kwargs["timeout"])
        return await real(*args, **kwargs)

    monkeypatch.setattr(router, "call_external", recording)
    return router, seen


def _failing_primary():
    return _stub(
        "primary", Cap.STRICT, timeout=30, failure=LLMException("down", status_code=503)
    )


async def test_the_walk_admits_the_fallbacks_own_ceiling(monkeypatch):
    fallback = _stub("fallback", Cap.STRICT, timeout=300)
    router, seen = _router_over(monkeypatch, _registry(_failing_primary(), fallback))

    await router.route(prompt=vetted("q"), bypass_cache=True)

    assert seen == [330.0]
    fallback._generate.assert_awaited_once()


async def test_inside_a_turn_the_budget_still_bounds_the_walk(monkeypatch):
    fallback = _stub("fallback", Cap.STRICT, timeout=300)
    router, seen = _router_over(monkeypatch, _registry(_failing_primary(), fallback))

    with bind_turn_deadline(60.0):
        bound = clamp_to_turn_budget(330.0)
        await router.route(prompt=vetted("q"), bypass_cache=True)

    assert seen[0] == pytest.approx(bound, abs=0.05)
    assert seen[0] < 60.0


def _configured_ceiling() -> int:
    """The CHAT provider's ceiling, which its client is built with."""
    return int(resolve_request_timeout(get_settings()))


async def test_a_one_provider_chain_is_unchanged(monkeypatch):
    only = _stub("only", Cap.STRICT, timeout=_configured_ceiling())
    router, seen = _router_over(monkeypatch, _registry(only))

    await router.route(prompt=vetted("q"), response_format=STRICT_SCHEMA)

    assert seen == [float(_configured_ceiling())]
    assert _sent_format(only) == STRICT_SCHEMA


async def test_a_provider_override_runs_under_its_ceiling_alone(monkeypatch):
    fallback = _stub("fallback", Cap.STRICT, timeout=300)
    primary = _stub("primary", Cap.STRICT, timeout=_configured_ceiling())
    router, seen = _router_over(monkeypatch, _registry(primary, fallback))

    await router.route(prompt=vetted("q"), provider_override="primary")

    assert seen == [float(_configured_ceiling())]
    fallback._generate.assert_not_called()


# --- the configured figure follows the client (#2983) ------------------------


@pytest.fixture
def local_settings(monkeypatch):
    monkeypatch.setenv("CHAT_PROVIDER", "local")
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "30")
    monkeypatch.setenv("LLM_PROVIDER_TIMEOUT_OVERRIDES", json.dumps({}))
    reset_settings()
    reset_registry()
    yield monkeypatch
    monkeypatch.undo()
    reset_settings()
    reset_registry()


def test_the_reported_ceiling_is_locals_client_default(local_settings):
    """``GET /admin/config/status`` models one attempt at this figure: with no
    override, local's client runs 60 s, so the report says 60, not 30."""
    assert resolve_request_timeout(get_settings()) == 60.0


def test_the_reported_ceiling_is_locals_override(local_settings):
    local_settings.setenv("LLM_PROVIDER_TIMEOUT_OVERRIDES", json.dumps({"local": 240}))
    reset_settings()

    assert resolve_request_timeout(get_settings()) == 240.0
