"""Verification status — the unified reading of whether a grounded cause is
reachable, and if not, why.

See ``docs/architecture/investigation-engine/insufficient-evidence-handling.md``.

Phase 0 (this module): a pure, compute-only **join of the two existing layers** —
the causal-graph grounding grade (``grade_cause_assurance``) and the
hypothesis-layer stall signals. It is NOT yet wired into the turn pipeline and
has no consumers; it changes no behavior. Later phases drive the structured
handoff from it (Phase 1), add the model-declared obtainability refinement
(Phase 2), and persist it + hook the terminal boundary (Phase 3). By reading the
existing signals rather than re-deriving them, this stays a coordinating read
over one source of truth per axis — not a third parallel signal.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from faultmaven.core.investigation.cause_assurance import (
    CauseAssuranceGrade,
    grade_cause_assurance,
)

# Thresholds come from the neutral ``exhaustion_thresholds`` module — the same
# definition ``_detect_exhaustion`` reads — so the dimensions the two readers
# measure identically cannot drift (turn/stall thresholds; category/evidence
# breadth). The hypothesis dimension is deliberately NOT shared (see that module):
# ``WORK_GATE_MIN_HYPOTHESES`` here gates *generation depth*, while the exhaustion
# detector gates *elimination depth* via its own ``EXHAUSTION_MIN_REFUTED``.
from faultmaven.core.investigation.exhaustion_thresholds import (
    EXHAUSTION_MIN_TURNS,
    EXHAUSTION_STALL_THRESHOLD,
    WORK_GATE_MIN_CATEGORIES,
    WORK_GATE_MIN_EVIDENCE,
    WORK_GATE_MIN_HYPOTHESES,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case


class VerificationStatus(str, Enum):
    """The join of two orthogonal axes — grounding (is a cause grounded?) ×
    progress (has progress stalled?) — plus the below-the-work-gate state
    (§5.1/§5.2). NOT a terminal disposition (RESOLVED/CLOSED); an assessment
    variable recomputed each turn."""

    HEALTHY = "healthy"
    """Grounded × progressing — a cause is grounded and work is advancing."""

    TREATMENT_BLOCKED = "treatment_blocked"
    """Grounded × stalled — have a cause but can't reach a *verified fix*
    (failed fix, no access, change window, waiting on another team) → escalate.
    ``FIX_FAILURE_CYCLE`` is one pattern that lands here, not the cell."""

    OPEN = "open"
    """Not grounded × progressing, with real diagnostic work underway — keep
    working, nothing special surfaced."""

    NOT_YET_PRODUCTIVE = "not_yet_productive"
    """Not grounded and the work gate has NOT been crossed — too little
    diagnostic work to judge. Separates 'the reasoner produced nothing' (a
    provider-health fact) from a genuine data wall; never a per-case
    'insufficient data' verdict."""

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    """Not grounded × stalled, after real diagnostic work — the (not-grounded ×
    stalled) cell. No cause can be grounded from currently available data →
    structured handoff. Phase 2 lets a model-declared obtainability judgment
    refine this *within* the gate; the judgment can never bypass the work gate,
    and its absence defaults to keep-engaging."""


def work_gate_passed(case: "Case") -> bool:
    """Whether real diagnostic work has happened (the §5.2 work gate): ≥N
    hypotheses across ≥N categories with ≥N evidence items.

    Also the **observability primitive** for the per-provider gate-crossing
    metric — a configured model that never crosses this is mis-provisioned, a
    provider-health fact rather than a per-case verdict.
    """
    categories = {h.category for h in case.hypotheses.values()}
    return (
        len(case.hypotheses) >= WORK_GATE_MIN_HYPOTHESES
        and len(categories) >= WORK_GATE_MIN_CATEGORIES
        and len(case.evidence) >= WORK_GATE_MIN_EVIDENCE
    )


def _is_grounded(case: "Case") -> bool:
    """Grounding axis. Reuses the drift-locked harvest grade so this reading can
    never diverge from §7 ``GROUNDED``."""
    return grade_cause_assurance(case) == CauseAssuranceGrade.GROUNDED


def is_stalled(case: "Case") -> bool:
    """Progress axis: ``EXHAUSTED``'s stall thresholds ONLY — deliberately NOT
    its ≥2 preconditions (those are the work gate).

    Public because callers gate the (relatively expensive) full
    ``assess_verification_status`` on it: a non-stalled case can never be
    ``INSUFFICIENT_EVIDENCE``/``TREATMENT_BLOCKED``, so this cheap two-int check
    short-circuits the grounding-grade computation. Single definition of *stalled*
    (shared thresholds), so no drift from reusing it as a pre-check."""
    return (
        case.current_turn >= EXHAUSTION_MIN_TURNS
        and case.turns_without_progress >= EXHAUSTION_STALL_THRESHOLD
    )


def assess_verification_status(case: "Case") -> VerificationStatus:
    """Compute the verification status as the grounding × progress join.

    Phase 0: reads only the two existing layers (grade + stall) — no model
    obtainability input (Phase 2), no persistence (Phase 3). The caller must run
    this AFTER any deductive-validation stamp in the turn pipeline (mirroring the
    #593 recompute), so the grounding-first disposition reads a fresh grade
    rather than pre-empting the deductive arm.
    """
    if _is_grounded(case):
        return (
            VerificationStatus.TREATMENT_BLOCKED
            if is_stalled(case)
            else VerificationStatus.HEALTHY
        )
    # Not grounded. Separate "no real work yet" from a genuine wall (§5.2) so a
    # model that produced nothing is never blamed on the case.
    if not work_gate_passed(case):
        return VerificationStatus.NOT_YET_PRODUCTIVE
    return (
        VerificationStatus.INSUFFICIENT_EVIDENCE
        if is_stalled(case)
        else VerificationStatus.OPEN
    )
