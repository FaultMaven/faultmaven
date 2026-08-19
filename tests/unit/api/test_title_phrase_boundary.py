"""#1098 — a generated title is cut at a phrase boundary, not at a word count.

The title is capped at ``MAX_TITLE_WORDS_DEFAULT`` words. The cap landed
wherever the count ran out, which is routinely mid-phrase — the two reported
titles were exact 8-word prefixes of the problem statement:

    "The Checkout-api Deployment in Checkout-staging Has Been Repeatedly"
    "Checkout-staging's Checkout-api Became Unavailable And Degraded After Release"

This is the report's most prominent line (the summary H1 is
``f"Resolution Summary: {case.title}"``), and the same string is the case title
everywhere else, so the two consumers the issue names share one producer.

Three paths applied that cap: the two extractive ones checked only whether the
FINAL word was a known incomplete ending — and rejected the whole candidate when
it was — while the LLM over-length clip had no completeness check at all.
"""

import pytest

from faultmaven.modules.case.api.routes import (
    EXTRACTIVE_MAX_CONTENT_LENGTH,
    MAX_TITLE_WORDS_DEFAULT,
    MIN_TITLE_WORDS,
    _generate_smart_extractive_title,
    _word_can_end_title,
    get_extractive_fallback_title,
    truncate_title_at_phrase_boundary,
)

pytestmark = pytest.mark.unit

# The reported problem statements, verbatim.
_RUN_1 = (
    "The checkout-api deployment in checkout-staging has been repeatedly "
    "OOM-killed since the v2.14.0 release, causing readiness failures, "
    "elevated latency, and an 8% error rate."
)
_RUN_2 = (
    "checkout-staging's checkout-api became unavailable and degraded after "
    "release v2.14.0, with repeated container OOM kills causing crash loops, "
    "readiness failures and elevated latency."
)


# ---------------------------------------------------------------------------
# The boundary rule
# ---------------------------------------------------------------------------


def test_the_reported_title_no_longer_ends_mid_verb_phrase():
    """Run 1's cut left a dangling adverb — the verb it modified was in the
    words the cap dropped."""
    title = _generate_smart_extractive_title(_RUN_1, MAX_TITLE_WORDS_DEFAULT)

    assert title == "The Checkout-api Deployment in Checkout-staging"
    assert not title.lower().endswith("repeatedly")


def test_the_cap_still_applies():
    """The fix moves the cut, it does not remove the limit."""
    title = _generate_smart_extractive_title(_RUN_2, MAX_TITLE_WORDS_DEFAULT)

    assert len(title.split()) <= MAX_TITLE_WORDS_DEFAULT


def test_a_manner_adverb_cannot_end_a_title():
    """Detected morphologically, not by list, so an adverb the list never
    anticipated is still caught."""
    assert not _word_can_end_title("repeatedly")
    assert not _word_can_end_title("intermittently")
    assert not _word_can_end_title("sporadically")


def test_an_ly_noun_can_still_end_a_title():
    """The allowlist exists so the morphological rule does not eat nouns."""
    assert _word_can_end_title("anomaly")
    assert _word_can_end_title("supply")
    assert truncate_title_at_phrase_boundary(
        "checkout api latency anomaly".split(), 8, MIN_TITLE_WORDS
    ) == ["checkout", "api", "latency", "anomaly"]


def test_connectives_are_walked_back_over_not_just_detected():
    """The old check tested ONE word and threw the whole candidate away. Backing
    up keeps the good prefix — which is the difference between a usable title
    and falling through to a weaker source."""
    words = "the database connection pool was exhausted during the".split()

    kept = truncate_title_at_phrase_boundary(words, 8, MIN_TITLE_WORDS)

    # "during the" is walked back over; "exhausted" ends a phrase and stands.
    assert kept == ["the", "database", "connection", "pool", "was", "exhausted"]


def test_a_title_with_no_usable_boundary_is_refused():
    """Falling under the minimum returns None so the caller drops to its next
    source — the behaviour the single-word check already had."""
    assert (
        truncate_title_at_phrase_boundary("the a an of".split(), 8, MIN_TITLE_WORDS)
        is None
    )


def test_a_complete_short_title_is_untouched():
    """Nothing to cut, nothing to walk back."""
    words = "postgresql connection timeout".split()

    assert truncate_title_at_phrase_boundary(words, 8, MIN_TITLE_WORDS) == words


# ---------------------------------------------------------------------------
# Every path that applies the cap
# ---------------------------------------------------------------------------


def test_the_extractive_fallback_path_uses_the_same_rule():
    """The second extractive path had its own copy of the single-word check."""
    title = get_extractive_fallback_title(_RUN_1, "", None, MAX_TITLE_WORDS_DEFAULT)

    assert title == "The Checkout-api Deployment in Checkout-staging"


@pytest.mark.asyncio
async def test_the_llm_path_does_not_persist_a_mid_phrase_clip():
    """This path clipped to the cap with NO completeness check — an over-length
    model response was silently cut wherever the count ran out."""
    from unittest.mock import AsyncMock

    from faultmaven.infrastructure.llm.providers.base import StopReason
    from faultmaven.modules.case.api.routes import _generate_title_with_llm

    class _Resp:
        content = (
            "Checkout Api Deployment Repeatedly OOM Killed Since The v2.14.0 Release"
        )
        is_truncated = False
        stop_reason = StopReason.STOP

    provider = AsyncMock()
    provider.generate = AsyncMock(return_value=_Resp())

    # Routing sends short signals to the fast extractive path, so the LLM would
    # never be consulted on _RUN_1 alone — the signal has to clear
    # EXTRACTIVE_MAX_CONTENT_LENGTH for this path to be the one under test.
    long_signals = _RUN_1 + " " + _RUN_2 + " " + _RUN_1
    assert len(long_signals) >= EXTRACTIVE_MAX_CONTENT_LENGTH

    title, source = await _generate_title_with_llm(
        context_text=_RUN_1,
        case=None,
        max_words=MAX_TITLE_WORDS_DEFAULT,
        user_signals=long_signals,
        llm_provider=provider,
    )

    assert source == "llm", "this test must exercise the LLM path, not a fallback"
    assert len(title.split()) <= MAX_TITLE_WORDS_DEFAULT
    # The raw clip ended "…Since The"; both are walked back over.
    assert title == "Checkout Api Deployment Repeatedly OOM Killed"
