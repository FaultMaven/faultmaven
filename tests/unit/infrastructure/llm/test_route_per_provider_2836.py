"""A call is fitted to the provider that serves it (#2836).

The router shapes a call before the registry picks the provider. The dispatch
fits a strict ``json_schema`` to the receiving provider's declared capability,
and Fireworks refuses the tools its denylisted models cannot carry.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.exceptions import CallShapeUnsupported, LLMException
from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ProviderConfig
from faultmaven.infrastructure.llm.providers.fireworks_provider import (
    FireworksProvider,
)
from faultmaven.infrastructure.llm.providers.openrouter_provider import (
    OpenRouterProvider,
)
from faultmaven.infrastructure.llm.providers.registry import (
    ProviderRegistry,
    ProviderState,
)
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

    def __init__(self, capability, failure=None):
        self.capability = capability
        self.config = SimpleNamespace(default_model="m")
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


# --- the dispatch fits a strict json_schema to the provider -----------------


@pytest.mark.parametrize(
    "capability,sent,expected",
    [
        (Cap.BEST_EFFORT, STRICT_SCHEMA, JSON_OBJECT),
        (Cap.FUNCTION_CALLING, STRICT_SCHEMA, JSON_OBJECT),
        (Cap.NONE, STRICT_SCHEMA, "<absent>"),
        # Unchanged: a strict schema to STRICT, and a json_object anywhere.
        (Cap.STRICT, STRICT_SCHEMA, STRICT_SCHEMA),
        (Cap.STRICT, JSON_OBJECT, JSON_OBJECT),
        (Cap.BEST_EFFORT, JSON_OBJECT, JSON_OBJECT),
        (Cap.FUNCTION_CALLING, JSON_OBJECT, JSON_OBJECT),
        (Cap.NONE, JSON_OBJECT, JSON_OBJECT),
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


# --- OpenRouter: the routed model decides -----------------------------------


_OPENROUTER_REPLY = {
    "choices": [{"message": {"role": "assistant", "content": "{}"}}],
    "usage": {"total_tokens": 2},
}


@pytest.mark.parametrize(
    "model,expected",
    [
        # FUNCTION_CALLING: JSON mode, no strict schema.
        ("anthropic/claude-sonnet-4-6", JSON_OBJECT),
        # STRICT: the schema passes through.
        ("openai/gpt-5", STRICT_SCHEMA),
    ],
)
async def test_openrouter_receives_the_shape_its_route_declares(
    mock_aiohttp_session, model, expected
):
    provider = OpenRouterProvider(
        ProviderConfig(
            name="openrouter",
            api_key="test-key-not-real",
            base_url="https://openrouter.ai/api/v1",
            models=[model],
            default_model=model,
        )
    )
    session = mock_aiohttp_session(_OPENROUTER_REPLY)
    with patch("aiohttp.ClientSession", return_value=session):
        await send_to_model(provider, prompt=vetted("q"), response_format=STRICT_SCHEMA)

    assert session.post.call_args.kwargs["json"]["response_format"] == expected
