"""A truncated title is never persisted, and the sentinel blacklist is gone.

Case titles are persisted and shown in every list view, so a title cut
mid-word is a permanent artifact of a transient limit. This path used to catch
one specific shape of it — a hardcoded list of placeholder strings that the
Gemini provider substituted into ``content`` when it had nothing real to
return. Two layers, one inventing a sentinel and the other string-matching it
back out, both existing because there was no field to carry the fact (#1094).

The field exists now, so the blacklist is deleted and the check keys on the
stop reason instead — which also covers the eight providers whose truncation
the blacklist never saw, because they never wrote a sentinel.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.infrastructure.llm.providers import LLMResponse, StopReason
from faultmaven.modules.case.api.routes import _generate_title_with_llm

pytestmark = [pytest.mark.unit, pytest.mark.api]

# Long enough to take the LLM path rather than the extractive shortcut.
CONTEXT = (
    "The postgres primary in the eu-west cluster started refusing new "
    "connections at 14:02 UTC. Replication lag on the two standbys climbed "
    "from under a second to eleven minutes over the same window, and the "
    "connection pooler began returning 'too many clients already'. Restarting "
    "the pooler cleared the symptom for about ninety seconds before it "
    "returned identically. "
) * 2


def _provider(response):
    provider = MagicMock()
    provider.generate = AsyncMock(return_value=response)
    return provider


def _response(content: str, stop_reason: StopReason) -> LLMResponse:
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=8,
        response_time_ms=10,
        stop_reason=stop_reason,
    )


@pytest.mark.asyncio
class TestTruncatedTitle:
    async def test_a_cut_title_falls_back_instead_of_being_persisted(self):
        """No retry here, deliberately.

        The budget is a handful of tokens for a handful of words. A cut means
        the model ignored the length instruction, not that it needed more room,
        so a bigger cap would only buy a longer wrong answer.
        """
        provider = _provider(
            _response(
                "PostgreSQL Replication Lag On The Eu-West Prim", StopReason.MAX_TOKENS
            )
        )

        title, source = await _generate_title_with_llm(
            CONTEXT, MagicMock(), llm_provider=provider, user_signals=CONTEXT
        )

        assert source == "fallback"
        assert "Prim" != title[-4:]
        provider.generate.assert_awaited_once()

    async def test_a_complete_title_is_used(self):
        provider = _provider(
            _response("PostgreSQL Connection Exhaustion", StopReason.STOP)
        )

        title, source = await _generate_title_with_llm(
            CONTEXT, MagicMock(), llm_provider=provider, user_signals=CONTEXT
        )

        assert source == "llm"
        assert title == "PostgreSQL Connection Exhaustion"

    async def test_a_safety_block_now_arrives_as_empty_content(self):
        """The provider no longer writes "[Content blocked by safety filters]".

        With the sentinel retired, a blocked response reaches this function as
        empty content and falls through the existing empty-content guard into
        the fallback — no string matching required, and no chance of the
        sentinel itself being persisted as a title.
        """
        provider = _provider(_response("", StopReason.CONTENT_FILTER))

        title, source = await _generate_title_with_llm(
            CONTEXT, MagicMock(), llm_provider=provider, user_signals=CONTEXT
        )

        assert source == "fallback"
        assert "[" not in title
