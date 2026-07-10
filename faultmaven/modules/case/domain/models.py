"""Case data models - Milestone-based investigation system.

This module defines the complete data structure for FaultMaven's investigation system
based on the Investigation Architecture Specification v2.0.

Key Models:
- Case: Root case entity with milestone-based progress tracking
- CaseState: Lifecycle state (INQUIRY -> INVESTIGATING -> RESOLVED/CLOSED)
- InvestigationProgress: 7 milestones tracking verification, diagnosis, and resolution
- ProblemVerification: Consolidated symptom, scope, timeline, and changes data
- Evidence: Categorized evidence collection with hypothesis evaluation
- Hypothesis: Optional systematic root cause exploration
- Solution: Proposed and applied solutions with verification

Architecture:
- Milestone-based progress (not phase-based)
- Two-track lifecycle: Status (user-facing) + Progress (internal detail)
- Evidence-driven advancement
- Optional hypotheses for systematic exploration
- Repository abstraction (no direct database imports)
"""

import logging
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# Cap synthetic SKIPPED inserts per turn_history gap; a larger gap signals
# corruption, not a normal one-turn interruption, so we renumber instead.
_MAX_TURN_BACKFILL = 100

# ============================================================
# Status & Lifecycle Models (Section 2)
# ============================================================


class CaseState(str, Enum):
    """
    Case lifecycle state — passive label describing a case's current condition.

    Values fall into two categories:
    - **Phases** (active work): INQUIRY, INVESTIGATING
    - **Dispositions** (terminal resolution): RESOLVED, CLOSED

    Case Actions (phase transitions and dispositions):
      INQUIRY → INVESTIGATING  (phase transition)
      INQUIRY → CLOSED         (disposition)
      INVESTIGATING → RESOLVED (disposition; includes the same-turn KB-resolution variant)
      INVESTIGATING → CLOSED   (disposition)

    v3: INQUIRY → RESOLVED edge removed. KB-driven cases collapse INVESTIGATING
    to one or two turns via per-runbook Cause attribution rather than skipping
    the phase. See docs/architecture/investigation-engine/investigation-lifecycle-logic.md
    §1.2 INVESTIGATING → RESOLVED → KB-Resolution Path.
    """

    INQUIRY = "inquiry"
    """
    Phase: Pre-investigation exploration.

    Characteristics:
    - User asking questions
    - Agent providing quick guidance
    - No formal investigation commitment
    - May transition to INVESTIGATING or reach a disposition

    Typical Duration: Minutes to hours
    """

    INVESTIGATING = "investigating"
    """
    Phase: Active formal investigation.

    Characteristics:
    - Working through stages (DIAGNOSIS, MITIGATION, TREATMENT)
    - Gathering evidence
    - Testing hypotheses
    - Applying solutions
    - May reach a disposition (RESOLVED or CLOSED)

    Typical Duration: Hours to days
    """

    RESOLVED = "resolved"
    """
    Disposition: Case closed WITH solution.

    Characteristics:
    - Problem was fixed
    - Solution verified
    - closure_reason = "resolved"
    - No further case actions allowed

    Disposition: Terminal (permanent)
    """

    CLOSED = "closed"
    """
    Disposition: Case closed WITHOUT solution.

    Characteristics:
    - Investigation abandoned/escalated
    - OR inquiry-only (no investigation)
    - closure_reason = "abandoned" | "escalated" | "inquiry_only" | "duplicate" | "other"
    - No further case actions allowed

    Disposition: Terminal (permanent)
    """

    @property
    def is_terminal(self) -> bool:
        """Check if this state is a disposition (terminal)."""
        return self in [CaseState.RESOLVED, CaseState.CLOSED]

    @property
    def is_active(self) -> bool:
        """Check if this state is a phase (active, not terminal)."""
        return self in [CaseState.INQUIRY, CaseState.INVESTIGATING]

    @property
    def is_phase(self) -> bool:
        """Check if this state represents an active phase (INQUIRY or INVESTIGATING)."""
        return self.is_active

    @property
    def is_disposition(self) -> bool:
        """Check if this state represents a terminal disposition (RESOLVED or CLOSED)."""
        return self.is_terminal


class CaseSeverity(str, Enum):
    """
    Case severity levels for prioritization and filtering.

    Severity indicates the impact and urgency of the issue:
    - LOW: Minor issue, minimal impact on operations
    - MEDIUM: Moderate issue, some impact on operations
    - HIGH: Significant issue, major impact on operations
    - CRITICAL: Severe issue, complete service disruption

    Used by API service layer for case filtering and prioritization.
    """

    LOW = "low"
    """Minor issue with minimal operational impact."""

    MEDIUM = "medium"
    """Moderate issue with some operational impact."""

    HIGH = "high"
    """Significant issue with major operational impact."""

    CRITICAL = "critical"
    """Severe issue causing complete service disruption."""

    @classmethod
    def from_string(cls, value: str) -> "CaseSeverity":
        """Convert string to CaseSeverity, case-insensitive.

        Args:
            value: String value to convert

        Returns:
            CaseSeverity enum value

        Raises:
            ValueError: If value is not a valid severity
        """
        value_lower = value.lower()
        for severity in cls:
            if severity.value == value_lower:
                return severity
        raise ValueError(
            f"Invalid severity: {value}. Must be one of: {[s.value for s in cls]}"
        )


class CaseAction(BaseModel):
    """
    Record of one case action (phase transition or disposition change).
    Provides audit trail for case lifecycle.
    """

    from_state: CaseState = Field(description="Status before the action")

    to_state: CaseState = Field(description="Status after the action")

    triggered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the action occurred",
    )

    triggered_by: str = Field(
        description="Who triggered: user_id or 'system' for automatic actions"
    )

    reason: str = Field(
        description="Human-readable reason for the action", max_length=500
    )

    @model_validator(mode="after")
    def validate_action(self):
        """Ensure case action is valid."""
        if not is_valid_action(self.from_state, self.to_state):
            raise ValueError(
                f"Invalid case action: {self.from_state} -> {self.to_state}"
            )
        return self

    class Config:
        frozen = True  # Immutable once created


def is_valid_action(from_state: CaseState, to_state: CaseState) -> bool:
    """
    Validate a case action (phase transition or disposition change).

    Valid Case Actions:
    - INQUIRY → INVESTIGATING (phase transition: start investigation)
    - INQUIRY → CLOSED (disposition: no investigation needed)
    - INVESTIGATING → RESOLVED (disposition: solution verified; includes same-turn KB-resolution)
    - INVESTIGATING → CLOSED (disposition: abandoned/escalated)

    Invalid:
    - RESOLVED → * (disposition is terminal)
    - CLOSED → * (disposition is terminal)
    - INVESTIGATING → INQUIRY (no backward phase transition)
    """
    # v3: INQUIRY → RESOLVED edge removed. KB-resolution flows through
    # INVESTIGATING via the same-turn milestone collapse (see
    # investigation-lifecycle-logic.md §1.2).
    valid_actions = {
        CaseState.INQUIRY: [
            CaseState.INVESTIGATING,
            CaseState.CLOSED,
        ],
        CaseState.INVESTIGATING: [CaseState.RESOLVED, CaseState.CLOSED],
        CaseState.RESOLVED: [],  # Disposition — terminal
        CaseState.CLOSED: [],  # Disposition — terminal
    }

    return to_state in valid_actions.get(from_state, [])


# Backward compatibility alias
is_valid_transition = is_valid_action


class MessageType(str, Enum):
    """Types of messages in a case conversation (restored from old implementation)"""

    USER_QUERY = "user_query"
    AGENT_RESPONSE = "agent_response"
    SYSTEM_EVENT = "system_event"
    DATA_UPLOAD = "data_upload"
    CASE_NOTE = "case_note"
    CASE_ACTION = "case_action"


class ParticipantRole(str, Enum):
    """Participant roles in case collaboration"""

    OWNER = "owner"
    COLLABORATOR = "collaborator"
    VIEWER = "viewer"
    SUPPORT = "support"


class InvestigationStrategy(str, Enum):
    """
    Investigation approach mode.
    Affects decision thresholds, workflow behavior, and agent prompts.
    """

    ACTIVE_INCIDENT = "active_incident"
    """
    Service is down NOW. Priority: Speed over completeness.

    Characteristics:
    - Accept hypothesis with TESTING state for quick mitigation
    - Skip to solution phase even without complete root cause analysis
    - Escalate after 3 failed attempts
    - Evidence threshold: SUPPORTS is sufficient (not STRONGLY_SUPPORTS)
    - Time pressure: Minutes matter

    Use when:
    - temporal_state = ONGOING
    - urgency_level = CRITICAL or HIGH
    - User needs immediate restoration
    """

    POST_MORTEM = "post_mortem"
    """
    Historical analysis. Priority: Thorough understanding.

    Characteristics:
    - Require VALIDATED hypothesis before root cause conclusion
    - Complete all milestones systematically
    - Escalate after hypothesis space exhausted (not time-based)
    - Evidence threshold: STRONGLY_SUPPORTS required
    - Time pressure: Days acceptable

    Use when:
    - temporal_state = HISTORICAL or INTERMITTENT (resolved)
    - No immediate service impact
    - Learning/prevention goal
    """


# ============================================================
# Closure Reason Enum
# ============================================================
#
# Sub-categorization of CLOSED state. Engine-derived from case state at
# transition time. None for non-terminal and RESOLVED cases.
#
# Both values are programmatic — derived from the state the case was closed from
# (unified opportunistic flow, no path fork):
#   - inquiry_only: INQUIRY → CLOSED (no investigation started)
#   - closed_after_investigation: INVESTIGATING → CLOSED. Folds in the former
#     `mitigation_sufficient`: a case stabilized then closed is simply an
#     investigation that closed; the documented mitigation is preserved.
#   - closed_insufficient_evidence: INVESTIGATING → CLOSED from the
#     INSUFFICIENT_EVIDENCE verification-status cell (not grounded, work-gated,
#     stalled — often a declared data wall). Capture-on-close only: the honest
#     partial (residual candidates + the specific unmet/unobtainable need,
#     already persisted on the case) is signal for calibration and the flywheel.
#     The engine never nudges toward this close (no SUGGEST_CLOSE); see
#     insufficient-evidence-handling.md §5.4.
#
# The LLM does NOT emit closure_reason; user-motivation context (e.g., "we're
# escalating") lives in the LLM-authored persistent Report's free-form summary.

VALID_CLOSURE_REASONS: set[str] = {
    "inquiry_only",
    "closed_after_investigation",
    "closed_insufficient_evidence",
}


# ============================================================
# Investigation Progress Models (Section 3)
# ============================================================


class CauseState(str, Enum):
    """Engine-derived knowledge state of the root cause (assessment variable).

    Recomputed every turn from the LLM's grounded cause-identification signal
    plus the active-hypothesis count (see investigation-flow-redesign.md R1).
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


class InvestigationProgress(BaseModel):
    """
    Evidence-driven progress tracking with two distinct milestone types:

    1. STAGE-GATE MILESTONES (4): Drive stage transitions.
       Set by the LLM in structured output when it detects user compliance
       with a ProposedAction (Framework §4.1). The LLM is the compliance
       detector — the user's action is the trigger; the LLM recognizes it.
    2. PROGRESS INDICATORS (6): Provide LLM context and analytics.
       Set by LLM in structured output. Do NOT drive stage transitions.
    """

    # ============================================================
    # STAGE-GATE MILESTONES (drive stage transitions)
    # Set by the LLM in structured output (Framework §4.1).
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
    # Set by LLM in structured output. Advisory, not controlling.
    # ============================================================
    symptom_verified: bool = Field(
        default=False,
        description="Symptom confirmed with concrete evidence (logs, metrics, user reports)",
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
    # See investigation-flow-redesign.md §4.1, R1.
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
            "INSUFFICIENT_EVIDENCE). Recomputed each turn from case state "
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

    last_anti_anchoring_turn: int = Field(
        default=0,
        ge=0,
        description=(
            "Turn the anti-anchoring intervention last fired (0 = never). Drives "
            "its cooldown so a detected fixation is acted on at most once per few "
            "turns rather than churning the differential every turn."
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
        """
        # MITIGATION: mitigation accepted but not yet verified.
        if (
            self.mitigation is not None
            and self.mitigation.accepted
            and not self.mitigation.verified
        ):
            return InvestigationStage.MITIGATION

        # TREATMENT: solution_accepted but not yet verified
        if self.solution_accepted and not self.solution_verified:
            return InvestigationStage.TREATMENT

        # Default: DIAGNOSIS. Distinguish sub-phase via symptom_verified /
        # cause_state if needed.
        return InvestigationStage.DIAGNOSIS

    @property
    def stage_display_name(self) -> str:
        """
        User-facing stage name for UI display (redesign R4).

        - DIAGNOSIS → "Investigating"
        - MITIGATION → "Mitigating"
        - TREATMENT → "Resolving"
        """
        stage = self.current_stage
        if stage == InvestigationStage.DIAGNOSIS:
            return "Investigating"
        elif stage == InvestigationStage.MITIGATION:
            return "Mitigating"
        else:  # TREATMENT
            return "Resolving"

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


class InvestigationStage(str, Enum):
    """
    Investigation stage within the Investigating Phase.

    These three stages are pure DERIVED DISPLAY labels in the unified
    opportunistic flow. They are re-derived from the action-compliance
    gates (see ``InvestigationProgress.current_stage``); they do NOT drive
    prompt dispatch and there is NO path fork or prospective routing.

    - DIAGNOSIS → "Investigating" (default view)
    - MITIGATION → "Mitigating" (an optional inserted sub-activity)
    - TREATMENT → "Resolving"

    MITIGATION is not a separate path — it is an optional "stop the
    bleeding" insert that surfaces while the investigation continues.
    """

    DIAGNOSIS = "diagnosis"
    """
    Understand the problem, diagnose root cause, propose actions.

    This is the default and starting stage. Covers symptom verification,
    hypothesis formulation, hypothesis validation, and root cause analysis.

    Activities (natural flow, not rigid steps):
    - Verify symptoms with evidence (logs, metrics, user reports)
    - Assess scope and timeline
    - Form and test hypotheses
    - Identify root cause
    - Propose a concrete action (solution or mitigation)

    Transitions:
    - User complies with proposed solution → TREATMENT (solution_accepted)
    - User accepts mitigation offer → MITIGATION (mitigation_accepted)
    - Returns here from MITIGATION after mitigation verified
    """

    MITIGATION = "mitigation"
    """
    Apply and verify a temporary fix to stop the bleeding.

    Optional stage — only entered when user accepts a mitigation proposal
    during DIAGNOSIS. The goal is to stabilize, NOT to find root cause.

    Activities:
    - Guide user through applying temporary fix
    - Verify mitigation effectiveness
    - Communicate that this is temporary

    Transitions:
    - User confirms mitigation worked → back to DIAGNOSIS (mitigation_verified)
    - For root cause analysis and permanent fix
    """

    TREATMENT = "treatment"
    """
    Verify the applied fix resolves the problem.

    Entered when user demonstrates acceptance by executing the proposed
    solution and submitting results. If fix fails, performs extended
    diagnosis within TREATMENT (does NOT regress to DIAGNOSIS).

    Activities:
    - Verify fix results from user's submission
    - If fix failed: targeted evidence gathering, new hypothesis, revised fix
    - If fix worked: confirm resolution

    Transitions:
    - User confirms fix worked → RESOLVED (solution_verified via User-Agent Handshake)
    - Fix failed → stay in TREATMENT, iterate with new evidence
    """


class TemporalState(str, Enum):
    """
    Problem temporal classification.
    Context signal only — does not drive a path fork.
    """

    ONGOING = "ongoing"
    """
    Problem is currently happening.

    Characteristics:
    - Active user impact
    - Real-time symptoms
    - Urgency to stabilize
    """

    HISTORICAL = "historical"
    """
    Problem occurred in the past.

    Characteristics:
    - No current impact
    - Post-mortem investigation
    - Can take time for thorough RCA
    """


# ============================================================
# Problem Context Models (Section 4)
# ============================================================


class UrgencyLevel(str, Enum):
    """
    Urgency classification.

    Context signal used (with TemporalState) to inform how the agent
    prioritizes mitigation vs. root-cause work within the unified
    opportunistic flow. It does not select a path — there is no path fork.
    """

    CRITICAL = "critical"
    """
    Complete unavailability, data corruption risk, security breach
    """

    HIGH = "high"
    """
    Significant degradation, >10% users affected, SLA at risk
    """

    MEDIUM = "medium"
    """
    Partial degradation, <10% users affected
    """

    LOW = "low"
    """
    Cosmetic issues, workaround available
    """

    UNKNOWN = "unknown"
    """
    Urgency not yet assessed.
    """


class ProblemConfirmation(BaseModel):
    """
    Agents initial problem understanding during inquiry.
    """

    problem_type: str = Field(
        description="Classified problem type: error | slowness | unavailability | data_issue | other",
        max_length=100,
    )

    severity_guess: str = Field(
        description="Initial severity assessment: critical | high | medium | low | unknown",
        max_length=50,
    )

    preliminary_guidance: str = Field(
        description="Initial guidance or suggestions", max_length=2000
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this confirmation was created",
    )

    @field_validator("problem_type")
    @classmethod
    def valid_problem_type(cls, v):
        """Validate problem type"""
        allowed = ["error", "slowness", "unavailability", "data_issue", "other"]
        if v not in allowed:
            raise ValueError(f"problem_type must be one of: {allowed}")
        return v

    @field_validator("severity_guess")
    @classmethod
    def valid_severity(cls, v):
        """Validate severity"""
        allowed = ["critical", "high", "medium", "low", "unknown"]
        if v not in allowed:
            raise ValueError(f"severity_guess must be one of: {allowed}")
        return v


class KnowledgeResolution(BaseModel):
    """Records instant resolution via KB match during INQUIRY phase."""

    match_id: str  # ID of case/runbook that solved it
    match_type: str  # "past_case" | "runbook" | "documentation"
    solution_applied: str  # What user actually did
    user_confirmation: str  # User's message confirming fix
    resolution_turn: int  # Turn when confirmed


class PreliminaryUrgency(BaseModel):
    """Early urgency assessment using semantic business impact."""

    level: UrgencyLevel
    is_ongoing: bool = False  # Whether the problem is currently happening
    is_incident_report: bool = False  # Whether user is reporting an incident (not FAQ)
    impact_assessment: str  # Free-text business impact description
    assessed_at_turn: int


class KnowledgeMatch(BaseModel):
    """Records a potential KB match during INQUIRY."""

    match_id: str
    match_type: str  # "past_case" | "runbook" | "documentation"
    relevance_score: float  # 0.0-1.0
    summary: str
    potential_solution: Optional[str] = None


class InquiryData(BaseModel):
    """
    Pre-investigation INQUIRY state data.
    Captures early problem exploration before formal investigation commitment.
    """

    problem_confirmation: Optional[ProblemConfirmation] = Field(
        default=None, description="Agent initial understanding of the problem"
    )

    # ============================================================
    # Problem Statement Confirmation Workflow
    # ============================================================
    proposed_problem_statement: Optional[str] = Field(
        default=None,
        description="""
        Agent formalized problem statement (clear, specific, actionable) - ITERATIVE REFINEMENT pattern.

        UI Display:
        - When None: Display "To be defined" or blank (no problem detected yet)
        - When set: Display the statement text

        Lifecycle:
        1. LLM creates initial formalization from conversation context
        2. LLM can UPDATE iteratively based on user corrections/refinements
        3. Becomes IMMUTABLE once problem_statement_confirmed = True
        4. Copied to case.description when investigation starts

        Pattern: Iterative Refinement - refine until user confirms without reservation
        """,
        max_length=1000,
    )

    problem_statement_confirmed: bool = Field(
        default=False, description="User confirmed the formalized problem statement"
    )

    problem_statement_confirmed_at: Optional[datetime] = Field(
        default=None, description="When user confirmed the problem statement"
    )

    handshake_deferred_at_turn: Optional[int] = Field(
        default=None,
        description=(
            "Turn number on which the same-turn-confirmation guard fired. "
            "When current_turn == this+1, context_builder injects HANDSHAKE_DEFERRED "
            "(re-present + ask) instead of NOT_YET_CONFIRMED, and the engine "
            "deterministically emits confirmation suggestions. Self-clears by "
            "becoming stale on subsequent turns."
        ),
        ge=0,
    )

    # ============================================================
    # Investigation Decision
    # ============================================================
    decided_to_investigate: bool = Field(
        default=False, description="Whether user committed to formal investigation"
    )

    decision_made_at: Optional[datetime] = Field(
        default=None, description="When user decided to investigate (or not)"
    )

    inquiry_turns: int = Field(
        default=0, ge=0, description="Number of turns spent in INQUIRY state"
    )

    knowledge_matches: List[KnowledgeMatch] = Field(
        default_factory=list, description="Potential solutions found in KB"
    )

    knowledge_resolution: Optional[KnowledgeResolution] = Field(
        default=None, description="Resolution details if fixed via KB match"
    )

    preliminary_urgency: Optional[PreliminaryUrgency] = Field(
        default=None, description="Early urgency assessment"
    )

    @model_validator(mode="after")
    def validate_problem_statement_immutability(self) -> "InquiryData":
        """
        Enforce immutability of proposed_problem_statement once confirmed.

        Spec Reference: Case Data Model Design lines 966-996
        Rule: proposed_problem_statement becomes IMMUTABLE after problem_statement_confirmed = True
        """
        # This validator runs after field assignment, so we cannot prevent the mutation
        # directly. Instead, we validate the final state is consistent.
        # The immutability should be enforced at the service layer by not allowing
        # updates to this field when confirmed=True.

        if self.problem_statement_confirmed and not self.proposed_problem_statement:
            raise ValueError(
                "proposed_problem_statement cannot be empty when problem_statement_confirmed is True"
            )

        if self.problem_statement_confirmed and not self.problem_statement_confirmed_at:
            # Auto-set confirmation timestamp if missing
            self.problem_statement_confirmed_at = datetime.now(timezone.utc)

        return self

    @model_validator(mode="after")
    def validate_decision_consistency(self) -> "InquiryData":
        """Validate investigation decision consistency."""
        if self.decided_to_investigate and not self.decision_made_at:
            # Auto-set decision timestamp if missing
            self.decision_made_at = datetime.now(timezone.utc)

        return self


class Change(BaseModel):
    """
    Recent change that may be relevant to the problem.
    """

    description: str = Field(description="What changed", min_length=1, max_length=500)

    occurred_at: datetime = Field(description="When the change occurred")

    change_type: str = Field(
        description="Type of change: deployment | config | scaling | code | infrastructure | data | other",
        max_length=50,
    )

    changed_by: Optional[str] = Field(
        default=None,
        description="Who made the change (user, system, team)",
        max_length=200,
    )

    details: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional structured details (version numbers, config values, etc.)",
    )

    @field_validator("change_type")
    @classmethod
    def valid_change_type(cls, v):
        """Validate change type"""
        allowed = [
            "deployment",
            "config",
            "scaling",
            "code",
            "infrastructure",
            "data",
            "other",
        ]
        if v not in allowed:
            raise ValueError(f"change_type must be one of: {allowed}")
        return v


class Correlation(BaseModel):
    """
    Correlation between a change and the symptom.
    """

    change_description: str = Field(
        description="Description of the change", max_length=500
    )

    timing_description: str = Field(
        description="Temporal relationship: '2 minutes before', 'immediately after', 'coincides with', etc.",
        max_length=200,
    )

    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in this correlation (0.0 = weak, 1.0 = strong)",
    )

    correlation_type: str = Field(
        description="Type: temporal | causal | coincidental | other", max_length=50
    )

    evidence: Optional[str] = Field(
        default=None,
        description="Evidence supporting this correlation",
        max_length=1000,
    )

    @field_validator("correlation_type")
    @classmethod
    def valid_correlation_type(cls, v):
        """Validate correlation type"""
        allowed = ["temporal", "causal", "coincidental", "other"]
        if v not in allowed:
            raise ValueError(f"correlation_type must be one of: {allowed}")
        return v


class ProblemVerification(BaseModel):
    """
    Consolidated problem verification data.

    Contains all data gathered during verification phase:
    - Symptom details
    - Scope assessment
    - Timeline
    - Recent changes
    - Correlations
    """

    # ============================================================
    # Symptom
    # ============================================================
    symptom_statement: str = Field(
        description="Clear statement of the problem symptom",
        min_length=1,
        max_length=1000,
    )

    symptom_indicators: List[str] = Field(
        default_factory=list,
        description="Specific metrics/observations confirming symptom (e.g., 'Error rate: 15%', 'P99 latency: 5s')",
    )

    # ============================================================
    # Scope
    # ============================================================
    affected_services: List[str] = Field(
        default_factory=list, description="Services/components affected"
    )

    affected_users: Optional[str] = Field(
        default=None,
        description="User impact description: 'all users' | '10% of users' | 'premium tier' | etc.",
        max_length=200,
    )

    affected_regions: List[str] = Field(
        default_factory=list, description="Geographic regions affected"
    )

    severity: str = Field(
        description="Assessed severity: CRITICAL | HIGH | MEDIUM | LOW", max_length=50
    )

    user_impact: Optional[str] = Field(
        default=None, description="Description of user-facing impact", max_length=1000
    )

    # ============================================================
    # Timeline
    # ============================================================
    started_at: Optional[datetime] = Field(
        default=None, description="When problem began (best estimate)"
    )

    noticed_at: Optional[datetime] = Field(
        default=None, description="When problem was noticed/reported"
    )

    resolved_naturally_at: Optional[datetime] = Field(
        default=None, description="If problem resolved on its own, when?"
    )

    duration: Optional[timedelta] = Field(
        default=None, description="How long problem lasted (for historical problems)"
    )

    temporal_state: Optional[TemporalState] = Field(
        default=None, description="ONGOING | HISTORICAL"
    )

    # ============================================================
    # Changes
    # ============================================================
    recent_changes: List[Change] = Field(
        default_factory=list,
        description="Recent changes that may be relevant (deployments, configs, etc.)",
    )

    correlations: List[Correlation] = Field(
        default_factory=list,
        description="Identified correlations between changes and symptom",
        max_items=10,  # Limit to top 10
    )

    correlation_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence in change-symptom correlation (0.0 = no correlation, 1.0 = certain)",
    )

    # ============================================================
    # Urgency Assessment
    # ============================================================
    urgency_level: UrgencyLevel = Field(
        default=UrgencyLevel.UNKNOWN,
        description="Urgency classification for path routing",
    )

    urgency_factors: List[str] = Field(
        default_factory=list, description="Factors contributing to urgency assessment"
    )

    # ============================================================
    # Diagnostic Feasibility (Advisory)
    # ============================================================
    rca_infeasible: bool = Field(
        default=False,
        description=(
            "Advisory signal: root cause analysis is infeasible for this problem. "
            "Set by the LLM during verification when the problem involves "
            "uncontrollable external dependencies, deprecated/EOL systems, "
            "or known intractable conditions where mitigation is the accepted "
            "strategy. Influences post-mitigation agent behavior only."
        ),
    )

    rca_infeasible_rationale: Optional[str] = Field(
        default=None,
        description=(
            "Why RCA is infeasible. Populated by the LLM when rca_infeasible=True. "
            "E.g., 'Black-box 3rd-party API with no internal telemetry'."
        ),
        max_length=500,
    )

    # ============================================================
    # Metadata
    # ============================================================
    verified_at: Optional[datetime] = Field(
        default=None, description="When verification was completed"
    )

    verification_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Overall confidence in verification accuracy",
    )

    # ============================================================
    # Computed Properties
    # ============================================================
    @property
    def is_complete(self) -> bool:
        """Check if verification has all required data"""
        return (
            bool(self.symptom_statement)
            and bool(self.severity)
            and self.temporal_state is not None
            and self.urgency_level != UrgencyLevel.UNKNOWN
        )

    @property
    def time_to_detection(self) -> Optional[timedelta]:
        """Time between problem start and detection"""
        if self.started_at and self.noticed_at:
            return self.noticed_at - self.started_at
        return None

    # ============================================================
    # Validation
    # ============================================================
    @field_validator("severity")
    @classmethod
    def valid_severity(cls, v):
        """Validate severity"""
        allowed = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
        if v.upper() not in allowed:
            raise ValueError(f"severity must be one of: {allowed}")
        return v.upper()

    @model_validator(mode="after")
    def timeline_consistency(self):
        """Ensure timeline fields are consistent"""
        started = self.started_at
        noticed = self.noticed_at
        resolved = self.resolved_naturally_at

        if started and noticed and started > noticed:
            raise ValueError("started_at cannot be after noticed_at")

        if started and resolved and started > resolved:
            raise ValueError("started_at cannot be after resolved_naturally_at")

        if noticed and resolved and noticed > resolved:
            raise ValueError("noticed_at cannot be after resolved_naturally_at")

        return self


# ============================================================
# Evidence Models (Section 5)
# ============================================================


class EvidenceCategory(str, Enum):
    """
    Evidence classification by investigation purpose.

    Four claim-attached categories forming the presence/absence
    verification quartet: ``symptom_evidence`` (symptom present),
    ``causal_evidence`` (cause present), ``symptom_absence_evidence``
    (symptom gone after a fix), ``causal_absence_evidence`` (cause gone
    after a fix). Every row is the LLM's deliberate decision to record a
    specific extract as evidence for a specific claim, created only during
    INVESTIGATING. Contextual data lives on ``uploaded_files`` — no
    evidence row is needed until the agent extracts a claim-relevant
    slice. Rejection is expressed as the absence of an evidence row;
    hypothesis-level refutation lives on ``hypothesis_evidence.stance``.

    The verification gates (``mitigation_verified`` / ``solution_verified``)
    are NOT driven by an evidence category — they are set by the LLM via the
    User-Agent Handshake / compliance detection. The absence rows are the
    durable audit trail that the readiness checks consult
    (``assess_resolution_readiness`` via ``_has_causal_absence``) to decide
    RESOLVED vs CLOSED. (The pre-migration ``mitigation_evidence`` /
    ``solution_evidence`` stage-completion categories were removed in the
    GAP-5 legacy→absence migration.)
    """

    SYMPTOM_EVIDENCE = "symptom_evidence"
    """
    Shows problem manifestation.

    Purpose: Prove the problem exists and establish scope/timeline.

    Examples:
    - Error logs showing failures
    - Metrics showing degradation (high CPU, slow response times)
    - User impact reports
    - Deployment logs showing recent changes

    Advances Milestones: symptom_verified
    """

    CAUSAL_EVIDENCE = "causal_evidence"
    """
    Points to root cause.

    Purpose: Test hypothesis about what caused the problem.

    Examples:
    - Connection pool metrics (for "pool exhausted" hypothesis)
    - Memory dumps (for "memory leak" hypothesis)
    - Network traces (for "latency" hypothesis)
    - Config changes (for "misconfiguration" hypothesis)

    Advances Milestones: root_cause_identified
    """

    SYMPTOM_ABSENCE_EVIDENCE = "symptom_absence_evidence"
    """
    Shows that a previously-observed symptom is no longer present.

    Purpose: Verify a fix attempt worked — at the level of "is the
    user-visible problem gone?" The cause may or may not still be
    there; this category answers symptom presence, not causation.

    Collected during MITIGATION (sufficient for mitigation_verified)
    and during TREATMENT (defense-in-depth confirmation alongside
    causal-absence evidence).

    Examples:
    - ``kubectl get pods`` showing ``Running`` after CrashLoopBackOff
    - Latency back under SLO after a workaround
    - Error rate trending to zero in monitoring after rollback
    - User confirms "the dashboard loads now"

    Advances Milestones: mitigation_verified (primary), contributes to
    solution_verified.
    """

    CAUSAL_ABSENCE_EVIDENCE = "causal_absence_evidence"
    """
    Shows that a previously-identified cause is no longer present.

    Purpose: Verify a permanent fix at the level of "is the root
    cause eliminated?" This is distinct from symptom absence — a
    cause can be removed without an immediate symptom change (cached
    state, downstream debris), and a symptom can be masked without
    the cause being removed (mitigation).

    Collected during TREATMENT.

    Examples:
    - ``cat /etc/app/config.yaml`` showing the corrected setting
    - DB query plan showing the expected index is used after rebuild
    - Deployment manifest showing the bad image tag has been replaced
    - Config-management run output confirming the drift has been
      reconciled

    Advances Milestones: solution_verified.
    """


class EvidenceSourceType(str, Enum):
    """
    Fundamental type of data source.

    Aligned with the data classifier (Tier 0); each value names what kind
    of data the Evidence row's source is. ``USER_DESCRIPTION`` is the
    chat-quote case where the source is a verbatim system-output quote
    embedded in the user's message, not a file.
    """

    LOGS = "logs"
    """
    Time-ordered diagnostic output.

    Includes:
    - Application logs
    - System logs
    - Command output (kubectl, curl, docker logs, etc.)
    - Distributed trace data
    - API responses
    - Error messages

    Characteristics: Time-ordered textual records of system behavior
    """

    METRICS = "metrics"
    """
    Quantitative measurements.

    Includes:
    - Time-series metrics (CPU, memory, latency)
    - Dashboards and graphs
    - Performance data
    - Resource usage statistics
    - Monitoring alerts (triggered by metrics)

    Characteristics: Numerical data, often time-series
    """

    CONFIGURATION = "configuration"
    """
    Structured system/application configuration.

    Includes:
    - Config files (YAML, JSON, TOML, env vars)
    - Database schema
    - Infrastructure definitions (Kubernetes manifests, Terraform)
    - Dependency lists

    Characteristics: Defines how system should behave
    """

    CODE = "code"
    """
    Source code.

    Includes:
    - Application code snippets
    - Code reviews
    - Function definitions
    - Scripts
    - SQL queries

    Characteristics: Executable or interpretable program text
    """

    TEXT = "text"
    """
    Prose content from a file (uploaded documentation, README, runbook
    excerpt, ticket export). Post-010: this is for prose *files*, not
    for the user's own typed narrative or observations — those aren't
    evidence under the strict model. For verbatim system-output quotes
    the user typed inline in chat (e.g., "Got: HTTP/1.1 503 Service
    Unavailable"), use ``USER_DESCRIPTION`` instead.
    """

    IMAGE = "image"
    """
    Visual content.

    Includes:
    - Screenshots (errors, dashboards, terminals)
    - Architecture diagrams
    - Graphs and charts
    - Photos

    Characteristics: Requires visual interpretation
    """

    USER_DESCRIPTION = "user_description"
    """
    Verbatim system-output quote the user typed inline in a short chat
    message (below the 10K-char threshold that triggers paste-to-file
    intake). The extract is system output (an error message, a log line,
    a metric reading) that the user quoted in their own message rather
    than uploading as a file.

    This is the ONE case where ``evidence.source_file_id`` legitimately
    is NULL — the source is the user's chat message at the same turn
    (recoverable via ``collected_at_turn`` and the case_messages join).

    The user's own descriptions, opinions, or paraphrases are NOT
    evidence (per the strict "extract must be system output" rule); the
    agent should request actual output rather than promoting a
    paraphrase. This category is exclusively for cases where the user
    pasted/typed a verbatim system slice into chat.
    """


class EvidenceStance(str, Enum):
    """
    How evidence relates to a hypothesis.
    Evaluated by LLM after evidence submission against ALL active hypotheses.
    One evidence can have different stances for different hypotheses.
    """

    SUPPORTS = "supports"
    """Evidence supports hypothesis (increase confidence)"""

    NEUTRAL = "neutral"
    """Evidence neither supports nor contradicts"""

    REFUTES = "refutes"
    """Evidence contradicts hypothesis (decrease confidence)"""


# =============================================================================
# Uploaded File (Raw File Metadata)
# =============================================================================


class UploadedFile(BaseModel):
    """
    File the user submitted to a case (upload, paste, page capture).

    Post-010 strict evidence model — two-table separation:
    - **UploadedFile**: the file-of-record. Exists in any case state
      (INQUIRY, INVESTIGATING, terminal). Carries the raw bytes (via
      ``storage_ref``) and the preprocessing artifacts (``summary``,
      ``structural_index``, ``data_type``, coverage timestamps) that
      describe the file's content.
    - **Evidence**: the claim-anchored extract-of-record. Created only
      during INVESTIGATING when the LLM extracts a focused slice via
      ``evidence_to_add`` to support a specific claim (symptom, cause,
      mitigation, or solution).

    Files are not evidence. An evidence row references its source file
    via ``Evidence.source_file_id``; the file row holds the file-level
    metadata so the evidence row can stay focused on the claim it
    supports. Not all uploaded files produce evidence — the LLM
    decides which slices, if any, are claim-relevant.
    """

    file_id: str = Field(
        default_factory=lambda: f"file_{uuid4().hex[:12]}",
        description="Unique file identifier (same as data_id in data service)",
        pattern=r"^(file_|data_)[a-f0-9]{12,16}$",  # Accept both file_ and data_ prefixes
    )

    filename: str = Field(description="Original filename", min_length=1, max_length=255)

    @field_validator("filename", mode="after")
    @classmethod
    def _filename_not_empty(cls, v: str) -> str:
        """Mirror of the DB uploaded_files_filename_not_empty CHECK:
        filename must not be whitespace-only. Pydantic ``min_length=1``
        accepts a single space; the DB rejects it. Same rule, two layers
        — neither bypassable independently."""
        if not v.strip():
            raise ValueError("filename must not be whitespace-only")
        return v

    size_bytes: int = Field(ge=0, description="File size in bytes")

    content_type: Optional[str] = Field(
        default=None,
        description="MIME type as reported on upload (e.g., text/plain, application/pdf).",
        max_length=100,
    )

    content_hash: Optional[str] = Field(
        default=None,
        description=(
            "SHA-256 of the raw file content. Used for storage-backend dedup "
            "and integrity checks. NULL when the upload is still streaming "
            "or hashing was skipped."
        ),
        max_length=64,
    )

    uploaded_at_turn: int = Field(
        ge=0, description="Turn number when file was uploaded"
    )

    uploaded_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="Upload timestamp"
    )

    uploaded_by: Optional[str] = Field(
        default=None,
        description=(
            "User who uploaded the file. NULL for system-generated uploads "
            "or after the originating user is deleted (FK SET NULL)."
        ),
        max_length=36,
    )

    upload_source: str = Field(
        default="file_upload",
        description=(
            "Provenance of the upload: how the file got into the system. "
            "Values: file_upload, paste, screenshot, page_capture, "
            "agent_generated, conversion_source. Distinct from "
            "evidence.source_type, which classifies the data shape. "
            "page_capture is the marker the rerank-page-sections pass uses "
            "to detect Copilot extension page submissions; see "
            "context_builder.py."
        ),
        max_length=50,
    )

    storage_ref: Optional[str] = Field(
        default=None,
        description=(
            "Opaque key passed to IFileStorageBackend.retrieve_file(). The "
            "backend interprets it (local FS path, S3 key, Azure blob name, "
            "etc.). May be None if processing pending."
        ),
        max_length=5000,
    )

    # ------------------------------------------------------------------
    # Preprocessing artifacts (migration 010)
    #
    # These describe the FILE, not any claim about it. Populated by the
    # Tier 0+1 preprocessor at upload time. Under the strict evidence
    # model, files are not evidence — evidence is a claim-anchored
    # extract from a file. The preprocessor's file-level outputs
    # (summary, structural index, data-type classification, coverage
    # timestamps) belong here rather than on a synthetic Evidence row.
    # ------------------------------------------------------------------
    summary: Optional[str] = Field(
        default=None,
        description=(
            "Preprocessing-generated short summary of the file. Used by "
            "the investigation agent to orient on file content without "
            "loading the whole file. May be None when preprocessing was "
            "skipped (e.g., KB-conversion source uploads)."
        ),
    )

    structural_index: Optional[str] = Field(
        default=None,
        description=(
            "Preprocessing-generated structural index of the file content "
            "(file_extract + search_map + file_meta). Read by the LLM "
            "in <evidence_collected> when the agent inspects the file. "
            "May be None when preprocessing was skipped."
        ),
    )

    data_type: Optional[str] = Field(
        default=None,
        description=(
            "Preprocessor's data-type classification of the file's "
            "content (e.g., 'logs', 'metrics', 'configuration'). "
            "Distinct from evidence.source_type, which classifies an "
            "individual extract's source type."
        ),
        max_length=50,
    )

    coverage_start_ts: Optional[datetime] = Field(
        default=None,
        description=(
            "Earliest timestamp parsed from the file's content. None "
            "when the file has no parseable timestamps."
        ),
    )

    coverage_end_ts: Optional[datetime] = Field(
        default=None,
        description=(
            "Latest timestamp parsed from the file's content. None when "
            "the file has no parseable timestamps."
        ),
    )


# =============================================================================
# Evidence (Investigation Data Linked to Hypotheses)
# =============================================================================


class Evidence(BaseModel):
    """
    A claim-anchored extract recorded during INVESTIGATING.

    Post-010 single creation path: every Evidence row originates as an
    ``EvidenceToAdd`` entry the LLM declared on a specific turn. The LLM
    chooses the ``category`` from the four claim-anchored values and the
    ``source_type`` from ``EvidenceSourceType``. The system only infers
    ``advances_milestones`` (Tier 2 inference via ``CATEGORY_MILESTONE_MAP``),
    and only when the LLM has not already overridden it (Tier 3).

    LLM declares: summary, extract (optional), category, source_type,
        source_file_id (required unless source_type=USER_DESCRIPTION),
        likelihood, optionally advances_milestones
    LLM evaluates: stance per hypothesis (creates hypothesis_evidence links)
    System fills: evidence_id, collected_at_turn, collected_by,
        coverage timestamps (parsed from extract), advances_milestones (Tier 2)
    """

    evidence_id: str = Field(
        default_factory=lambda: f"ev_{uuid4().hex[:12]}",
        description="Unique evidence identifier",
        pattern=r"^ev_[a-f0-9]{12}$",
    )

    # ============================================================
    # Purpose Classification (LLM-declared via EvidenceToAdd)
    # ============================================================
    category: EvidenceCategory = Field(
        description=(
            "Claim-anchored category declared by the LLM (verification "
            "quartet): SYMPTOM_EVIDENCE | CAUSAL_EVIDENCE | "
            "SYMPTOM_ABSENCE_EVIDENCE | CAUSAL_ABSENCE_EVIDENCE"
        )
    )

    primary_purpose: str = Field(
        description="What this evidence validates (milestone name or hypothesis ID)",
        max_length=100,
    )

    # ============================================================
    # Content — two-field shape (see case-schema.md §4.3)
    # ============================================================
    summary: str = Field(
        description=(
            "Short label (≤500 chars) the LLM wrote when declaring this "
            "evidence via ``evidence_to_add``. ALWAYS present — use it for "
            "UI list views, headers, and quick scanning. The optional "
            "``extract`` field carries the verbatim slice that supports "
            "the summary."
        ),
        min_length=1,
        max_length=500,
    )

    @field_validator("summary", mode="after")
    @classmethod
    def _summary_not_empty(cls, v: str) -> str:
        """Mirror of the DB evidence_summary_not_empty CHECK: summary must
        not be whitespace-only. Pydantic ``min_length=1`` accepts a single
        space; the DB rejects it. Same rule, two layers — neither
        bypassable independently."""
        if not v.strip():
            raise ValueError("summary must not be whitespace-only")
        return v

    extract: Optional[str] = Field(
        default=None,
        description=(
            "Optional verbatim quote that supports the ``summary``. The "
            "LLM populates this when grounding the finding in a specific "
            "system-output slice (a log line, a metric reading, a config "
            "snippet). Distinct from ``summary`` (short label) and from "
            "``uploaded_files.storage_ref`` (file pointer). May be NULL "
            "when the summary is self-contained. File-level preprocessing "
            "artifacts (structural index, file summary) live on "
            "``uploaded_files``, never here — this field is for "
            "claim-relevant quotes only."
        ),
    )

    @field_validator("extract", mode="after")
    @classmethod
    def _extract_not_empty_when_set(cls, v: Optional[str]) -> Optional[str]:
        """Mirror of the DB evidence_extract_not_empty CHECK: if extract is
        set, it must not be whitespace-only. Cross-layer defense in depth —
        same rule, two layers, neither bypassable independently."""
        if v is not None and not v.strip():
            raise ValueError("extract must not be empty whitespace; pass None to omit")
        return v

    analysis: Optional[str] = Field(
        default=None,
        description="Agent analysis of this evidence and its significance to the investigation",
        max_length=2000,
    )

    # ============================================================
    # Processing Mode (Scenario-Driven Data Processing)
    # ============================================================
    processing_mode: Optional[str] = Field(
        default=None,
        description="Processing mode: triage | directed_analysis | semantic_search",
        max_length=50,
    )

    # ============================================================
    # Source Information
    # ============================================================
    source_type: EvidenceSourceType = Field(description="Type of evidence source")

    source_file_id: Optional[str] = Field(
        default=None,
        description=(
            "FK to the UploadedFile this extract came from. Required unless "
            "``source_type=USER_DESCRIPTION`` (the narrow case where the LLM "
            "extracted a verbatim system-output quote from the user's short "
            "chat message — no file involved). Enforced by the "
            "``evidence_source_invariant`` CHECK constraint at the DB level "
            "and by the ``_source_requires_file_unless_user_description`` "
            "validator at the Pydantic level."
        ),
    )

    is_primary: bool = Field(
        default=False,
        description=(
            "True for the principal evidence row in this case (the one "
            "that anchors the investigation summary). list_evidence_tool "
            "uses this for surface-level dashboards."
        ),
    )

    reliability_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "LLM-assessed reliability of this evidence (0.0-1.0). "
            "NULL when the agent has not scored the evidence."
        ),
    )

    tags: List[str] = Field(
        default_factory=list,
        description=(
            "Free-form tag list for evidence classification. Tag values must "
            "not contain commas (the SQLite serializer uses comma-separation; "
            "the comma-ban keeps round-trip lossless)."
        ),
    )

    @field_validator("tags", mode="after")
    @classmethod
    def _no_commas_in_tags(cls, v: List[str]) -> List[str]:
        for t in v:
            if "," in t:
                raise ValueError(
                    f"tag values must not contain commas (got {t!r}); commas "
                    "would break the SQLite serialization round-trip"
                )
        return v

    vectorized: bool = Field(
        default=False,
        description=(
            "Whether this evidence's structural index has been persisted into "
            "the case vector store. Set to True by the investigation engine "
            "after a successful vectorize_file run; persisted across turns so "
            "proactive and reactive vectorization paths skip already-indexed "
            "evidence instead of re-embedding on every turn."
        ),
    )

    # ============================================================
    # Milestone Advancement
    # ============================================================
    advances_milestones: List[str] = Field(
        default_factory=list,
        description="Which milestones this evidence helped complete",
    )

    # ============================================================
    # Metadata
    # ============================================================
    collected_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When evidence was collected",
    )

    collected_by: str = Field(
        description="Who collected: user_id or 'system' for automated collection"
    )

    collected_at_turn: int = Field(
        ge=0, description="Turn number when evidence was collected"
    )

    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Structured diagnostic metadata from the preprocessing pipeline. "
            "Top-level keys are namespaced — see "
            "docs/architecture/data-and-storage/schemas/case-schema.md §4.3 "
            "'evidence.metadata JSON contract'. "
            "Canonical shape in "
            "faultmaven/core/preprocessing/evidence_metadata.py::EvidenceMetadata. "
            "Optional — chat-quoted Evidence rows have no preprocessing trace."
        ),
    )

    # Phase 3 — Case-level timeline. See case-schema.md §4.3 and
    # docs/working/WIP-data-processing-improvement-plan.md §Phase 3.
    # The time span the evidence's *content* covers, distinct from:
    #   collected_at (upload receipt time), collected_at_turn (agent turn).
    # Nullable: NULL for evidence without parseable timestamps (configs,
    # source code, screenshots, short pastes).
    coverage_start_ts: Optional[datetime] = Field(
        default=None,
        description=(
            "Earliest timestamp parsed from the evidence's content. "
            "None when the content has no parseable timestamps."
        ),
    )
    coverage_end_ts: Optional[datetime] = Field(
        default=None,
        description=(
            "Latest timestamp parsed from the evidence's content. "
            "None when the content has no parseable timestamps."
        ),
    )

    @model_validator(mode="after")
    def _source_requires_file_unless_user_description(self) -> "Evidence":
        """Mirror of the DB ``evidence_source_invariant`` CHECK (migration
        010): every Evidence row has a source.

        - source_file_id IS NOT NULL  → the extract came from a file
        - source_file_id IS NULL      → only legal when source_type is
          ``USER_DESCRIPTION`` (the LLM extracted a verbatim system-output
          quote from the user's short chat message; the source is the
          user message at ``collected_at_turn``)

        Cross-layer defense: same rule in Pydantic and the DB, neither
        bypassable independently."""
        if (
            self.source_file_id is None
            and self.source_type != EvidenceSourceType.USER_DESCRIPTION
        ):
            raise ValueError(
                "evidence.source_file_id is required unless "
                "source_type=USER_DESCRIPTION (the chat-quote case)"
            )
        return self


# =============================================================================
# Evidence Needs (Demand-Side Counterpart to Evidence Rows)
# =============================================================================
#
# Evidence needs are a flat pool on the case — a registry of *what data
# the investigation requires*. They are the demand-side counterpart to
# Evidence rows. Needs are NOT anchored to specific hypotheses; the
# hypothesis-evidence relationship is recorded through the existing
# ``hypothesis_evidence`` junction at evidence-collection time.
#
# Design reference:
# docs/architecture/investigation-engine/evidence-needs-design.md


class NeedPurpose(str, Enum):
    """Why this need was created — maps to evidence categories.

    A symptom_verification need produces SYMPTOM_EVIDENCE (presence)
    initially and SYMPTOM_ABSENCE_EVIDENCE (absence) on re-check after
    mitigation/solution. A causal_verification need produces
    CAUSAL_EVIDENCE (presence) initially and CAUSAL_ABSENCE_EVIDENCE
    (absence) on re-check after solution.

    The same need produces multiple evidence rows of different
    categories across the case's lifetime; the need's state stays
    FULFILLED once fulfilled — re-check evidence is appended via
    ``fulfilling_evidence_ids``, it does not reset the state.
    """

    SYMPTOM_VERIFICATION = "symptom_verification"
    CAUSAL_VERIFICATION = "causal_verification"


class NeedState(str, Enum):
    """Lifecycle states of an evidence need.

    PENDING        — Need identified, no evidence yet.
    PARTIALLY_MET  — Some evidence collected but insufficient.
    FULFILLED      — Sufficient evidence collected (terminal-positive).
    SUPERSEDED     — No longer relevant (terminal-negative). Either all
                     motivating hypotheses retired (engine rule) or LLM
                     judged irrelevant (LLM update emission).

    FULFILLED and SUPERSEDED are terminal — a need cannot resurrect from
    SUPERSEDED. If the LLM later concludes the underlying data is
    relevant again, it must create a new need.
    """

    PENDING = "pending"
    PARTIALLY_MET = "partially_met"
    FULFILLED = "fulfilled"
    SUPERSEDED = "superseded"


class NeedPriority(str, Enum):
    """Priority hint for surfacing needs as EVIDENCE-type suggestions.

    High-priority unfulfilled needs are surfaced first; medium and low
    are deferred until higher priorities are addressed. Priority is an
    LLM hint, not a hard ordering.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class NeedObtainability(str, Enum):
    """Whether the discriminating data a causal_verification need requests can
    be gathered at all — the one judgment the engine cannot compute, so it is
    model-declared (verification-status handling, insufficient-evidence §5.3).

    UNKNOWN      — default; the model has not declared. Fail-safe: treated as
                   still-obtainable (keep-engaging), never contributes to a wall.
    OBTAINABLE   — the data can be gathered; the need stays a live ask.
    UNOBTAINABLE — the data cannot be gathered (never collected, rotated away,
                   too costly, no access). A causal_verification need declared
                   UNOBTAINABLE is a *declared discriminator wall* for its
                   candidate; it yields its surface slot and stops rotating, and
                   is retained in the insufficient-evidence record as the
                   specific unmet need.

    Monotonic in effect: only UNOBTAINABLE moves the reading toward
    INSUFFICIENT_EVIDENCE, never back toward safety.
    """

    UNKNOWN = "unknown"
    OBTAINABLE = "obtainable"
    UNOBTAINABLE = "unobtainable"


class EvidenceNeed(BaseModel):
    """A verification requirement on a case.

    Each need represents "data that would advance the investigation."
    Needs live in a flat pool on the case — not anchored to specific
    hypotheses. ``motivating_hypothesis_ids`` records *why* the need
    exists (which hypotheses motivated creating it) for context and
    for the engine's retirement-supersession rule, but is not a hard
    ownership association.

    Lifecycle (see evidence-needs-design.md §7):

    - Created by the LLM via ``EvidenceNeedUpdate`` emissions at
      problem-statement confirmation (symptom needs) and at hypothesis
      creation (causal needs).
    - Updated by the LLM as evidence arrives (state, fulfilling
      evidence linkage, motivating hypothesis IDs).
    - Auto-superseded by the engine on hypothesis retirement when the
      motivating list becomes empty AND purpose is CAUSAL_VERIFICATION
      AND state is not FULFILLED. Symptom needs (empty motivating
      list by design) are exempt — they're motivated by the problem
      statement, not by a hypothesis.
    """

    need_id: str = Field(
        default_factory=lambda: f"eneed_{uuid4().hex[:12]}",
        description="Unique evidence-need identifier",
        pattern=r"^eneed_[a-f0-9]{12}$",
    )

    case_id: str = Field(
        description="Case this need belongs to",
        min_length=17,
        max_length=36,
    )

    purpose: NeedPurpose = Field(
        description=(
            "Why this need exists: symptom_verification (motivated by "
            "the problem statement) or causal_verification (motivated "
            "by one or more hypotheses)."
        ),
    )

    request_text: str = Field(
        description=(
            "What data would fulfill this need, in a form suitable for "
            "surfacing to the user as an EVIDENCE-type suggestion. "
            "Example: 'kubectl get pods -n production showing current "
            "restart counts'."
        ),
        min_length=1,
        max_length=500,
    )

    rationale: str = Field(
        description=(
            "Why this data would advance the investigation. Used in "
            "the LLM's <evidence_needs> context block to remind the "
            "LLM why the need was created. Example: 'confirms whether "
            "the pod-level OOMKill pattern is still active after the "
            "memory-limit increase'."
        ),
        min_length=1,
        max_length=500,
    )

    priority: NeedPriority = Field(
        default=NeedPriority.MEDIUM,
        description="LLM hint for surfacing-order on the suggestion side.",
    )

    state: NeedState = Field(
        default=NeedState.PENDING,
        description="Lifecycle state — see NeedState.",
    )

    obtainability: NeedObtainability = Field(
        default=NeedObtainability.UNKNOWN,
        description=(
            "Whether the discriminating data can be gathered at all — the one "
            "judgment the engine cannot compute (insufficient-evidence §5.3). "
            "Model-declared, opt-in; UNKNOWN default is fail-safe (keep-"
            "engaging). Scoped to causal_verification needs; auto-revoked to "
            "UNKNOWN once the need is FULFILLED or SUPERSEDED (the question is "
            "moot). Only UNOBTAINABLE moves the case toward INSUFFICIENT_"
            "EVIDENCE — never back toward safety."
        ),
    )

    motivating_hypothesis_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Hypothesis IDs that motivated this need's existence. Empty "
            "list means the need is motivated by the problem statement "
            "(symptom needs). Engine appends/removes IDs as hypotheses "
            "share needs (cross-hypothesis evaluation per "
            "evidence-needs-design.md §5.2) and as hypotheses are "
            "retired (engine auto-supersession rule)."
        ),
    )

    fulfilling_evidence_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Evidence rows that fulfill this need. Multiple entries may "
            "accumulate across stages: presence evidence collected during "
            "DIAGNOSIS plus absence evidence collected during "
            "MITIGATION/TREATMENT. The list is append-only in practice — "
            "the need's state stays FULFILLED once fulfilled even when "
            "post-fix absence evidence is added."
        ),
    )

    superseded_reason: Optional[str] = Field(
        default=None,
        description=(
            "Human-readable explanation when state=SUPERSEDED. Set by "
            "engine auto-supersession ('all motivating hypotheses "
            "retired') or by LLM emission ('superseded by refined "
            "problem statement'). Required when state=SUPERSEDED, "
            "must be None otherwise."
        ),
        max_length=500,
    )

    created_at_turn: int = Field(
        description="Turn number when the need was created.",
        ge=0,
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Wall-clock creation time.",
    )

    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Wall-clock last-update time.",
    )

    @property
    def is_outstanding(self) -> bool:
        """The need still awaits (sufficient) data — PENDING or PARTIALLY_MET.

        FULFILLED and SUPERSEDED are the terminal states; an outstanding need is
        one the investigation is still waiting on.
        """
        return self.state in (NeedState.PENDING, NeedState.PARTIALLY_MET)

    def revoke_obtainability_if_terminal(self) -> None:
        """Reset ``obtainability`` to UNKNOWN once the need is terminal
        (FULFILLED/SUPERSEDED) — the §5.3 auto-revoke ("the question is moot").

        The construction validator enforces this, but ``EvidenceNeed`` runs with
        ``validate_assignment`` off (the apply layer mutates fields in place and
        relies on the validator NOT re-firing mid-update), so every site that
        flips ``state`` to a terminal value in place must call this to keep a
        stale UNOBTAINABLE from lingering on a resolved/moot need. One method so
        the invariant lives in a single place rather than per call-site.
        """
        if self.state in (NeedState.FULFILLED, NeedState.SUPERSEDED):
            self.obtainability = NeedObtainability.UNKNOWN

    @field_validator("request_text", "rationale", mode="after")
    @classmethod
    def _text_not_whitespace_only(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be whitespace-only")
        return v

    @field_validator("motivating_hypothesis_ids", mode="after")
    @classmethod
    def _hypothesis_ids_well_formed(cls, v: List[str]) -> List[str]:
        # Hypothesis IDs follow the same pattern as elsewhere in the
        # codebase (uuid4-based). Cross-row existence is checked at the
        # engine apply-layer, not here — Pydantic only validates shape.
        for hyp_id in v:
            if not hyp_id or not isinstance(hyp_id, str):
                raise ValueError(
                    "motivating_hypothesis_ids entries must be non-empty strings"
                )
        # Deduplicate while preserving order (LLM may re-emit the same ID).
        seen: set[str] = set()
        deduped: List[str] = []
        for hyp_id in v:
            if hyp_id not in seen:
                seen.add(hyp_id)
                deduped.append(hyp_id)
        return deduped

    @field_validator("fulfilling_evidence_ids", mode="after")
    @classmethod
    def _evidence_ids_well_formed(cls, v: List[str]) -> List[str]:
        for ev_id in v:
            if not ev_id or not isinstance(ev_id, str):
                raise ValueError(
                    "fulfilling_evidence_ids entries must be non-empty strings"
                )
        seen: set[str] = set()
        deduped: List[str] = []
        for ev_id in v:
            if ev_id not in seen:
                seen.add(ev_id)
                deduped.append(ev_id)
        return deduped

    @model_validator(mode="after")
    def _validate_state_consistency(self) -> "EvidenceNeed":
        # SUPERSEDED needs must carry a reason; non-SUPERSEDED needs
        # must not. The asymmetric requirement mirrors how
        # ``closure_reason`` is enforced on Case for terminal states.
        if self.state == NeedState.SUPERSEDED and not (
            self.superseded_reason and self.superseded_reason.strip()
        ):
            raise ValueError(
                "EvidenceNeed.state=SUPERSEDED requires a non-empty "
                "superseded_reason"
            )
        if self.state != NeedState.SUPERSEDED and self.superseded_reason is not None:
            raise ValueError(
                "EvidenceNeed.superseded_reason must be None unless " "state=SUPERSEDED"
            )

        # FULFILLED needs must carry at least one fulfilling evidence
        # ID. The engine's apply-layer is responsible for adding the
        # ID; this validator catches a bad LLM emission that claims
        # FULFILLED with no supporting evidence.
        if self.state == NeedState.FULFILLED and not self.fulfilling_evidence_ids:
            raise ValueError(
                "EvidenceNeed.state=FULFILLED requires at least one "
                "fulfilling_evidence_id"
            )

        # Obtainability (§5.3) is a still-outstanding, causal_verification-only
        # judgment. Auto-revoke it to UNKNOWN when the need is terminal (data
        # arrived or need is moot) or is a symptom need (out of scope), so a
        # stale UNOBTAINABLE can never linger on a resolved/irrelevant need or
        # be read for a symptom ask. Coerce rather than reject — a mis-scoped
        # declaration is fail-safe, not a turn-breaking error.
        if (
            self.purpose != NeedPurpose.CAUSAL_VERIFICATION
            or self.state in (NeedState.FULFILLED, NeedState.SUPERSEDED)
        ) and self.obtainability != NeedObtainability.UNKNOWN:
            object.__setattr__(self, "obtainability", NeedObtainability.UNKNOWN)

        return self


# ============================================================
# Hypothesis Models (Section 6)
# ============================================================


class HypothesisCategory(str, Enum):
    """
    Hypothesis categories for anchoring detection.

    If agent tests 4+ hypotheses in same category without validation,
    it is "anchored" and should try different category.
    """

    CODE = "code"
    """Code bugs, logic errors, null pointers, etc."""

    CONFIG = "config"
    """Configuration issues, misconfigurations, wrong settings"""

    ENVIRONMENT = "environment"
    """Environment issues, resource exhaustion, system limits"""

    NETWORK = "network"
    """Network issues, connectivity, latency, DNS"""

    DATA = "data"
    """Data quality issues, corruption, consistency problems"""

    DATABASE = "database"
    """Database performance, queries, indexes, connections"""

    HARDWARE = "hardware"
    """Hardware failures, disk issues, CPU/memory"""

    SECURITY = "security"
    """Security issues, authentication/authorization failures, access control"""

    EXTERNAL = "external"
    """External dependencies, third-party services"""

    HUMAN = "human"
    """Human errors, operational mistakes"""

    OTHER = "other"
    """Does not fit above categories"""


# ============================================================
# Case Entity Registry (Phase 4)
# ============================================================


class EntityType(str, Enum):
    """Controlled vocabulary for ``case_entities.entity_type``.

    Extending this vocabulary requires a design-doc edit (not just a
    code change) so the retrieval paths — agent tools, context-builder
    auto-injection — stay in sync with what producers emit. See
    ``docs/working/WIP-data-processing-improvement-plan.md`` §Phase 4.
    """

    IP = "ip"
    HOSTNAME = "hostname"
    USER = "user"
    PID = "pid"
    PORT = "port"
    SERVICE = "service"
    PATH = "path"
    DEVICE = "device"
    METRIC_NAME = "metric_name"


class CaseEntity(BaseModel):
    """One row in the case-level entity registry.

    Populated by the preprocessing pipeline post-extraction. The
    composite (case_id, entity_type, entity_value, evidence_id) is the
    primary key — re-extracting an evidence upserts by that tuple,
    preserving idempotency across re-runs.
    """

    case_id: str = Field(description="Case that owns this entity observation")
    entity_type: EntityType = Field(
        description=("Type of entity. Controlled vocabulary — see EntityType enum.")
    )
    entity_value: str = Field(
        max_length=255,
        description="The entity itself (IP address, hostname, PID, etc.)",
    )
    evidence_id: str = Field(
        description="Evidence row the entity was extracted from",
        pattern=r"^ev_[a-f0-9]{12}$",
    )
    mention_count: int = Field(
        default=1,
        ge=1,
        description="How many times the entity appeared in the evidence",
    )
    in_error_context: bool = Field(
        default=False,
        description=(
            "True when the entity appeared primarily in error / warning "
            "lines. Lets the agent distinguish 'IP X was involved in an "
            "error' from 'IP X showed up in ambient traffic'."
        ),
    )
    first_seen_ts: Optional[datetime] = Field(
        default=None,
        description=(
            "Earliest timestamp associated with this entity in this "
            "evidence — typically equals the evidence's coverage_start_ts "
            "(Phase 3a) when the evidence is time-bound, else None."
        ),
    )


class HypothesisState(str, Enum):
    """Hypothesis lifecycle state"""

    CAPTURED = "captured"
    """
    Generated but not yet actively testing.
    Hypothesis is in the queue.
    """

    ACTIVE = "active"
    """
    Currently being tested.
    Evidence is being gathered.
    """

    VALIDATED = "validated"
    """
    Evidence strongly supports hypothesis.
    Root cause identified.
    """

    REFUTED = "refuted"
    """
    Evidence contradicts hypothesis.
    Not the root cause.
    """

    INCONCLUSIVE = "inconclusive"
    """
    Evidence is ambiguous.
    Cannot determine if hypothesis is correct.
    """

    RETIRED = "retired"
    """
    No longer relevant.
    Investigation moved in different direction.
    """


class HypothesisGenerationMode(str, Enum):
    """How hypothesis was generated"""

    OPPORTUNISTIC = "opportunistic"
    """
    Generated from strong correlation or obvious clue.
    Example: Deploy immediately preceded errors -> hypothesis: "Bug in new deploy"
    """

    SYSTEMATIC = "systematic"
    """
    Generated methodically when root cause unclear.
    Example: Generic slowness -> generate hypotheses for common causes
    """

    FORCED_ALTERNATIVE = "forced_alternative"
    """
    User requested alternative hypotheses.
    Example: User: "What else could it be?"
    """


class HypothesisEvidenceLink(BaseModel):
    """
    Many-to-many relationship between hypothesis and evidence.

    ONE evidence can have DIFFERENT stances for DIFFERENT hypotheses:
    - Evidence "Pool at 95%" -> STRONGLY_SUPPORTS "pool exhausted" hypothesis
    - Evidence "Pool at 95%" -> REFUTES "network latency" hypothesis
    - Evidence "Pool at 95%" -> IRRELEVANT to "memory leak" hypothesis

    Stored in hypothesis_evidence junction table.
    LLM evaluates evidence against ALL active hypotheses after submission.
    """

    hypothesis_id: str = Field(description="Hypothesis being evaluated")

    evidence_id: str = Field(description="Evidence being evaluated")

    stance: EvidenceStance = Field(
        description="How this evidence relates to THIS hypothesis (including IRRELEVANT)"
    )

    reasoning: str = Field(
        description="LLM's explanation of the relationship", max_length=1000
    )

    stance_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in the stance assessment (0.0-1.0). Use for granularity instead of STRONGLY_ variants.",
    )

    analyzed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this relationship was established",
    )


# ============================================================
# Causal Graph Models (Two-Dimensional Hypothesis Methodology)
# ------------------------------------------------------------
# A hypothesis is a causal CHAIN, not a flat statement: an ordered path
# root -> intermediate states -> active problem (D). The case owns ONE causal
# DAG (nodes + edges); a Hypothesis is a named root->D path over it.
# See docs/architecture/investigation-engine/
#     two-dimensional-hypothesis-methodology.md
# ============================================================


class NodeType(str, Enum):
    """Role of a causal node on the chain (methodology §2)."""

    PROBLEM = "problem"
    """The active problem D — the shared terminal node every chain ends at.
    Exactly one per case; seeded from problem_verification.symptom_statement."""

    INTERMEDIATE = "intermediate"
    """An observed/observable effect between a cause and D. Never a root cause."""

    ROOT = "root"
    """A candidate root cause — the terminal node of a chain (M1/M3)."""


class NodeState(str, Enum):
    """Empirical status of a single causal node (methodology M4/M7)."""

    CANDIDATE = "candidate"
    """Posited but not yet empirically established."""

    VALIDATED = "validated"
    """Confirmed by empirical evidence (§7.1) or deduction over an exhaustive
    set (§7.1.1). Never set by assertion (M4)."""

    REFUTED = "refuted"
    """Contradicted by evidence (or, for a root, by counterfactual
    disconfirmation — a failed fix, M6)."""

    INCONCLUSIVE = "inconclusive"
    """Could not be proved or disproved (e.g., the rung is untestable, R5)."""


class ValidationMethod(str, Enum):
    """How a node reached VALIDATED (methodology M4)."""

    NONE = "none"
    """Not validated. The only legal method while node_state != VALIDATED."""

    EMPIRICAL = "empirical"
    """Direct observable facts matched the predicted state (§7.1)."""

    DEDUCTIVE = "deductive"
    """Proof by exclusion over a certified-exhaustive OR-set (§7.1.1)."""


class InterventionQuadrant(str, Enum):
    """Where × how-durable an intervention acts (methodology §7.4)."""

    REMEDIATION = "remediation"
    """Permanent fix at the root cause — the ideal; resolves the case."""

    DEFENSIVE_FIX = "defensive_fix"
    """Permanent fix at an intermediate node — durable, but does not address
    the upstream root (closes on symptom_absence)."""

    MITIGATION = "mitigation"
    """Temporary interception of an intermediate node to suppress D under
    current constraints (the mitigation insert)."""

    LOOP_BREAK = "loop_break"
    """Break a causal cycle when no static root exists (R9)."""


class NodeEvidenceLink(BaseModel):
    """Evidence attached to a specific causal NODE with a stance.

    Methodology §9.1: evidence links target nodes (rungs), not a whole chain —
    a SUPPORTS/REFUTES stance bears on the specific rung it tests, which is
    what makes step-by-step descent (S3) and AND-validation (S1) computable.
    """

    evidence_id: str = Field(description="Evidence bearing on this node")

    stance: EvidenceStance = Field(
        description="How this evidence relates to THIS node (supports/refutes/neutral)"
    )

    reasoning: str = Field(
        description="Explanation of the relationship", max_length=1000
    )

    stance_confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence in the stance assessment (0.0-1.0)",
    )

    linked_at_turn: int = Field(
        default=0, ge=0, description="Turn when the link was established"
    )

    analyzed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this relationship was established",
    )


class CausalNode(BaseModel):
    """A node in the case's causal graph (methodology §2, §3).

    A node is the active problem (PROBLEM), an intermediate state, or a
    candidate root cause (ROOT). Its node_state is established only by
    empirical/deductive validation (M4), never by assertion.
    """

    node_id: str = Field(
        default_factory=lambda: f"cn_{uuid4().hex[:12]}",
        description="Unique causal-node identifier",
        pattern=r"^cn_[a-f0-9]{12}$",
    )

    statement: str = Field(
        description="The state/cause this node asserts",
        min_length=1,
        max_length=500,
    )

    node_type: NodeType = Field(description="problem (D) / intermediate / root")

    node_state: NodeState = Field(
        default=NodeState.CANDIDATE, description="Empirical status of this node"
    )

    validation_method: ValidationMethod = Field(
        default=ValidationMethod.NONE,
        description="How the node reached VALIDATED (M4). NONE unless validated.",
    )

    belief: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Current belief this node holds (0.0-1.0); propagated by gate type",
    )

    signature_consistent: bool = Field(
        default=True,
        description=(
            "F3 signature screening: whether this node's mechanism could produce "
            "the observed signature of D. False = screened out at formation."
        ),
    )

    actionable: bool = Field(
        default=False,
        description=(
            "M1: a ROOT node must be actionable (a performable, independent "
            "remediation can be named) before it can be validated/confirmed."
        ),
    )

    category: Optional[HypothesisCategory] = Field(
        default=None, description="Failure family (for anchoring/diversity)"
    )

    state_epoch: int = Field(
        default=0,
        ge=0,
        description=(
            "M6/§7.3 contamination epoch — bumped when a state-mutating action "
            "may have invalidated evidence collected in a prior epoch."
        ),
    )

    evidence_links: List[NodeEvidenceLink] = Field(
        default_factory=list, description="Evidence bearing on this node"
    )

    generated_at_turn: int = Field(
        ge=0, description="Turn when this node was first posited"
    )
    last_updated_turn: int = Field(default=0, ge=0)
    last_progress_at_turn: int = Field(default=0, ge=0)
    iterations_without_progress: int = Field(
        default=0,
        ge=0,
        description=(
            "Stagnation counter for decay/anchoring. Advances ONLY on "
            "investigation turns where this node was eligible to progress and "
            "didn't (new evidence analyzed, a test result returned, a state "
            "transition attempted) — never on clarifying/awaiting-user turns "
            "(TurnOutcome.CONVERSATION). Engine-maintained in Phase 2; see "
            "methodology §6.1 'Decay counts investigation turns'."
        ),
    )

    refutation_reason: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Why the node was refuted. REQUIRED when node_state=REFUTED.",
    )

    rationale: Optional[str] = Field(
        default=None, max_length=1000, description="Why this node was posited"
    )

    metadata: Dict[str, Any] = Field(default_factory=dict)
    proposed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("statement", mode="after")
    @classmethod
    def _statement_not_empty(cls, v: str) -> str:
        """Mirror of the Hypothesis/DB statement-not-empty rule."""
        if not v.strip():
            raise ValueError("statement must not be whitespace-only")
        return v

    @model_validator(mode="after")
    def _refutation_reason_pairs_with_state(self) -> "CausalNode":
        """node_state=REFUTED and refutation_reason travel together
        (mirror of the Hypothesis rule)."""
        if self.node_state == NodeState.REFUTED and not self.refutation_reason:
            raise ValueError("refutation_reason is required when node_state=REFUTED.")
        if self.refutation_reason and self.node_state != NodeState.REFUTED:
            raise ValueError("refutation_reason is only valid when node_state=REFUTED.")
        return self

    @model_validator(mode="after")
    def _validated_requires_method(self) -> "CausalNode":
        """M4 (schema backstop): a node is VALIDATED only with an actual
        validation method — never by assertion. A VALIDATED node with
        method=NONE cannot exist in memory."""
        if (
            self.node_state == NodeState.VALIDATED
            and self.validation_method == ValidationMethod.NONE
        ):
            raise ValueError(
                "a VALIDATED node requires validation_method EMPIRICAL or "
                "DEDUCTIVE (M4: validation is never asserted)."
            )
        return self

    @model_validator(mode="after")
    def _validated_root_must_be_actionable(self) -> "CausalNode":
        """M1 (schema backstop): a ROOT node cannot be VALIDATED unless it is
        actionable (a performable remediation can be named)."""
        if (
            self.node_type == NodeType.ROOT
            and self.node_state == NodeState.VALIDATED
            and not self.actionable
        ):
            raise ValueError("a VALIDATED ROOT node must be actionable (M1).")
        return self


class CausalEdge(BaseModel):
    """A directed cause -> effect edge in the causal graph (methodology S1).

    cause_node_id produces effect_node_id. Edges sharing the same
    (effect_node_id, and_group) are co-necessary (an AND-set, M7); a null or
    distinct and_group denotes an independent (OR) alternative cause.
    """

    edge_id: str = Field(
        default_factory=lambda: f"ce_{uuid4().hex[:12]}",
        description="Unique causal-edge identifier",
        pattern=r"^ce_[a-f0-9]{12}$",
    )

    cause_node_id: str = Field(description="Upstream cause node")
    effect_node_id: str = Field(description="Downstream effect node (closer to D)")

    and_group: Optional[str] = Field(
        default=None,
        description=(
            "AND-set key (M7): edges with the same (effect_node_id, and_group) "
            "are co-necessary. Null/distinct = independent OR alternative cause."
        ),
    )

    reasoning: Optional[str] = Field(
        default=None, max_length=1000, description="Why this causal link holds"
    )

    created_at_turn: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _no_self_loop(self) -> "CausalEdge":
        if self.cause_node_id == self.effect_node_id:
            raise ValueError("a causal edge cannot connect a node to itself")
        return self


class Hypothesis(BaseModel):
    """
    Hypothesis for systematic root cause exploration.

    Philosophy: Hypotheses are OPTIONAL. Agent may:
    - Identify root cause directly from evidence (no hypotheses)
    - OR generate hypotheses for systematic testing (when unclear)
    """

    hypothesis_id: str = Field(
        default_factory=lambda: f"hyp_{uuid4().hex[:12]}",
        description="Unique hypothesis identifier",
        pattern=r"^hyp_[a-f0-9]{12}$",
    )

    statement: str = Field(
        description="Hypothesis statement (what we think caused the problem)",
        min_length=1,
        max_length=500,
    )

    @field_validator("statement", mode="after")
    @classmethod
    def _statement_not_empty(cls, v: str) -> str:
        """Mirror of the DB hypotheses_statement_not_empty CHECK: statement
        must not be whitespace-only. Pydantic ``min_length=1`` accepts a
        single space; the DB rejects it. Same rule, two layers — neither
        bypassable independently."""
        if not v.strip():
            raise ValueError("statement must not be whitespace-only")
        return v

    category: HypothesisCategory = Field(
        description="Hypothesis category (for anchoring detection)"
    )

    state: HypothesisState = Field(
        default=HypothesisState.CAPTURED, description="Current hypothesis state"
    )

    likelihood: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Estimated likelihood this hypothesis is correct (0.0-1.0)",
    )

    initial_likelihood: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Original likelihood when hypothesis was generated",
    )

    # ============================================================
    # Causal Chain (Two-Dimensional Hypothesis Methodology)
    # ------------------------------------------------------------
    # A hypothesis is a CHAIN: a root->D path through the case's causal graph
    # (Case.causal_nodes / causal_edges). These fields make it a chain header.
    # Phase 1 adds them additively; the engine populates and validates them in
    # Phase 2 (M3 checkpoint: a chain may not validate / carry a Solution until
    # it terminates in a root node). `statement` and `evidence_links` below are
    # transitional (flat-model fields) until the Phase-2 engine rewire retires
    # them in favor of per-node evidence (CausalNode.evidence_links).
    # See docs/.../two-dimensional-hypothesis-methodology.md §9.1.
    # ============================================================
    root_node_id: Optional[str] = Field(
        default=None,
        description=(
            "The chain's ROOT causal node (its candidate root cause). NULL while "
            "the chain is still being expanded backward toward a root (lazy "
            "expansion). When set, must equal path[0]."
        ),
    )

    path: List[str] = Field(
        default_factory=list,
        description=(
            "Ordered node_ids root -> ... -> D (the active problem). path[0] is "
            "the root node, path[-1] is the PROBLEM node. Empty until the chain "
            "is materialized."
        ),
    )

    # ============================================================
    # Evidence Relationships (Many-to-Many)
    # ============================================================
    evidence_links: List[HypothesisEvidenceLink] = Field(
        default_factory=list,
        description="""
        Relationship rows from the hypothesis_evidence junction table.

        ONE evidence can:
        - SUPPORTS hypothesis A
        - REFUTES hypothesis B
        - Be NEUTRAL to hypothesis C

        Each list entry binds (hypothesis_id, evidence_id, stance, reasoning,
        stance_confidence). LLM evaluates each evidence against ALL active
        hypotheses after submission.
        """,
    )

    # ============================================================
    # Metadata
    # ============================================================
    generated_at_turn: int = Field(
        ge=0, description="Turn number when hypothesis was generated"
    )

    last_updated_turn: int = Field(
        default=0, ge=0, description="Turn number when hypothesis was last updated"
    )

    last_progress_at_turn: int = Field(
        default=0, ge=0, description="Turn number when hypothesis last showed progress"
    )

    iterations_without_progress: int = Field(
        default=0, ge=0, description="Count of consecutive iterations without progress"
    )

    generation_mode: HypothesisGenerationMode = Field(
        description="How hypothesis was generated"
    )

    retirement_reason: Optional[str] = Field(
        default=None, description="Reason if hypothesis was retired"
    )

    refutation_reason: Optional[str] = Field(
        default=None,
        max_length=200,
        description=(
            "Evidence or reasoning that disproves the hypothesis. "
            "REQUIRED when state=REFUTED (enforced via model validator). "
            "Not used for other statuses. state=REFUTED and refutation_reason "
            "travel together — an update carrying one without the other is "
            "rejected at the orchestration layer."
        ),
    )

    rationale: str = Field(
        description="Why this hypothesis was generated", max_length=1000
    )

    # ============================================================
    # Testing History
    # ============================================================
    tested_at: Optional[datetime] = Field(
        default=None, description="When hypothesis testing began"
    )

    concluded_at: Optional[datetime] = Field(
        default=None, description="When hypothesis was validated/refuted/retired"
    )

    # ============================================================
    # Computed Properties
    # ============================================================
    @property
    def supporting_evidence(self) -> List[str]:
        """Get evidence IDs that support this hypothesis"""

        return [
            link.evidence_id
            for link in self.evidence_links
            if link.stance == EvidenceStance.SUPPORTS
        ]

    @property
    def refuting_evidence(self) -> List[str]:
        """Get evidence IDs that refute this hypothesis"""

        return [
            link.evidence_id
            for link in self.evidence_links
            if link.stance == EvidenceStance.REFUTES
        ]

    @property
    def evidence_score(self) -> float:
        """
        Evidence balance score.
        Returns: -1.0 (all refuting) to 1.0 (all supporting)
        """
        total_support = len(self.supporting_evidence)
        total_refute = len(self.refuting_evidence)
        total = total_support + total_refute

        if total == 0:
            return 0.0

        return (total_support - total_refute) / total

    @model_validator(mode="after")
    def _validate_refutation_reason_pairs_with_state(self) -> "Hypothesis":
        """Pair integrity: state=REFUTED requires refutation_reason.

        The two fields travel together — a Hypothesis with state=REFUTED
        cannot exist in memory without a refutation_reason, and vice versa.
        RETIRED has its own ``retirement_reason`` field and is a distinct
        path (abandonment without disproof, no reason required).
        """
        if self.state == HypothesisState.REFUTED and not self.refutation_reason:
            raise ValueError(
                "refutation_reason is required when state=REFUTED. If there "
                "is no disproof evidence, use state=RETIRED instead."
            )
        if self.refutation_reason and self.state != HypothesisState.REFUTED:
            raise ValueError(
                "refutation_reason is only valid when state=REFUTED. "
                f"Current state is {self.state.value}."
            )
        return self

    @model_validator(mode="after")
    def _root_heads_the_path(self) -> "Hypothesis":
        """Chain consistency: when both are set, the root node is the head of
        the root->D path (path[0]). Lenient by design — a chain mid-expansion
        may have root_node_id set with an empty path, or a path with no root
        yet; only an outright head/root mismatch is rejected."""
        if self.root_node_id and self.path and self.path[0] != self.root_node_id:
            raise ValueError(
                "root_node_id must equal path[0] (the chain runs root -> D)."
            )
        return self


# ============================================================
# Solution Models (Section 7)
# ============================================================


class SolutionType(str, Enum):
    """Type of solution/mitigation"""

    ROLLBACK = "rollback"
    """Revert to previous version/state"""

    CONFIG_CHANGE = "config_change"
    """Modify configuration settings"""

    RESTART = "restart"
    """Restart service/component"""

    SCALING = "scaling"
    """Scale resources (increase/decrease)"""

    CODE_FIX = "code_fix"
    """Fix code bug (requires deployment)"""

    WORKAROUND = "workaround"
    """Temporary workaround (not root fix)"""

    INFRASTRUCTURE = "infrastructure"
    """Infrastructure changes (servers, networking, etc.)"""

    DATA_FIX = "data_fix"
    """Fix data corruption or inconsistency"""

    OTHER = "other"
    """Does not fit above categories"""


class Solution(BaseModel):
    """
    Proposed or applied solution/mitigation.
    """

    solution_id: str = Field(
        default_factory=lambda: f"sol_{uuid4().hex[:12]}",
        description="Unique solution identifier",
        pattern=r"^sol_[a-f0-9]{12}$",
    )

    # ============================================================
    # Solution Type
    # ============================================================
    solution_type: SolutionType = Field(description="Type of solution")

    # ============================================================
    # Causal-graph linkage (Two-Dimensional Hypothesis Methodology §7.4, §9.1)
    # ------------------------------------------------------------
    # Which causal node this intervention acts on, and which intervention
    # quadrant it occupies. Phase 1 adds these additively; the engine sets and
    # enforces them in Phase 2 (M5: a REMEDIATION solution requires its node's
    # root to be validated; mitigation/defensive interceptions are exempt).
    # ============================================================
    node_id: Optional[str] = Field(
        default=None,
        description="Causal node this solution remediates or intercepts",
    )

    quadrant: Optional[InterventionQuadrant] = Field(
        default=None,
        description=(
            "Intervention quadrant (§7.4): remediation (perm@root) / "
            "defensive_fix (perm@intermediate) / mitigation (temp@intermediate) "
            "/ loop_break."
        ),
    )

    # ============================================================
    # Solution Details
    # ============================================================
    title: str = Field(description="Short solution title", min_length=1, max_length=200)

    immediate_action: Optional[str] = Field(
        default=None,
        description="Quick fix or mitigation (temporary)",
        max_length=2000,
    )

    longterm_fix: Optional[str] = Field(
        default=None, description="Permanent solution (comprehensive)", max_length=2000
    )

    # ============================================================
    # Implementation
    # ============================================================
    implementation_steps: List[str] = Field(
        default_factory=list, description="Step-by-step implementation instructions"
    )

    commands: List[str] = Field(
        default_factory=list, description="Specific commands to execute"
    )

    risks: List[str] = Field(
        default_factory=list, description="Risks or side effects of this solution"
    )

    # ============================================================
    # Lifecycle
    # ============================================================
    proposed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When solution was proposed",
    )

    proposed_by: str = Field(
        default="agent", description="Who proposed: 'agent' or user_id"
    )

    applied_at: Optional[datetime] = Field(
        default=None, description="When solution was applied"
    )

    applied_by: Optional[str] = Field(
        default=None, description="Who applied the solution"
    )

    verified_at: Optional[datetime] = Field(
        default=None, description="When solution effectiveness was verified"
    )

    # ============================================================
    # Verification
    # ============================================================
    verification_method: Optional[str] = Field(
        default=None, description="How effectiveness was verified", max_length=500
    )

    verification_evidence_id: Optional[str] = Field(
        default=None, description="Evidence ID proving solution worked"
    )

    effectiveness: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="How well solution worked (0.0 = failed, 1.0 = perfect)",
    )

    # ============================================================
    # Validation
    # ============================================================
    @model_validator(mode="after")
    def solution_content_required(self):
        """Ensure solution has actionable content"""
        immediate = self.immediate_action
        longterm = self.longterm_fix
        steps = self.implementation_steps
        commands = self.commands

        if not any([immediate, longterm, steps, commands]):
            raise ValueError(
                "Solution must have at least one of: immediate_action, longterm_fix, implementation_steps, or commands"
            )

        return self

    @model_validator(mode="after")
    def verification_consistency(self):
        """Ensure verification fields are consistent"""
        verified_at = self.verified_at
        effectiveness = self.effectiveness

        if verified_at and effectiveness is None:
            raise ValueError("verified_at requires effectiveness score")

        if effectiveness is not None and not verified_at:
            raise ValueError("effectiveness requires verified_at")

        return self


# ============================================================
# Proposed Action Models (Evidence-Driven Framework)
# ============================================================


class InvestigationActionType(str, Enum):
    """Type of action proposed during investigation."""

    MITIGATION = "mitigation"
    """Temporary fix to stop the bleeding."""

    SOLUTION = "solution"
    """Permanent fix based on root cause analysis."""

    DIAGNOSTIC = "diagnostic"
    """Data collection or investigation action (does not trigger stage-gate milestones)."""


class ProposedAction(BaseModel):
    """
    A concrete action proposed by the agent for the user to execute.

    ProposedActions are the mechanism by which the agent communicates
    actionable next steps. User compliance with a proposed action
    triggers stage-gate milestone transitions via compliance detection.
    """

    action_id: str = Field(
        default_factory=lambda: f"act_{uuid4().hex[:12]}",
        description="Unique action identifier",
    )

    case_id: str = Field(description="Case this action belongs to")

    action_type: InvestigationActionType = Field(
        description="Whether this is a mitigation or solution action"
    )

    description: str = Field(
        description="Human-readable description of the proposed action",
        max_length=2000,
    )

    commands: List[str] = Field(
        default_factory=list,
        description="Specific commands for the user to execute",
    )

    proposed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the action was proposed",
    )

    proposed_in_turn: int = Field(
        description="Turn number when this action was proposed"
    )

    state: str = Field(
        default="pending",
        description="pending | accepted | rejected | superseded",
    )

    superseded_in_turn: Optional[int] = Field(
        default=None,
        description=(
            "Turn in which this action was superseded (state='superseded'). "
            "None while the action is live or if it left liveness another way."
        ),
    )

    superseded_reason: Optional[Literal["reproposal", "license_lost"]] = Field(
        default=None,
        description=(
            "Why the engine superseded this action: 'reproposal' (a newer "
            "SOLUTION offer replaced it) or 'license_lost' (the established-"
            "cause license that admitted the offer fell — demotion, "
            "retraction, MECE hold, or proxy decay). Closed vocabulary: the "
            "value feeds the solution_offer_superseded_total metric label. "
            "Forensic field; the context builder renders only pending "
            "actions."
        ),
    )

    downgrade_reason: Optional[str] = Field(
        default=None,
        description=(
            "If the engine downgraded action_type from the LLM's intent "
            "(e.g. MITIGATION → DIAGNOSTIC because no SYMPTOM_EVIDENCE "
            "existed yet), this carries the explanation. Rendered to the "
            "LLM via context_builder on the next turn so the agent can "
            "recover (gather the missing evidence and re-propose). None "
            "when no downgrade occurred."
        ),
        max_length=500,
    )

    @field_validator("state")
    @classmethod
    def valid_action_state(cls, v):
        allowed = ["pending", "accepted", "rejected", "superseded"]
        if v not in allowed:
            raise ValueError(f"state must be one of: {allowed}")
        return v


class ActionAttempt(BaseModel):
    """
    Records a user's attempt to execute a ProposedAction.

    When the user submits results after executing (or attempting to execute)
    a proposed action, an ActionAttempt is created. Compliance detection
    analyzes the attempt to determine if stage-gate milestones should be set.
    """

    attempt_id: str = Field(
        default_factory=lambda: f"att_{uuid4().hex[:12]}",
        description="Unique attempt identifier",
    )

    action_id: str = Field(description="ProposedAction this attempt relates to")

    user_message: str = Field(
        description="The user's message containing attempt results",
        max_length=10000,
    )

    submitted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the attempt was submitted",
    )

    compliance_detected: bool = Field(
        default=False,
        description="Whether the user appears to have executed the proposed action",
    )

    compliance_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence that user complied with the proposed action",
    )


# ============================================================
# Turn Tracking Models (Section 8)
# ============================================================


class TurnOutcome(str, Enum):
    """
    Turn outcome classification.

    NOTE: Outcomes are LLM-observable only (what happened this turn).
    Workflow control uses direct metrics (turns_without_progress).
    Outcomes are for analytics and prompt context, not control flow.

    Each member carries an LLM-facing ``description`` accessible at
    runtime via ``TurnOutcome.MEMBER.description``. The prompt block in
    ``SCHEMA_INSTRUCTIONS`` is auto-generated from these descriptions so
    there is no second source of truth to drift against — adding a value
    here automatically extends the prompt.

    Maintainer-only notes (implementation details that should NOT reach
    the LLM) live as ``#`` comments next to the value, not in the
    description string.
    """

    description: str  # Type hint for the runtime attribute set in __new__.

    def __new__(cls, value: str, description: str) -> "TurnOutcome":
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.description = description
        return obj

    MILESTONE_COMPLETED = (
        "milestone_completed",
        "one or more milestones flipped True this turn.",
    )
    DATA_PROVIDED = (
        "data_provided",
        "user shared data/evidence (uploaded, pasted).",
    )
    DATA_REQUESTED = (
        "data_requested",
        "you asked the user for data; awaiting response.",
    )
    # Maintainer note: system tracks the data_not_provided pattern — 3+
    # consecutive turns triggers degraded mode (see progress_monitor.py).
    # Not exposed to the LLM in the description below.
    DATA_NOT_PROVIDED = (
        "data_not_provided",
        "you previously requested data and the user did not address the request this turn.",
    )
    HYPOTHESIS_TESTED = (
        "hypothesis_tested",
        "a hypothesis was validated or refuted this turn.",
    )
    CASE_RESOLVED = (
        "case_resolved",
        "solution verified; case is resolvable.",
    )
    CONVERSATION = (
        "conversation",
        "normal Q&A, no data requests or milestone changes.",
    )
    OTHER = (
        "other",
        "does not fit any of the above.",
    )
    # Maintainer note: synthesized by Case.reconcile_turn_sequence to backfill a
    # turn whose record was lost (e.g. an interrupted save). Not LLM-emitted.
    SKIPPED = (
        "skipped",
        "turn not recorded — recovered after an interrupted turn.",
    )


class InvestigationMomentum(str, Enum):
    """
    Investigation momentum indicator for progress tracking.

    Used to signal overall investigation health and guide agent behavior.
    Calculated from recent progress patterns (evidence collection, hypothesis updates).
    """

    HIGH = "high"
    """
    Evidence flowing, hypotheses being tested, confidence increasing.
    Investigation progressing well.
    """

    MODERATE = "moderate"
    """
    Some progress being made, investigation moving forward.
    Default state when enough data to assess.
    """

    LOW = "low"
    """
    Little progress recently, confidence plateaued.
    May need different approach or more data.
    """

    BLOCKED = "blocked"
    """
    Critical evidence unavailable, investigation stalled.
    Likely to enter degraded mode if continues.
    """


class TurnProgress(BaseModel):
    """
    Record of what happened in one turn.
    Turn = one user message + one agent response.
    """

    turn_number: int = Field(ge=0, description="Sequential turn number")

    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When turn occurred",
    )

    # ============================================================
    # What Advanced This Turn
    # ============================================================
    milestones_completed: List[str] = Field(
        default_factory=list,
        description="Milestone names completed this turn (e.g., 'symptom_verified')",
    )

    evidence_added: List[str] = Field(
        default_factory=list, description="Evidence IDs added this turn"
    )

    hypotheses_generated: List[str] = Field(
        default_factory=list, description="Hypothesis IDs generated this turn"
    )

    hypotheses_validated: List[str] = Field(
        default_factory=list, description="Hypothesis IDs validated this turn"
    )

    solutions_proposed: List[str] = Field(
        default_factory=list, description="Solution IDs proposed this turn"
    )

    # ============================================================
    # Progress Assessment
    # ============================================================
    progress_made: bool = Field(description="Did investigation advance this turn?")

    # ============================================================
    # Outcome
    # ============================================================
    outcome: TurnOutcome = Field(description="Turn outcome classification")

    # ============================================================
    # User Interaction
    # ============================================================
    user_message_summary: Optional[str] = Field(
        default=None, description="Summary of user message", max_length=500
    )

    agent_response_summary: Optional[str] = Field(
        default=None, description="Summary of agent response", max_length=500
    )

    # ============================================================
    # System Feedback (for iterative correction)
    # ============================================================
    system_feedback: Optional[str] = Field(
        default=None,
        description="Instruction or error from system to agent (e.g., 'Invalid evidence ID')",
        max_length=1000,
    )

    # ============================================================
    # Progress Metrics (populated by WorkingConclusionGenerator)
    # ============================================================
    momentum: Optional[InvestigationMomentum] = Field(
        default=None,
        description="Investigation momentum indicator for this turn",
    )

    blocked_reasons: List[str] = Field(
        default_factory=list,
        description="Reasons why investigation is blocked or progressing slowly",
    )

    next_steps: List[str] = Field(
        default_factory=list,
        description="Suggested next steps for the investigation",
    )

    # ============================================================
    # Observability Fields (for progress monitoring and validation tracking)
    # ============================================================
    repair_pattern: Optional[str] = Field(
        default=None,
        description="Agent state repair pattern detected this turn: "
        "hypothesis_anchoring, hypothesis_deadlock, exhausted, "
        "fix_failure_cycle, action_loop",
    )

    validation_repairs: List[str] = Field(
        default_factory=list,
        description="State repairs made by StateValidator this turn (e.g., 'Fixed milestone ordering')",
    )

    # ============================================================
    # Computed Properties
    # ============================================================
    @property
    def advancement_count(self) -> int:
        """Total items advanced this turn"""
        return (
            len(self.milestones_completed)
            + len(self.evidence_added)
            + len(self.hypotheses_validated)
            + len(self.solutions_proposed)
        )

    @property
    def is_skipped(self) -> bool:
        """True for a synthetic recovery placeholder (a turn that was *not
        recorded*, backfilled by ``Case.reconcile_turn_sequence``).

        The single source of truth for SKIPPED screening — analyses that count or
        window ``turn_history`` must exclude these so a placeholder isn't mistaken
        for real diagnostic work.
        """
        return self.outcome is TurnOutcome.SKIPPED

    # ============================================================
    # Configuration
    # ============================================================
    class Config:
        frozen = True  # Immutable once created


# ============================================================
# Conclusion Models (Section 10)
# ============================================================


class ConfidenceLevel(str, Enum):
    """
    Categorical confidence levels.
    Maps to numeric confidence scores.
    """

    SPECULATION = "speculation"
    """
    Low confidence guess.
    Score: < 0.5
    """

    PROBABLE = "probable"
    """
    Likely but not certain.
    Score: 0.5 - 0.69
    """

    CONFIDENT = "confident"
    """
    High confidence.
    Score: 0.7 - 0.89
    """

    VERIFIED = "verified"
    """
    Evidence-backed certainty.
    Score: >= 0.9
    """

    @staticmethod
    def from_score(score: float) -> "ConfidenceLevel":
        """Convert numeric score to categorical level"""
        if score < 0.5:
            return ConfidenceLevel.SPECULATION
        elif score < 0.7:
            return ConfidenceLevel.PROBABLE
        elif score < 0.9:
            return ConfidenceLevel.CONFIDENT
        else:
            return ConfidenceLevel.VERIFIED


class WorkingConclusion(BaseModel):
    """
    Agent current best understanding of the problem.
    Updated iteratively as investigation progresses.

    Less authoritative than RootCauseConclusion.
    """

    statement: str = Field(
        description="Current conclusion statement", min_length=1, max_length=1000
    )

    likelihood: float = Field(
        ge=0.0, le=1.0, description="Likelihood of this conclusion (0.0-1.0)"
    )

    reasoning: str = Field(
        description="Why agent believes this conclusion", max_length=2000
    )

    supporting_evidence_ids: List[str] = Field(
        default_factory=list, description="Evidence IDs supporting this conclusion"
    )

    caveats: List[str] = Field(
        default_factory=list, description="Limitations or uncertainties"
    )

    # ============================================================
    # Metadata
    # ============================================================
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this conclusion was formed/updated",
    )

    supersedes_conclusion_at: Optional[datetime] = Field(
        default=None, description="Timestamp of previous conclusion this replaces"
    )


class RootCauseConclusion(BaseModel):
    """
    Final determination of root cause.
    More authoritative than WorkingConclusion.
    """

    root_cause: str = Field(
        description="Definitive statement of root cause", min_length=1, max_length=1000
    )

    confidence_level: ConfidenceLevel = Field(
        description="Categorical confidence level"
    )

    likelihood: float = Field(
        ge=0.0, le=1.0, description="Numeric likelihood score (0.0-1.0)"
    )

    mechanism: str = Field(
        description="How this root cause led to the symptom", max_length=2000
    )

    # ============================================================
    # Evidence Basis
    # ============================================================
    evidence_basis: List[str] = Field(
        default_factory=list, description="Evidence IDs supporting this conclusion"
    )

    validated_hypothesis_id: Optional[str] = Field(
        default=None,
        description="If identified via hypothesis validation, the hypothesis ID",
    )

    # ============================================================
    # Contributing Factors
    # ============================================================
    contributing_factors: List[str] = Field(
        default_factory=list,
        description="Secondary factors that made the problem worse or more likely",
    )

    # ============================================================
    # Metadata
    # ============================================================
    determined_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When root cause was determined",
    )

    determined_by: str = Field(
        default="agent", description="Who determined: 'agent' or user_id"
    )

    # ============================================================
    # Validation
    # ============================================================
    @model_validator(mode="after")
    def confidence_consistency(self):
        """Ensure confidence_level matches likelihood"""
        level = self.confidence_level
        score = self.likelihood

        if level and score is not None:
            expected_level = ConfidenceLevel.from_score(score)
            if level != expected_level:
                raise ValueError(
                    f"confidence_level {level} does not match likelihood {score} (expected {expected_level})"
                )

        return self


# ============================================================
# Special State Models (Section 11)
# ============================================================


class EscalationType(str, Enum):
    """Reason for escalation"""

    EXPERTISE_REQUIRED = "expertise_required"
    """
    Requires specialized domain expertise.
    Beyond agent knowledge.
    """

    PERMISSIONS_REQUIRED = "permissions_required"
    """
    User lacks permissions for needed actions.
    Requires higher privileges.
    """

    NO_PROGRESS = "no_progress"
    """
    Investigation is stuck despite best efforts.
    Human insight needed.
    """

    USER_REQUEST = "user_request"
    """
    User explicitly requested escalation.
    """

    CRITICAL_SEVERITY = "critical_severity"
    """
    Problem too critical for agent-only investigation.
    Human oversight required.
    """

    OTHER = "other"
    """
    Does not fit standard escalation reasons.
    """


class EscalationState(BaseModel):
    """
    Investigation escalated to human expert.
    Tracks escalation lifecycle.
    """

    escalation_type: EscalationType = Field(description="Why escalation was needed")

    reason: str = Field(
        description="Detailed explanation of escalation reason", max_length=1000
    )

    escalated_to: Optional[str] = Field(
        default=None, description="Team or person escalated to", max_length=200
    )

    escalated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When escalation occurred",
    )

    # ============================================================
    # Context Transfer
    # ============================================================
    context_summary: str = Field(
        description="Summary of investigation so far for escalation recipient",
        max_length=5000,
    )

    key_findings: List[str] = Field(
        default_factory=list, description="Key findings to communicate to expert"
    )

    # ============================================================
    # Resolution
    # ============================================================
    resolution: Optional[str] = Field(
        default=None, description="How escalation was resolved", max_length=2000
    )

    resolved_at: Optional[datetime] = Field(
        default=None, description="When escalation was resolved"
    )

    @property
    def is_active(self) -> bool:
        """Check if escalation is still active"""
        return self.resolved_at is None


# ============================================================
# Documentation Models (Section 12)
# ============================================================


class DocumentType(str, Enum):
    """Type of generated document."""

    RUNBOOK = "runbook"
    """Runbook entry for future reference"""

    CHAT_SUMMARY = "chat_summary"
    """Summary of investigation conversation"""

    TIMELINE = "timeline"
    """Timeline visualization of events"""

    EVIDENCE_BUNDLE = "evidence_bundle"
    """Compiled evidence package"""

    OTHER = "other"
    """Does not fit standard document types"""


class GeneratedDocument(BaseModel):
    """
    A generated document artifact.
    """

    document_id: str = Field(
        default_factory=lambda: f"doc_{uuid4().hex[:12]}",
        description="Unique document identifier",
    )

    document_type: DocumentType = Field(description="Type of document")

    title: str = Field(description="Document title", min_length=1, max_length=200)

    content_ref: str = Field(
        description="Reference to document content (S3 URI, file path, etc.)",
        max_length=1000,
    )

    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When document was generated",
    )

    format: str = Field(
        description="Document format: markdown | pdf | html | json | other",
        max_length=50,
    )

    size_bytes: Optional[int] = Field(
        default=None, ge=0, description="Document size in bytes"
    )

    @field_validator("format")
    @classmethod
    def valid_format(cls, v):
        """
        Validate format.
        """
        allowed = ["markdown", "pdf", "html", "json", "txt", "other"]
        if v not in allowed:
            raise ValueError(f"format must be one of: {allowed}")
        return v


class DocumentationData(BaseModel):
    """
    Documentation generated when case closes.
    Captures lessons learned and artifacts.
    """

    documents_generated: List[GeneratedDocument] = Field(
        default_factory=list, description="All documents generated for this case"
    )

    runbook_entry: Optional[str] = Field(
        default=None,
        description="Runbook entry created from this case",
        max_length=5000,
    )

    # ============================================================
    # Lessons Learned
    # ============================================================
    lessons_learned: List[str] = Field(
        default_factory=list, description="Key takeaways from investigation"
    )

    what_went_well: List[str] = Field(
        default_factory=list, description="Positive aspects of investigation"
    )

    what_could_improve: List[str] = Field(
        default_factory=list, description="Areas for improvement"
    )

    # ============================================================
    # Prevention
    # ============================================================
    preventive_measures: List[str] = Field(
        default_factory=list, description="How to prevent recurrence"
    )

    monitoring_recommendations: List[str] = Field(
        default_factory=list, description="Monitoring/alerts to add"
    )

    # ============================================================
    # Metadata
    # ============================================================
    generated_at: Optional[datetime] = Field(
        default=None, description="When documentation was generated"
    )

    generated_by: str = Field(
        default="agent", description="Who generated: 'agent' or user_id"
    )


# ============================================================
# Investigation Journal (Durable Long-Term Memory)
# ============================================================


class JournalEntry(BaseModel):
    """A single entry in the investigation journal.

    Captures a distilled insight, decision, or context that the agent
    needs to remember across the entire investigation. Entries are
    append-only and always included in the LLM context.
    """

    turn: int = Field(description="Turn number when this entry was created")

    entry_type: Literal[
        "finding", "decision", "user_context", "ruled_out", "blocker", "milestone"
    ] = Field(description="Type of journal entry")

    content: str = Field(
        description="The distilled insight (max 200 chars)",
        max_length=200,
    )

    evidence_id: Optional[str] = Field(
        default=None,
        description="Evidence ID this entry relates to, if any",
    )

    hypothesis_id: Optional[str] = Field(
        default=None,
        description="Hypothesis ID this entry relates to, if any",
    )


# ============================================================
# Core Case Model (Section 1)
# ============================================================


class Case(BaseModel):
    """
    Root case entity.
    Represents one complete troubleshooting investigation.
    """

    # ============================================================
    # Core Identity
    # ============================================================
    case_id: str = Field(
        default_factory=lambda: f"case_{uuid4().hex[:12]}",
        description="Unique case identifier",
        min_length=17,
        max_length=17,
        pattern=r"^case_[a-f0-9]{12}$",
    )

    user_id: Optional[str] = Field(
        default=None,
        description=(
            "User who created the case. NULL after the originating user is "
            "deleted (FK SET NULL). Required to be non-empty at creation time; "
            "creation logic enforces this separately from Pydantic validation."
        ),
        max_length=36,
    )

    organization_id: str = Field(
        description="Organization this case belongs to",
        min_length=1,
        max_length=36,
    )

    title: str = Field(
        description="Short case title for list views and headers (e.g., 'API Performance Issue')",
        min_length=1,
        max_length=200,
    )

    description: str = Field(
        default="",
        description="""
        Confirmed problem description - canonical, user-facing, displayed prominently in UI.

        Lifecycle:
        1. Empty initially during INQUIRY (while agent formalizes problem)
        2. Set when user confirms proposed_problem_statement and decides to investigate
        3. Immutable after status becomes INVESTIGATING (provides stable reference)
        4. Used for UI display, search, and documentation

        Example: "API experiencing slowness with 30% of requests taking >5s response time
                  across all US regions, started 2 hours ago coinciding with v2.1.3 deployment"
        """,
        max_length=2000,
    )

    # ============================================================
    # Status (PRIMARY - User-Facing Lifecycle)
    # Phase (INQUIRY, INVESTIGATING) or Disposition (RESOLVED, CLOSED)
    # ============================================================
    state: CaseState = Field(
        default=CaseState.INQUIRY,
        description="Current lifecycle state (phase or disposition)",
    )

    action_history: List[CaseAction] = Field(
        default_factory=list,
        description="Complete history of case actions (phase transitions and dispositions)",
    )

    closure_reason: Optional[str] = Field(
        default=None,
        description="Why case was closed: resolved | abandoned | escalated | inquiry_only | duplicate | other",
        max_length=100,
    )

    disposition_eligibility: Optional[Dict[str, str]] = Field(
        default=None,
        description=(
            "Per-disposition eligibility view for the case-action dropdown. "
            "Denormalized read view maintained at the single chokepoint "
            "``CaseRepository.save()`` via ``derive_disposition_eligibility``. "
            "Shape: ``{'resolved': str, 'closed': str}`` where each value is "
            "one of ``ready`` / ``needs_info`` / ``not_eligible``. "
            "Frontend uses this to gate Resolve/Close affordances on the "
            "current case content, not just the structural action graph. "
            "Always populated for persisted cases; may be None on in-memory "
            "Case objects before the first save."
        ),
    )

    pending_transition: Optional[Dict[str, Any]] = Field(
        default=None,
        description="""
        Pending status transition awaiting user confirmation (User-Agent Handshake pattern).

        Used for terminal transitions that require explicit user confirmation:
        - to_state: Target status (str)
        - reason: Why transition is being proposed (str)
        - summary: Agent's explanation to user (str)
        - evidence_ids: Supporting evidence (List[str])
        - proposed_at: When transition was proposed (str ISO datetime)
        - proposed_by: Who proposed it ("agent" | "user" | user_id)
        - closure_reason: Derived closure categorization for CLOSED
          proposals (str, set by propose_transition)
        - needs_info: RESOLVED proposal parked while the readiness ask is
          outstanding (bool; resolution NEEDS_INFO flow)
        - re_presented: The confirmation was already re-presented once for
          an ambiguous typed reply; the next non-answer withdraws the
          proposal instead of re-asking (bool; pending-gate escape lane)

        Cleared after transition executes or is cancelled. propose_transition
        rebuilds the dict from scratch, so the per-proposal flags
        (needs_info / re_presented) reset on every new proposal.
        """,
    )

    last_suggestions: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "DECIDE suggestions with intent metadata from the last agent turn. "
            "Used by the intent resolver to match typed responses against offered choices. "
            "Updated after each turn; only suggestions carrying intent metadata are stored."
        ),
    )

    kb_context: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "Deterministic KB pre-fetch results injected at key transitions. "
            "Populated at INQUIRY→INVESTIGATING (symptom search) and when "
            "root_cause_identified completes (remediation search). Included "
            "in the LLM context as historical suggestions, not absolute truths."
        ),
    )

    # ============================================================
    # Investigation Progress (SECONDARY - Internal Detail)
    # ============================================================
    progress: InvestigationProgress = Field(
        default_factory=InvestigationProgress,
        description="Milestone-based progress tracking",
    )

    # ============================================================
    # Turn Tracking
    # ============================================================
    current_turn: int = Field(
        default=0,
        ge=0,
        description="Current turn number (increments with each user-agent exchange)",
    )

    turns_without_progress: int = Field(
        default=0,
        ge=0,
        description="Consecutive turns with no milestone advancement (for stuck detection)",
    )

    turn_history: List[TurnProgress] = Field(
        default_factory=list, description="Complete history of all turns"
    )

    # ============================================================
    # Conversation Messages (RESTORED)
    # ============================================================
    messages: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="""
        Complete conversation history (user queries + agent responses).

        Per case-storage-design.md Section 4.7, each message contains:
        - message_id: str - Unique identifier
        - case_id: str - Case this message belongs to
        - turn_number: int - Which turn this message belongs to
        - role: str - "user" | "assistant" | "system"
        - content: str - The actual message text
        - created_at: datetime - When message was created (ISO format)
        - token_count: Optional[int] - Number of tokens in content
        - metadata: dict - Additional data (sources, tools used, etc.)

        NOTE: Does NOT contain session_id (per case-and-session-concepts.md)
        Sessions provide authentication only, not message ownership.

        Relationship to turn_history:
        - messages[i].turn_number references turn_history[j].turn_number
        - Provides the "what was said" to complement turn_history's "what happened"
        """,
    )

    message_count: int = Field(
        default=0, ge=0, description="Total number of messages (user + agent combined)"
    )

    # ============================================================
    # Investigation Strategy
    # ============================================================
    investigation_strategy: InvestigationStrategy = Field(
        default=InvestigationStrategy.POST_MORTEM,
        description="Investigation approach: ACTIVE_INCIDENT (speed) vs POST_MORTEM (thoroughness)",
    )

    # ============================================================
    # Problem Context
    # ============================================================
    inquiry: InquiryData = Field(
        default_factory=InquiryData,
        description="Pre-investigation INQUIRY state data",
    )

    problem_verification: Optional[ProblemVerification] = Field(
        default=None,
        description="Consolidated verification data (symptom, scope, timeline, changes)",
    )

    # ============================================================
    # Investigation Data
    # ============================================================
    uploaded_files: List["UploadedFile"] = Field(
        default_factory=list,
        description="""
        All files uploaded to this case (raw file metadata).

        Files can be uploaded at ANY phase (INQUIRY or INVESTIGATING).
        Evidence is DERIVED from uploaded files after analysis during INVESTIGATING phase.

        Difference from evidence:
        - uploaded_files: Raw file metadata (file_id, filename, size, upload time)
        - evidence: Investigation data linked to hypotheses (only in INVESTIGATING phase)
        """,
    )

    evidence: List[Evidence] = Field(
        default_factory=list, description="All evidence collected during investigation"
    )

    evidence_needs: List[EvidenceNeed] = Field(
        default_factory=list,
        description=(
            "Demand-side pool: verification requirements the investigation "
            "has identified. Created by the LLM at problem-statement "
            "confirmation (symptom needs) and at hypothesis creation "
            "(causal needs). See evidence-needs-design.md."
        ),
    )

    hypotheses: Dict[str, Hypothesis] = Field(
        default_factory=dict, description="Generated hypotheses (key = hypothesis_id)"
    )

    solutions: List[Solution] = Field(
        default_factory=list, description="Proposed and applied solutions"
    )

    # ============================================================
    # Causal Graph (Two-Dimensional Hypothesis Methodology §3, §9.1)
    # ------------------------------------------------------------
    # The case owns ONE causal DAG rooted at the active problem D. Nodes are the
    # problem / intermediate states / candidate roots; edges are cause->effect
    # (with and_group for AND-sets). A Hypothesis is a named root->D path over
    # this graph (Hypothesis.path / root_node_id). Phase 1 adds the collections
    # additively; the engine seeds the PROBLEM node and grows the graph by lazy
    # backward expansion in Phase 2/3.
    # ============================================================
    causal_nodes: Dict[str, CausalNode] = Field(
        default_factory=dict,
        description="Causal-graph nodes, keyed by node_id (D + intermediate + roots)",
    )

    causal_edges: List[CausalEdge] = Field(
        default_factory=list,
        description="Directed cause->effect edges (and_group marks AND-sets)",
    )

    proposed_actions: List[ProposedAction] = Field(
        default_factory=list,
        description="Actions proposed by agent for user to execute (evidence-driven framework)",
    )

    action_attempts: List[ActionAttempt] = Field(
        default_factory=list,
        description="User attempts to execute proposed actions (compliance tracking)",
    )

    # ============================================================
    # Cross-Cutting State
    # ============================================================
    working_conclusion: Optional[WorkingConclusion] = Field(
        default=None,
        description="Agent current best understanding (updated iteratively)",
    )

    root_cause_conclusion: Optional[RootCauseConclusion] = Field(
        default=None, description="Final root cause determination"
    )

    investigation_journal: List[JournalEntry] = Field(
        default_factory=list,
        description="Structured log of key findings, decisions, and context. "
        "Append-only. Always included in full in LLM context.",
    )

    # ============================================================
    # Special States
    # ============================================================
    escalation_state: Optional[EscalationState] = Field(
        default=None, description="Escalated to human expert"
    )

    # ============================================================
    # Documentation
    # ============================================================
    documentation: DocumentationData = Field(
        default_factory=DocumentationData,
        description="Generated documentation and lessons learned",
    )

    # ============================================================
    # Timestamps
    # ============================================================
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When case was created",
    )

    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Last modification timestamp",
    )

    last_activity_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Most recent user/agent interaction (for 'updated Xm ago' display)",
    )

    version: int = Field(
        default=1,
        ge=1,
        description=(
            "Optimistic concurrency control token. Incremented on every "
            "successful aggregate save. Callers that read-modify-write a "
            "case must pass the loaded version back through save(case); "
            "`save` raises StaleCaseException on mismatch. Scoped "
            "single-row UPDATEs (update_evidence_vectorized, etc.) do "
            "NOT bump this field — they operate on child tables."
        ),
    )

    resolved_at: Optional[datetime] = Field(
        default=None, description="When case reached RESOLVED state"
    )

    closed_at: Optional[datetime] = Field(
        default=None,
        description="When case reached terminal state (RESOLVED or CLOSED)",
    )

    # ============================================================
    # Computed Properties
    # ============================================================
    def find_uploaded_file(self, file_id: Optional[str]) -> Optional["UploadedFile"]:
        """Resolve an UploadedFile by file_id from this case's aggregate.

        The canonical aggregate-traversal point for code that holds an
        Evidence row (which carries source_file_id) and needs the file's
        metadata (filename, content_type, content_hash, storage_ref,
        upload_source, etc.). Replaces the old denormalized
        evidence.original_filename / evidence.content_ref / evidence.data_type
        fields — those were copies of upload data; this method walks the
        FK instead.

        None-safe at the source_file_id end: chat-extracted evidence
        (source_type=USER_DESCRIPTION) has source_file_id IS NULL, so
        callers can pass it through without a guard:

            file_meta = case.find_uploaded_file(ev.source_file_id)
            filename = file_meta.filename if file_meta else None
            is_page_capture = file_meta is not None and file_meta.upload_source == "page_capture"

        Assumes the case aggregate is fully loaded (uploaded_files
        populated by the repository's get_case path). Partial-load
        paths must NOT call this — they would silently return None for
        every lookup.
        """
        if not file_id:
            return None
        return next(
            (f for f in self.uploaded_files if f.file_id == file_id),
            None,
        )

    @property
    def current_stage(self) -> Optional[InvestigationStage]:
        """
        Computed investigation stage (only when INVESTIGATING).
        Returns: UNDERSTANDING | DIAGNOSING | RESOLVING | None
        """
        if self.state != CaseState.INVESTIGATING:
            return None
        return self.progress.current_stage

    @property
    def current_momentum(self) -> Optional[InvestigationMomentum]:
        """
        Get momentum from the most recent turn for real-time dashboard display.

        Returns the momentum value from the latest turn in turn_history,
        or None if no turns recorded yet.
        """
        if not self.turn_history:
            return None
        return self.turn_history[-1].momentum

    @property
    def is_terminal(self) -> bool:
        """
        Check if case is in terminal state.
        Terminal states: RESOLVED, CLOSED (no further transitions).
        """
        return self.state in [CaseState.RESOLVED, CaseState.CLOSED]

    @property
    def time_to_resolution(self) -> Optional[timedelta]:
        """
        Time from case creation to terminal state.
        Returns None if case not yet closed.
        """
        if self.closed_at:
            return self.closed_at - self.created_at
        return None

    @property
    def evidence_count_by_category(self) -> Dict[str, int]:
        """Count evidence by category for analytics"""
        counts: Dict[str, int] = {}
        for ev in self.evidence:
            cat = ev.category.value
            counts[cat] = counts.get(cat, 0) + 1
        return counts

    @property
    def active_hypotheses(self) -> List[Hypothesis]:
        """Get hypotheses currently being tested"""
        return [
            h for h in self.hypotheses.values() if h.state == HypothesisState.ACTIVE
        ]

    @property
    def validated_hypotheses(self) -> List[Hypothesis]:
        """Get validated hypotheses (found root cause)"""
        return [
            h for h in self.hypotheses.values() if h.state == HypothesisState.VALIDATED
        ]

    @property
    def warnings(self) -> List[Dict[str, Any]]:
        """
        Get active warnings for UI display.

        Returns list of warning dictionaries with type, severity, message.
        Used by frontend to display alert banners.
        """
        warnings: List[Dict[str, Any]] = []

        # Info: Escalation active
        if self.escalation_state and self.escalation_state.is_active:
            warnings.append(
                {
                    "type": "escalation",
                    "severity": "info",
                    "message": f"Escalated to {self.escalation_state.escalated_to or 'expert'}",
                    "escalated_at": self.escalation_state.escalated_at.isoformat(),
                }
            )

        # Warning: Terminal state but no documentation
        if self.is_terminal and len(self.documentation.documents_generated) == 0:
            warnings.append(
                {
                    "type": "no_documentation",
                    "severity": "info",
                    "message": "Case closed but no documentation generated",
                    "action": "Generate post-mortem or runbook",
                }
            )

        return warnings

    # ============================================================
    # Validation
    # ============================================================
    @field_validator("title")
    @classmethod
    def title_not_empty(cls, v):
        """Ensure title is not just whitespace"""
        if not v or not v.strip():
            raise ValueError("Title cannot be empty")
        return v.strip()

    @field_validator("description")
    @classmethod
    def description_valid(cls, v):
        """Ensure description is meaningful if not empty"""
        if v and not v.strip():
            raise ValueError("Description cannot be only whitespace")
        return v.strip() if v else ""

    @model_validator(mode="after")
    def description_required_when_investigating(self):
        """Ensure description is set before transitioning to INVESTIGATING"""
        state = self.state
        description = self.description.strip()

        # INVESTIGATING requires confirmed problem description
        if state == CaseState.INVESTIGATING and not description:
            raise ValueError(
                "description must be set (from confirmed proposed_problem_statement) "
                "before transitioning to INVESTIGATING status"
            )

        return self

    @field_validator("closure_reason")
    @classmethod
    def valid_closure_reason(cls, v):
        """closure_reason is a sub-categorization of CLOSED state, all
        engine-derived. None for non-terminal and RESOLVED cases.
        See: VALID_CLOSURE_REASONS in this module."""
        if v is not None and v not in VALID_CLOSURE_REASONS:
            raise ValueError(
                f"closure_reason must be one of: {sorted(VALID_CLOSURE_REASONS)}"
            )
        return v

    @field_validator("action_history")
    @classmethod
    def action_history_ordered(cls, v):
        """Ensure action history is chronologically ordered."""
        if len(v) > 1:
            for i in range(len(v) - 1):
                if v[i].triggered_at > v[i + 1].triggered_at:
                    raise ValueError("Action history must be chronologically ordered")
        return v

    @field_validator("turn_history")
    @classmethod
    def turn_history_sequential(cls, v):
        """Detect non-sequential turn numbers WITHOUT failing.

        A sequencing anomaly is *repairable derived state*, so it must never
        brick persistence (raising here is what permanently wedged a case after
        a single interrupted turn). We log it for visibility; the actual repair —
        backfilling ``SKIPPED`` placeholders so numbers stay consecutive and
        existing turn_numbers (referenced by hypotheses/messages) stay valid — is
        done by :meth:`Case.reconcile_turn_sequence` on load and before save.
        """
        if len(v) > 1:
            for i in range(len(v) - 1):
                if v[i].turn_number + 1 != v[i + 1].turn_number:
                    logger.warning(
                        "Non-sequential turn_history at index %d (%s -> %s); "
                        "will be reconciled.",
                        i,
                        v[i].turn_number,
                        v[i + 1].turn_number,
                    )
                    break
        return v

    @property
    def effective_current_turn(self) -> int:
        """The committed turn number: the last recorded ``turn_history`` number,
        or ``current_turn`` when no turn has been recorded yet.

        Repositories persist THIS (not the raw ``current_turn``) so the stored
        counter can never run ahead of ``turn_history`` — the drift that wedged
        cases. The in-memory ``current_turn`` (the in-flight turn number business
        logic reads) is left untouched; on a successful turn the two are equal.
        """
        return (
            self.turn_history[-1].turn_number
            if self.turn_history
            else self.current_turn
        )

    def reconcile_turn_sequence(self) -> int:
        """Make ``turn_history`` strictly consecutive and ``current_turn`` consistent.

        Returns the number of repairs applied (backfilled + renumbered turns);
        ``0`` means the history was already healthy. **Never raises** — a
        sequencing anomaly is repairable derived state, so it must never brick
        persistence.

        Repairs:

        * **gap** (``next > prev + 1``) → backfill ``SKIPPED`` placeholder turns
          for the missing numbers (timestamped from the preceding turn so the
          history stays time-ordered), preserving every existing ``turn_number``
          so references from hypotheses/messages stay valid.
        * **duplicate / out-of-order** (``next <= prev``) or a **gap larger than
          ``_MAX_TURN_BACKFILL``** (corruption, not a one-turn interruption) →
          renumber the later entry to ``prev + 1``.

        ``current_turn``: on the healthy fast path it is only ever *raised* to the
        last number (never lowered, so an in-flight turn number is preserved);
        when a repair rewrites the history, the now-authoritative last number
        wins (so a destructive renumber can't leave the counter stranded ahead).

        A no-op on healthy cases. Called on load and before save so a transient
        anomaly self-heals into a visible, contained ``SKIPPED`` turn instead of
        permanently wedging the case.
        """
        history = self.turn_history
        if not history:
            return 0

        # Fast path: already consecutive → no allocation. Only keep current_turn
        # from falling behind the last recorded turn (never lower it).
        if all(
            history[i].turn_number + 1 == history[i + 1].turn_number
            for i in range(len(history) - 1)
        ):
            last = history[-1].turn_number
            if self.current_turn < last:
                self.current_turn = last
            return 0

        repairs = 0
        rebuilt: List[TurnProgress] = [history[0]]
        for entry in history[1:]:
            prev = rebuilt[-1].turn_number
            gap = entry.turn_number - prev - 1
            if entry.turn_number <= prev or gap > _MAX_TURN_BACKFILL:
                # duplicate / out-of-order, or a gap too large to be a normal
                # one-turn interruption → renumber (TurnProgress is frozen).
                if gap > _MAX_TURN_BACKFILL:
                    logger.error(
                        "Turn-sequence gap of %d on case %s exceeds cap (%d); "
                        "renumbering instead of backfilling.",
                        gap,
                        getattr(self, "case_id", "?"),
                        _MAX_TURN_BACKFILL,
                    )
                rebuilt.append(entry.model_copy(update={"turn_number": prev + 1}))
                repairs += 1
                continue
            for missing in range(prev + 1, entry.turn_number):
                rebuilt.append(
                    TurnProgress(
                        turn_number=missing,
                        timestamp=rebuilt[-1].timestamp,
                        outcome=TurnOutcome.SKIPPED,
                        progress_made=False,
                        user_message_summary="(turn not recorded)",
                        agent_response_summary=(
                            "(turn not recorded — recovered after an "
                            "interrupted turn)"
                        ),
                    )
                )
                repairs += 1
            rebuilt.append(entry)

        self.turn_history = rebuilt
        last = rebuilt[-1].turn_number
        # Keep the monotonic guarantee: never lower an in-flight current_turn.
        # The one exception is a cap-renumber that TRUNCATED the trailing number
        # downward (corruption recovery) — there the lowered last is authoritative
        # so the next turn doesn't re-open the huge gap.
        if last < history[-1].turn_number or self.current_turn < last:
            self.current_turn = last
        logger.warning(
            "Reconciled turn_history for case %s: %d repair(s), %d turns.",
            getattr(self, "case_id", "?"),
            repairs,
            len(rebuilt),
        )
        return repairs

    @model_validator(mode="after")
    def validate_timestamp_ordering(self) -> "Case":
        """
        Enforce timestamp chronological ordering per DB spec.

        Spec Reference: DB Design Specification lines 183-188
        Constraint: cases_timestamp_order_check
        """
        # created_at <= updated_at
        if self.created_at > self.updated_at:
            raise ValueError(
                f"created_at ({self.created_at}) cannot be after updated_at ({self.updated_at})"
            )

        # created_at <= last_activity_at
        if self.created_at > self.last_activity_at:
            raise ValueError(
                f"created_at ({self.created_at}) cannot be after last_activity_at ({self.last_activity_at})"
            )

        # resolved_at must be after created_at (if set)
        if self.resolved_at and self.created_at > self.resolved_at:
            raise ValueError(
                f"created_at ({self.created_at}) cannot be after resolved_at ({self.resolved_at})"
            )

        # closed_at must be after created_at (if set)
        if self.closed_at and self.created_at > self.closed_at:
            raise ValueError(
                f"created_at ({self.created_at}) cannot be after closed_at ({self.closed_at})"
            )

        # resolved_at <= closed_at (if both set)
        if self.resolved_at and self.closed_at and self.resolved_at > self.closed_at:
            raise ValueError(
                f"resolved_at ({self.resolved_at}) cannot be after closed_at ({self.closed_at})"
            )

        return self

    @model_validator(mode="after")
    def validate_state_timestamp_consistency(self) -> "Case":
        """
        Enforce state-timestamp consistency per DB spec.

        Spec Reference: DB Design Specification lines 157-176
        """
        # Skip validation during atomic_update() to avoid Catch-22
        if getattr(self, "_in_atomic_update", False):
            return self

        # Allow atomic transitions by checking if multiple terminal fields are being set
        # This is a private flag used to verify transient states during atomic updates
        if hasattr(self, "_in_terminal_transition"):
            return self

        # RESOLVED requires resolved_at and closed_at
        if self.state == CaseState.RESOLVED:
            if not self.resolved_at:
                raise ValueError("RESOLVED state requires resolved_at timestamp")
            if not self.closed_at:
                raise ValueError("RESOLVED state requires closed_at timestamp")

        # Non-RESOLVED must not have resolved_at
        if self.state != CaseState.RESOLVED and self.resolved_at:
            raise ValueError(
                f"resolved_at can only be set when state is RESOLVED (current: {self.state})"
            )

        # RESOLVED or CLOSED requires closed_at
        if self.state in [CaseState.RESOLVED, CaseState.CLOSED] and not self.closed_at:
            raise ValueError(
                f"Terminal state {self.state} requires closed_at timestamp"
            )

        # Non-terminal must not have closed_at
        if self.state not in [CaseState.RESOLVED, CaseState.CLOSED] and self.closed_at:
            raise ValueError(
                f"closed_at can only be set when state is RESOLVED or CLOSED (current: {self.state})"
            )

        # closure_reason is a sub-categorization of CLOSED only.
        #   - state == CLOSED:    closure_reason MUST be set (engine-derived)
        #   - state == RESOLVED:  closure_reason MUST be None (resolution
        #                          itself is the reason; sub-categorization
        #                          would be redundant with the state)
        #   - non-terminal:        closure_reason MUST be None
        if self.state == CaseState.CLOSED and not self.closure_reason:
            raise ValueError("CLOSED state requires closure_reason")
        if self.state != CaseState.CLOSED and self.closure_reason:
            raise ValueError(
                f"closure_reason can only be set when state is CLOSED "
                f"(current: {self.state})"
            )

        return self

    def atomic_transition(self):
        """
        Context manager to allow atomic updates of interdependent fields.
        Useful for transitioning to terminal states where state/timestamps depend on each other.
        """

        class AtomicContext:
            def __init__(self, case):
                self.case = case

            def __enter__(self):
                self.case._in_terminal_transition = True
                return self.case

            def __exit__(self, exc_type, exc_val, exc_tb):
                if hasattr(self.case, "_in_terminal_transition"):
                    del self.case._in_terminal_transition
                # Only validate if no exception occurred during the block
                if exc_type is None:
                    self.case.validate_state_timestamp_consistency()

        return AtomicContext(self)

    @model_validator(mode="after")
    def validate_description_for_investigation_and_resolution(self) -> "Case":
        """
        Description = problem statement. Required for the two states that
        carry "we know what the problem is" semantics:

        * INVESTIGATING: entry gate — investigation without a stated problem
          is wandering. Also requires problem_statement_confirmed and
          decided_to_investigate (separate inquiry-readiness checks).
        * RESOLVED: the case must have a known problem to be meaningfully
          resolved (the resolution would otherwise have nothing to attach
          to). Per the legitimate transitions spec (RESOLVED only comes
          from INVESTIGATING), this should be naturally satisfied; the
          check is defense-in-depth against bypassed validators.

        INQUIRY: empty allowed (still being formulated).
        CLOSED: empty allowed when path is inquiry → closed early-abandon;
                non-empty when path went through INVESTIGATING. The state
                itself imposes no requirement — closure_reason captures
                the why-closed fact instead.

        Mirror of the DB CHECK cases_description_required_for_investigation.
        """
        if self.state in (CaseState.INVESTIGATING, CaseState.RESOLVED):
            if not self.description or self.description == "":
                raise ValueError(
                    f"{self.state.value} state requires non-empty description "
                    "(description carries the case's problem statement)"
                )

        if self.state == CaseState.INVESTIGATING:
            # Inquiry-phase readiness — separate from the description check
            # but enforced at the same gate.
            if not self.inquiry.problem_statement_confirmed:
                raise ValueError(
                    "INVESTIGATING state requires confirmed problem statement"
                )
            if not self.inquiry.decided_to_investigate:
                raise ValueError(
                    "INVESTIGATING state requires investigation commitment"
                )

        return self

    # ============================================================
    # Atomic State Update Helper
    # ============================================================
    def atomic_update(self, **updates: Any) -> None:
        """
        Perform atomic updates to multiple fields, bypassing incremental validation.

        This method is necessary for state transitions that require multiple fields
        to be updated simultaneously (e.g., setting state=RESOLVED requires
        resolved_at to be set, but resolved_at can only be set when state=RESOLVED).

        The validation Catch-22:
        - Cannot set state=RESOLVED if resolved_at is None (validator line 3361)
        - Cannot set resolved_at if status != RESOLVED (validator line 3367)

        Usage:
            case.atomic_update(
                state=CaseState.RESOLVED,
                resolved_at=datetime.now(UTC),
                closed_at=datetime.now(UTC),
                closure_reason="resolved"
            )

        Args:
            **updates: Field names and values to update atomically

        Raises:
            ValueError: If the post-update state violates a cross-field invariant
                (e.g., state=RESOLVED with no resolved_at). Pre-update state is
                restored before the exception propagates.

        Note:
            Sets _in_atomic_update flag to bypass per-field validators that would
            otherwise reject transient inconsistent states during multi-field
            updates. After all updates apply, this method re-runs the cross-field
            validators on the final state to catch callers that forgot a required
            field — without that revalidation step, atomic_update would silently
            accept malformed transitions (e.g., state=RESOLVED without timestamps).
        """
        snapshot = {f: getattr(self, f) for f in updates}

        object.__setattr__(self, "_in_atomic_update", True)
        try:
            for field_name, value in updates.items():
                object.__setattr__(self, field_name, value)
        finally:
            object.__setattr__(self, "_in_atomic_update", False)

        try:
            self.validate_state_timestamp_consistency()
            self.validate_timestamp_ordering()
            self.validate_description_for_investigation_and_resolution()
        except ValueError:
            object.__setattr__(self, "_in_atomic_update", True)
            try:
                for field_name, value in snapshot.items():
                    object.__setattr__(self, field_name, value)
            finally:
                object.__setattr__(self, "_in_atomic_update", False)
            raise

    # ============================================================
    # Configuration
    # ============================================================
    model_config = ConfigDict(
        validate_assignment=True,  # Validate on field assignment
        use_enum_values=False,  # Keep enum instances
        json_encoders={
            datetime: lambda v: v.isoformat()
            + ("Z" if v.tzinfo in (None, timezone.utc) else ""),
            timedelta: lambda v: v.total_seconds(),
        },
    )
