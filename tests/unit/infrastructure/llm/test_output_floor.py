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

Visible output is measured with the provider's tokenizer when it has one AND
the body is at or under ``_TOKENIZER_EXACT_MAX_CHARS``; everything else — the
five providers with no tokenizer, and any longer body — is bounded above by its
UTF-8 BYTE count. The invariant across all of them is that the estimate must
never UNDER-state, since under-stating fires the guard on a body that met its
floor. Bytes rather than characters because a byte-level BPE token consumes at
least one byte, which makes the bound provable for every script; the character
form is only its ASCII corollary and multi-byte text breaks it. It is never read from ``output_tokens``, because
OpenAI's ``completion_tokens`` and Anthropic's ``output_tokens`` INCLUDE hidden
reasoning, so on exactly the starved call this guard exists to catch the
reported count reads as ample and a check built on it would fail open.

These tests exercise the REAL ``route()`` signature on a real ``LLMRouter``
(only the registry behind it is mocked), so a signature drift cannot hide
behind a Mock that accepts anything.
"""

import logging
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


def _response(
    content: str,
    stop_reason: StopReason,
    output_tokens: int = 0,
    provider: str = "openai",
    model: str = "gpt-5.4-mini",
):
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider=provider,
        model=model,
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

    async def test_retry_runs_at_a_bigger_EFFECTIVE_cap_through_the_real_router(
        self, router, mock_registry
    ):
        """The defect this pins could not be seen through a stub.

        ``route()`` raises any cap below the floor UP to the floor, so a
        helper reasoning from the caller's NOMINAL max_tokens computes a retry
        cap the router then bumps straight back to the same effective value.
        With max_tokens=800 and a 2000 floor, both attempts ran at an
        effective 2000 — byte-identical requests, billed twice, for a
        guaranteed second failure.

        This drives the REAL router and records the cap the registry actually
        received, so the assertion is about what went to the provider rather
        than what the helper believed it asked for.
        """
        effective_caps = []
        starved = _response("x" * 215, StopReason.MAX_TOKENS)
        good = _response("word " * 4000, StopReason.STOP)

        async def fake_route_request(*args, **kwargs):
            effective_caps.append(kwargs["max_tokens"])
            return starved if len(effective_caps) == 1 else good

        mock_registry.route_request = AsyncMock(side_effect=fake_route_request)

        async def call(cap: int):
            return await router.route(
                prompt="test",
                max_tokens=cap,
                min_output_tokens=2000,
                bypass_cache=True,
            )

        result = await generate_with_truncation_retry(
            call, max_tokens=800, min_output_tokens=2000, label="floored call"
        )
        assert result is good
        assert effective_caps == [2000, 4000], (
            "the retry must reach the provider at a genuinely bigger cap; "
            f"got {effective_caps}"
        )

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


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestFloorMeasuresWithTheRealTokenizer:
    """The visible-output estimate is measured, not guessed.

    ``len(text)//4`` is not a conservative approximation in either direction —
    it is shape-dependent. Measured against tiktoken: English prose runs ~6.5
    chars/token (the heuristic overstates, guard under-fires) while id-dense
    JSON, base64 and CJK run 0.9-2.1 chars/token (it understates, and the
    guard fires on a body that MET the floor). The false positive is the
    unsafe direction — it kills an otherwise-usable turn — and dense
    structured output is exactly the shape this feature targets.
    """

    async def test_dense_json_above_the_floor_is_not_raised_on(
        self, router, mock_registry
    ):
        """Regression for the estimator direction. This id-dense JSON body is
        ~1000 real tokens against a 600-token floor — comfortably clear — but
        `len//4` scores it ~460 and would raise."""
        import json
        import uuid

        body = json.dumps(
            [
                {
                    "id": str(uuid.uuid4()),
                    "ts": "2026-08-19T22:14:03.221Z",
                    "v": i * 1.37,
                }
                for i in range(20)
            ],
            separators=(",", ":"),
        )
        assert len(body) // 4 < 600, "sample must be one the old heuristic failed"
        mock_registry.route_request = AsyncMock(
            return_value=_response(body, StopReason.MAX_TOKENS)
        )
        response = await router.route(
            prompt="test", max_tokens=2000, min_output_tokens=600, bypass_cache=True
        )
        assert response.is_truncated

    async def test_genuinely_starved_body_still_raises(self, router, mock_registry):
        """The guard must still fire on the failure it exists for — the
        fm#1094 stub misses the floor by an order of magnitude under any
        tokenizer."""
        mock_registry.route_request = AsyncMock(
            return_value=_response("x" * 215, StopReason.MAX_TOKENS)
        )
        with pytest.raises(LLMOutputFloorError):
            await router.route(
                prompt="test",
                max_tokens=2000,
                min_output_tokens=500,
                bypass_cache=True,
            )


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestFloorOnNoStopSignal:
    """Supplement A: the gate is three-valued, and UNKNOWN is the value where
    a reader would want to be told the guard is off.

    ``normalize_stop_reason`` maps everything outside its alias table to
    UNKNOWN, so this is not a HuggingFace footnote: any finish reason a
    provider API adds later disables the floor on that provider. Raising on
    no-signal is NOT the fix — that would treat "we don't know" as "it was
    cut" and buy false positives on short answers — so a starved-looking
    no-signal body is warned about and returned.
    """

    async def test_no_signal_starved_body_warns_and_returns(
        self, router, mock_registry, caplog
    ):
        mock_registry.route_request = AsyncMock(
            return_value=_response("x" * 215, StopReason.UNKNOWN)
        )
        with caplog.at_level(logging.WARNING):
            response = await router.route(
                prompt="test",
                max_tokens=2000,
                min_output_tokens=500,
                bypass_cache=True,
            )
        assert response.content == "x" * 215
        assert any("NOT ENFORCEABLE" in r.message for r in caplog.records)

    async def test_no_signal_body_above_the_floor_is_silent(
        self, router, mock_registry, caplog
    ):
        mock_registry.route_request = AsyncMock(
            return_value=_response("word " * 4000, StopReason.UNKNOWN)
        )
        with caplog.at_level(logging.WARNING):
            await router.route(
                prompt="test",
                max_tokens=2000,
                min_output_tokens=500,
                bypass_cache=True,
            )
        assert not any("NOT ENFORCEABLE" in r.message for r in caplog.records)


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestFloorFailureAccounting:
    """A floor-starved call left via neither the success block nor the generic
    failure handler, so it appeared in no latency, token or SLA series: cost
    dashboards under-reported real spend and SLA availability dropped starved
    calls from both numerator and denominator — overstating provider health
    during exactly the incident the tracker exists to surface. The provider
    did answer and the tokens were billed; only the ANSWER is unusable."""

    async def test_starved_call_is_recorded_before_raising(self, router, mock_registry):
        mock_registry.route_request = AsyncMock(
            return_value=_response("x" * 215, StopReason.MAX_TOKENS)
        )
        with (
            patch("faultmaven.infrastructure.llm.router.sla_tracker") as mock_sla,
            patch("faultmaven.infrastructure.llm.router.llm_tokens") as mock_tokens,
            patch("faultmaven.infrastructure.llm.router.llm_latency") as mock_latency,
        ):
            with pytest.raises(LLMOutputFloorError):
                await router.route(
                    prompt="test",
                    max_tokens=2000,
                    min_output_tokens=500,
                    bypass_cache=True,
                )
        mock_sla.record_request_metrics.assert_called_once()
        assert mock_sla.record_request_metrics.call_args.kwargs["success"] is False
        mock_latency.labels.return_value.observe.assert_called_once()
        mock_tokens.labels.return_value.inc.assert_called_once_with(2000)


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestFloorValidation:
    """The error text promises a positive INTEGER. Without a type test a
    float floor (config arithmetic, or a JSON value parsed as a float) passes
    the comparison, is assigned into max_tokens, and reaches the provider as
    ``{"max_completion_tokens": 1500.5}`` — every provider 400s and it
    surfaces as the misleading "All providers failed"."""

    @pytest.mark.parametrize("bad", [1500.5, 500.0, True, False])
    async def test_non_integer_floor_rejected(self, router, bad):
        with pytest.raises(ValueError, match="positive integer"):
            await router.route(prompt="test", min_output_tokens=bad)

    async def test_integer_floor_accepted(self, router, mock_registry):
        await router.route(prompt="test", min_output_tokens=500, bypass_cache=True)
        assert mock_registry.route_request.call_args.kwargs["min_output_tokens"] == 500


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestInferenceHeadroomWarning:
    """The bump raises max_tokens only TO the floor, leaving zero room for
    hidden reasoning billed from the same budget — so on INFERENCE every
    MAX_TOKENS stop at that cap raises by construction. What headroom a model
    needs is a property of that model's reasoning, which this layer cannot
    know, so it warns rather than inventing an undocumented multiplier."""

    async def test_warns_when_inference_has_no_headroom(
        self, router, mock_registry, caplog
    ):
        with caplog.at_level(logging.WARNING):
            await router.route(
                prompt="test",
                max_tokens=500,
                reasoning_intent=ReasoningIntent.INFERENCE,
                min_output_tokens=500,
                bypass_cache=True,
            )
        assert any(
            "size max_tokens above the floor" in r.message for r in caplog.records
        )

    async def test_silent_when_headroom_exists(self, router, mock_registry, caplog):
        with caplog.at_level(logging.WARNING):
            await router.route(
                prompt="test",
                max_tokens=4000,
                reasoning_intent=ReasoningIntent.INFERENCE,
                min_output_tokens=500,
                bypass_cache=True,
            )
        assert not any(
            "size max_tokens above the floor" in r.message for r in caplog.records
        )

    async def test_floored_retry_that_merely_truncates_gets_the_warning(self, caplog):
        """#12: the floor path used to end in a bare return, so a retry that
        came back truncated-but-floor-meeting skipped the helper's labelled
        retry-also-failed signal — lost on exactly the calls that opted into
        stricter guarantees. One rung now, so both entries emit it."""
        cut = _response("word " * 4000, StopReason.MAX_TOKENS)

        async def call(cap: int):
            if cap == 2000:
                raise LLMOutputFloorError("starved below floor")
            return cut

        with caplog.at_level(logging.WARNING):
            result = await generate_with_truncation_retry(
                call, max_tokens=2000, label="floored call"
            )
        assert result is cut
        assert any("truncated again" in r.message for r in caplog.records)

    async def test_floor_does_not_change_the_unfloored_doubling(self):
        """Guard on the shared rung: with no floor declared the arithmetic is
        exactly what it was before #1117 — nominal cap, one doubling."""
        caps_seen = []
        cut = _response("x" * 100, StopReason.MAX_TOKENS)

        async def call(cap: int):
            caps_seen.append(cap)
            return cut

        await generate_with_truncation_retry(call, max_tokens=1500, label="plain")
        assert caps_seen == [1500, 3000]


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestFloorOnProvidersWithoutATokenizer:
    """B1: the floor must not be *guessed* on the five providers
    ``estimate_tokens`` cannot tokenize.

    ``estimate_tokens`` dispatches to tiktoken only for openai, openrouter,
    anthropic and fireworks; gemini, groq, cohere, local and huggingface fall
    through to ``len // 4`` — a middle-of-the-range divisor that understates
    dense output by 2-4x and so fires the guard on bodies that MET the floor.
    That matters most on Gemini, the one provider where declaring INFERENCE
    LIFTS a starvation guard and makes the floor load-bearing.

    Where no tokenizer exists the estimate is the body's UTF-8 byte count —
    an upper bound on the token count for ANY script, since a byte-level BPE
    token consumes at least one byte — so it errs toward NOT raising.
    """

    # Every provider in the registry, tokenizer-backed or not. Parametrised
    # rather than hardcoded to "openai" — that hardcoding is exactly why the
    # fallback branch went untested.
    _ALL_PROVIDERS = [
        "openai",
        "openrouter",
        "anthropic",
        "fireworks",
        "gemini",
        "groq",
        "cohere",
        "local",
        "huggingface",
    ]

    @pytest.mark.parametrize("provider", _ALL_PROVIDERS)
    async def test_dense_body_above_the_floor_never_raises(
        self, router, mock_registry, provider
    ):
        """The reviewer's B1 case, run on EVERY provider. 3143+ chars of
        id-dense JSON against a 1000-token floor: ~1622 real tokens, but 785
        under ``len // 4`` — which raised on five of nine providers."""
        import json
        import uuid

        body = json.dumps(
            [
                {
                    "id": str(uuid.uuid4()),
                    "ts": "2026-08-19T22:14:03.221Z",
                    "v": i * 1.37,
                    "src": "pool-exhaustion",
                }
                for i in range(34)
            ],
            separators=(",", ":"),
        )
        assert len(body) >= 3143
        assert len(body) // 4 < 1000, "sample must be one the old divisor failed"
        mock_registry.route_request = AsyncMock(
            return_value=_response(body, StopReason.MAX_TOKENS, provider=provider)
        )
        response = await router.route(
            prompt="test", max_tokens=4000, min_output_tokens=1000, bypass_cache=True
        )
        assert response.is_truncated

    @pytest.mark.parametrize("provider", _ALL_PROVIDERS)
    async def test_genuinely_starved_body_still_raises_everywhere(
        self, router, mock_registry, provider
    ):
        """The other end of the contract: the conservative fallback must not
        blunt the guard. The fm#1094 body — 215 chars, ~53 real tokens — is an
        order of magnitude under a 500-token floor on any tokenizer."""
        mock_registry.route_request = AsyncMock(
            return_value=_response("x" * 215, StopReason.MAX_TOKENS, provider=provider)
        )
        with pytest.raises(LLMOutputFloorError):
            await router.route(
                prompt="test",
                max_tokens=2000,
                min_output_tokens=500,
                bypass_cache=True,
            )

    async def test_unknown_provider_does_not_crash_the_floor_check(
        self, router, mock_registry
    ):
        """``LLMResponse.provider`` is unvalidated and downstream repos build
        their own responses; ``estimate_tokens`` calls ``provider.lower()``.
        An AttributeError here is swallowed and reported as "All providers
        failed" — a floor bug wearing a routing bug's clothes."""
        mock_registry.route_request = AsyncMock(
            return_value=_response("word " * 2000, StopReason.MAX_TOKENS, provider=None)
        )
        response = await router.route(
            prompt="test", max_tokens=4000, min_output_tokens=500, bypass_cache=True
        )
        assert response.is_truncated

    async def test_degenerate_long_run_is_bounded(self, router, mock_registry):
        """tiktoken's merge loop is super-linear on a long unbroken run of one
        character — the "model looped until MAX_TOKENS" body this branch
        selects for. Measured unbounded: 32k chars = ~1.3 s of BLOCKING CPU on
        the event loop, against 1-4 ms for prose. Bounded by prefix-and-scale.
        """
        import time

        mock_registry.route_request = AsyncMock(
            return_value=_response("x" * 32000, StopReason.MAX_TOKENS)
        )
        started = time.perf_counter()
        await router.route(
            prompt="test", max_tokens=40000, min_output_tokens=500, bypass_cache=True
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert elapsed_ms < 250, (
            f"floor check took {elapsed_ms:.0f} ms on a degenerate body — the "
            f"tokenizer input is meant to be bounded"
        )
        # …and the bound must not buy its speed with a wrong number. Asserting
        # only elapsed time let a mechanism that UNDER-states pass unnoticed.
        from faultmaven.infrastructure.llm.router import (
            _estimate_visible_output_tokens,
        )
        from faultmaven.utils.token_estimation import estimate_tokens

        body = "x" * 32000
        assert _estimate_visible_output_tokens(
            body, "openai", "gpt-5.4-mini"
        ) >= estimate_tokens(body, provider="openai")


@pytest.mark.unit
@pytest.mark.llm
class TestEstimateNeverUnderStates:
    """The invariant behind the CPU bound, bound as a PROPERTY rather than as
    a mechanism: whatever the estimator does internally, it must never report
    fewer tokens than the text really holds.

    Under-stating fires the floor on a body that met it and kills an
    otherwise-usable turn; over-stating only wastes a guard. The first version
    of the bound — tokenize a 4k prefix, scale by ``len/4000`` — violated this
    on any body whose sparse part comes first, which is why the assertion here
    is on the direction and not on the sampling scheme.
    """

    def _real_tokens(self, text):
        from faultmaven.utils.token_estimation import estimate_tokens

        return estimate_tokens(text, provider="openai", model="gpt-4")

    def _adversarial_body(self):
        """4k of prose followed by 12k of id-dense JSON.

        The reviewer's exact shape: the prefix is ~6.5 chars/token and the
        tail ~2, so any estimate extrapolated from the head understates the
        whole by roughly 3x.
        """
        import json
        import uuid

        prose = (
            "The database connection pool became exhausted during the "
            "traffic spike. " * 100
        )[:4000]
        dense = json.dumps(
            [
                {
                    "id": str(uuid.uuid4()),
                    "ts": "2026-08-19T22:14:03.221Z",
                    "v": i * 1.37,
                }
                for i in range(200)
            ],
            separators=(",", ":"),
        )[:12000]
        return prose + dense

    @pytest.mark.parametrize(
        "provider", ["openai", "openrouter", "anthropic", "fireworks", "gemini"]
    )
    def test_never_under_states_on_a_mixed_density_body(self, provider):
        from faultmaven.infrastructure.llm.router import (
            _estimate_visible_output_tokens,
        )

        body = self._adversarial_body()
        estimate = _estimate_visible_output_tokens(body, provider, None)
        assert estimate >= self._real_tokens(body), (
            f"{provider}: estimated {estimate} for a body holding "
            f"{self._real_tokens(body)} real tokens — under-stating fires the "
            f"floor on a body that met it"
        )

    @pytest.mark.parametrize(
        "shape",
        [
            "prose",
            "dense_json",
            "degenerate_run",
            "base64",
            "mixed",
            # Multi-byte shapes. Their absence is exactly why a
            # character-based ceiling survived review: every shape above is
            # ASCII, where bytes and characters coincide.
            "emoji",
            "cjk",
            "mixed_script",
        ],
    )
    def test_never_under_states_across_shapes(self, shape):
        import base64
        import json
        import os
        import uuid

        from faultmaven.infrastructure.llm.router import (
            _estimate_visible_output_tokens,
        )

        bodies = {
            "prose": "The pool became exhausted during the spike. " * 300,
            "dense_json": json.dumps(
                [{"id": str(uuid.uuid4()), "v": i} for i in range(300)],
                separators=(",", ":"),
            ),
            "degenerate_run": "x" * 20000,
            "base64": base64.b64encode(os.urandom(9000)).decode(),
            "mixed": self._adversarial_body(),
            "emoji": "🔥🚀💡" * 400,
            "cjk": "数据库连接池耗尽导致请求超时" * 200,
            "mixed_script": ("pool exhausted 数据库 🔥 переполнение " * 200),
        }
        body = bodies[shape]
        estimate = _estimate_visible_output_tokens(body, "openai", "gpt-4")
        assert estimate >= self._real_tokens(body), f"{shape} under-stated"


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestLongMixedBodyDoesNotFalsePositive:
    """Item 1 end-to-end, through the real ``route()``.

    The 16,000-char adversarial body holds ~7,180 real tokens. Under
    prefix-and-scale it scored 2,448 and raised on any floor in 2449-7180 — a
    body that exceeded a 4000-token floor by 77% being killed.
    """

    async def test_no_raise_at_a_floor_the_body_genuinely_met(
        self, router, mock_registry
    ):
        import json
        import uuid

        prose = (
            "The database connection pool became exhausted during the "
            "traffic spike. " * 100
        )[:4000]
        dense = json.dumps(
            [
                {
                    "id": str(uuid.uuid4()),
                    "ts": "2026-08-19T22:14:03.221Z",
                    "v": i * 1.37,
                }
                for i in range(200)
            ],
            separators=(",", ":"),
        )[:12000]
        body = prose + dense
        assert len(body) == 16000

        mock_registry.route_request = AsyncMock(
            return_value=_response(body, StopReason.MAX_TOKENS, provider="openai")
        )
        response = await router.route(
            prompt="test", max_tokens=20000, min_output_tokens=4000, bypass_cache=True
        )
        assert response.is_truncated


@pytest.mark.unit
@pytest.mark.llm
class TestCeilingIsBytesNotCharacters:
    """The ceiling is the body's UTF-8 BYTE count, and that is load-bearing.

    ``tokens <= chars`` is only the ASCII corollary of the real bound. What is
    provable is ``tokens <= bytes``: a byte-level BPE token maps to a
    non-empty byte string, so N bytes cannot produce more than N tokens for
    any script. Outside ASCII the character form fails — measured, a 4,000
    mixed-script fuzz put 3,912 strings over the character rule (worst 3.00
    tokens per character) and zero over the byte count.
    """

    def _real_tokens(self, text):
        from faultmaven.utils.token_estimation import estimate_tokens

        return estimate_tokens(text, provider="openai", model="gpt-4")

    def test_emoji_body_would_have_broken_the_character_rule(self):
        """The case that motivates the unit. Asserts BOTH halves: the old
        character ceiling really would have under-stated here (so this test
        would fail against it), and the byte ceiling really does bound it."""
        from faultmaven.infrastructure.llm.router import (
            _estimate_visible_output_tokens,
        )

        body = "🔥🚀💡" * 100
        real = self._real_tokens(body)
        assert real > len(body), (
            "sample must be one the character rule under-states: "
            f"{real} tokens vs {len(body)} chars"
        )
        assert real <= len(body.encode("utf-8"))
        # gemini has no tokenizer, so this takes the conservative branch.
        assert _estimate_visible_output_tokens(body, "gemini", None) >= real

    @pytest.mark.parametrize(
        "provider", ["gemini", "groq", "cohere", "local", "huggingface", "openai"]
    )
    def test_multibyte_body_never_under_states_on_any_provider(self, provider):
        from faultmaven.infrastructure.llm.router import (
            _estimate_visible_output_tokens,
        )

        # Long enough that the tokenizer-backed providers also take the
        # conservative branch, so this covers both untokenizable cases.
        body = "🔥🚀💡 数据库连接池耗尽 " * 500
        estimate = _estimate_visible_output_tokens(body, provider, None)
        assert estimate >= self._real_tokens(body)

    def test_ascii_boundary_cases_are_unchanged_by_the_unit_switch(self):
        """Both contract boundaries are pure ASCII, where bytes == chars, so
        switching the unit moved neither of them."""
        from faultmaven.infrastructure.llm.router import (
            _estimate_visible_output_tokens,
        )

        starved = "x" * 215
        assert len(starved.encode("utf-8")) == len(starved)
        # Still below a 500-token floor -> still raises (covered end-to-end
        # elsewhere; this pins the number the check compares).
        assert _estimate_visible_output_tokens(starved, "gemini", None) < 500

        dense = "a" * 3143
        assert len(dense.encode("utf-8")) == len(dense)
        assert _estimate_visible_output_tokens(dense, "gemini", None) >= 1000
