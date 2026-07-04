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

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case


# Work-gate + stall thresholds. These MIRROR ``ProgressMonitor``'s ``EXHAUSTED``
# thresholds (``progress_monitor.py``) via the design decomposition: the ≥2
# preconditions are the WORK GATE (did real diagnostic work happen?), while the
# turn/stall thresholds are the PROGRESS AXIS (has progress stalled?). Wiring the
# turn thresholds into the work gate would re-break the fast-exhaustion case.
# Kept as module constants for the compute-only Phase 0; Phase 1 wiring should
# source them from the same place ``ProgressMonitor`` reads, so the two never
# drift (the composition-seam closure this whole effort exists to make).
_WORK_GATE_MIN_CATEGORIES = 2
_WORK_GATE_MIN_HYPOTHESES = 2
_WORK_GATE_MIN_EVIDENCE = 2
_STALL_MIN_TURN = 8
_STALL_MIN_TURNS_WITHOUT_PROGRESS = 5


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
        len(case.hypotheses) >= _WORK_GATE_MIN_HYPOTHESES
        and len(categories) >= _WORK_GATE_MIN_CATEGORIES
        and len(case.evidence) >= _WORK_GATE_MIN_EVIDENCE
    )


def _is_grounded(case: "Case") -> bool:
    """Grounding axis. Reuses the drift-locked harvest grade so this reading can
    never diverge from §7 ``GROUNDED``."""
    return grade_cause_assurance(case) == CauseAssuranceGrade.GROUNDED


def _is_stalled(case: "Case") -> bool:
    """Progress axis: ``EXHAUSTED``'s stall thresholds ONLY — deliberately NOT
    its ≥2 preconditions (those are the work gate)."""
    return (
        case.current_turn >= _STALL_MIN_TURN
        and case.turns_without_progress >= _STALL_MIN_TURNS_WITHOUT_PROGRESS
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
            if _is_stalled(case)
            else VerificationStatus.HEALTHY
        )
    # Not grounded. Separate "no real work yet" from a genuine wall (§5.2) so a
    # model that produced nothing is never blamed on the case.
    if not work_gate_passed(case):
        return VerificationStatus.NOT_YET_PRODUCTIVE
    return (
        VerificationStatus.INSUFFICIENT_EVIDENCE
        if _is_stalled(case)
        else VerificationStatus.OPEN
    )
