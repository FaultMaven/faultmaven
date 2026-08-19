"""The shared "give it more room once" recovery, and its boundaries.

The helper deliberately does NOT raise on truncation. What to do with a cut
that survives the retry differs per consumer — a read path returns the partial
with a notice, a write path refuses to persist anything — so the helper hands
the final response back and the caller decides. These tests pin that split, and
the two ways the retry must NOT fire: on a stop reason that is merely unknown,
and on a safety block.
"""

import pytest

from faultmaven.infrastructure.llm.providers import LLMResponse, StopReason
from faultmaven.infrastructure.llm.truncation import (
    TRUNCATION_NOTICE,
    annotate_if_truncated,
    generate_with_truncation_retry,
)

pytestmark = [pytest.mark.unit, pytest.mark.llm]


def _response(stop_reason: StopReason, content: str = "answer") -> LLMResponse:
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=10,
        response_time_ms=5,
        stop_reason=stop_reason,
    )


class _Recorder:
    """Returns a scripted sequence, remembering the cap each call was made at."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.caps = []

    async def __call__(self, cap: int) -> LLMResponse:
        self.caps.append(cap)
        return self._responses[min(len(self.caps) - 1, len(self._responses) - 1)]


@pytest.mark.asyncio
class TestRetryPolicy:
    async def test_complete_response_is_not_retried(self):
        call = _Recorder(_response(StopReason.STOP))
        result = await generate_with_truncation_retry(call, max_tokens=1000)

        assert call.caps == [1000]
        assert result.is_truncated is False

    async def test_truncated_response_retries_once_at_double(self):
        call = _Recorder(
            _response(StopReason.MAX_TOKENS, "cut"),
            _response(StopReason.STOP, "whole"),
        )
        result = await generate_with_truncation_retry(call, max_tokens=1000)

        assert call.caps == [1000, 2000]
        assert result.content == "whole"
        assert result.is_truncated is False

    async def test_retry_is_bounded_by_the_ceiling(self):
        call = _Recorder(_response(StopReason.MAX_TOKENS))
        await generate_with_truncation_retry(call, max_tokens=1000, ceiling=1500)

        assert call.caps == [1000, 1500]

    async def test_no_retry_when_already_at_the_ceiling(self):
        """Retrying at the same cap cannot differ; it only spends money."""
        call = _Recorder(_response(StopReason.MAX_TOKENS))
        result = await generate_with_truncation_retry(
            call, max_tokens=1000, ceiling=1000
        )

        assert call.caps == [1000]
        assert result.is_truncated is True

    async def test_still_truncated_is_returned_not_raised(self):
        """The helper reports; the caller decides. It never chooses for them."""
        call = _Recorder(_response(StopReason.MAX_TOKENS, "cut"))
        result = await generate_with_truncation_retry(call, max_tokens=1000)

        assert call.caps == [1000, 2000]
        assert result.is_truncated is True
        assert result.content == "cut"

    @pytest.mark.parametrize(
        "reason", [StopReason.UNKNOWN, StopReason.CONTENT_FILTER, StopReason.TOOL_CALLS]
    )
    async def test_only_max_tokens_triggers_a_retry(self, reason):
        """UNKNOWN is not evidence of a cut, and a safety block is not one.

        Retrying every no-signal response would double the bill for the
        majority of traffic on providers that simply do not report. Retrying a
        CONTENT_FILTER at double the budget buys the same refusal again.
        """
        call = _Recorder(_response(reason))
        await generate_with_truncation_retry(call, max_tokens=1000)

        assert call.caps == [1000]


class TestAnnotation:
    def test_notice_prepended_only_when_truncated(self):
        cut = annotate_if_truncated("half an ans", _response(StopReason.MAX_TOKENS))
        whole = annotate_if_truncated("a full answer", _response(StopReason.STOP))

        assert cut.startswith(TRUNCATION_NOTICE)
        assert "half an ans" in cut
        assert whole == "a full answer"

    def test_notice_annotates_rather_than_replaces(self):
        """The substitute-a-sentinel channel is retired; this must not revive it.

        A placeholder that REPLACES content is the anti-pattern #1094 removed
        from the Gemini provider. The notice is additive: the partial answer is
        still there, flagged.
        """
        cut = annotate_if_truncated(
            "the real partial text", _response(StopReason.MAX_TOKENS)
        )

        assert "the real partial text" in cut

    def test_the_notice_survives_a_head_keeping_character_cap(self):
        """Placement is load-bearing, not stylistic.

        Every consumer that annotates feeds a relay which trims to a character
        cap by KEEPING THE HEAD. A truncated synthesis by definition filled its
        token budget, so it lands well over that cap — meaning a notice
        appended at the TAIL is dropped in exactly the case where the answer is
        longest and its incompleteness matters most, leaving only the relay's
        own generic marker, which cannot distinguish "the relay trimmed this"
        from "the model was cut off".

        Sized against the real budget rather than a made-up number, so a change
        to the wrapper or the cap re-checks this rather than silently
        invalidating it.
        """
        from faultmaven.core.investigation.milestone_engine import (
            KB_QA_RELAY_PREFIX,
            KB_QA_RELAY_SUFFIX,
            MilestoneEngine,
        )

        budget = (
            MilestoneEngine.TOOL_RESULT_MAX_CHARS
            - len(KB_QA_RELAY_PREFIX)
            - len(KB_QA_RELAY_SUFFIX)
        )
        # A synthesis that actually filled a 2000-token cap, at the 3.9-4.1
        # chars/token measured on real KB answers.
        oversized = "A" * 7900
        assert len(oversized) > budget, "the premise of this test stopped holding"

        annotated = annotate_if_truncated(
            oversized, _response(StopReason.MAX_TOKENS, oversized)
        )

        assert TRUNCATION_NOTICE in annotated[:budget]

    def test_the_notice_survives_the_real_kb_qa_relay_formatter(self):
        """The same property, asserted through the code that actually trims.

        The test above computes the budget and slices it itself, which pins the
        arithmetic but not the formatter. This drives
        ``_format_tool_result`` — the thing that really wraps and cuts a kb_qa
        answer on its way to the model — so a change to HOW it trims (not just
        to the cap) is caught too. The failing case here is the DEFAULT case:
        an answer flagged MAX_TOKENS filled its budget by definition, so it is
        always over the relay allowance.
        """
        from faultmaven.core.investigation.milestone_engine import MilestoneEngine
        from faultmaven.models.interfaces import ToolResult

        # ~8060 chars: a full 2000-token answer at the 4.03 chars/token
        # measured on real KB answers.
        answer = "The runbook says to check the connection pool. " * 172
        annotated = annotate_if_truncated(
            answer, _response(StopReason.MAX_TOKENS, answer)
        )

        relayed = MilestoneEngine._format_tool_result(
            ToolResult(success=True, data=annotated), tool_name="kb_qa"
        )

        assert len(annotated) > MilestoneEngine.TOOL_RESULT_MAX_CHARS - 590, (
            "the premise stopped holding: this answer no longer overflows the "
            "relay budget, so the test would pass even with a tail notice"
        )
        assert TRUNCATION_NOTICE in relayed
