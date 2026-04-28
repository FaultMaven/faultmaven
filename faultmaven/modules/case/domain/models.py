"""Case data models - Milestone-based investigation system.

This module defines the complete data structure for FaultMaven's investigation system
based on the Investigation Architecture Specification v2.0.

Key Models:
- Case: Root case entity with milestone-based progress tracking
- CaseStatus: Lifecycle status (INQUIRY -> INVESTIGATING -> RESOLVED/CLOSED)
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

from datetime import UTC, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

# ============================================================
# Status & Lifecycle Models (Section 2)
# ============================================================


class CaseStatus(str, Enum):
    """
    Case lifecycle status — passive label describing a case's current condition.

    Values fall into two categories:
    - **Phases** (active work): INQUIRY, INVESTIGATING
    - **Dispositions** (terminal resolution): RESOLVED, CLOSED

    Case Actions (phase transitions and dispositions):
      INQUIRY → INVESTIGATING  (phase transition)
      INQUIRY → RESOLVED       (fast-track disposition)
      INQUIRY → CLOSED         (disposition)
      INVESTIGATING → RESOLVED (disposition)
      INVESTIGATING → CLOSED   (disposition)
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
        """Check if this status is a disposition (terminal)."""
        return self in [CaseStatus.RESOLVED, CaseStatus.CLOSED]

    @property
    def is_active(self) -> bool:
        """Check if this status is a phase (active, not terminal)."""
        return self in [CaseStatus.INQUIRY, CaseStatus.INVESTIGATING]

    @property
    def is_phase(self) -> bool:
        """Check if this status represents an active phase (INQUIRY or INVESTIGATING)."""
        return self.is_active

    @property
    def is_disposition(self) -> bool:
        """Check if this status represents a terminal disposition (RESOLVED or CLOSED)."""
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

    from_status: CaseStatus = Field(description="Status before the action")

    to_status: CaseStatus = Field(description="Status after the action")

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
        if not is_valid_action(self.from_status, self.to_status):
            raise ValueError(
                f"Invalid case action: {self.from_status} -> {self.to_status}"
            )
        return self

    class Config:
        frozen = True  # Immutable once created


# Backward compatibility alias
CaseStatusTransition = CaseAction


def is_valid_action(from_status: CaseStatus, to_status: CaseStatus) -> bool:
    """
    Validate a case action (phase transition or disposition change).

    Valid Case Actions:
    - INQUIRY → INVESTIGATING (phase transition: start investigation)
    - INQUIRY → RESOLVED (disposition: fast-track KB resolution)
    - INQUIRY → CLOSED (disposition: no investigation needed)
    - INVESTIGATING → RESOLVED (disposition: solution verified)
    - INVESTIGATING → CLOSED (disposition: abandoned/escalated)

    Invalid:
    - RESOLVED → * (disposition is terminal)
    - CLOSED → * (disposition is terminal)
    - INVESTIGATING → INQUIRY (no backward phase transition)
    """
    valid_actions = {
        CaseStatus.INQUIRY: [
            CaseStatus.INVESTIGATING,
            CaseStatus.RESOLVED,
            CaseStatus.CLOSED,
        ],
        CaseStatus.INVESTIGATING: [CaseStatus.RESOLVED, CaseStatus.CLOSED],
        CaseStatus.RESOLVED: [],  # Disposition — terminal
        CaseStatus.CLOSED: [],  # Disposition — terminal
    }

    return to_status in valid_actions.get(from_status, [])


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
    - Accept hypothesis with TESTING status for quick mitigation
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
# Investigation Progress Models (Section 3)
# ============================================================


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
    mitigation_accepted: bool = Field(
        default=False,
        description=(
            "User complied with proposed temp fix (inferred from submission). "
            "Triggers DIAGNOSIS → MITIGATION transition."
        ),
    )

    mitigation_verified: bool = Field(
        default=False,
        description=(
            "User confirmed mitigation worked. "
            "Triggers MITIGATION → DIAGNOSIS return for RCA."
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

    root_cause_identified: bool = Field(
        default=False,
        description="Root cause determined (directly or via hypothesis validation)",
    )

    solution_proposed: bool = Field(
        default=False,
        description=(
            "Set programmatically when ProposedAction with action_type=SOLUTION "
            "is created. Not directly set by LLM."
        ),
    )

    # ============================================================
    # Root Cause Metadata (populated when root_cause_identified=True)
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
        Compute investigation stage from STAGE-GATE MILESTONES only.
        Progress indicators do NOT affect stage computation.

        Returns one of 3 InvestigationStage enum values:
        - DIAGNOSIS: Understanding, diagnosing, proposing actions
        - MITIGATION: Applying and verifying temporary fix
        - TREATMENT: Applying permanent fix, verifying resolution
        """
        # TREATMENT: solution_accepted but not yet verified
        if self.solution_accepted and not self.solution_verified:
            return InvestigationStage.TREATMENT

        # MITIGATION: mitigation_accepted but not yet verified
        if self.mitigation_accepted and not self.mitigation_verified:
            return InvestigationStage.MITIGATION

        # Default: DIAGNOSIS (initial state, or returned from MITIGATION)
        return InvestigationStage.DIAGNOSIS

    @property
    def stage_display_name(self) -> str:
        """
        User-facing stage name for UI display.

        1:1 mapping (no collapsing needed):
        - DIAGNOSIS → "Diagnosing"
        - MITIGATION → "Mitigating"
        - TREATMENT → "Resolving"
        """
        stage = self.current_stage
        if stage == InvestigationStage.DIAGNOSIS:
            return "Diagnosing"
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
        return self.root_cause_identified

    @property
    def resolution_complete(self) -> bool:
        """Check if resolution is complete (solution verified)."""
        return self.solution_verified

    @property
    def completed_milestones(self) -> List[str]:
        """Get list of completed milestone and indicator names."""
        milestone_map = {
            # Stage-gate milestones
            "mitigation_accepted": self.mitigation_accepted,
            "mitigation_verified": self.mitigation_verified,
            "solution_accepted": self.solution_accepted,
            "solution_verified": self.solution_verified,
            # Progress indicators
            "symptom_verified": self.symptom_verified,
            "root_cause_identified": self.root_cause_identified,
            "solution_proposed": self.solution_proposed,
        }
        return [name for name, completed in milestone_map.items() if completed]

    @property
    def pending_milestones(self) -> List[str]:
        """Get list of pending progress indicator names."""
        indicator_map = {
            "symptom_verified": self.symptom_verified,
            "root_cause_identified": self.root_cause_identified,
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
        """Ensure root cause fields are consistent"""
        identified = self.root_cause_identified
        likelihood = self.root_cause_likelihood
        method = self.root_cause_method

        if identified:
            if likelihood == 0.0:
                raise ValueError(
                    "root_cause_likelihood must be > 0 when root_cause_identified=True"
                )
            if method is None:
                raise ValueError(
                    "root_cause_method must be set when root_cause_identified=True"
                )

        return self

    @model_validator(mode="after")
    def solution_ordering(self):
        """Ensure solution milestones are ordered correctly."""
        # solution_verified requires solution_accepted
        if self.solution_verified and not self.solution_accepted:
            raise ValueError("Cannot verify solution without acceptance first")

        # mitigation_verified requires mitigation_accepted
        if self.mitigation_verified and not self.mitigation_accepted:
            raise ValueError("Cannot verify mitigation without acceptance first")

        return self


class InvestigationStage(str, Enum):
    """
    Investigation stage within the Investigating Phase.

    2-stage model with mitigation detour:
    - DIAGNOSIS: Understand, diagnose, propose actions (core stage)
    - TREATMENT: Verify permanent fix, resolve case (core stage)
    - MITIGATION: Apply and verify temporary fix (optional detour)

    DIAGNOSIS and TREATMENT are the two core stages every investigation
    passes through. MITIGATION is an optional detour that temporarily
    narrows focus to "stop the bleeding" before returning to DIAGNOSIS.

    Computed from stage-gate milestones. Stage transitions are
    inference-based — user compliance with proposed actions triggers
    transitions via compliance detection. The stage determines which
    prompt template the LLM receives.

    Investigation Paths:
    - ROOT_CAUSE: DIAGNOSIS → TREATMENT
    - MITIGATION_FIRST: DIAGNOSIS → MITIGATION (detour) → DIAGNOSIS → TREATMENT
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
    Used for investigation path routing.
    """

    ONGOING = "ongoing"
    """
    Problem is currently happening.

    Characteristics:
    - Active user impact
    - Real-time symptoms
    - Urgency to mitigate

    Routing: Likely MITIGATION path if high urgency
    """

    HISTORICAL = "historical"
    """
    Problem occurred in the past.

    Characteristics:
    - No current impact
    - Post-mortem investigation
    - Can take time for thorough RCA

    Routing: Likely ROOT_CAUSE path
    """


# ============================================================
# Problem Context Models (Section 4)
# ============================================================


class UrgencyLevel(str, Enum):
    """
    Urgency classification for path routing.

    Used with TemporalState to determine investigation path:
    - ONGOING + HIGH/CRITICAL -> MITIGATION
    - HISTORICAL + LOW/MEDIUM -> ROOT_CAUSE
    - Other combinations -> USER_CHOICE
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
    Pre-investigation INQUIRY status data.
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
        default=0, ge=0, description="Number of turns spent in INQUIRY status"
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
            "strategy. Does NOT affect path selection — influences post-mitigation "
            "agent behavior only."
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

    Post-redesign (2026-02-11):
    - UNCLASSIFIED removed (single-phase evidence creation)
    - OTHER renamed to CONTEXTUAL_EVIDENCE (clearer purpose)
    - REJECTED added (track rejected submissions for deduplication)

    Evidence is created AFTER LLM evaluation with complete classification.
    """

    # ===== RELEVANT EVIDENCE (4 categories) =====

    SYMPTOM_EVIDENCE = "symptom_evidence"
    """
    Shows problem manifestation.

    Purpose: Prove the problem exists and establish scope/timeline.

    IMPORTANT: This category describes what the data CONTAINS, not whether the user
    has committed to investigating. A log file with errors is SYMPTOM_EVIDENCE even
    during INQUIRY phase (exploratory upload before problem confirmed).

    Examples:
    - Error logs showing failures
    - Metrics showing degradation (high CPU, slow response times)
    - User impact reports
    - Deployment logs showing recent changes

    Advances Milestones: symptom_verified
    (Note: Milestone validation only runs during INVESTIGATING status. Evidence
    created during INQUIRY sits inert until investigation begins.)
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

    MITIGATION_EVIDENCE = "mitigation_evidence"
    """
    Shows whether a temporary fix worked.

    Purpose: Verify mitigation effectiveness during MITIGATION stage.

    Examples:
    - Post-mitigation metrics showing improvement
    - Error rates dropping after temporary fix
    - User confirmation that bleeding stopped
    - Logs showing stabilization after workaround

    Used during MITIGATION stage to verify temporary fix effectiveness.
    Does not advance progress indicator milestones (mitigation_verified
    is a stage-gate milestone set by compliance detection).
    """

    SOLUTION_EVIDENCE = "solution_evidence"
    """
    Shows whether a permanent fix worked.

    Purpose: Verify solution effectiveness during TREATMENT stage.

    Examples:
    - Error rate after rollback (before/after comparison)
    - Latency metrics after optimization
    - Resource usage after scaling
    - Success rate after config change
    - User confirmation that fix resolved the problem

    Used during TREATMENT stage. solution_verified is a stage-gate
    milestone set via User-Agent Handshake, not by evidence validation.
    """

    # Backward compatibility alias
    RESOLUTION_EVIDENCE = "solution_evidence"

    CONTEXTUAL_EVIDENCE = "contextual_evidence"
    """
    Provides baseline, environmental, or background context.

    Purpose: Help understand system or problem context without directly
    showing symptoms, proving causes, or validating resolutions.

    Characteristics:
    - Describes "what is already there" (baseline, normal state)
    - Neither problematic nor a fix
    - System configuration, architecture, or operational context
    - Historical baseline or reference data

    Examples:
    - System architecture diagrams
    - Current/baseline configuration files
    - "Normal" resource usage patterns (for comparison)
    - System inventory (versions, dependencies, infrastructure)
    - SLA requirements or business context
    - Historical incident reports (for reference)

    INQUIRY Phase Usage:
    - If uploaded data truly shows NO problems (clean logs, normal metrics),
      classify as CONTEXTUAL_EVIDENCE
    - If data shows problems (errors, anomalies), classify as SYMPTOM_EVIDENCE
      even during INQUIRY phase (classify based on content, not user's commitment)

    Does NOT directly advance milestones, but helps LLM understand environment.
    """

    # ===== REJECTED SUBMISSIONS =====

    REJECTED = "rejected"
    """
    Submission analyzed but rejected as not useful for investigation.

    IMPORTANT: This is NOT evidence. It exists in the evidence table for
    practical reasons (deduplication, audit trail, cost avoidance), not
    because it's evidence.

    Purpose: Track rejected submissions for:
    - Deduplication: Prevent re-upload via content_hash
    - Audit trail: Record what was submitted and evaluated
    - Cost avoidance: Don't re-analyze same file
    - User feedback: Explain why rejected

    Can be "un-rejected" if investigation context changes.

    Examples:
    - Screenshots unrelated to issue
    - Logs from unrelated services
    - Accidental uploads
    - Files determined not useful after analysis

    Reasoning captured in `primary_purpose` field.

    Note: Duplicate files are also marked as REJECTED with reference to original.
    """


class EvidenceSourceType(str, Enum):
    """
    Fundamental type of data source.

    Post-redesign (2026-02-14): Updated to align with data-classification-strategy.md (6 types).

    Migration mapping:
    - log_file, command_output, trace_data, api_response, other → LOGS
    - metrics_data, monitoring_alert → METRICS
    - config_file, database_query → CONFIGURATION
    - code_review → CODE
    - user_report → TEXT
    - screenshot → IMAGE
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
    Unstructured prose.

    Includes:
    - User's typed narrative
    - Problem descriptions
    - Observations
    - Impact reports
    - Steps to reproduce
    - Context explanations

    Characteristics: Human-written context, not machine-generated data
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


class EvidenceForm(str, Enum):
    """How evidence entered the system.

    Form is determined by payload context:
    - DOCUMENT: Turn had attachments (file upload or pasted data)
    - USER_TEXT: Query-only turn, no attachments
    - SUBMITTED_DATA: Evidence created by agent tools (search_file, deep_analysis)
    """

    DOCUMENT = "document"
    """Data submitted as attachment via /turns endpoint — file uploads AND pasted data."""

    USER_TEXT = "user_text"
    """Query-only turn with no attachments (questions, descriptions, observations)."""

    SUBMITTED_DATA = "submitted_data"
    """Evidence derived from agent tool use (search_file, deep_analysis results)."""


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
    Raw file metadata for files uploaded to a case.

    Key Distinction:
    - UploadedFile: Raw file metadata, exists in ANY case state (INQUIRY, INVESTIGATING, etc.)
    - Evidence: Data classified by the LLM based on content. Created via evidence_to_add
      when the LLM evaluates the submission.

    Evidence classification is content-based, not stage-based (see Section 5.2 of
    evidence-driven-investigation-framework.md). The LLM evaluates the data and
    classifies it by what it contains:
    - Error logs → symptom_evidence (even during INQUIRY)
    - Normal configs → contextual_evidence
    - Post-fix metrics → solution_evidence

    UploadedFile records exist independently of Evidence. Not all uploaded files
    produce Evidence — the LLM decides what is relevant during its analysis.
    """

    file_id: str = Field(
        default_factory=lambda: f"file_{uuid4().hex[:12]}",
        description="Unique file identifier (same as data_id in data service)",
        pattern=r"^(file_|data_)[a-f0-9]{12,16}$",  # Accept both file_ and data_ prefixes
    )

    filename: str = Field(description="Original filename", min_length=1, max_length=255)

    size_bytes: int = Field(ge=0, description="File size in bytes")

    data_type: str = Field(
        description="Detected data type from preprocessing (log, metric, config, code, text, image, etc.)",
        max_length=50,
    )

    uploaded_at_turn: int = Field(
        ge=0, description="Turn number when file was uploaded"
    )

    uploaded_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="Upload timestamp"
    )

    source_type: str = Field(
        default="file_upload",
        description="file_upload | paste | screenshot | page_injection | agent_generated",
        max_length=50,
    )

    preprocessing_summary: Optional[str] = Field(
        default=None,
        description="Brief summary from preprocessing pipeline (<500 chars)",
        max_length=500,
    )

    content_ref: Optional[str] = Field(
        default=None,
        description="Reference to stored file content (S3 URI or data_id). May be None if processing pending.",
        max_length=5000,
    )


# =============================================================================
# Evidence (Investigation Data Linked to Hypotheses)
# =============================================================================


class Evidence(BaseModel):
    """
    Evidence collected during investigation.
    Categorized by purpose to drive milestone advancement.

    NOTE: Evidence.category is SYSTEM-INFERRED, not LLM-specified!
    System categorizes based on:
    - Which milestones are incomplete (if symptom not verified -> SYMPTOM_EVIDENCE)
    - Hypothesis evaluation results (if creates hypothesis_evidence links -> CAUSAL_EVIDENCE)
    - Solution state (if solution proposed -> RESOLUTION_EVIDENCE)

    LLM provides: summary, analysis
    LLM evaluates: stance per hypothesis (creates hypothesis_evidence links)
    System infers: category, advances_milestones
    """

    evidence_id: str = Field(
        default_factory=lambda: f"ev_{uuid4().hex[:12]}",
        description="Unique evidence identifier",
        pattern=r"^ev_[a-f0-9]{12}$",
    )

    # ============================================================
    # Purpose Classification (SYSTEM-INFERRED)
    # ============================================================
    category: EvidenceCategory = Field(
        description="System-inferred category: SYMPTOM_EVIDENCE | CAUSAL_EVIDENCE | RESOLUTION_EVIDENCE | OTHER"
    )

    primary_purpose: str = Field(
        description="What this evidence validates (milestone name or hypothesis ID)",
        max_length=100,
    )

    # ============================================================
    # Content (Three-Tier Storage)
    # ============================================================
    summary: str = Field(
        description="Brief summary of evidence content (<500 chars) for UI display and quick scanning",
        min_length=1,
        max_length=500,
    )

    preprocessed_content: str = Field(description="""
        Extracted relevant diagnostic information from preprocessing pipeline.

        This is what the agent uses for hypothesis evaluation and evidence analysis.
        Contains only the high-signal portions extracted from raw files.

        Examples:
        - Logs: Crime scene extraction (approx. 200 lines around errors)
        - Metrics: Anomaly detection results with statistical analysis
        - Config: Parsed configuration with secrets redacted
        - Code: AST-extracted functions and classes
        - Text: LLM-generated summary
        - Images: Vision model description

        Size: Typically 5 to 50 KB (compressed from larger raw files).
        Compression ratios: 200:1 for logs, 167:1 for metrics, 50:1 for code.

        This field is REQUIRED for all evidence. Raw files remain in S3 for audit/deep dive.
        """)

    content_ref: Optional[str] = Field(
        default=None,
        description="S3 URI to original raw file (1-10MB) for audit, compliance, and deep dive analysis. May be None for user-typed evidence.",
        max_length=5000,
    )

    content_size_bytes: int = Field(
        ge=0, description="Size of original raw file in bytes"
    )

    preprocessing_method: str = Field(description="""
        Preprocessing method used to extract preprocessed_content from raw file.
        Examples: crime_scene_extraction, anomaly_detection, parse_and_sanitize,
        ast_extraction, vision_analysis, single_shot_summary, map_reduce_summary
        """)

    compression_ratio: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Ratio of preprocessed to raw content size (e.g., 0.005 = 200:1 compression)",
    )

    analysis: Optional[str] = Field(
        default=None,
        description="Agent analysis of this evidence and its significance to the investigation",
        max_length=2000,
    )

    # ============================================================
    # Data Type & Deduplication (from Preprocessing Pipeline)
    # ============================================================
    data_type: Optional[str] = Field(
        default=None,
        description=(
            "Unified data type from preprocessing (logs, metrics, configuration, "
            "code, text, image). None for legacy evidence without preprocessing."
        ),
        max_length=50,
    )

    content_hash: Optional[str] = Field(
        default=None,
        description=(
            "SHA-256 hash of raw file content for deduplication. "
            "Computed from raw bytes before any extraction. "
            "UNIQUE per (case_id, content_hash) — prevents duplicate uploads."
        ),
        max_length=64,
    )

    extraction_method: Optional[str] = Field(
        default=None,
        description=(
            "Extraction method used: structural_index, statistical_profile, "
            "parse_and_sanitize, ast_extraction, structure_extraction, metadata_extraction"
        ),
        max_length=100,
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

    form: EvidenceForm = Field(
        description="How evidence was provided: DOCUMENT, USER_TEXT, or SUBMITTED_DATA"
    )

    source_file_id: Optional[str] = Field(
        default=None,
        description="ID of the UploadedFile this evidence was derived from (None if from user input)",
    )

    original_filename: Optional[str] = Field(
        default=None,
        description="Original filename when uploaded (e.g., 'OpenSSH_2k.log'). Used by search_file tool for display.",
        max_length=512,
    )

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
            "Optional for backward compatibility with evidence rows that predate "
            "the Phase 1 classifier-confidence work."
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


class HypothesisStatus(str, Enum):
    """Hypothesis lifecycle status"""

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

    category: HypothesisCategory = Field(
        description="Hypothesis category (for anchoring detection)"
    )

    status: HypothesisStatus = Field(
        default=HypothesisStatus.CAPTURED, description="Current hypothesis status"
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
    # Evidence Relationships (Many-to-Many)
    # ============================================================
    evidence_links: Dict[str, HypothesisEvidenceLink] = Field(
        default_factory=dict,
        description="""
        Maps evidence_id to relationship details.

        ONE evidence can:
        - STRONGLY_SUPPORTS hypothesis A
        - REFUTES hypothesis B
        - Be IRRELEVANT to hypothesis C

        Backed by hypothesis_evidence junction table in database.
        LLM evaluates each evidence against ALL active hypotheses after submission.
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
            "REQUIRED when status=REFUTED (enforced via model validator). "
            "Not used for other statuses. status=REFUTED and refutation_reason "
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
            evidence_id
            for evidence_id, link in self.evidence_links.items()
            if link.stance == EvidenceStance.SUPPORTS
        ]

    @property
    def refuting_evidence(self) -> List[str]:
        """Get evidence IDs that refute this hypothesis"""

        return [
            evidence_id
            for evidence_id, link in self.evidence_links.items()
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
    def _validate_refutation_reason_pairs_with_status(self) -> "Hypothesis":
        """Pair integrity: status=REFUTED requires refutation_reason.

        The two fields travel together — a Hypothesis with status=REFUTED
        cannot exist in memory without a refutation_reason, and vice versa.
        RETIRED has its own ``retirement_reason`` field and is a distinct
        path (abandonment without disproof, no reason required).
        """
        if self.status == HypothesisStatus.REFUTED and not self.refutation_reason:
            raise ValueError(
                "refutation_reason is required when status=REFUTED. If there "
                "is no disproof evidence, use status=RETIRED instead."
            )
        if self.refutation_reason and self.status != HypothesisStatus.REFUTED:
            raise ValueError(
                "refutation_reason is only valid when status=REFUTED. "
                f"Current status is {self.status.value}."
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
    # Solution Details
    # ============================================================
    title: str = Field(description="Short solution title", min_length=1, max_length=200)

    immediate_action: Optional[str] = Field(
        default=None, description="Quick fix or mitigation (temporary)", max_length=2000
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

    status: str = Field(
        default="pending",
        description="pending | accepted | rejected | superseded",
    )

    @field_validator("status")
    @classmethod
    def valid_action_status(cls, v):
        allowed = ["pending", "accepted", "rejected", "superseded"]
        if v not in allowed:
            raise ValueError(f"status must be one of: {allowed}")
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
    """

    MILESTONE_COMPLETED = "milestone_completed"
    """
    One or more milestones completed.
    Investigation advanced.
    """

    DATA_PROVIDED = "data_provided"
    """
    User provided data/evidence this turn.
    """

    DATA_REQUESTED = "data_requested"
    """
    Agent requested data from user.
    Awaiting user response.
    """

    DATA_NOT_PROVIDED = "data_not_provided"
    """
    Agent requested data, user did not provide.
    LLM uses this when user did not address request.
    System tracks pattern - if 3+ consecutive, triggers degraded mode.
    """

    HYPOTHESIS_TESTED = "hypothesis_tested"
    """
    Hypothesis was tested (validated/refuted).
    """

    CASE_RESOLVED = "case_resolved"
    """
    Solution verified.
    Case can transition to RESOLVED status (terminal).
    """

    CONVERSATION = "conversation"
    """
    Normal Q&A, no data requests or milestones.
    """

    OTHER = "other"
    """
    Does not fit standard outcomes.
    """


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

    # ============================================================
    # Configuration
    # ============================================================
    class Config:
        frozen = True  # Immutable once created


# ============================================================
# Path Selection Models (Section 9)
# ============================================================


def determine_investigation_path(
    temporal_state: "TemporalState", urgency_level: "UrgencyLevel"
) -> "InvestigationPath":
    """
    Determine investigation path from temporal state and urgency.

    Path Selection Matrix:
    +--------------+-----------------------+------------------------+
    | Temporal     | Urgency               | Path                   |
    +--------------+-----------------------+------------------------+
    | ONGOING      | CRITICAL/HIGH         | MITIGATION_FIRST       |
    | ONGOING      | MEDIUM                | USER_CHOICE            |
    | ONGOING      | LOW                   | ROOT_CAUSE             |
    | HISTORICAL   | any                   | ROOT_CAUSE             |
    +--------------+-----------------------+------------------------+

    Logic:
    - ONGOING + HIGH/CRITICAL urgency -> MITIGATION_FIRST
      * Problem is happening NOW, users affected
      * Need quick mitigation, then return for RCA

    - HISTORICAL + any urgency -> ROOT_CAUSE
      * Problem happened before (not ongoing)
      * No immediate need for temporary fix, do thorough RCA

    - Ambiguous cases -> USER_CHOICE
      * ONGOING + MEDIUM: Could go either way

    Args:
        temporal_state: Whether problem is ONGOING or HISTORICAL
        urgency_level: Urgency classification (CRITICAL/HIGH/MEDIUM/LOW)

    Returns:
        Investigation path (MITIGATION_FIRST, ROOT_CAUSE, or USER_CHOICE)
    """
    # ONGOING problem
    if temporal_state == TemporalState.ONGOING:
        if urgency_level in [UrgencyLevel.CRITICAL, UrgencyLevel.HIGH]:
            # Happening now + urgent -> Quick mitigation first
            return InvestigationPath.MITIGATION_FIRST

        if urgency_level == UrgencyLevel.MEDIUM:
            # Happening now but medium urgency -> Let user decide
            return InvestigationPath.USER_CHOICE

        else:  # LOW or UNKNOWN
            # Happening now but low urgency -> Can do thorough RCA
            return InvestigationPath.ROOT_CAUSE

    # HISTORICAL problem — always ROOT_CAUSE regardless of urgency
    else:  # TemporalState.HISTORICAL
        return InvestigationPath.ROOT_CAUSE


class InvestigationPath(str, Enum):
    """
    Investigation routing strategy (2-stage model with mitigation detour).

    IMPORTANT: Path is SYSTEM-DETERMINED from matrix (temporal_state x urgency_level).
    LLM provides inputs (temporal_state, urgency_level) during DIAGNOSIS.
    System calls determine_investigation_path() to select path deterministically.

    Two paths through the 2-stage model:
    - MITIGATION_FIRST: DIAGNOSIS → MITIGATION (detour) → DIAGNOSIS → TREATMENT
    - ROOT_CAUSE: DIAGNOSIS → TREATMENT
    """

    MITIGATION_FIRST = "mitigation_first"
    """
    Mitigation-first path: stop the bleeding, then find root cause.

    Stage Flow: DIAGNOSIS → MITIGATION → DIAGNOSIS → TREATMENT
    - DIAGNOSIS: Verify symptoms, assess urgency, propose temp fix
    - MITIGATION: Apply and verify temporary fix
    - DIAGNOSIS: Return for root cause analysis, propose permanent fix
    - TREATMENT: Apply and verify permanent fix

    Use When: ONGOING + HIGH/CRITICAL urgency
    - Problem is happening NOW
    - User needs immediate restoration
    - But also wants to prevent recurrence
    """

    ROOT_CAUSE = "root_cause"
    """
    Direct root cause analysis path.

    Stage Flow: DIAGNOSIS → TREATMENT
    - DIAGNOSIS: Verify symptoms, diagnose root cause, propose fix
    - TREATMENT: Apply and verify permanent fix

    Use When: HISTORICAL + any urgency, or ONGOING + LOW/MEDIUM urgency
    - Problem happened before, or urgency allows thorough analysis
    - No immediate need for a temporary fix
    """

    USER_CHOICE = "user_choice"
    """
    Ambiguous case — let user decide.

    Use When: Ambiguous temporal_state x urgency combinations
    - ONGOING + MEDIUM urgency (might want quick fix or proper fix)
    """


class PathSelection(BaseModel):
    """
    Path selection details.
    Records how investigation path was chosen.

    IMPORTANT: Path is SYSTEM-DETERMINED from matrix (temporal_state x urgency_level).
    LLM provides inputs (temporal_state, urgency_level) during verification.
    System calls determine_investigation_path() to select path deterministically.
    LLM does NOT choose the path directly!
    """

    path: InvestigationPath = Field(
        description="Selected investigation path (system-determined from matrix)"
    )

    auto_selected: bool = Field(
        description="True if system auto-selected, False if user chose"
    )

    rationale: str = Field(description="Why this path was selected", max_length=500)

    alternate_path: Optional[InvestigationPath] = Field(
        default=None,
        description="Alternative path user could have chosen (if auto-selected)",
    )

    selected_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When path was selected",
    )

    selected_by: str = Field(
        default="system",
        description="Who selected: 'system' for auto, or user_id for manual",
    )

    # ============================================================
    # Decision Inputs
    # ============================================================
    temporal_state: Optional[TemporalState] = Field(
        default=None, description="Temporal state used in decision"
    )

    urgency_level: Optional[UrgencyLevel] = Field(
        default=None, description="Urgency level used in decision"
    )

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

    user_id: str = Field(
        description="User who created the case", min_length=1, max_length=255
    )

    organization_id: str = Field(
        description="Organization this case belongs to", min_length=1, max_length=255
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
    status: CaseStatus = Field(
        default=CaseStatus.INQUIRY,
        description="Current lifecycle status (phase or disposition)",
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

    pending_transition: Optional[Dict[str, Any]] = Field(
        default=None,
        description="""
        Pending status transition awaiting user confirmation (User-Agent Handshake pattern).

        Used for terminal transitions that require explicit user confirmation:
        - to_status: Target status (str)
        - reason: Why transition is being proposed (str)
        - summary: Agent's explanation to user (str)
        - evidence_ids: Supporting evidence (List[str])
        - proposed_at: When transition was proposed (str ISO datetime)
        - proposed_by: Who proposed it ("agent" | "user" | user_id)

        Cleared after transition executes or is cancelled.
        """,
    )

    last_suggestions: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "COOPERATIVE suggestions with intent metadata from the last agent turn. "
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
    # Investigation Path & Strategy
    # ============================================================
    path_selection: Optional[PathSelection] = Field(
        default=None,
        description="Selected investigation path (MITIGATION vs ROOT_CAUSE)",
    )

    investigation_strategy: InvestigationStrategy = Field(
        default=InvestigationStrategy.POST_MORTEM,
        description="Investigation approach: ACTIVE_INCIDENT (speed) vs POST_MORTEM (thoroughness)",
    )

    # ============================================================
    # Problem Context
    # ============================================================
    inquiry: InquiryData = Field(
        default_factory=InquiryData,
        description="Pre-investigation INQUIRY status data",
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

    hypotheses: Dict[str, Hypothesis] = Field(
        default_factory=dict, description="Generated hypotheses (key = hypothesis_id)"
    )

    solutions: List[Solution] = Field(
        default_factory=list, description="Proposed and applied solutions"
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
        default=None, description="When case reached RESOLVED status"
    )

    closed_at: Optional[datetime] = Field(
        default=None,
        description="When case reached terminal state (RESOLVED or CLOSED)",
    )

    # ============================================================
    # Archival
    # ============================================================
    is_archived: bool = Field(
        default=False,
        description="Whether the case has been archived by the user. "
        "Archived cases are hidden from the default list view but remain "
        "fully accessible. Independent of case status.",
    )

    archived_at: Optional[datetime] = Field(
        default=None,
        description="When the case was archived",
    )

    # ============================================================
    # Computed Properties
    # ============================================================
    @property
    def current_stage(self) -> Optional[InvestigationStage]:
        """
        Computed investigation stage (only when INVESTIGATING).
        Returns: UNDERSTANDING | DIAGNOSING | RESOLVING | None
        """
        if self.status != CaseStatus.INVESTIGATING:
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
        return self.status in [CaseStatus.RESOLVED, CaseStatus.CLOSED]

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
    def valid_evidence(self) -> List[Evidence]:
        """
        Evidence with actionable categories (excludes REJECTED).

        Returns the 4 valid categories: SYMPTOM_EVIDENCE, CAUSAL_EVIDENCE,
        RESOLUTION_EVIDENCE, CONTEXTUAL_EVIDENCE.

        Design Reference:
            evidence-classification-design.md Section 3.3
        """
        return [ev for ev in self.evidence if ev.category != EvidenceCategory.REJECTED]

    @property
    def rejected_submissions(self) -> List[Evidence]:
        """
        Submissions analyzed but rejected as not useful.

        These exist for deduplication, audit trail, and cost avoidance.
        They are NOT evidence in the investigative sense.

        Design Reference:
            evidence-classification-design.md Section 3.3
        """
        return [ev for ev in self.evidence if ev.category == EvidenceCategory.REJECTED]

    @property
    def acceptance_rate(self) -> float:
        """
        Percentage of submissions that became valid evidence.

        Returns 1.0 if no evidence exists (avoid division by zero).

        Design Reference:
            evidence-classification-design.md Section 3.3
        """
        total = len(self.evidence)
        if total == 0:
            return 1.0
        return len(self.valid_evidence) / total

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
            h for h in self.hypotheses.values() if h.status == HypothesisStatus.ACTIVE
        ]

    @property
    def validated_hypotheses(self) -> List[Hypothesis]:
        """Get validated hypotheses (found root cause)"""
        return [
            h
            for h in self.hypotheses.values()
            if h.status == HypothesisStatus.VALIDATED
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
        status = self.status
        description = self.description.strip()

        # INVESTIGATING requires confirmed problem description
        if status == CaseStatus.INVESTIGATING and not description:
            raise ValueError(
                "description must be set (from confirmed proposed_problem_statement) "
                "before transitioning to INVESTIGATING status"
            )

        return self

    @field_validator("closure_reason")
    @classmethod
    def valid_closure_reason(cls, v):
        """Validate closure reason is from allowed set"""
        if v is not None:
            allowed = [
                "resolved",
                "abandoned",
                "escalated",
                "mitigation_sufficient",
                "inquiry_only",
                "duplicate",
                "other",
            ]
            if v not in allowed:
                raise ValueError(f"closure_reason must be one of: {allowed}")
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
        """Ensure turn numbers are sequential"""
        if len(v) > 1:
            for i in range(len(v) - 1):
                if v[i].turn_number + 1 != v[i + 1].turn_number:
                    raise ValueError("Turn numbers must be sequential")
        return v

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
    def validate_status_timestamp_consistency(self) -> "Case":
        """
        Enforce status-timestamp consistency per DB spec.

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
        if self.status == CaseStatus.RESOLVED:
            if not self.resolved_at:
                raise ValueError("RESOLVED status requires resolved_at timestamp")
            if not self.closed_at:
                raise ValueError("RESOLVED status requires closed_at timestamp")

        # Non-RESOLVED must not have resolved_at
        if self.status != CaseStatus.RESOLVED and self.resolved_at:
            raise ValueError(
                f"resolved_at can only be set when status is RESOLVED (current: {self.status})"
            )

        # RESOLVED or CLOSED requires closed_at
        if (
            self.status in [CaseStatus.RESOLVED, CaseStatus.CLOSED]
            and not self.closed_at
        ):
            raise ValueError(
                f"Terminal status {self.status} requires closed_at timestamp"
            )

        # Non-terminal must not have closed_at
        if (
            self.status not in [CaseStatus.RESOLVED, CaseStatus.CLOSED]
            and self.closed_at
        ):
            raise ValueError(
                f"closed_at can only be set when status is RESOLVED or CLOSED (current: {self.status})"
            )

        # Terminal states require closure_reason
        if (
            self.status in [CaseStatus.RESOLVED, CaseStatus.CLOSED]
            and not self.closure_reason
        ):
            raise ValueError(f"Terminal status {self.status} requires closure_reason")

        # Non-terminal must not have closure_reason
        if (
            self.status not in [CaseStatus.RESOLVED, CaseStatus.CLOSED]
            and self.closure_reason
        ):
            raise ValueError(
                f"closure_reason can only be set when status is RESOLVED or CLOSED (current: {self.status})"
            )

        return self

    def atomic_transition(self):
        """
        Context manager to allow atomic updates of interdependent fields.
        Useful for transitioning to terminal states where status/timestamps depend on each other.
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
                    self.case.validate_status_timestamp_consistency()

        return AtomicContext(self)

    @model_validator(mode="after")
    def validate_investigating_requirements(self) -> "Case":
        """
        Enforce INVESTIGATING status requirements per DB spec.

        Spec Reference: DB Design Specification lines 175-179
        """
        if self.status == CaseStatus.INVESTIGATING:
            if not self.description or self.description == "":
                raise ValueError("INVESTIGATING status requires non-empty description")

            # Must have confirmed problem statement and decision to investigate
            if not self.inquiry.problem_statement_confirmed:
                raise ValueError(
                    "INVESTIGATING status requires confirmed problem statement"
                )

            if not self.inquiry.decided_to_investigate:
                raise ValueError(
                    "INVESTIGATING status requires investigation commitment"
                )

        return self

    # ============================================================
    # Atomic State Update Helper
    # ============================================================
    def atomic_update(self, **updates: Any) -> None:
        """
        Perform atomic updates to multiple fields, bypassing incremental validation.

        This method is necessary for state transitions that require multiple fields
        to be updated simultaneously (e.g., setting status=RESOLVED requires
        resolved_at to be set, but resolved_at can only be set when status=RESOLVED).

        The validation Catch-22:
        - Cannot set status=RESOLVED if resolved_at is None (validator line 3361)
        - Cannot set resolved_at if status != RESOLVED (validator line 3367)

        Usage:
            case.atomic_update(
                status=CaseStatus.RESOLVED,
                resolved_at=datetime.now(UTC),
                closed_at=datetime.now(UTC),
                closure_reason="resolved"
            )

        Args:
            **updates: Field names and values to update atomically

        Note:
            Sets _in_atomic_update flag to signal validators to skip checks.
            Uses object.__setattr__() to bypass Pydantic's validate_assignment.
        """
        # Set flag to signal validators to skip consistency checks
        object.__setattr__(self, "_in_atomic_update", True)
        try:
            for field_name, value in updates.items():
                object.__setattr__(self, field_name, value)
        finally:
            # Clear flag after all updates complete
            object.__setattr__(self, "_in_atomic_update", False)

    # ============================================================
    # Configuration
    # ============================================================
    class Config:
        validate_assignment = True  # Validate on field assignment
        use_enum_values = False  # Keep enum instances
        json_encoders = {
            datetime: lambda v: v.isoformat()
            + ("Z" if v.tzinfo in (None, timezone.utc) else ""),
            timedelta: lambda v: v.total_seconds(),
        }
