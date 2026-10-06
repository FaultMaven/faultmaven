from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# This docstring is published: Pydantic emits it as the schema `description`
# for every field typed with this enum, so it reaches docs/reference/api/.
# Keep it consumer-facing — implementation history belongs in comments.
#
# The backend carries ONE vocabulary for the stage: the raw enum value.
# Prompts emit it (``CURRENT_STAGE: {enum}``) and the API serves it; display
# naming is owned by consumers, the way the Dashboard already labels
# ``CaseState``. A backend-side display mapping used to exist here and
# rendered DIAGNOSIS as "Investigating" — colliding with
# ``CaseState.investigating``, a different axis entirely. It reached only
# prompts, never the wire, and was deleted in #1075.
class InvestigationStage(str, Enum):
    """
    Investigation stage within the Investigating Phase.

    These three stages are pure DERIVED labels in the unified opportunistic
    flow. They are re-derived from the action-compliance gates (see
    ``InvestigationProgress.current_stage``); they do NOT drive prompt
    dispatch and there is NO path fork or prospective routing.

    DIAGNOSIS is the default. MITIGATION is not a separate path — it is an
    optional "stop the bleeding" insert that surfaces while the
    investigation continues. TREATMENT follows solution acceptance.
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

    REVISION_PENDING = "revision_pending"
    """Evidence shows a real problem the confirmed statement describes
    inaccurately. A revised statement waits for the user's re-confirmation;
    cause work arriving meanwhile is staged and applied on confirmation."""

    INVALIDATED = "invalidated"
    """Evidence shows the reported symptom was never present: a false alarm.
    The case can be closed, never resolved; nothing progresses until new
    evidence shows a problem, or the user disputes the finding."""


class StatementRecordKind(str, Enum):
    """What happened to the problem statement at one point in the case."""

    CONFIRMED = "confirmed"
    """The user confirmed the statement that opened the investigation."""

    REVISED = "revised"
    """The user re-confirmed a revision the evidence called for."""

    EDITED = "edited"
    """The user edited the statement directly."""

    INVALIDATED = "invalidated"
    """The evidence showed the reported symptom was never present."""

    INVALIDATION_WITHDRAWN = "invalidation_withdrawn"
    """A false-alarm finding was withdrawn after the user disputed it."""


class ProblemStatementRecord(BaseModel):
    """One event in the problem statement's history. ``text`` is the statement
    the event concerns: the new one for confirmed/revised/edited, the one
    found absent for invalidated."""

    kind: StatementRecordKind
    text: str
    turn: int = 0
    evidence_ids: List[str] = Field(default_factory=list)
    rationale: Optional[str] = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StagedCauseWork(BaseModel):
    """The cause work one turn sent while a revision awaited re-confirmation.

    ``updates`` is that turn's cause-work subset of the state update, dumped as
    JSON. ``evidence_added`` is the evidence ids the turn minted, in order —
    what its ``new_index_N`` evidence refs resolve against — so the work
    replays a turn later exactly as it would have applied then. One bundle per
    turn, because ``new_index_N`` refs are per turn.
    """

    turn: int
    updates: Dict[str, Any] = Field(default_factory=dict)
    evidence_added: List[str] = Field(default_factory=list)


class PendingRevision(BaseModel):
    """A revised statement awaiting the user's re-confirmation, and the cause
    work that arrived while it waited. The staged work is replayed through the
    normal apply path when the user confirms, and discarded when they decline.
    """

    text: str
    evidence_ids: List[str] = Field(default_factory=list)
    basis: str = ""
    proposed_at_turn: int = 0
    prior_status: ProblemStatus = ProblemStatus.UNVERIFIED
    offer_key: str = ""
    staged: List[StagedCauseWork] = Field(default_factory=list)


class ProblemInvalidation(BaseModel):
    """The finding that the reported symptom was never present."""

    rationale: str
    evidence_ids: List[str] = Field(default_factory=list)
    turn: int = 0
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
        max_length=10,  # Limit to top 10 (V2 spelling of the deprecated max_items)
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
    # Statement lifecycle — written only by core/investigation/problem_status
    # ============================================================
    statement_history: List[ProblemStatementRecord] = Field(
        default_factory=list,
        description=(
            "Every change to the problem statement, oldest first: the "
            "confirmation that opened the investigation, re-confirmed "
            "revisions, direct edits, and false-alarm findings."
        ),
    )

    pending_revision: Optional[PendingRevision] = Field(
        default=None,
        description="A revised statement awaiting the user's re-confirmation.",
    )

    invalidation: Optional[ProblemInvalidation] = Field(
        default=None,
        description="Why the reported symptom was found never to have been present.",
    )

    declined_revision_keys: List[str] = Field(
        default_factory=list,
        description=(
            "Offer keys of revisions the user declined, so the same wording is "
            "not proposed again."
        ),
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
