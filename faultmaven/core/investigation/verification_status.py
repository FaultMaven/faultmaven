"""Verification status — the unified reading of whether a grounded cause is
reachable, and if not, why.

See ``docs/architecture/investigation-engine/insufficient-evidence-handling.md``.

This module is a pure, compute-only **join of the two existing layers** — the
causal-graph grounding grade (``grade_cause_assurance``) and the hypothesis-layer
stall signals. By reading the existing signals rather than re-deriving them, it
stays a coordinating read over one source of truth per axis — not a third
parallel signal. Its output is consumed by the turn pipeline
(``milestone_engine._recompute_assessment_state`` writes it onto
``case.progress.verification_status`` each turn, and ``engine_owned_affordances``
drives the code-guarded structured handoff from it) and by the terminal
capture-on-close hook. The ``VerificationStatus`` enum lives in the domain layer
(``modules.case.contracts``) so it can be a persisted field; this module imports
and re-exports it.
"""

from __future__ import annotations

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
from faultmaven.modules.case.contracts import (
    HypothesisState,
    NeedObtainability,
    NeedPurpose,
    VerificationStatus,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

# ``VerificationStatus`` is defined in the domain layer (``modules.case``) and
# imported here — the same direction as ``CauseState`` / ``NeedObtainability`` —
# so it can be a persisted field on ``InvestigationProgress`` without inverting
# the module dependency. Re-exported so existing ``from
# ...verification_status import VerificationStatus`` call sites keep working.
__all__ = [
    "VerificationStatus",
    "assess_verification_status",
    "work_gate_passed",
    "is_stalled",
    "is_progress_stalled",
]


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
    """Time-threshold arm of the progress axis: ``EXHAUSTED``'s stall thresholds
    ONLY — deliberately NOT its ≥2 preconditions (those are the work gate).

    The full progress axis is ``is_progress_stalled`` (this OR a declared data
    wall); this cheap two-int check is the fast arm, evaluated first."""
    return (
        case.current_turn >= EXHAUSTION_MIN_TURNS
        and case.turns_without_progress >= EXHAUSTION_STALL_THRESHOLD
    )


def _residual_candidates(case: "Case") -> list:
    """Candidates still in play (design §2 *residual candidate*): hypotheses
    whose state is NOT ``REFUTED`` or ``RETIRED``."""
    return [
        h
        for h in case.hypotheses.values()
        if h.state not in (HypothesisState.REFUTED, HypothesisState.RETIRED)
    ]


def _candidate_unresolvable(case: "Case", hypothesis_id: str) -> bool:
    """Whether a candidate is *unresolvable* (D1): it has **≥1** outstanding
    ``causal_verification`` discriminator **and all** of them are declared
    ``UNOBTAINABLE``. A candidate with **zero** discriminators is *unknown*, not
    unresolvable — the guard against a vacuous ∀-over-the-empty-set."""
    discriminators = [
        n
        for n in case.evidence_needs
        if n.purpose == NeedPurpose.CAUSAL_VERIFICATION
        and n.is_outstanding
        and hypothesis_id in n.motivating_hypothesis_ids
    ]
    if not discriminators:
        return False
    return all(
        n.obtainability == NeedObtainability.UNOBTAINABLE for n in discriminators
    )


def _declared_wall(case: "Case") -> bool:
    """Declared-data-wall arm of the progress axis (§5.3): **every** residual
    candidate is unresolvable. A stall the model's obtainability declarations
    *establish* — so a fully-declared wall reaches the handoff immediately rather
    than waiting out the time thresholds. Requires ≥1 residual candidate (an
    empty differential is not a wall)."""
    residual = _residual_candidates(case)
    if not residual:
        return False
    return all(_candidate_unresolvable(case, h.hypothesis_id) for h in residual)


def is_progress_stalled(case: "Case") -> bool:
    """Progress axis for the NOT-grounded disposition (§5.3): stalled by the time
    thresholds **or** by a declared data wall. Used for the
    ``OPEN``→``INSUFFICIENT_EVIDENCE`` decision and the handoff trigger.

    The wall arm is scoped to the not-grounded branch on purpose — it is about
    not being able to *ground* a cause, so it must not touch the grounded
    ``TREATMENT_BLOCKED`` (fix-reachability) disposition, which reads the plain
    time-``is_stalled``. Monotonic: the wall can only move a not-grounded case
    toward ``INSUFFICIENT_EVIDENCE``. The cheap time check is evaluated first via
    short-circuit, before the (relatively expensive) ``_declared_wall`` walk."""
    return is_stalled(case) or _declared_wall(case)


def assess_verification_status(case: "Case") -> VerificationStatus:
    """Compute the verification status as the grounding × progress join.

    Reads the grounding grade × the progress axis (``is_progress_stalled`` —
    time thresholds OR a model-declared data wall). No persistence yet (Phase 3).
    The caller must run this AFTER any deductive-validation stamp in the turn
    pipeline (mirroring the #593 recompute), so the grounding-first disposition
    reads a fresh grade rather than pre-empting the deductive arm.
    """
    if _is_grounded(case):
        # Grounded × stalled = TREATMENT_BLOCKED, meaning "have a cause but can't
        # reach a *verified fix*". That is a fix-reachability stall — the time
        # thresholds only. The declared data wall is about not being able to
        # *ground* a cause (discriminator obtainability); on an already-grounded
        # case it is moot, so it must NOT flip HEALTHY → TREATMENT_BLOCKED. The
        # wall arm therefore belongs only to the not-grounded branch below.
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
        if is_progress_stalled(case)
        else VerificationStatus.OPEN
    )
