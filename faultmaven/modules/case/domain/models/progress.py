from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .problem import InvestigationStage

# ============================================================
# Investigation Progress Models (Section 3)
# ============================================================


class CauseState(str, Enum):
    """Engine-derived knowledge state of the root cause (assessment variable).

    Recomputed every turn from the LLM's grounded cause-identification signal
    plus the active-hypothesis count (R1 cause_state gate, recorded in
    investigation-invariants.md's INV-17/19/20/21 flow-redesign retirement note).
    NEVER path-stripped — recording a cause the engine legitimately knows is a
    truth signal, not an earned process milestone. Drives whether the diagnostic
    machinery (hypothesis formulation + evidence-needs) runs this turn.
    """

    UNKNOWN = "unknown"
    """No cause hypothesis yet. Diagnostic machinery active."""

    CANDIDATES = "candidates"
    """Multiple plausible causes (≥2 ACTIVE hypotheses). Diagnostic machinery active."""

    IDENTIFIED = "identified"
    """Single cause known with grounded confidence. Diagnostic machinery skipped."""


class ProblemStatus(str, Enum):
    """Where the confirmed problem statement stands against the evidence.

    The single source of truth for "is the problem verified":
    ``InvestigationProgress.symptom_verified`` is derived from it and never
    stored. Every transition is written by
    ``faultmaven.core.investigation.problem_status`` — nothing else assigns it.
    """

    UNVERIFIED = "unverified"
    """The statement is confirmed by the user but not yet shown by evidence.
    No cause work (hypotheses, chains, root-cause conclusions) is accepted."""

    VERIFIED = "verified"
    """Evidence shows the stated symptom. Cause work is accepted."""


class VerificationStatus(str, Enum):
    """The join of two orthogonal axes — grounding (is a cause grounded?) ×
    progress (has progress stalled?) — plus the below-the-work-gate state
    (assessment variable).

    Recomputed each turn from case state and persisted in the progress blob;
    NOT a terminal disposition (RESOLVED/CLOSED). The engine's honest reading of
    whether a grounded cause is reachable and, if not, why. Computed by
    ``core.investigation.verification_status.assess_verification_status`` (which
    imports this enum from contracts, the same direction as ``NeedObtainability``
    and ``CauseState``). See
    insufficient-evidence-handling.md §5.1 / §5.4.
    """

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
    structured handoff. A model-declared obtainability judgment can refine into
    this *within* the gate; the judgment can never bypass the work gate, and its
    absence defaults to keep-engaging."""

    RESTATEMENT_HELD = "restatement_held"
    """Not grounded × stalled where the block is LEXICAL, not evidential (#1195):
    a ROOT that clears every validation bar — causally grounded, net supporting,
    AND-gate satisfied, not refuted — is held at INCONCLUSIVE by the §7.1
    restatement guard ALONE (``causal_graph.restatement_held_root_ids``) because
    its statement adds no content beyond the problem and the other hypotheses.

    Carved out of ``INSUFFICIENT_EVIDENCE``, whose claim — "no cause can be
    grounded from currently available data" — is FALSE on this shape and whose
    handoff asks for discriminating data that cannot move the hold. The engine
    already tells the MODEL so ("MORE SUPPORTING EVIDENCE WILL NOT VALIDATE IT",
    the restatement recovery note in ``context_builder``); this cell is what
    stops it telling the USER the opposite in the same turn. The recovery is to
    state the cause DISTINCTLY — name the mechanism, or settle the overlapping
    alternative — never to fetch more evidence."""


class CauseAssuranceGrade(str, Enum):
    """The assurance behind a case's identified cause, as one of three mutually
    exclusive grades — the M2 confirmation ladder (two-dimensional-hypothesis-
    methodology §0/§7.2) read off the causal graph.

    Computed by ``core.investigation.cause_assurance.grade_cause_assurance``
    (which imports this enum from contracts, the same direction as
    ``VerificationStatus``) and persisted on ``InvestigationProgress`` each turn.
    ``CONFIRMED`` is the §7 bar for auto-seeding reusable knowledge and the only
    grade whose conclusion may read "verified"; the other two are held back, for
    different user-facing reasons.
    """

    NO_ROOT = "no_root"
    """No VALIDATED root at all — e.g. a pure LLM-authored RootCauseConclusion
    with zero causal graph (#590 A1). Not graph-identified; ask the user to
    identify a cause."""

    MECHANISTIC = "mechanistic"
    """≥1 VALIDATED root (empirical rung evidence or a deductive derivation),
    but none counterfactually confirmed. Mechanistic grade per M2/M4: the cause
    is established enough to treat, but "verified" is withheld until the cause's
    removal is observed to remove the problem. Ask the user to confirm it."""

    CONFIRMED = "confirmed"
    """≥1 VALIDATED root borne out by a counterfactual confirmation — a SUPPORTS
    evidence link backed by a ``causal_absence_evidence`` row (the cause was
    removed and the problem went with it, M2 gone⇒gone). The only grade that may
    auto-seed reusable knowledge, and the only one that reads "verified"."""


class SolutionState(str, Enum):
    """Engine-derived knowledge state of the fix (assessment variable).

    UNKNOWN | SELECTED only this round. CANDIDATES (multi-solution deliberation,
    redesign §6) is reserved for the follow-on that reuses the hypothesis machinery
    and is intentionally not produced yet.
    """

    UNKNOWN = "unknown"
    """No solution chosen yet."""

    CANDIDATES = "candidates"
    """Multiple/complex candidate solutions needing deliberation. RESERVED — see redesign §6 follow-on; not produced this round."""

    SELECTED = "selected"
    """A single solution has been chosen."""


class SolutionFeasible(str, Enum):
    """Whether the SELECTED solution can be applied within this session.

    LLM-settable. DEFERRED routes to CLOSE-with-documented-solution (redesign §6 Q2).
    """

    NOW = "now"
    """Solution can be implemented during this troubleshooting session."""

    DEFERRED = "deferred"
    """Solution is known but implementation takes time / happens out-of-band."""


class MitigationRecord(BaseModel):
    """A single forward-only mitigation (the inserted "stop the bleeding" move).

    Replaces the legacy path-coupled mitigation gates
    (redesign R2). The engine materializes this record from the LLM's accept/verify
    gate signals plus the workaround ProposedAction:
    - ``proposed_at_turn`` is set when a ``solution_type=workaround`` action is created.
    - ``accepted`` / ``verified`` mirror the LLM gate signals (compliance detection).
    - ``completed_at_turn`` is set the turn ``verified`` flips True (the boundary for
      up-weighting pre-mitigation evidence in any later RCA).

    Single record per investigation for now (redesign §3.2.1); the flow stays open
    to user-led action so a non-mitigating insert is never a dead-end.
    """

    proposed_at_turn: Optional[int] = Field(
        default=None, description="Turn a workaround mitigation was first proposed"
    )
    accepted: bool = Field(
        default=False, description="User complied with the proposed mitigation"
    )
    verified: bool = Field(
        default=False,
        description="User confirmed the mitigation stabilized the situation",
    )
    completed_at_turn: Optional[int] = Field(
        default=None,
        description="Turn `verified` flipped True (Gate-3-equivalent boundary)",
    )

    @model_validator(mode="after")
    def _verified_requires_accepted(self) -> "MitigationRecord":
        """A mitigation cannot be verified before it was accepted."""
        if self.verified and not self.accepted:
            raise ValueError(
                "mitigation.verified=True requires mitigation.accepted=True"
            )
        return self


def investigation_stage(
    *,
    mitigation_accepted: bool,
    mitigation_verified: bool,
    solution_accepted: bool,
    solution_verified: bool,
) -> InvestigationStage:
    """The stage label the four gate milestones derive (redesign R4).

    The one copy of the rule. :attr:`InvestigationProgress.current_stage` reads
    the gates off a loaded progress blob and asks this; the cross-enterprise
    operator list, which never loads a case, reads the same four booleans out of
    the stored blob and asks this too — so the label cannot mean one thing on a
    case page and another in the operator's list.

    A mitigation that was never recorded is neither accepted nor verified, which
    is how a caller with no ``MitigationRecord`` states it.
    """
    # MITIGATION: mitigation accepted but not yet verified.
    if mitigation_accepted and not mitigation_verified:
        return InvestigationStage.MITIGATION

    # TREATMENT: solution_accepted but not yet verified
    if solution_accepted and not solution_verified:
        return InvestigationStage.TREATMENT

    # Default: DIAGNOSIS. Distinguish sub-phase via symptom_verified /
    # cause_state if needed.
    return InvestigationStage.DIAGNOSIS


class InvestigationProgress(BaseModel):
    """
    Evidence-driven progress tracking with three kinds of state, each under
    its own banner below (investigation-data-models.md §1.2):

    1. ACTION-COMPLIANCE GATES (the STAGE-GATE MILESTONES banner:
       ``mitigation``, ``solution_accepted``, ``solution_verified``). Drive the
       derived stage label and the resolution handshake. ``mitigation`` and
       ``solution_accepted`` are materialized from the LLM's compliance
       signals (Framework §4.1): the user's action is the trigger; the LLM
       recognizes it. The mitigation gate is a single record, not booleans.
       ``solution_verified`` is set only on the user's explicit confirmation,
       never by the LLM; that confirmed resolution also backfills
       ``solution_accepted``.
    2. PROGRESS INDICATORS (``problem_status``, ``solution_proposed``).
       ``problem_status`` is moved by the LLM's justified symptom claims
       through ``core/investigation/problem_status.py`` and gates cause work:
       no hypothesis, chain or root-cause conclusion is accepted until it is
       VERIFIED. ``symptom_verified`` is its derived boolean view.
       ``solution_proposed`` is engine-derived from the standing SOLUTION
       proposal.
    3. ASSESSMENT VARIABLES. Truth signals the engine recomputes every
       INVESTIGATING turn, NEVER path-stripped: ``cause_state``,
       ``cause_identification_contested``, ``cause_assurance``,
       ``cause_overclaim``, ``verification_status`` and ``solution_state``
       (which mirrors ``solution_proposed``). ``cause_state`` drives whether
       the diagnostic machinery runs. The section also holds state that is not
       recomputed: the LLM-set ``solution_feasible``, the ``work_gate_crossed``
       latch (set once, never reset), the
       ``deferred_disposition_declined_signatures`` refusal log and the
       ``last_anti_anchoring_turn`` cooldown stamp.

    Root-cause metadata and milestone completion timestamps follow them.
    """

    # ============================================================
    # STAGE-GATE MILESTONES (drive stage transitions)
    # mitigation and solution_accepted: materialized from the LLM's compliance
    # signals (Framework §4.1). solution_verified: set only on the user's
    # explicit confirmation, never by the LLM.
    # ============================================================
    mitigation: Optional[MitigationRecord] = Field(
        default=None,
        description=(
            "Mitigation insert record (redesign R2). Materialized by the "
            "engine from the LLM's mitigation accept/verify gate signals "
            "plus the workaround ProposedAction. Replaces the legacy "
            "path-coupled mitigation gates."
        ),
    )

    solution_accepted: bool = Field(
        default=False,
        description=(
            "User complied with proposed solution (inferred from submission). "
            "Triggers DIAGNOSIS → TREATMENT transition."
        ),
    )

    solution_verified: bool = Field(
        default=False,
        description=(
            "Solution effectiveness verified via User-Agent Handshake. "
            "NOT directly settable by LLM — requires explicit user confirmation. "
            "Triggers TREATMENT → RESOLVED transition."
        ),
    )

    # ============================================================
    # PROGRESS INDICATORS (LLM context, non-stage-driving)
    # problem_status is moved by the LLM's justified symptom claims through
    # core/investigation/problem_status.py, its only writer; solution_proposed
    # is engine-derived (see its field).
    # ============================================================
    problem_status: ProblemStatus = Field(
        default=ProblemStatus.UNVERIFIED,
        description=(
            "Where the confirmed problem statement stands against the "
            "evidence. VERIFIED once evidence shows the stated symptom; cause "
            "work is accepted only then."
        ),
    )

    solution_proposed: bool = Field(
        default=False,
        description=(
            "Engine-derived at the assessment recompute (INV-32): True iff a "
            "LIVE ProposedAction with action_type=SOLUTION stands (state "
            "pending/accepted) or the gate ladder advanced (solution_accepted/"
            "solution_verified). Not set by LLM; not a write-once latch — a "
            "superseded or license-lost offer drops it."
        ),
    )

    # ============================================================
    # ASSESSMENT VARIABLES (engine-derived knowledge state)
    # Truth signals, recomputed every turn, NEVER path-stripped.
    # See two-dimensional-hypothesis-methodology.md §9.2 (cause_state); the
    # R1 cause_state gate is recorded in investigation-invariants.md's
    # INV-17/19/20/21 flow-redesign retirement note.
    # ============================================================
    cause_state: CauseState = Field(
        default=CauseState.UNKNOWN,
        description=(
            "Engine-derived knowledge state of the root cause "
            "(UNKNOWN | CANDIDATES | IDENTIFIED). Replaces the boolean "
            "root_cause_identified. IDENTIFIED is the grounded cause-known "
            "signal; CANDIDATES is derived from >=2 ACTIVE hypotheses. Drives "
            "whether the diagnostic machinery runs. Recomputed each turn by the "
            "engine; never path-stripped."
        ),
    )

    verification_status: VerificationStatus = Field(
        default=VerificationStatus.NOT_YET_PRODUCTIVE,
        description=(
            "Engine-derived verification status — the grounding × progress join "
            "(HEALTHY | TREATMENT_BLOCKED | OPEN | NOT_YET_PRODUCTIVE | "
            "INSUFFICIENT_EVIDENCE | RESTATEMENT_HELD). Recomputed each turn "
            "from case state "
            "alongside cause_state (never path-stripped) and persisted in the "
            "progress blob, so the model-declared obtainability signal it reads "
            "survives across turns. Drives the code-guarded insufficient-evidence "
            "handoff and the terminal capture-on-close. Default NOT_YET_PRODUCTIVE "
            "(too little work to judge). See insufficient-evidence-handling.md §5.4."
        ),
    )

    cause_assurance: CauseAssuranceGrade = Field(
        default=CauseAssuranceGrade.NO_ROOT,
        description=(
            "Engine-derived assurance grade behind the identified cause — the "
            "M2 confirmation ladder (NO_ROOT | MECHANISTIC | CONFIRMED). "
            "Recomputed each turn from the causal graph alongside cause_state "
            "(never path-stripped) and persisted in the progress blob so the "
            "grade × conclusion-confidence seam is queryable per turn (#656). "
            "CONFIRMED (counterfactual, gone⇒gone) is the sole harvest "
            "authority and the only grade whose conclusion reads 'verified'."
        ),
    )

    cause_overclaim: bool = Field(
        default=False,
        description=(
            "Whether the recorded RootCauseConclusion currently claims "
            "'verified' while cause_assurance is below CONFIRMED — the M2 "
            "over-claim seam. Derived each recompute from the same predicate "
            "as the seam trace; persisted so the WARNING is edge-triggered "
            "(warn on the transition into over-claim, not once per turn) and "
            "the standing seam is queryable per case."
        ),
    )

    cause_identification_contested: bool = Field(
        default=False,
        description=(
            "Whether cause identification is MECE-contested (§7.1.2, #656): "
            ">=2 simultaneously-validated DISTINCT standing chain roots "
            "(duplicate emissions and same-live-causal-line roots collapse "
            "to one cause; a counterfactually confirmed root settles the "
            "contest). While contested, cause_state never reads IDENTIFIED "
            "and the engine conclusion mirror is withheld, until "
            "discriminating evidence resolves the contest. Recomputed each "
            "turn; persisted so the WARNING and the hold counter are "
            "edge-triggered and the standing contest is queryable per case."
        ),
    )

    deferred_disposition_declined_signatures: List[str] = Field(
        default_factory=list,
        description=(
            "Every justifying-state signature the user has REFUSED the "
            "engine's deferred-implementation disposition offer against "
            "(empty = never refused). The offer is re-proposed only when the "
            "current signature is not among these, so a refusal POSTPONES it "
            "until something about the case actually changes — it never "
            "permanently disarms it. A decline counter would instead teach "
            "the engine to abandon, which is soft-collapse (D4). Persisted in "
            "the progress blob so the refusal survives the turn that cleared "
            "``pending_transition``; before this, nothing recorded it and the "
            "proposal re-fired every single turn (fm#1122: five identical "
            "offers against five explicit declines). A SET rather than the "
            "last signature: the cause-identification leg is recomputed every "
            "turn and can flip back (``chain`` while cause_state is "
            "IDENTIFIED, ``rcc`` while it is not), so a single slot re-arms "
            "the offer on every oscillation between two states the user has "
            "already refused in. Bounded by "
            "``_MAX_DECLINED_DISPOSITION_SIGNATURES``, oldest dropped first."
        ),
    )

    last_anti_anchoring_turn: int = Field(
        default=0,
        ge=0,
        description=(
            "Turn the anti-anchoring intervention last fired (0 = never). Drives "
            "its cooldown so a detected fixation is acted on at most once per few "
            "turns rather than churning the differential every turn."
        ),
    )

    work_gate_crossed: bool = Field(
        default=False,
        description=(
            "Once-per-case latch (DF-6 / INV-39): whether this case has EVER "
            "crossed the §5.2 work gate (work_gate_passed — >=2 hypotheses "
            "across >=2 categories with >=2 evidence). Set True at the first "
            "crossing during INVESTIGATING and never reset (a later drop below "
            "the gate does not clear it). Drives the per-provider provider-floor "
            "metric (work_gate_crossed_total) exactly once so a mis-provisioned "
            "model that never crosses is observable as a fleet-health fact, not "
            "re-counted per turn. Recomputed each turn; persisted in the "
            "progress blob (migration-free)."
        ),
    )

    solution_state: SolutionState = Field(
        default=SolutionState.UNKNOWN,
        description=(
            "Knowledge state of the fix (UNKNOWN | SELECTED). CANDIDATES "
            "(multi-solution deliberation) is reserved for a follow-on and not "
            "produced this round."
        ),
    )

    solution_feasible: SolutionFeasible = Field(
        default=SolutionFeasible.NOW,
        description=(
            "Whether the SELECTED solution can be applied this session "
            "(NOW | DEFERRED). DEFERRED routes to CLOSE-with-documented-solution."
        ),
    )

    # ============================================================
    # Root Cause Metadata (populated when cause_state=IDENTIFIED)
    # ============================================================
    root_cause_likelihood: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Likelihood in root cause identification (0.0 = unknown, 1.0 = certain)",
    )

    root_cause_method: Optional[str] = Field(
        default=None,
        description="How root cause was identified: direct_analysis | hypothesis_validation | single_shot_validation | correlation | user_provided | other",
    )

    # ============================================================
    # Milestone Completion Timestamps
    # ============================================================
    verification_completed_at: Optional[datetime] = Field(
        default=None,
        description="When symptom verification milestone was completed",
    )

    investigation_completed_at: Optional[datetime] = Field(
        default=None, description="When root cause was identified"
    )

    resolution_completed_at: Optional[datetime] = Field(
        default=None, description="When solution was verified"
    )

    # ============================================================
    # Computed Properties
    # ============================================================
    @property
    def current_stage(self) -> "InvestigationStage":
        """
        Compute investigation stage as a derived UI view (redesign R4).
        The stage no longer drives prompt dispatch — it is a pure display
        label re-derived from the action-compliance gates.

        Returns one of 3 InvestigationStage enum values:
        - MITIGATION: a mitigation is accepted but not yet verified
        - TREATMENT: solution accepted but not yet verified
        - DIAGNOSIS: everything else (default investigating view)

        DIAGNOSIS is one stage with two phases distinguished by
        ``symptom_verified`` / ``cause_state``, not by the stage enum.
        Callers needing the phase distinction must consult those signals.

        The rule itself is :func:`investigation_stage`.
        """
        mitigation = self.mitigation
        return investigation_stage(
            mitigation_accepted=mitigation is not None and mitigation.accepted,
            mitigation_verified=mitigation is not None and mitigation.verified,
            solution_accepted=self.solution_accepted,
            solution_verified=self.solution_verified,
        )

    @property
    def symptom_verified(self) -> bool:
        """Whether evidence shows the stated symptom — the boolean view of
        ``problem_status``. Read-only: transitions go through
        ``core/investigation/problem_status.py``."""
        return self.problem_status == ProblemStatus.VERIFIED

    @property
    def verification_complete(self) -> bool:
        """Check if symptom verification is complete."""
        return self.symptom_verified

    @property
    def investigation_complete(self) -> bool:
        """Check if investigation progress indicators completed."""
        return self.cause_state == CauseState.IDENTIFIED

    @property
    def resolution_complete(self) -> bool:
        """Check if resolution is complete (solution verified)."""
        return self.solution_verified

    @property
    def completed_milestones(self) -> List[str]:
        """Get list of completed milestone and indicator names."""
        milestone_map = {
            # Stage-gate milestones
            "mitigation_accepted": bool(
                self.mitigation is not None and self.mitigation.accepted
            ),
            "mitigation_verified": bool(
                self.mitigation is not None and self.mitigation.verified
            ),
            "solution_accepted": self.solution_accepted,
            "solution_verified": self.solution_verified,
            # Progress indicators
            "symptom_verified": self.symptom_verified,
            "root_cause_identified": self.cause_state == CauseState.IDENTIFIED,
            "solution_proposed": self.solution_proposed,
        }
        return [name for name, completed in milestone_map.items() if completed]

    @property
    def pending_milestones(self) -> List[str]:
        """Get list of pending progress indicator names."""
        indicator_map = {
            "symptom_verified": self.symptom_verified,
            "root_cause_identified": self.cause_state == CauseState.IDENTIFIED,
            "solution_proposed": self.solution_proposed,
        }
        return [name for name, completed in indicator_map.items() if not completed]

    # ============================================================
    # Validation
    # ============================================================
    @field_validator("root_cause_method")
    @classmethod
    def valid_root_cause_method(cls, v):
        """Validate root cause method"""
        if v is not None:
            allowed = [
                "direct_analysis",
                "hypothesis_validation",
                "single_shot_validation",
                "correlation",
                "user_provided",
                "other",
            ]
            if v not in allowed:
                raise ValueError(f"root_cause_method must be one of: {allowed}")
        return v

    @model_validator(mode="after")
    def root_cause_consistency(self):
        """Ensure root cause fields are consistent with cause_state."""
        identified = self.cause_state == CauseState.IDENTIFIED
        likelihood = self.root_cause_likelihood
        method = self.root_cause_method

        if identified:
            if likelihood == 0.0:
                raise ValueError(
                    "root_cause_likelihood must be > 0 when cause_state=IDENTIFIED"
                )
            if method is None:
                raise ValueError(
                    "root_cause_method must be set when cause_state=IDENTIFIED"
                )

        return self

    @model_validator(mode="after")
    def solution_ordering(self):
        """Ensure solution milestones are ordered correctly.

        The mitigation verified⇒accepted ordering is enforced by
        MitigationRecord's own validator, not here (redesign R3).
        """
        # solution_verified requires solution_accepted
        if self.solution_verified and not self.solution_accepted:
            raise ValueError("Cannot verify solution without acceptance first")

        return self
