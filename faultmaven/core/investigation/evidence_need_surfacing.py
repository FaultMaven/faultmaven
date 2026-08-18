"""Render-time surfacing of causal evidence-needs (pure view, writes nothing).

See ``select_surfaced_causal_needs`` for the full cap-and-rotate contract (#604).
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from faultmaven.modules.case.contracts import (
    NeedObtainability,
    NeedPurpose,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, EvidenceNeed


# SURFACE cap (not an existence cap): at most this many CAUSAL_VERIFICATION
# needs are SHOWN at once — see ``select_surfaced_causal_needs``.
_SURFACED_CAUSAL_CAP = 3


#: Recorded asks at or above which a need is a candidate for ask-exhaustion.
#:
#: NOT a new policy: the prompt has stated "First mention: full request. Second:
#: brief reminder. Third+: stop surfacing" since the mention-decay rule was
#: written. Two recorded asks means the next one is the third — the one the
#: stated policy already says not to make. The engine stops making it instead of
#: asking the model to refrain, because on fm#1079 the model read that rule, and
#: the rendered ask count, and surfaced one need seven times anyway
#: (``case_897ce7909658`` / ``eneed_1fd2c33f2a43``, turns 3, 5, 6, 7, 9, 10, 12)
#: and another eight times (``case_6a540e0da057`` / ``eneed_930baee1cae6``,
#: turns 5–12), while the user was cooperating and supplying data.
_ASK_REPEAT_FLOOR = 2

#: Turns that must have elapsed since the FIRST recorded ask.
#:
#: Same derivation: the earliest a third mention can occur is two turns after
#: the first (t, t+1, t+2), so this is the age at which the stated policy's
#: "third+" case becomes reachable. Nothing about the number is tuned — both
#: thresholds are read off the policy the prompt already states.
#:
#: The AGE arm exists because the COUNT arm cannot be trusted on its own.
#: ``surfaced_turns`` records a surfacing only when an EVIDENCE-type
#: ``SuggestedFollowUp`` is emitted (``evidence_need_linking``), so a re-ask that
#: appears only in ``agent_response`` prose is invisible to it — on the fm#1079
#: run the nagging need showed 2 recorded surfacings against 5 prose asks. Age
#: is measured off ``min(surfaced_turns)``, which is correct as soon as ONE ask
#: has been recorded and cannot be pushed later by asks that go uncounted, so an
#: undercounting channel delays exhaustion by at most the turns before the
#: second recorded ask rather than preventing it.
_ASK_DECAY_AGE_TURNS = 2


def is_ask_exhausted(need: "EvidenceNeed", current_turn: int) -> bool:
    """Whether the engine has stopped putting this need to the user as an ask.

    An *ask-exhausted* need is still a live need — it stays outstanding in the
    pool, still matches inbound uploads, still counts toward the
    verification-status rollup, and is still rendered in ``<evidence_needs>``
    (in its own section, so the block does not misreport a withheld ask as a
    live one). What stops is the mechanical re-offer: the EVIDENCE suggestion is
    dropped at the seam and the need yields its rotating surface slot.

    Deliberately NOT an obtainability declaration. ``UNOBTAINABLE`` is the
    model's judgment that data cannot be gathered, and it is load-bearing —
    ``verification_status._candidate_unresolvable`` reads it, so a wrongly
    declared wall carries a case toward ``INSUFFICIENT_EVIDENCE`` on an untrue
    premise. Repetition is evidence that an ask is not working; it is not
    evidence that the data does not exist, and only the model (or the user) can
    tell those apart. So this predicate suppresses the nag and leaves the
    judgment where it belongs. Exhaustion feeds nothing that can conclude.

    Monotone in the ask history: once exhausted, a need stays exhausted for as
    long as it is outstanding. Re-arming on later activity would restore exactly
    the loop this exists to break. The escapes are the correct ones and all
    remove the need from ``is_outstanding``: it gets FULFILLED by data that
    arrives, or the model supersedes it, or the model declares the wall.
    """
    if not need.is_outstanding:
        return False
    if need.times_surfaced < _ASK_REPEAT_FLOOR:
        return False
    return current_turn - min(need.surfaced_turns) >= _ASK_DECAY_AGE_TURNS


def select_surfaced_causal_needs(case: "Case") -> "list[EvidenceNeed]":
    """The ≤``_SURFACED_CAUSAL_CAP`` causal evidence-needs to SHOW this turn.

    A SURFACE cap, not an existence cap: every need stays PENDING in
    ``case.evidence_needs`` (so a datum that arrives still grounds regardless of what is
    shown). This is a pure render-time VIEW — it writes nothing and supersedes nothing
    (superseding is terminal, so a yielded slot could never re-open; #604). The prompt
    block is the single choke point that reads this; the copilot's EVIDENCE suggestions
    are downstream of what the prompt showed, so they inherit the cap by construction.

    Applies to ALL outstanding ``CAUSAL_VERIFICATION`` needs — "≤N causal asks, whatever
    authored them" is the user-facing invariant. SYMPTOM needs are untouched.

    Ordering + coverage:
      - Rarity-first: needs are ranked by how many outstanding causal needs share their
        ``request_text`` (fewer sharers = more discriminating = shown first). This is a
        FIRST-ORDER proxy — a stale need with unique text can rank high — so it governs
        the ORDER of surfacing, not WHETHER a need is seen; the paged rotation below
        guarantees coverage regardless of ranking precision.
      - Paged rotation under non-progress: the window advances one full
        ``_SURFACED_CAUSAL_CAP``-sized PAGE per non-progress turn, so the whole pool is
        covered in ``ceil(pool / _SURFACED_CAUSAL_CAP)`` non-progress turns (vs a 1-rank
        slide, which needs ~``pool`` turns). This coverage is deliberately BOUNDED, not
        unlimited: the engine can declare exhaustion once ``turns_without_progress``
        reaches the stall threshold (``progress_monitor``), so for pools larger than
        roughly ``_SURFACED_CAUSAL_CAP * stall_threshold`` the lowest-ranked needs may not
        surface before the case is closed as exhausted. Realistic pools sit well inside
        that bound, and rarity-first ordering means the most-discriminating asks are
        always shown first anyway. On ANY progress the counter resets and the window
        returns to page 0 — correct, because a progressing case is not deadlocked
        (rotation is the safety net for a genuinely stuck one).
    """
    # A need the model declared UNOBTAINABLE yields its surface slot and stops
    # rotating back in — re-asking for data already declared ungettable is futile
    # churn. It stays outstanding in the pool (so the verification-status rollup
    # still counts it toward the declared wall, and the close record can name it
    # as the unmet need), it just isn't surfaced.
    #
    # An ASK-EXHAUSTED need yields its slot for the same reason on a different
    # trigger (``is_ask_exhausted``): the engine has stopped putting it to the
    # user, so holding one of the three rotating slots open for it would spend a
    # slot on an ask that is not being made — starving live discriminators
    # exactly as an UNOBTAINABLE one would.
    causal = [
        n
        for n in case.evidence_needs
        if n.is_outstanding
        and n.purpose == NeedPurpose.CAUSAL_VERIFICATION
        and n.obtainability != NeedObtainability.UNOBTAINABLE
        and not is_ask_exhausted(n, case.current_turn)
    ]
    if len(causal) <= _SURFACED_CAUSAL_CAP:
        return causal
    shared = Counter(n.request_text for n in causal)  # sharers per rendered datum
    ranked = sorted(causal, key=lambda n: (shared[n.request_text], n.need_id))
    pool_size = len(ranked)
    # Paged, non-overlapping rotation: one CAP-sized page per non-progress turn, so the
    # pool is covered in ceil(pool_size / CAP) turns — fast enough to sweep a realistic
    # pool before the exhaustion horizon, unlike a 1-rank slide.
    page = case.turns_without_progress  # one full page per non-progress turn
    offset = (page * _SURFACED_CAUSAL_CAP) % pool_size
    return [ranked[(offset + i) % pool_size] for i in range(_SURFACED_CAUSAL_CAP)]
