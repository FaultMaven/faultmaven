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

import logging

import pytest

from faultmaven.modules.case.api.routes import (
    EXTRACTIVE_MAX_CONTENT_LENGTH,
    MAX_TITLE_WORDS_DEFAULT,
    MIN_EXTRACTIVE_WORDS,
    MIN_TITLE_WORDS,
    _generate_smart_extractive_title,
    _word_can_end_title,
    get_extractive_fallback_title,
    is_title_valid,
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


def test_a_manner_adverb_cannot_end_a_cut_title():
    """Detected morphologically, not by list, so an adverb the list never
    anticipated is still caught."""
    assert not _word_can_end_title("repeatedly", was_cut=True)
    assert not _word_can_end_title("intermittently", was_cut=True)
    assert not _word_can_end_title("sporadically", was_cut=True)


def test_the_adverb_rule_does_not_fire_on_a_title_that_was_never_cut():
    """The rule's justification is that the adverb modifies a verb the cut
    removed. Under the cap the verb is still there, so "Restarting Repeatedly"
    is an idiomatic incident title, not a fragment — shortening it applies a
    reason that does not hold, and for a three-word title it drops below the
    extractive minimum and costs a paid LLM call."""
    assert _word_can_end_title("repeatedly", was_cut=False)

    for text in (
        "payments api restarting repeatedly",
        "checkout api failing intermittently",
        "service crashing constantly",
    ):
        words = text.split()
        assert (
            truncate_title_at_phrase_boundary(
                words, MAX_TITLE_WORDS_DEFAULT, MIN_EXTRACTIVE_WORDS
            )
            == words
        )


def test_a_connective_is_walked_back_even_when_nothing_was_cut():
    """No such gate for the connective rule: "…during the" reads broken however
    it got there, and the code this replaced rejected the candidate outright."""
    words = "checkout api errors spiked during the".split()

    assert truncate_title_at_phrase_boundary(
        words, MAX_TITLE_WORDS_DEFAULT, MIN_EXTRACTIVE_WORDS
    ) == ["checkout", "api", "errors", "spiked"]


def test_an_ly_noun_can_still_end_a_title():
    """The allowlist exists so the morphological rule does not eat nouns."""
    assert _word_can_end_title("anomaly", was_cut=True)
    assert _word_can_end_title("supply", was_cut=True)
    assert truncate_title_at_phrase_boundary(
        "checkout api latency anomaly".split(), 8, MIN_TITLE_WORDS
    ) == ["checkout", "api", "latency", "anomaly"]


def test_an_ly_compound_inherits_its_base_word():
    """A derived compound is judged by its final element, matched EXACTLY —
    never by suffix, because "totally".endswith("tally") would turn a real
    adverb into an allowed ender."""
    assert _word_can_end_title("bi-weekly", was_cut=True)
    assert _word_can_end_title("grafana-daily", was_cut=True)
    assert not _word_can_end_title("totally", was_cut=True)


def test_ly_proper_nouns_reaching_incident_titles_are_allowed():
    assert _word_can_end_title("fastly", was_cut=True)
    assert _word_can_end_title("july", was_cut=True)
    assert _word_can_end_title("italy", was_cut=True)


def test_a_symbol_only_token_cannot_end_a_title():
    """A dash or an arrow is punctuation, not a word. Stopping the walk-back on
    one leaves a candidate is_title_valid then rejects, so the good boundary one
    word earlier is never reached."""
    for token in ("-", "—", "→", "::"):
        assert not _word_can_end_title(token, was_cut=True)

    assert truncate_title_at_phrase_boundary(
        "checkout api down -".split(), MAX_TITLE_WORDS_DEFAULT, MIN_TITLE_WORDS
    ) == ["checkout", "api", "down"]


def test_the_kept_boundary_word_is_repaired_so_the_title_validates():
    """``_word_can_end_title`` judges a word with clinging punctuation stripped
    while the caller kept it verbatim, so a kept "Cluster," passed the boundary
    check and was then rejected by is_title_valid's alphanumeric-final-character
    rule — the case kept its placeholder title instead."""
    words = (
        "Checkout Api OOM Killed In Staging Cluster, After The Latest Release".split()
    )

    kept = truncate_title_at_phrase_boundary(
        words, MAX_TITLE_WORDS_DEFAULT, MIN_TITLE_WORDS
    )

    assert kept[-1] == "Cluster"
    assert is_title_valid(" ".join(kept))


def test_a_closing_bracket_is_not_stripped_off_the_kept_word():
    """is_title_valid accepts ")]}" as a final character, and trimming one would
    leave an unbalanced bracket."""
    kept = truncate_title_at_phrase_boundary(
        "Checkout Timeout (Staging)".split(), MAX_TITLE_WORDS_DEFAULT, MIN_TITLE_WORDS
    )

    assert kept == ["Checkout", "Timeout", "(Staging)"]
    assert is_title_valid(" ".join(kept))


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


# ---------------------------------------------------------------------------
# Mutations that survived the first cut of these tests.
# ---------------------------------------------------------------------------


def test_a_boundary_sitting_exactly_at_the_cap_is_kept():
    """Nothing pinned the cap's own index, so ``words[:max_words - 1]`` passed —
    an off-by-one that silently shortens every title by a word."""
    words = "alpha beta gamma delta epsilon zeta eta theta extra".split()

    kept = truncate_title_at_phrase_boundary(
        words, MAX_TITLE_WORDS_DEFAULT, MIN_TITLE_WORDS
    )

    assert kept == words[:MAX_TITLE_WORDS_DEFAULT]
    assert len(kept) == MAX_TITLE_WORDS_DEFAULT


def test_a_connective_carrying_punctuation_is_still_walked_back_over():
    """The boundary check strips clinging punctuation before judging. Without
    that strip "the," is not the word "the", so a connective ends the title."""
    words = "checkout api errors spiked after the,".split()

    assert truncate_title_at_phrase_boundary(
        words, MAX_TITLE_WORDS_DEFAULT, MIN_EXTRACTIVE_WORDS
    ) == ["checkout", "api", "errors", "spiked"]


def test_the_two_minimums_are_wired_to_their_own_paths():
    """The extractive path deliberately demands more words (3) than general
    validation (2). A comment called that deliberate; nothing held it, so
    swapping the two constants passed in both directions.

    Asserted at the CALL SITES, not on the helper with the constants passed by
    hand — the helper honours whatever it is given, so only the wiring can be
    wrong, and only the callers can pin it. One input discriminates: a boundary
    that leaves exactly two words.
    """
    text = "checkout degraded during the"
    assert truncate_title_at_phrase_boundary(
        text.split(), MAX_TITLE_WORDS_DEFAULT, MIN_TITLE_WORDS
    ) == ["checkout", "degraded"]

    # Smart extractive requires three — it must decline and fall through.
    assert _generate_smart_extractive_title(text, MAX_TITLE_WORDS_DEFAULT) is None
    # The fallback requires two — it must produce the title.
    assert (
        get_extractive_fallback_title(text, "", None, MAX_TITLE_WORDS_DEFAULT)
        == "Checkout Degraded"
    )


@pytest.mark.asyncio
async def test_the_llm_over_cap_refusal_is_observable(caplog):
    """Removing the refusal yields " ".join(None) -> TypeError, which the broad
    except at the call site turns into the SAME fallback — so the branch has no
    observable of its own unless it says something. It logs."""
    from unittest.mock import AsyncMock

    from faultmaven.infrastructure.llm.providers.base import StopReason
    from faultmaven.modules.case.api.routes import _generate_title_with_llm

    class _Resp:
        # Over the cap, and every word past the first is a connective, so the
        # walk-back cannot leave a usable title.
        content = "Checkout the a an of the and or but so if when while"
        is_truncated = False
        stop_reason = StopReason.STOP

    provider = AsyncMock()
    provider.generate = AsyncMock(return_value=_Resp())
    long_signals = _RUN_1 + " " + _RUN_2 + " " + _RUN_1

    with caplog.at_level(logging.INFO, logger="faultmaven.modules.case.api.routes"):
        title, source = await _generate_title_with_llm(
            context_text=_RUN_1,
            case=None,
            max_words=MAX_TITLE_WORDS_DEFAULT,
            user_signals=long_signals,
            llm_provider=provider,
        )

    assert source != "llm"
    assert any("no usable phrase boundary" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Non-ASCII invariants (cheap, and there were none).
# ---------------------------------------------------------------------------


def test_cjk_text_is_one_token_so_the_cap_never_engages():
    """CJK prose carries no spaces, so ``split()`` yields a single token — under
    both minimums, so the extractive paths decline and routing falls to the LLM
    exactly as it did before this change."""
    words = "结账服务在发布后反复崩溃".split()

    assert len(words) == 1
    assert (
        truncate_title_at_phrase_boundary(
            words, MAX_TITLE_WORDS_DEFAULT, MIN_EXTRACTIVE_WORDS
        )
        is None
    )


def test_cutting_never_splits_a_grapheme_or_a_surrogate_pair():
    """Cuts happen at whitespace-token granularity only, so no character can be
    halved — the mojibake failure mode is structurally unavailable."""
    words = "checkout 🇩🇪 région café 👩‍💻 restarting repeatedly now extra".split()

    kept = truncate_title_at_phrase_boundary(
        words, MAX_TITLE_WORDS_DEFAULT, MIN_TITLE_WORDS
    )

    assert all(w in words for w in kept)
    assert "".join(kept).encode("utf-8").decode("utf-8")


def test_accented_words_are_not_mistaken_for_punctuation():
    """The alphanumeric guard must use str.isalnum (Unicode-aware), not ASCII."""
    assert _word_can_end_title("région", was_cut=True)
    assert _word_can_end_title("café", was_cut=True)
    assert _word_can_end_title("日本語", was_cut=True)
