"""Case data models - Milestone-based investigation system.

This module defines the complete data structure for FaultMaven's investigation system
based on the Investigation Architecture Specification v2.0.

Key Models:
- Case: Root case entity with milestone-based progress tracking
- CaseStatus: Lifecycle status (INQUIRY -> INVESTIGATING -> RESOLVED/CLOSED)
- InvestigationProgress: 8 milestones tracking verification, diagnosis, and resolution
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
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

# ============================================================
# Status & Lifecycle Models (Section 2)
# ============================================================


class CaseStatus(str, Enum):
    """
    Case lifecycle status.

    Lifecycle Flow:
      INQUIRY -> INVESTIGATING -> RESOLVED (terminal)
                                 -> CLOSED (terminal)
               -> CLOSED (terminal)

    Terminal States: RESOLVED, CLOSED (no further transitions)
    """

    INQUIRY = "inquiry"
    """
    Pre-investigation exploration.

    Characteristics:
    - User asking questions
    - Agent providing quick guidance
    - No formal investigation commitment
    - May transition to INVESTIGATING or CLOSED

    Typical Duration: Minutes to hours
    """

    INVESTIGATING = "investigating"
    """
    Active formal investigation.

    Characteristics:
    - Working through milestones
    - Gathering evidence
    - Testing hypotheses
    - Applying solutions
    - May transition to RESOLVED or CLOSED

    Typical Duration: Hours to days
    """

    RESOLVED = "resolved"
    """
    TERMINAL STATE: Case closed WITH solution.

    Characteristics:
    - Problem was fixed
    - Solution verified
    - closure_reason = "resolved"
    - No further transitions allowed

    State: Terminal (permanent)
    """

    CLOSED = "closed"
    """
    TERMINAL STATE: Case closed WITHOUT solution.

    Characteristics:
    - Investigation abandoned/escalated
    - OR inquiry-only (no investigation)
    - closure_reason = "abandoned" | "escalated" | "inquiry_only" | "duplicate" | "other"
    - No further transitions allowed

    State: Terminal (permanent)
    """

    @property
    def is_terminal(self) -> bool:
        """Check if this status is terminal"""
        return self in [CaseStatus.RESOLVED, CaseStatus.CLOSED]

    @property
    def is_active(self) -> bool:
        """Check if case is active (not terminal)"""
        return self in [CaseStatus.INQUIRY, CaseStatus.INVESTIGATING]


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


class CaseStatusTransition(BaseModel):
    """
    Record of one status change.
    Provides audit trail for case lifecycle.
    """

    from_status: CaseStatus = Field(description="Status before transition")

    to_status: CaseStatus = Field(description="Status after transition")

    triggered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When transition occurred",
    )

    triggered_by: str = Field(
        description="Who triggered: user_id or 'system' for automatic transitions"
    )

    reason: str = Field(
        description="Human-readable reason for transition", max_length=500
    )

    @model_validator(mode="after")
    def validate_transition(self):
        """Ensure transition is valid"""
        if not is_valid_transition(self.from_status, self.to_status):
            raise ValueError(
                f"Invalid transition: {self.from_status} -> {self.to_status}"
            )
        return self

    class Config:
        frozen = True  # Immutable once created


def is_valid_transition(from_status: CaseStatus, to_status: CaseStatus) -> bool:
    """
    Validate status transition.

    Valid Transitions:
    - INQUIRY -> INVESTIGATING (standard investigation start)
    - INQUIRY -> RESOLVED (Fast-Track: KB match resolves issue)
    - INQUIRY -> CLOSED (inquiry-only, no investigation)
    - INVESTIGATING -> RESOLVED (solution verified)
    - INVESTIGATING -> CLOSED (abandoned/escalated)

    Invalid:
    - RESOLVED -> * (terminal)
    - CLOSED -> * (terminal)
    - INVESTIGATING -> INQUIRY (no backward)
    """
    valid_transitions = {
        CaseStatus.INQUIRY: [
            CaseStatus.INVESTIGATING,
            CaseStatus.RESOLVED,
            CaseStatus.CLOSED,
        ],
        CaseStatus.INVESTIGATING: [CaseStatus.RESOLVED, CaseStatus.CLOSED],
        CaseStatus.RESOLVED: [],  # Terminal
        CaseStatus.CLOSED: [],  # Terminal
    }

    return to_status in valid_transitions.get(from_status, [])


class MessageType(str, Enum):
    """Types of messages in a case conversation (restored from old implementation)"""

    USER_QUERY = "user_query"
    AGENT_RESPONSE = "agent_response"
    SYSTEM_EVENT = "system_event"
    DATA_UPLOAD = "data_upload"
    CASE_NOTE = "case_note"
    STATUS_CHANGE = "status_change"


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
    Milestone-based progress tracking.

    Philosophy: Track what's completed, not what phase we're in.
    Agent completes milestones opportunistically based on data availability.
    """

    # ============================================================
    # Verification Milestones
    # ============================================================
    symptom_verified: bool = Field(
        default=False,
        description="Symptom confirmed with concrete evidence (logs, metrics, user reports)",
    )

    scope_assessed: bool = Field(
        default=False,
        description="Scope determined: affected users/services/regions, blast radius",
    )

    timeline_established: bool = Field(
        default=False,
        description="Timeline determined: when problem started, when noticed, duration",
    )

    changes_identified: bool = Field(
        default=False,
        description="Recent changes identified: deployments, configs, scaling events",
    )

    # ============================================================
    # Investigation Milestones
    # ============================================================
    root_cause_identified: bool = Field(
        default=False,
        description="Root cause determined (directly or via hypothesis validation)",
    )

    root_cause_likelihood: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Likelihood in root cause identification (0.0 = unknown, 1.0 = certain)",
    )

    root_cause_method: Optional[str] = Field(
        default=None,
        description="How root cause was identified: direct_analysis | hypothesis_validation | correlation | other",
    )

    # ============================================================
    # Resolution Milestones
    # ============================================================
    solution_proposed: bool = Field(
        default=False, description="Solution or mitigation has been proposed"
    )

    solution_applied: bool = Field(
        default=False, description="Solution has been applied by user"
    )

    solution_verified: bool = Field(
        default=False,
        description="Solution effectiveness verified (error rate decreased, metrics improved)",
    )

    # ============================================================
    # Path-Specific Tracking
    # ============================================================
    mitigation_applied: bool = Field(
        default=False,
        description="""
        MITIGATION_FIRST path: Quick mitigation applied (stage 1 -> 4 complete).

        Used to track progress in MITIGATION_FIRST path (1-4-2-3-4):
        - Stage 1: Symptom verified
        - Stage 4: Quick mitigation applied (mitigation_applied = True)
        - Stage 2: Return to hypothesis formulation for RCA
        - Stage 3: Hypothesis validation
        - Stage 4: Permanent solution applied (solution_applied = True)

        When True: Agent should return to stage 2 (hypothesis formulation) for full RCA
        When False: Either ROOT_CAUSE path, or MITIGATION_FIRST has not applied mitigation yet

        Note: Different from solution_applied - mitigation is quick correlation-based fix,
        solution is comprehensive permanent fix after RCA.
        """,
    )

    mitigation_verified: bool = Field(
        default=False,
        description="Mitigation effectiveness confirmed (problem stopped)",
    )

    mitigation_effectiveness: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="How well mitigation worked: 1.0 = fully resolved, 0.5 = partially, 0.0 = ineffective",
    )

    mitigation_solution_id: Optional[str] = Field(
        default=None,
        description="Solution ID of applied mitigation (links to case.solutions)",
    )

    # ============================================================
    # Milestone Completion Timestamps
    # ============================================================
    verification_completed_at: Optional[datetime] = Field(
        default=None,
        description="When all verification milestones (symptom, scope, timeline, changes) were completed",
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
        Compute investigation stage from completed milestones.
        For UI display, NOT workflow control.

        Maps milestones to stages:
        - SYMPTOM_VERIFICATION (stage 1): Verification in progress
        - HYPOTHESIS_FORMULATION (stage 2): Hypotheses being generated
        - HYPOTHESIS_VALIDATION (stage 3): Testing hypotheses for root cause
        - SOLUTION (stage 4): Solution work (proposal, application, verification)

        MITIGATION_FIRST path handling:
        When mitigation_applied=True but root_cause_identified=False, the case has
        completed quick mitigation (stage 4) and needs to return to RCA (stage 2).
        In this situation, current_stage returns HYPOTHESIS_FORMULATION so the LLM
        receives the correct schema (with hypothesis fields) rather than being
        locked into the SOLUTION schema.
        """

        # MITIGATION_FIRST path: mitigation applied, now doing post-mitigation RCA
        # mitigation_applied is only True for MITIGATION_FIRST cases.
        # If mitigation is done but root cause is NOT identified, we need RCA.
        if self.mitigation_applied and not self.root_cause_identified:
            # If a full solution has also been proposed/applied (beyond just mitigation),
            # stay in SOLUTION stage
            if self.solution_proposed or self.solution_applied or self.solution_verified:
                return InvestigationStage.SOLUTION
            return InvestigationStage.HYPOTHESIS_FORMULATION

        # SOLUTION (Stage 4): Any solution work
        if self.solution_proposed or self.solution_applied or self.solution_verified:
            return InvestigationStage.SOLUTION

        # HYPOTHESIS_VALIDATION (Stage 3): Root cause identified or being validated
        if self.root_cause_identified:
            return InvestigationStage.HYPOTHESIS_VALIDATION

        # HYPOTHESIS_FORMULATION (Stage 2): Symptom verified, working on "why"
        if self.symptom_verified:
            return InvestigationStage.HYPOTHESIS_FORMULATION

        # SYMPTOM_VERIFICATION (Stage 1): Initial verification
        return InvestigationStage.SYMPTOM_VERIFICATION

    @property
    def stage_display_name(self) -> str:
        """
        User-facing stage name for UI display.

        Maps internal 4-stage system to 3 user-friendly names:
        - SYMPTOM_VERIFICATION → "Understanding"
        - HYPOTHESIS_FORMULATION, HYPOTHESIS_VALIDATION → "Diagnosing"
        - SOLUTION → "Resolving"
        """
        stage = self.current_stage
        if stage == InvestigationStage.SYMPTOM_VERIFICATION:
            return "Understanding"
        elif stage in (
            InvestigationStage.HYPOTHESIS_FORMULATION,
            InvestigationStage.HYPOTHESIS_VALIDATION,
        ):
            return "Diagnosing"
        else:  # SOLUTION
            return "Resolving"

    @property
    def verification_complete(self) -> bool:
        """Check if all verification milestones completed"""
        return (
            self.symptom_verified
            and self.scope_assessed
            and self.timeline_established
            and self.changes_identified
        )

    @property
    def investigation_complete(self) -> bool:
        """Check if investigation milestones completed"""
        return self.root_cause_identified

    @property
    def resolution_complete(self) -> bool:
        """Check if resolution milestones completed"""
        return (
            self.solution_proposed and self.solution_applied and self.solution_verified
        )

    # completion_percentage property removed — inaccurate and non-essential.
    # Milestone completion is tracked via completed_milestones/pending_milestones lists.

    @property
    def completed_milestones(self) -> List[str]:
        """Get list of completed milestone names"""
        milestone_map = {
            "symptom_verified": self.symptom_verified,
            "scope_assessed": self.scope_assessed,
            "timeline_established": self.timeline_established,
            "changes_identified": self.changes_identified,
            "root_cause_identified": self.root_cause_identified,
            "solution_proposed": self.solution_proposed,
            "solution_applied": self.solution_applied,
            "solution_verified": self.solution_verified,
            "mitigation_applied": self.mitigation_applied,
        }
        return [name for name, completed in milestone_map.items() if completed]

    @property
    def pending_milestones(self) -> List[str]:
        """Get list of pending milestone names"""
        milestone_map = {
            "symptom_verified": self.symptom_verified,
            "scope_assessed": self.scope_assessed,
            "timeline_established": self.timeline_established,
            "changes_identified": self.changes_identified,
            "root_cause_identified": self.root_cause_identified,
            "solution_proposed": self.solution_proposed,
            "solution_applied": self.solution_applied,
            "solution_verified": self.solution_verified,
        }

        return [name for name, completed in milestone_map.items() if not completed]

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
                "correlation",
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
        """Ensure solutions are applied in order"""
        proposed = self.solution_proposed
        applied = self.solution_applied
        verified = self.solution_verified

        if applied and not proposed:
            raise ValueError("Cannot apply solution without proposing first")

        if verified and not applied:
            raise ValueError("Cannot verify solution without applying first")

        return self


class InvestigationStage(str, Enum):
    """
    Investigation stage within INVESTIGATING phase (4 stages).

    Purpose: User-facing progress label computed from completed milestones.
    NOT used for workflow control - milestones drive advancement opportunistically.
    Only relevant when case status = INVESTIGATING.

    Stage Progression (Path-Dependent):
    - MITIGATION_FIRST: 1 -> 4 -> 2 -> 3 -> 4 (quick mitigation, then return for RCA)
    - ROOT_CAUSE: 1 -> 2 -> 3 -> 4 (traditional RCA)

    Stage determines the investigation focus based on what has been completed:
    - Stage 1: Where and when (symptom verification)
    - Stage 2: Why (hypothesis formulation)
    - Stage 3: Why really (hypothesis validation)
    - Stage 4: How (solution application)
    """

    SYMPTOM_VERIFICATION = "symptom_verification"
    """
    Stage 1: Symptom verification (where and when).

    Focus: Understanding what is happening and when it started
    Milestones: symptom_verified, scope_assessed, timeline_established, changes_identified

    Agent Actions:
    - Confirming symptom with evidence (logs, metrics, user reports)
    - Assessing scope and impact (affected users/services/regions)
    - Establishing timeline (when started, when noticed, duration)
    - Identifying recent changes (deployments, configs, scaling events)
    - Determining temporal state (ONGOING vs HISTORICAL)
    - Assessing urgency level (CRITICAL/HIGH/MEDIUM/LOW)

    Path Selection: Urgency + temporal state determines MITIGATION_FIRST vs ROOT_CAUSE path
    """

    HYPOTHESIS_FORMULATION = "hypothesis_formulation"
    """
    Stage 2: Hypotheses formulation (why).

    Focus: Generating theories about what caused the problem
    Prerequisites: Symptom verified (stage 1 complete)

    Agent Actions:
    - Analyzing evidence patterns and correlations
    - Generating hypotheses (opportunistic from strong clues, or systematic when unclear)
    - Categorizing hypotheses (CODE/CONFIG/ENVIRONMENT/NETWORK/DATA/etc)
    - Prioritizing hypotheses by likelihood

    Hypotheses are Optional: Agent may identify root cause directly from evidence without hypotheses.
    When root cause is unclear, hypotheses enable systematic exploration.

    Note: In MITIGATION_FIRST path, this stage occurs AFTER initial mitigation (stage 4)
    """

    HYPOTHESIS_VALIDATION = "hypothesis_validation"
    """
    Stage 3: Hypothesis validation (why really).

    Focus: Testing theories to identify root cause with confidence
    Prerequisites: Hypotheses generated (stage 2 complete)
    Milestone: root_cause_identified

    Agent Actions:
    - Requesting diagnostic data to test specific hypotheses
    - Analyzing evidence against ALL active hypotheses
    - Evaluating evidence stance (STRONGLY_SUPPORTS/SUPPORTS/CONTRADICTS/STRONGLY_CONTRADICTS)
    - Validating or refuting hypotheses based on evidence
    - Increasing/decreasing hypothesis likelihood based on evidence

    Outcome: Root cause identified with confidence level (VERIFIED/CONFIDENT/PROBABLE/SPECULATION)

    Note: In MITIGATION_FIRST path, this provides comprehensive RCA after initial mitigation
    """

    SOLUTION = "solution"
    """
    Stage 4: Solution (how).

    Focus: Applying fix to resolve the problem
    Prerequisites (Path-Dependent):
    - MITIGATION_FIRST path: Symptom verified (stage 1) - correlation-based quick fix
    - ROOT_CAUSE path: Root cause identified (stage 3) - evidence-based permanent fix
    Milestones: solution_proposed, solution_applied, solution_verified

    Agent Actions:
    - Proposing solutions (quick mitigation or permanent fix based on path)
    - Providing implementation steps and commands
    - Guiding user through application
    - Verifying effectiveness with before/after evidence

    Path-Specific Behavior:
    - MITIGATION_FIRST: After applying quick mitigation, returns to stage 2 for full RCA
    - ROOT_CAUSE: After permanent solution verified, case transitions to RESOLVED

    Solution Types: ROLLBACK, CONFIG_CHANGE, RESTART, SCALING, CODE_FIX, WORKAROUND, etc.
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
    quick_suggestions: List[str] = Field(
        default_factory=list,
        description="Quick fixes or guidance provided during inquiry",
    )

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

    Advances Milestones: symptom_verified, scope_assessed, timeline_established
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

    RESOLUTION_EVIDENCE = "resolution_evidence"
    """
    Validates fix effectiveness.

    Purpose: Prove that solution resolved the problem.

    Examples:
    - Error rate after rollback (before/after comparison)
    - Latency metrics after optimization
    - Resource usage after scaling
    - Success rate after config change

    Advances Milestones: solution_verified
    """

    CONTEXTUAL_EVIDENCE = "contextual_evidence"  # Was: OTHER
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

    Post-redesign (2026-02-11): Simplified from 12 types to 5 clear categories.

    Migration mapping:
    - log_file, command_output, trace_data, api_response, other → LOGS
    - metrics_data, monitoring_alert → METRICS
    - config_file, code_review, database_query → CONFIGURATION
    - screenshot → VISUAL
    - user_report → USER_DESCRIPTION
    """

    LOGS = "logs"
    """
    Any textual diagnostic output.

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
    System/application configuration.

    Includes:
    - Config files (YAML, JSON, TOML, env vars)
    - Code snippets
    - Database schema
    - Infrastructure definitions (Kubernetes manifests, Terraform)
    - Dependency lists

    Characteristics: Defines how system should behave
    """

    VISUAL = "visual"
    """
    Visual representations.

    Includes:
    - Screenshots (errors, dashboards, terminals)
    - Architecture diagrams
    - Graphs and charts
    - Images

    Characteristics: Requires visual interpretation
    """

    USER_DESCRIPTION = "user_description"
    """
    User's typed narrative.

    Includes:
    - Problem descriptions
    - Observations
    - Impact reports
    - Steps to reproduce
    - Context explanations

    Characteristics: Human-written context, not machine-generated data
    """


class EvidenceForm(str, Enum):
    """How evidence was provided by user"""

    DOCUMENT = "document"
    """Uploaded file (log, screenshot, config, etc.)"""

    USER_INPUT = "user_input"
    """Typed text answer or description"""


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
    - UploadedFile: Raw file metadata, exists in ANY case phase (INQUIRY or INVESTIGATING)
    - Evidence: Investigation-linked data derived from files, ONLY exists in INVESTIGATING phase

    Files uploaded during INQUIRY are tracked here but do NOT become evidence until
    the case transitions to INVESTIGATING and hypotheses are formulated.
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
        max_length=1000,
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
        max_length=1000,
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
            "code, text, image). Maps to UnifiedDataType enum. None for legacy evidence."
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
            "Tier 1 extraction method used: structural_index, statistical_profile, "
            "parse_and_sanitize, ast_extraction, structure_extraction, metadata_extraction"
        ),
        max_length=100,
    )

    # ============================================================
    # Source Information
    # ============================================================
    source_type: EvidenceSourceType = Field(description="Type of evidence source")

    form: EvidenceForm = Field(
        description="How evidence was provided: DOCUMENT (uploaded) or USER_INPUT (typed)"
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

    EXTERNAL = "external"
    """External dependencies, third-party services"""

    HUMAN = "human"
    """Human errors, operational mistakes"""

    OTHER = "other"
    """Does not fit above categories"""


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
# Turn Tracking Models (Section 8)
# ============================================================


class TurnOutcome(str, Enum):
    """
    Turn outcome classification.

    NOTE: Outcomes are LLM-observable only (what happened this turn).
    Workflow control uses direct metrics (turns_without_progress, degraded_mode).
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

    actions_taken: List[str] = Field(
        default_factory=list,
        description="Agent actions: 'verified_symptom', 'requested_logs', 'generated_hypothesis', etc.",
    )

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
    # Observability Fields (for stagnation and validation tracking)
    # ============================================================
    stagnation_detected: Optional[str] = Field(
        default=None,
        description="Stagnation type detected this turn: no_progress, hypothesis_anchoring, action_loop, hypothesis_deadlock",
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
    | HISTORICAL   | CRITICAL/HIGH         | USER_CHOICE (unusual)  |
    | HISTORICAL   | MEDIUM/LOW            | ROOT_CAUSE             |
    +--------------+-----------------------+------------------------+

    Logic:
    - ONGOING + HIGH/CRITICAL urgency -> MITIGATION_FIRST
      * Problem is happening NOW, users affected
      * Need quick mitigation, then return for RCA

    - HISTORICAL + LOW/MEDIUM urgency -> ROOT_CAUSE
      * Problem happened before (not ongoing)
      * No immediate pressure, can do thorough RCA

    - Ambiguous cases -> USER_CHOICE
      * ONGOING + MEDIUM: Could go either way
      * HISTORICAL + HIGH: Unusual but possible (e.g., "it happened before and might happen again")

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

    # HISTORICAL problem
    else:  # TemporalState.HISTORICAL
        if urgency_level in [UrgencyLevel.CRITICAL, UrgencyLevel.HIGH]:
            # Historical but high urgency -> Unusual, let user decide
            # (Could be "this happened before and we must prevent it")
            return InvestigationPath.USER_CHOICE
        else:  # MEDIUM, LOW, or UNKNOWN
            # Historical + low urgency -> Classic post-mortem
            return InvestigationPath.ROOT_CAUSE


class InvestigationPath(str, Enum):
    """
    Investigation routing strategy (4-stage workflow).

    IMPORTANT: Path is SYSTEM-DETERMINED from matrix (temporal_state x urgency_level).
    LLM provides inputs (temporal_state, urgency_level) during verification.
    System calls determine_investigation_path() to select path deterministically.

    INVESTIGATING phase has 4 stages:
    - Stage 1: Symptom verification (where and when)
    - Stage 2: Hypotheses formulation (why)
    - Stage 3: Hypothesis validation (why really)
    - Stage 4: Solution (how)

    Two paths based on urgency:
    - MITIGATION_FIRST: 1-4-2-3-4 (quick mitigation, then RCA)
    - ROOT_CAUSE: 1-2-3-4 (traditional RCA)
    """

    MITIGATION_FIRST = "mitigation_first"
    """
    Mitigation-first path (updated from "mitigation only").

    Characteristics:
    - Apply quick mitigation based on correlation (stage 1 -> 4)
    - THEN return to stage 2 for full RCA (stage 4 -> 2 -> 3 -> 4)
    - Urgency priority with comprehensive investigation after mitigation

    Stage Flow: 1 -> 4 -> 2 -> 3 -> 4
    - Stage 1: Verify symptom (where/when)
    - Stage 4: Apply quick mitigation (correlation-based fix)
    - Stage 2: Formulate hypotheses (why)
    - Stage 3: Validate hypothesis (why really)
    - Stage 4: Apply permanent solution (how)

    Use When: ONGOING + HIGH/CRITICAL urgency
    - Problem is happening NOW
    - User needs immediate restoration
    - But also wants to prevent recurrence

    Key Change: No longer "mitigation only" - returns to RCA after initial mitigation
    """

    ROOT_CAUSE = "root_cause"
    """
    Traditional RCA path.

    Characteristics:
    - Thorough investigation from start
    - Deep root cause analysis before solution
    - Systematic hypothesis testing

    Stage Flow: 1 -> 2 -> 3 -> 4
    - Stage 1: Verify symptom (where/when)
    - Stage 2: Formulate hypotheses (why)
    - Stage 3: Validate hypothesis (why really)
    - Stage 4: Apply solution (how)

    Use When: HISTORICAL + LOW/MEDIUM urgency
    - Problem happened before (not ongoing)
    - No immediate service impact
    - Learning/prevention goal
    - Can take time for thorough analysis
    """

    USER_CHOICE = "user_choice"
    """
    Ambiguous case - let user decide.

    Characteristics:
    - Unclear which path is better
    - Present options to user
    - User makes strategic decision

    Use When: Ambiguous temporal_state x urgency combinations
    - ONGOING + MEDIUM urgency
    - HISTORICAL + HIGH urgency (unusual but possible)
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


class DegradedModeType(str, Enum):
    """Reason for entering degraded mode"""

    NO_PROGRESS = "no_progress"
    """
    3+ consecutive turns without milestone advancement.
    Investigation is stuck (covers insufficient data, user non-engagement, data access issues).
    """

    LIMITED_DATA = "limited_data"
    """
    Cannot obtain required evidence.
    Insufficient data to proceed.
    """

    HYPOTHESIS_DEADLOCK = "hypothesis_deadlock"
    """
    All hypotheses are inconclusive.
    Cannot determine root cause.
    """

    EXTERNAL_DEPENDENCY = "external_dependency"
    """
    Waiting on external team/person.
    Outside agent control.
    """

    DATA_BLOCKER = "data_blocker"
    """
    Critical data quality issue detected immediately.
    Evidence is corrupted, missing, or unusable.
    Proactive degraded mode entry (Prompt Engineering Guide Section 14).
    """

    OTHER = "other"
    """
    Does not fit standard degradation reasons.
    """


class DegradedMode(BaseModel):
    """
    Investigation is blocked or struggling.
    Agent offers fallback options.
    """

    mode_type: DegradedModeType = Field(description="Why investigation degraded")

    entered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When degraded mode was entered",
    )

    reason: str = Field(
        description="Detailed explanation of why investigation degraded",
        max_length=1000,
    )

    attempted_actions: List[str] = Field(
        default_factory=list, description="What agent tried before degrading"
    )

    # ============================================================
    # Fallback
    # ============================================================
    fallback_offered: Optional[str] = Field(
        default=None, description="Fallback option presented to user", max_length=1000
    )

    user_choice: Optional[str] = Field(
        default=None,
        description="How user responded: 'accept_fallback' | 'provide_more_data' | 'escalate' | 'abandon'",
        max_length=100,
    )

    # ============================================================
    # Exit
    # ============================================================
    exited_at: Optional[datetime] = Field(
        default=None, description="When degraded mode was exited (if recovered)"
    )

    exit_reason: Optional[str] = Field(
        default=None,
        description="How investigation recovered from degraded mode",
        max_length=500,
    )

    @property
    def is_active(self) -> bool:
        """Check if still in degraded mode"""
        return self.exited_at is None


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
    """Type of generated document"""

    INCIDENT_REPORT = "incident_report"
    """Formal incident report"""

    POST_MORTEM = "post_mortem"
    """Post-mortem analysis"""

    RUNBOOK = "runbook"
    """Runbook entry for future reference"""

    CHAT_SUMMARY = "chat_summary"
    """Summary of investigation conversation"""

    TIMELINE = "timeline"
    """Timeline visualization of events"""

    EVIDENCE_BUNDLE = "evidence_bundle"
    """
    Compiled evidence package.
    """

    OTHER = "other"
    """
    Does not fit standard document types.
    """


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

    post_mortem_id: Optional[str] = Field(
        default=None, description="Link to post-mortem doc if created"
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
    # ============================================================
    status: CaseStatus = Field(
        default=CaseStatus.INQUIRY, description="Current lifecycle status"
    )

    status_history: List[CaseStatusTransition] = Field(
        default_factory=list, description="Complete history of status changes"
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

    # ============================================================
    # Special States
    # ============================================================
    degraded_mode: Optional[DegradedMode] = Field(
        default=None, description="Investigation is stuck or blocked"
    )

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

    resolved_at: Optional[datetime] = Field(
        default=None, description="When case reached RESOLVED status"
    )

    closed_at: Optional[datetime] = Field(
        default=None,
        description="When case reached terminal state (RESOLVED or CLOSED)",
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
    def is_stuck(self) -> bool:
        """
        Detect if investigation is blocked.
        Returns True if 3+ consecutive turns without progress.
        """
        return self.turns_without_progress >= 3

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
        return [
            ev for ev in self.evidence
            if ev.category != EvidenceCategory.REJECTED
        ]

    @property
    def rejected_submissions(self) -> List[Evidence]:
        """
        Submissions analyzed but rejected as not useful.

        These exist for deduplication, audit trail, and cost avoidance.
        They are NOT evidence in the investigative sense.

        Design Reference:
            evidence-classification-design.md Section 3.3
        """
        return [
            ev for ev in self.evidence
            if ev.category == EvidenceCategory.REJECTED
        ]

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

        # Warning: Investigation stuck
        if self.is_stuck:
            warnings.append(
                {
                    "type": "stuck",
                    "severity": "warning",
                    "message": f"No progress for {self.turns_without_progress} consecutive turns",
                    "action": "Consider providing more data, escalating, or closing case",
                }
            )

        # Error: Degraded mode active
        if self.degraded_mode and self.degraded_mode.is_active:
            warnings.append(
                {
                    "type": "degraded_mode",
                    "severity": "error",
                    "message": f"Investigation blocked: {self.degraded_mode.reason}",
                    "mode_type": self.degraded_mode.mode_type.value,
                    "action": self.degraded_mode.fallback_offered
                    or "Escalate or close case",
                }
            )

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
                "inquiry_only",
                "duplicate",
                "other",
            ]
            if v not in allowed:
                raise ValueError(f"closure_reason must be one of: {allowed}")
        return v

    @field_validator("status_history")
    @classmethod
    def status_history_ordered(cls, v):
        """Ensure status history is chronologically ordered"""
        if len(v) > 1:
            for i in range(len(v) - 1):
                if v[i].triggered_at > v[i + 1].triggered_at:
                    raise ValueError("Status history must be chronologically ordered")
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
