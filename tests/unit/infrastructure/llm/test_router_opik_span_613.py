"""The Opik span records an investigation prompt from its cache boundary on (#613).

The investigation prompt opens with ~80K characters of standing instructions
that are byte-identical on every call, then ``CACHE_BOUNDARY``, then this turn's
case. The span input is truncated to ``TELEMETRY_PAYLOAD_MAX_CHARS``; cut from
the start it would record only the static head on every call, and never the
case id, the state, the evidence or the user's message. So an input holding the
boundary is recorded from the boundary on and flagged ``prompt_prefix_elided``;
anything else is recorded as before.

Driven through ``LLMRouter.route`` with a real Anthropic provider and only the
HTTP layer mocked, because the span is written there, from the SANITIZED prompt
and messages the route builds. ``opik_context`` is patched in: ``opik`` is not a
dependency of the standalone install.

The same span and its trace carry the case and the enterprise, which is how the
case delete finds them (#2102): ``TestTheTraceIsTaggedWithItsCase`` below.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.infrastructure.llm import router as router_module
from faultmaven.infrastructure.llm.cache import LLMResponseCache
from faultmaven.infrastructure.llm.prompt_cache import CACHE_BOUNDARY
from faultmaven.infrastructure.llm.providers.anthropic import AnthropicProvider
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ProviderConfig
from faultmaven.infrastructure.llm.providers.registry import (
    ProviderRegistry,
    ProviderState,
)
from faultmaven.infrastructure.llm.router import TELEMETRY_PAYLOAD_MAX_CHARS
from tests.model_content import vetted_kwargs

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

#: Static head longer than the telemetry cap, with both quote characters — as
#: the real prompt has (``file_backed="true"``, "don't"). That is what makes
#: ``repr`` escape the boundary's apostrophe inside a stringified message list.
_HEAD = (
    'You are FaultMaven. Tag evidence file_backed="true"; don\'t guess.\n'
    + "STANDING INSTRUCTION LINE\n" * 600
)
_TAIL = (
    "\nSTATE: INVESTIGATING\n<case_identity>CASE_ID: case_613span</case_identity>\n"
    "CURRENT USER MESSAGE:\nwhy is the disk full?\n"
)
_PROMPT = _HEAD + CACHE_BOUNDARY + _TAIL

_RESP = {
    "content": [{"type": "text", "text": "ok"}],
    "usage": {"input_tokens": 5, "output_tokens": 5},
}


def _session():
    response = AsyncMock()
    response.status = 200
    response.json = AsyncMock(return_value=_RESP)
    response.text = AsyncMock(return_value="")
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _registry() -> ProviderRegistry:
    provider = AnthropicProvider(
        ProviderConfig(
            name="anthropic",
            api_key="test-key",
            base_url="https://api.anthropic.com/v1",
            models=["claude-sonnet-4-6"],
            default_model="claude-sonnet-4-6",
            timeout=30,
            confidence_score=0.9,
        )
    )
    reg = ProviderRegistry(settings=None)
    reg._providers = {"anthropic": provider}
    reg._fallback_chain = ["anthropic"]
    reg._provider_states = {"anthropic": ProviderState(name="anthropic")}
    reg._initialized = True
    return reg


async def _routed(*calls: dict, tracing: bool = True, opik_context=None) -> MagicMock:
    """Route ``calls`` through one router; return the patched ``opik_context``."""
    opik_context = opik_context or MagicMock()
    with (
        patch(
            "faultmaven.infrastructure.llm.router.get_registry",
            return_value=_registry(),
        ),
        patch(
            "faultmaven.infrastructure.llm.router._opik_tracing_enabled",
            return_value=tracing,
        ),
        patch("faultmaven.infrastructure.llm.router.OPIK_AVAILABLE", True),
        patch(
            "faultmaven.infrastructure.llm.router.opik_context",
            opik_context,
            create=True,
        ),
        patch("aiohttp.ClientSession", return_value=_session()),
    ):
        from faultmaven.infrastructure.llm.router import LLMRouter

        # The route BODY, which writes the span. Where opik is installed (the
        # cloud profile) ``route`` is wrapped in ``opik.track``, and forcing the
        # gate open above would otherwise drive the real SDK.
        route = getattr(LLMRouter.route, "__wrapped__", LLMRouter.route)
        router = LLMRouter()
        for call in calls:
            await route(router, model="claude-sonnet-4-6", **vetted_kwargs(call))
    return opik_context


async def _span_update(**route_kwargs) -> dict:
    """Route one call and return the kwargs the span was updated with."""
    opik_context = await _routed(route_kwargs)
    assert opik_context.update_current_span.call_count == 1
    return opik_context.update_current_span.call_args.kwargs


async def test_messages_with_the_boundary_are_recorded_from_it_on():
    """The tool loop's shape: [system, user prompt]."""
    span = await _span_update(
        prompt=None,
        messages=[
            {"role": "system", "content": "DA system instruction"},
            {"role": "user", "content": _PROMPT},
        ],
        cache_prompt=True,
    )

    recorded = span["input"]["messages"]
    assert "=== CURRENT CASE" in recorded
    assert "case_613span" in recorded and "why is the disk full?" in recorded
    assert "STANDING INSTRUCTION LINE" not in recorded
    assert "DA system instruction" not in recorded
    assert len(recorded) <= TELEMETRY_PAYLOAD_MAX_CHARS
    assert span["metadata"]["prompt_prefix_elided"] is True


async def test_a_prompt_with_the_boundary_is_recorded_from_it_on():
    span = await _span_update(prompt=_PROMPT)

    recorded = span["input"]["prompt"]
    assert recorded.startswith(CACHE_BOUNDARY)
    assert "case_613span" in recorded
    assert "STANDING INSTRUCTION LINE" not in recorded
    assert span["metadata"]["prompt_prefix_elided"] is True


async def test_input_without_the_boundary_is_recorded_as_before():
    prompt = _HEAD + _TAIL
    messages = [
        {"role": "system", "content": "DA system instruction"},
        {"role": "user", "content": prompt},
    ]
    span = await _span_update(prompt=None, messages=messages)

    assert span["input"]["messages"] == str(messages)[:TELEMETRY_PAYLOAD_MAX_CHARS]
    assert span["metadata"]["prompt_prefix_elided"] is False

    span = await _span_update(prompt=prompt)
    assert span["input"]["prompt"] == prompt[:TELEMETRY_PAYLOAD_MAX_CHARS]
    assert span["metadata"]["prompt_prefix_elided"] is False


@pytest.mark.usefixtures("restore_tenant_context")
class TestTheTraceIsTaggedWithItsCase:
    """#2102: ``LLMTraceEraser`` searches TRACE metadata by these two keys."""

    OWNER = {"case_id": "case_2102", "enterprise_id": "ent-2102"}

    async def test_a_call_for_a_case_tags_the_span_and_its_trace(self):
        set_current_enterprise_id("ent-2102")

        opik_context = await _routed({"prompt": "hi", "case_id": "case_2102"})

        metadata = opik_context.update_current_span.call_args.kwargs["metadata"]
        assert self.OWNER.items() <= metadata.items()
        opik_context.update_current_trace.assert_called_once_with(
            metadata=self.OWNER, thread_id="case_2102"
        )

    async def test_a_call_for_no_case_tags_the_enterprise_alone(self):
        set_current_enterprise_id("ent-2102")

        opik_context = await _routed({"prompt": "hi"})

        metadata = opik_context.update_current_span.call_args.kwargs["metadata"]
        assert metadata["enterprise_id"] == "ent-2102" and "case_id" not in metadata
        opik_context.update_current_trace.assert_called_once_with(
            metadata={"enterprise_id": "ent-2102"}
        )

    async def test_a_cached_answer_records_content_so_it_is_tagged_too(self):
        set_current_enterprise_id("ent-2102")
        answer = LLMResponse(
            content="from the cache",
            confidence=0.9,
            provider="anthropic",
            model="claude-sonnet-4-6",
            tokens_used=1,
            response_time_ms=1,
        )

        with patch.object(LLMResponseCache, "check", return_value=answer):
            opik_context = await _routed({"prompt": "hi", "case_id": "case_2102"})

        span = opik_context.update_current_span.call_args.kwargs
        assert span["metadata"]["cached"] is True  # the cached path did run
        assert self.OWNER.items() <= span["metadata"].items()
        opik_context.update_current_trace.assert_called_once_with(
            metadata=self.OWNER, thread_id="case_2102"
        )

    async def test_no_content_reaches_a_span_whose_trace_could_not_be_tagged(self):
        opik_context = MagicMock()
        opik_context.update_current_trace.side_effect = RuntimeError("no trace")

        await _routed(
            {"prompt": "hi", "case_id": "case_2102"}, opik_context=opik_context
        )

        opik_context.update_current_span.assert_not_called()

    async def test_tracing_off_touches_neither(self):
        opik_context = await _routed(
            {"prompt": "hi", "case_id": "case_2102"}, tracing=False
        )

        opik_context.update_current_span.assert_not_called()
        opik_context.update_current_trace.assert_not_called()

    async def test_the_decorator_does_not_attach_a_return_value_to_the_trace(self):
        """With ``create_duplicate_root_span=False`` the SDK would record the
        raw return value as the trace's output."""
        with (
            patch.object(router_module, "OPIK_AVAILABLE", True),
            patch.object(router_module, "opik", create=True) as opik,
        ):
            router_module._opik_track_llm("x")(AsyncMock())

        opik.track.assert_called_once()
        assert "create_duplicate_root_span" not in opik.track.call_args.kwargs
