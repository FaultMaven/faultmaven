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
    def test_notice_appended_only_when_truncated(self):
        cut = annotate_if_truncated("half an ans", _response(StopReason.MAX_TOKENS))
        whole = annotate_if_truncated("a full answer", _response(StopReason.STOP))

        assert cut.endswith(TRUNCATION_NOTICE)
        assert cut.startswith("half an ans")
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
