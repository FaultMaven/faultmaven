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
    page = case.turns_without_progress  # one full page per non-progress turn
    offset = (page * _SURFACED_CAUSAL_CAP) % pool_size
    return [ranked[(offset + i) % pool_size] for i in range(_SURFACED_CAUSAL_CAP)]
