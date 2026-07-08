"""Render-time surfacing of causal evidence-needs (pure view, writes nothing).

The engine keeps every causal ``EvidenceNeed`` PENDING in the pool; what the
prompt SHOWS each turn is a capped, rotating window over that pool. Capping at
render — not emission — is what prevents the demand-side deadlock (#604): an
existence cap lets a few unanswerable asks lock the slots forever; a surface
cap rotates past them, so an answerable discriminator is never permanently
hidden behind unanswerable ones.
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


# SURFACE cap (not an existence cap): at most this many CAUSAL_VERIFICATION needs are
# SHOWN at once (``select_surfaced_causal_needs``). All needs stay PENDING in the pool;
# capping at render — not emission — keeps the full pool available so the surfaced window
# can rotate, which is what prevents the demand-side deadlock (#604): an existence cap
# lets 3 unanswerable asks lock the slots forever; a surface cap rotates past them.
_SURFACED_CAUSAL_CAP = 3

# Advance the surfaced window by one full page each this-many turns-of-non-progress
# (``case.turns_without_progress``). See ``select_surfaced_causal_needs`` for the paged
# coverage guarantee this provides and its (exhaustion-horizon) bound.
_ROTATE_EVERY_K = 1


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
    causal = [
        n
        for n in case.evidence_needs
        if n.is_outstanding
        and n.purpose == NeedPurpose.CAUSAL_VERIFICATION
        and n.obtainability != NeedObtainability.UNOBTAINABLE
    ]
    if len(causal) <= _SURFACED_CAUSAL_CAP:
        return causal
    shared = Counter(n.request_text for n in causal)  # sharers per rendered datum
    ranked = sorted(causal, key=lambda n: (shared[n.request_text], n.need_id))
    pool_size = len(ranked)
    # Paged, non-overlapping rotation: one CAP-sized page per non-progress turn, so the
    # pool is covered in ceil(pool_size / CAP) turns — fast enough to sweep a realistic
    # pool before the exhaustion horizon, unlike a 1-rank slide.
    page = case.turns_without_progress // _ROTATE_EVERY_K
    offset = (page * _SURFACED_CAUSAL_CAP) % pool_size
    return [ranked[(offset + i) % pool_size] for i in range(_SURFACED_CAUSAL_CAP)]
