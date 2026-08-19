"""The per-call output floor (#1117): min_output_tokens on LLMRouter.route().

Reasoning models bill hidden reasoning against the same token budget the
visible answer is drawn from, so a nominally-large ``max_tokens`` can still
yield a starved stub (fm#1094: ~1,946 reasoning tokens against a 2,000 budget,
215 characters of answer). The floor lets a caller declare the minimum visible
output it needs, enforced twice:

  1. pre-call, ``max_tokens`` is raised to at least the floor — a total budget
     below the floor can never satisfy it;
  2. post-call, a response cut at the cap (MAX_TOKENS) with less visible
     output than the floor raises ``LLMOutputFloorError`` instead of returning
     a body the caller pre-declared unusable.

The floor bounds STARVATION, not verbosity: a response the model finished
cleanly (STOP) below the floor is a short answer and is returned as-is.
Default is absent — callers that do not set a floor keep the existing
behavior of receiving truncated responses to inspect.

Visible output is estimated from content length (~4 chars/token), not read
from ``output_tokens``: OpenAI's ``completion_tokens`` and Anthropic's
``output_tokens`` INCLUDE hidden reasoning, so on exactly the starved call
this guard exists to catch, the reported count reads as ample and a check
built on it would fail open.

These tests exercise the REAL ``route()`` signature on a real ``LLMRouter``
(only the registry behind it is mocked), so a signature drift cannot hide
behind a Mock that accepts anything.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.exceptions import LLMOutputFloorError
from faultmaven.infrastructure.llm.cache import LLMResponseCache
from faultmaven.infrastructure.llm.providers import (
    LLMResponse,
    ReasoningIntent,
    StopReason,
)
from faultmaven.infrastructure.llm.providers.base import ToolCall
from faultmaven.infrastructure.llm.truncation import generate_with_truncation_retry


def _response(content: str, stop_reason: StopReason, output_tokens: int = 0):
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="openai",
        model="gpt-5.4-mini",
        tokens_used=2000,
        response_time_ms=100,
        output_tokens=output_tokens,
        stop_reason=stop_reason,
    )


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.get_available_providers.return_value = ["openai"]
    registry.get_fallback_chain.return_value = ["openai"]
    registry.route_request = AsyncMock(
        return_value=_response("x" * 8000, StopReason.STOP)
    )
    return registry


@pytest.fixture
def router(mock_registry):
    with patch(
        "faultmaven.infrastructure.llm.router.get_registry",
        return_value=mock_registry,
    ):
        from faultmaven.infrastructure.llm.router import LLMRouter

        r = LLMRouter()
        # registry is a @property calling get_registry() each time, so the
        # patch must stay active for the test's lifetime.
        yield r


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestFloorPreCall:
    async def test_max_tokens_raised_to_floor(self, router, mock_registry):
        """A total budget below the floor can never satisfy it — the router
        raises max_tokens before the call ever leaves."""
        await router.route(
            prompt="test", max_tokens=200, min_output_tokens=1500, bypass_cache=True
        )
        call_kwargs = mock_registry.route_request.call_args.kwargs
        assert call_kwargs["max_tokens"] == 1500

    async def test_max_tokens_above_floor_untouched(self, router, mock_registry):
        await router.route(
            prompt="test", max_tokens=4000, min_output_tokens=1500, bypass_cache=True
        )
        call_kwargs = mock_registry.route_request.call_args.kwargs
        assert call_kwargs["max_tokens"] == 4000

    async def test_knobs_forwarded_to_providers(self, router, mock_registry):
        """Both knobs travel to the provider layer, where the per-provider
        translation lives."""
        await router.route(
            prompt="test",
            reasoning_intent=ReasoningIntent.EXTRACTION,
            min_output_tokens=500,
            bypass_cache=True,
        )
        call_kwargs = mock_registry.route_request.call_args.kwargs
        assert call_kwargs["reasoning_intent"] is ReasoningIntent.EXTRACTION
        assert call_kwargs["min_output_tokens"] == 500

    async def test_absent_knobs_forward_none(self, router, mock_registry):
        """Default = absent: existing callers see exactly the old behavior."""
        await router.route(prompt="test", max_tokens=1000, bypass_cache=True)
        call_kwargs = mock_registry.route_request.call_args.kwargs
        assert call_kwargs["reasoning_intent"] is None
        assert call_kwargs["min_output_tokens"] is None
        assert call_kwargs["max_tokens"] == 1000

    async def test_string_intent_normalized_to_enum(self, router, mock_registry):
        await router.route(
            prompt="test",
            reasoning_intent="inference",
            min_output_tokens=500,
            bypass_cache=True,
        )
        call_kwargs = mock_registry.route_request.call_args.kwargs
        assert call_kwargs["reasoning_intent"] is ReasoningIntent.INFERENCE

    async def test_inference_without_floor_rejected(self, router):
        """INFERENCE lifts provider starvation guards (Gemini's structured
        thinkingLevel cap among them), and reasoning bills against the same
        budget as the answer — the floor is what makes lifting the guard
        safe. The pairing is a mechanism, not a convention: enforced at the
        router chokepoint with a ValueError naming what is required."""
        with pytest.raises(ValueError, match="min_output_tokens"):
            await router.route(
                prompt="test", reasoning_intent=ReasoningIntent.INFERENCE
            )
        # Same enforcement for the string spelling.
        with pytest.raises(ValueError, match="min_output_tokens"):
            await router.route(prompt="test", reasoning_intent="inference")

    async def test_extraction_needs_no_floor(self, router, mock_registry):
        """The pairing invariant is INFERENCE-only: EXTRACTION requests the
        provider's MINIMUM reasoning, which tightens the starvation guards
        rather than lifting them."""
        await router.route(prompt="test", reasoning_intent=ReasoningIntent.EXTRACTION)
        call_kwargs = mock_registry.route_request.call_args.kwargs
        assert call_kwargs["reasoning_intent"] is ReasoningIntent.EXTRACTION
        assert call_kwargs["min_output_tokens"] is None

    async def test_unknown_intent_raises_at_the_call_site(self, router):
        """A typo'd intent is a caller bug — it must fail loudly, not mutate
        into silent provider behavior."""
        with pytest.raises(ValueError):
            await router.route(prompt="test", reasoning_intent="thinking-hard")

    @pytest.mark.parametrize("bad_floor", [0, -5])
    async def test_non_positive_floor_rejected(self, router, bad_floor):
        with pytest.raises(ValueError):
            await router.route(prompt="test", min_output_tokens=bad_floor)


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestFloorPostCall:
    async def test_reasoning_call_with_floor_cannot_return_starved_max_tokens_stop(
        self, router, mock_registry
    ):
        """The #1117 guarantee: a call configured with reasoning and a floor
        cannot YIELD a MAX_TOKENS stop with output below the floor — the
        router raises instead of returning the starved body.

        The mock reproduces the fm#1094 shape: output_tokens reads as the
        full budget (hidden reasoning is inside the count) while the visible
        content is a 215-char stub.
        """
        mock_registry.route_request = AsyncMock(
            return_value=_response("x" * 215, StopReason.MAX_TOKENS, output_tokens=2000)
        )
        with pytest.raises(LLMOutputFloorError) as exc_info:
            await router.route(
                prompt="test",
                max_tokens=2000,
                reasoning_intent=ReasoningIntent.INFERENCE,
                min_output_tokens=500,
                bypass_cache=True,
            )
        assert "min_output_tokens=500" in str(exc_info.value)
        # Non-retryable by derivation: an identical retry starves identically.
        assert exc_info.value.retryable is False

    async def test_truncated_but_floor_met_is_returned(self, router, mock_registry):
        """Cut at the cap, but the caller got at least what it declared it
        needs — returned for the ordinary truncation handling (annotate /
        retry-bigger) to deal with."""
        mock_registry.route_request = AsyncMock(
            return_value=_response("x" * 8000, StopReason.MAX_TOKENS)
        )
        response = await router.route(
            prompt="test", max_tokens=2000, min_output_tokens=500, bypass_cache=True
        )
        assert response.is_truncated

    async def test_clean_stop_below_floor_is_returned(self, router, mock_registry):
        """The floor bounds starvation, not verbosity: a model that FINISHED
        below the floor gave a short answer, not a starved one."""
        mock_registry.route_request = AsyncMock(
            return_value=_response("short answer", StopReason.STOP)
        )
        response = await router.route(
            prompt="test", max_tokens=2000, min_output_tokens=500, bypass_cache=True
        )
        assert response.content == "short answer"

    async def test_no_floor_keeps_existing_truncation_behavior(
        self, router, mock_registry
    ):
        """Without a floor, a starved truncated response is still RETURNED —
        pre-#1117 callers depend on inspecting is_truncated themselves."""
        mock_registry.route_request = AsyncMock(
            return_value=_response("x" * 215, StopReason.MAX_TOKENS)
        )
        response = await router.route(prompt="test", max_tokens=2000, bypass_cache=True)
        assert response.is_truncated
        assert response.content == "x" * 215

    async def test_visible_output_measured_from_content_not_output_tokens(
        self, router, mock_registry
    ):
        """output_tokens INCLUDES hidden reasoning on OpenAI/Anthropic — a
        floor check reading it would fail open on exactly the starved case.
        A stub body must trip the floor even when output_tokens looks ample."""
        mock_registry.route_request = AsyncMock(
            return_value=_response("stub", StopReason.MAX_TOKENS, output_tokens=99999)
        )
        with pytest.raises(LLMOutputFloorError):
            await router.route(
                prompt="test",
                max_tokens=2000,
                min_output_tokens=500,
                bypass_cache=True,
            )

    async def test_tool_call_payload_counts_as_visible_output(
        self, router, mock_registry
    ):
        """Tool-call responses can carry their whole payload in tool_calls
        with EMPTY content — a floor check reading content alone would call
        every floored tools call fully starved. Serialized tool calls count
        toward the visible estimate."""
        tool_response = _response("", StopReason.MAX_TOKENS)
        tool_response.tool_calls = [
            ToolCall(
                id="call_1",
                type="function",
                function={"name": "emit", "arguments": "y" * 4000},
            )
        ]
        mock_registry.route_request = AsyncMock(return_value=tool_response)
        response = await router.route(
            prompt="test", max_tokens=2000, min_output_tokens=500, bypass_cache=True
        )
        assert response.tool_calls


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestFloorComposesWithTruncationRetry:
    """generate_with_truncation_retry is what every natural floor adopter
    already wraps its calls in (#1100). A floor violation on the FIRST
    attempt is the same starved-MAX_TOKENS failure the helper's retry rung
    exists to recover — surfaced as an exception because the caller
    pre-declared the body unusable — so the helper treats it as a first
    truncation and retries once at the bigger cap. Only a starved RETRY (or
    no cap headroom) lets the error propagate: at that point fail-loudly is
    the floor's contract.
    """

    async def test_starved_first_attempt_recovered_by_bigger_retry(self):
        """The composition the fix exists for: floor-starved first attempt →
        one retry at 2x cap → caller gets the good response, no exception."""
        good = _response("x" * 8000, StopReason.STOP)
        caps_seen = []

        async def call(cap: int):
            caps_seen.append(cap)
            if len(caps_seen) == 1:
                raise LLMOutputFloorError("starved below floor")
            return good

        result = await generate_with_truncation_retry(
            call, max_tokens=2000, label="floored call"
        )
        assert result is good
        assert caps_seen == [2000, 4000]

    async def test_starved_retry_propagates(self):
        """Recovery is one rung: a second violation at the bigger cap
        re-raises — returning a partial would hand the caller the body it
        said it cannot use."""

        async def call(cap: int):
            raise LLMOutputFloorError("starved below floor")

        with pytest.raises(LLMOutputFloorError):
            await generate_with_truncation_retry(
                call, max_tokens=2000, label="floored call"
            )

    async def test_no_headroom_reraises_without_second_call(self):
        """Already at the ceiling: there is no bigger cap to retry into, so
        the first violation propagates after exactly one provider call."""
        calls = []

        async def call(cap: int):
            calls.append(cap)
            raise LLMOutputFloorError("starved below floor")

        with pytest.raises(LLMOutputFloorError):
            await generate_with_truncation_retry(
                call, max_tokens=2000, ceiling=2000, label="floored call"
            )
        assert calls == [2000]

    async def test_ordinary_truncation_path_unchanged(self):
        """The pre-existing rung is untouched: a RETURNED truncated response
        (no floor declared) still gets the one bigger retry and the partial
        comes back if that is also cut."""
        cut = _response("x" * 100, StopReason.MAX_TOKENS)

        async def call(cap: int):
            return cut

        result = await generate_with_truncation_retry(
            call, max_tokens=2000, label="plain call"
        )
        assert result is cut


@pytest.mark.unit
@pytest.mark.llm
class TestCacheIntentKey:
    """The response cache keys on reasoning_intent (#1118): an identical
    prompt asked with a different intent is a different call — an extraction
    answer must never be served to an inference caller, or vice versa."""

    def test_same_prompt_different_intents_are_distinct_entries(self):
        cache = LLMResponseCache()
        extraction_answer = _response("grounded summary", StopReason.STOP)
        inference_answer = _response("candidate causes", StopReason.STOP)

        cache.store(
            "prompt", "gpt-5.4-mini", extraction_answer, reasoning_intent="extraction"
        )
        cache.store(
            "prompt", "gpt-5.4-mini", inference_answer, reasoning_intent="inference"
        )

        got_extraction = cache.check(
            "prompt", "gpt-5.4-mini", reasoning_intent="extraction"
        )
        got_inference = cache.check(
            "prompt", "gpt-5.4-mini", reasoning_intent="inference"
        )
        assert got_extraction.content == "grounded summary"
        assert got_inference.content == "candidate causes"

    def test_no_intent_is_its_own_namespace(self):
        """A call with no declared intent neither serves nor is served by an
        intent-declared entry."""
        cache = LLMResponseCache()
        cache.store(
            "prompt",
            "gpt-5.4-mini",
            _response("intent answer", StopReason.STOP),
            reasoning_intent="extraction",
        )
        assert cache.check("prompt", "gpt-5.4-mini") is None
        cache.store(
            "prompt", "gpt-5.4-mini", _response("plain answer", StopReason.STOP)
        )
        assert (
            cache.check("prompt", "gpt-5.4-mini", reasoning_intent="extraction").content
            == "intent answer"
        )
        assert cache.check("prompt", "gpt-5.4-mini").content == "plain answer"

    def test_enum_and_string_spellings_share_a_key(self):
        """The key uses the enum's VALUE, so the enum and its string spelling
        are the same intent, not two cache namespaces."""
        cache = LLMResponseCache()
        cache.store(
            "prompt",
            "gpt-5.4-mini",
            _response("answer", StopReason.STOP),
            reasoning_intent=ReasoningIntent.EXTRACTION,
        )
        assert (
            cache.check("prompt", "gpt-5.4-mini", reasoning_intent="extraction").content
            == "answer"
        )
