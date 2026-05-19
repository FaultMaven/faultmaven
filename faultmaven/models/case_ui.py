"""Phase-adaptive UI response models for milestone-based investigation.

These models provide optimized responses for the browser extension UI based on case status.
Each status (INQUIRY, INVESTIGATING, RESOLVED) returns a different response schema
with fields relevant to that phase of the investigation.

This eliminates the need for multiple API calls to assemble UI state.
"""

from datetime import datetime
from typing import Annotated, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from faultmaven.models.api_models import ProgressTransparencyInfo
from faultmaven.modules.case.domain.models import (
    CaseStatus,
    ConfidenceLevel,
    HypothesisStatus,
    InvestigationStage,
    PathSelection,
)
from faultmaven.modules.case.domain.services.case_action_manager import (
    CaseActionManager,
)

# ============================================================
# Supporting Models for Phase-Adaptive Responses
# ============================================================


class InquiryRequestSummary(BaseModel):
    """Summary of user's initial request during INQUIRY phase."""

    original_message: str = Field(
        description="User's original problem description", max_length=1000
    )

    parsed_intent: str = Field(
        description="Detected intent: diagnose_error | performance_issue | availability | guidance | other",
        max_length=100,
    )

    severity: str = Field(
        description="Detected severity: critical | high | medium | low | unknown",
        max_length=50,
    )


class InquiryQuestion(BaseModel):
    """A question the agent needs answered during INQUIRY."""

    question_id: str = Field(description="Unique question identifier")

    text: str = Field(description="The question text", max_length=500)

    priority: str = Field(description="Priority: high | medium | low", max_length=50)

    answered: bool = Field(
        default=False, description="Whether user answered this question"
    )

    answer: Optional[str] = Field(
        default=None, description="User's answer if provided", max_length=2000
    )


class WorkingConclusionSummary(BaseModel):
    """Agent's current understanding during INVESTIGATING phase."""

    summary: str = Field(
        description="Current best theory about the problem", max_length=1000
    )

    confidence: float = Field(ge=0.0, le=1.0, description="Confidence level (0.0-1.0)")

    last_updated: datetime = Field(description="When this conclusion was last updated")


class InvestigationProgressSummary(BaseModel):
    """Progress metrics for INVESTIGATING phase.

    Purely descriptive — all backward-looking facts, no speculative
    forward-looking predictions. The frontend derives "what's next"
    from current_stage if needed.
    """

    completed_indicators: List[str] = Field(
        default_factory=list,
        description="Completed progress indicators (e.g. symptom_verified, root_cause_identified)",
    )

    completed_stage_gates: List[str] = Field(
        default_factory=list,
        description="Completed stage-gate milestones (e.g. mitigation_accepted, solution_verified)",
    )

    current_stage: InvestigationStage = Field(
        description="Current stage: DIAGNOSIS | MITIGATION | TREATMENT"
    )

    turns_without_progress: int = Field(
        default=0,
        description="Consecutive turns without milestone, evidence, or hypothesis progress",
    )

    total_evidence: int = Field(
        default=0, ge=0, description="Total evidence items collected"
    )

    active_hypotheses: int = Field(
        default=0, ge=0, description="Number of hypotheses currently being tested"
    )


class HypothesisSummary(BaseModel):
    """Summary of a hypothesis for INVESTIGATING phase UI."""

    hypothesis_id: str = Field(description="Hypothesis identifier")

    text: str = Field(description="Hypothesis statement", max_length=500)

    likelihood: float = Field(ge=0.0, le=1.0, description="Likelihood score (0.0-1.0)")

    status: HypothesisStatus = Field(
        description="Status: CAPTURED | ACTIVE | VALIDATED | REFUTED | INCONCLUSIVE | RETIRED"
    )

    evidence_count: int = Field(
        ge=0, description="Number of evidence items related to this hypothesis"
    )

    refutation_reason: Optional[str] = Field(
        default=None,
        max_length=200,
        description=(
            "Reason the hypothesis was refuted. Populated only when "
            "status=REFUTED; None otherwise. Mirrors the domain model's "
            "pair-integrity invariant."
        ),
    )


class EvidenceSummary(BaseModel):
    """Summary of evidence for INVESTIGATING phase UI."""

    evidence_id: str = Field(description="Evidence identifier")

    type: str = Field(
        description="Evidence type: log_file | metrics_data | config_file | etc.",
        max_length=100,
    )

    summary: str = Field(
        description="Brief summary of evidence content", max_length=500
    )

    timestamp: datetime = Field(description="When evidence was collected")

    relevance_score: float = Field(
        ge=0.0, le=1.0, description="Relevance to current investigation (0.0-1.0)"
    )

    collected_at_turn: int = Field(
        default=0, ge=0, description="Turn number when evidence was collected"
    )

    category: str = Field(
        default="OTHER",
        description="Evidence purpose: SYMPTOM_EVIDENCE | CAUSAL_EVIDENCE | RESOLUTION_EVIDENCE | OTHER",
        max_length=50,
    )

    source_filename: Optional[str] = Field(
        default=None,
        description="Original filename of the source file, if evidence originated from an attachment.",
        max_length=255,
    )


class RootCauseSummary(BaseModel):
    """Root cause information for RESOLVED phase."""

    description: str = Field(description="What caused the problem", max_length=1000)

    root_cause_id: str = Field(description="Root cause identifier")

    category: str = Field(
        description="Category: code | config | environment | network | data | hardware | external | human | other",
        max_length=100,
    )

    severity: str = Field(
        description="Severity: critical | high | medium | low", max_length=50
    )


class SolutionSummary(BaseModel):
    """Solution information for RESOLVED phase."""

    description: str = Field(
        description="What was done to fix the problem", max_length=2000
    )

    applied_at: datetime = Field(description="When solution was applied")

    applied_by: str = Field(description="Who applied the solution (user_id or 'agent')")


class VerificationStatus(BaseModel):
    """Solution verification status for RESOLVED phase."""

    verified: bool = Field(description="Whether solution effectiveness was verified")

    verification_method: str = Field(
        description="How verification was done", max_length=500
    )

    details: str = Field(
        description="Verification details and metrics", max_length=1000
    )


class ResolutionSummary(BaseModel):
    """Overall resolution metrics for RESOLVED phase."""

    total_duration_minutes: int = Field(
        ge=0, description="Total time from case creation to resolution"
    )

    milestones_completed: int = Field(
        ge=0, description="Total milestones completed (should be 8)"
    )

    hypotheses_tested: int = Field(ge=0, description="Number of hypotheses tested")

    evidence_collected: int = Field(ge=0, description="Total evidence items collected")

    key_insights: List[str] = Field(
        default_factory=list, description="Key learnings from this investigation"
    )


class ReportAvailability(BaseModel):
    """Report generation availability status for RESOLVED phase."""

    report_type: str = Field(
        description="Type: resolution_summary | closure_summary | runbook",
        max_length=100,
    )

    status: str = Field(
        description="Status: available | recommended | in_progress | not_applicable",
        max_length=50,
    )

    reason: Optional[str] = Field(
        default=None,
        description="Reason for status (e.g., why recommended)",
        max_length=500,
    )


class InquiryResponseData(BaseModel):
    """Nested inquiry data for INQUIRY phase response."""

    proposed_problem_statement: Optional[str] = Field(
        default=None,
        description="Agent's formalized problem statement (if ready)",
        max_length=1000,
    )

    problem_statement_confirmed: bool = Field(
        default=False, description="Whether user confirmed the problem statement"
    )

    decided_to_investigate: bool = Field(
        default=False,
        description="Whether agent has enough info to start investigation",
    )

    inquiry_turns: int = Field(
        default=0,
        ge=0,
        description="Number of conversation turns during inquiry phase",
    )

    problem_confirmation: Optional[Dict] = Field(
        default=None, description="Problem type and severity guess"
    )


# InvestigationStrategyData removed (slice 2 cleanup, completed alongside
# the path_selection exposure): the descriptive `approach` string was the
# regex bait that fed the frontend's `getApproachHint` helper. With that
# helper removed in slice 4 and the structured `path_selection` field now
# exposed on CaseUIResponse_*, neither field had any remaining consumer.
# The model class and its `investigation_strategy` field on
# CaseUIResponse_Investigating are removed entirely; clients that want
# path information read `path_selection` instead.


class TemporalStateData(BaseModel):
    """Temporal information about problem occurrence."""

    started_at: Optional[datetime] = Field(
        default=None, description="When the problem started"
    )

    last_occurrence_at: Optional[datetime] = Field(
        default=None, description="Most recent occurrence of the problem"
    )

    state: Optional[str] = Field(
        default=None,
        description="Temporal state: ongoing | historical | intermittent",
        max_length=50,
    )


class ImpactData(BaseModel):
    """Impact assessment for problem scope."""

    affected_services: Optional[List[str]] = Field(
        default=None, description="List of affected services"
    )

    affected_users: Optional[str] = Field(
        default=None,
        description="User impact description (e.g., 'All users in US region')",
        max_length=500,
    )

    affected_regions: Optional[List[str]] = Field(
        default=None, description="List of affected geographical regions"
    )


class ProblemVerificationData(BaseModel):
    """Problem verification details for INVESTIGATING phase."""

    urgency_level: Optional[str] = Field(
        default=None,
        description="Urgency: critical | high | medium | low | unknown",
        max_length=50,
    )

    severity: Optional[str] = Field(
        default=None,
        description="Severity: critical | high | medium | low",
        max_length=50,
    )

    temporal_state: Optional[TemporalStateData] = Field(
        default=None, description="When the problem occurred and its temporal pattern"
    )

    impact: Optional[ImpactData] = Field(
        default=None, description="Scope of impact (services, users, regions)"
    )

    user_impact: Optional[str] = Field(
        default=None, description="Human-readable user impact summary", max_length=1000
    )


# ============================================================
# Phase-Adaptive Response Models
# ============================================================


class CaseUIResponse_Inquiry(BaseModel):
    """
    UI response for INQUIRY phase.

    Focus: Understanding the problem, asking clarifying questions.
    User hasn't committed to full investigation yet.
    """

    case_id: str = Field(description="Case identifier")

    status: Literal[CaseStatus.INQUIRY] = Field(
        default=CaseStatus.INQUIRY,
        description="Always 'inquiry' for this response type",
    )

    title: str = Field(description="Case title", max_length=200)

    current_turn: int = Field(ge=0, description="Current turn counter")

    created_at: datetime = Field(description="When case was created")

    updated_at: datetime = Field(description="Last update timestamp")

    uploaded_files_count: int = Field(ge=0, description="Total files uploaded")

    valid_next_states: List[str] = Field(
        default_factory=list,
        description="Allowed status transitions from current state for user-initiated changes",
    )

    path_selection: Optional[PathSelection] = Field(
        default=None,
        description=(
            "Investigation path recommendation + user-confirmation state. "
            "Populated after Gate 1 (problem statement confirmation) once "
            "urgency signals are available; Gate 2 is open when "
            "user_confirmed is False. Frontend renders the Gate 2 chip / "
            "path-aware progress dots based on this object."
        ),
    )

    # ============================================================
    # Inquiry-Specific Fields (Nested)
    # ============================================================
    inquiry: InquiryResponseData = Field(description="Nested inquiry phase data")


class CaseUIResponse_Investigating(BaseModel):
    """
    UI response for INVESTIGATING phase.

    Focus: Active investigation, milestone progress, hypothesis testing.
    User has committed to investigation and agent is working through milestones.
    """

    case_id: str = Field(description="Case identifier")

    status: Literal[CaseStatus.INVESTIGATING] = Field(
        default=CaseStatus.INVESTIGATING,
        description="Always 'investigating' for this response type",
    )

    title: str = Field(description="Case title", max_length=200)

    current_turn: int = Field(ge=0, description="Current turn counter")

    created_at: datetime = Field(description="When case was created")

    updated_at: datetime = Field(description="Last update timestamp")

    uploaded_files_count: int = Field(default=0, description="Number of uploaded files")

    valid_next_states: List[str] = Field(
        default_factory=list,
        description="Allowed status transitions from current state for user-initiated changes",
    )

    problem_statement: Optional[str] = Field(
        default=None,
        description="Confirmed problem statement carried over from INQUIRY (sourced from case.description).",
        max_length=1000,
    )

    # ============================================================
    # Investigation-Specific Fields
    # ============================================================
    path_selection: Optional[PathSelection] = Field(
        default=None,
        description=(
            "Investigation path commitment + Gate-3 state. user_confirmed "
            "is always True for INVESTIGATING (Gate 2 must pass before "
            "transition). mitigation_completed_at_turn is set after "
            "mitigation_verified; rca_after_mitigation_confirmed drives "
            "Gate 3 prompts on the mitigation-first path."
        ),
    )

    working_conclusion: Optional[WorkingConclusionSummary] = Field(
        default=None, description="Agent's current understanding of the problem"
    )

    progress: InvestigationProgressSummary = Field(
        description="Milestone-based progress tracking"
    )

    active_hypotheses: List[HypothesisSummary] = Field(
        default_factory=list, description="Hypotheses currently being tested"
    )

    latest_evidence: List[EvidenceSummary] = Field(
        default_factory=list, description="Most recent evidence collected (last 5)"
    )

    next_actions: List[str] = Field(
        default_factory=list, description="Suggested next steps for investigation"
    )

    agent_status: str = Field(
        description="What agent is currently doing", max_length=500
    )

    # ============================================================
    # Additional Investigation Data (from BACKEND_REMAINING_WORK)
    # ============================================================
    # `investigation_strategy` removed — replaced by the structured
    # `path_selection` field above. See InvestigationStrategyData comment
    # earlier in this file for the rationale.

    problem_verification: Optional[ProblemVerificationData] = Field(
        default=None,
        description="Problem verification details (urgency, severity, impact)",
    )

    progress_transparency: Optional[ProgressTransparencyInfo] = Field(
        default=None,
        description="Progress transparency state. Present when investigation "
        "has stalled and agent is surfacing milestone dependencies.",
    )


class CaseUIResponse_Resolved(BaseModel):
    """
    UI response for RESOLVED phase.

    Focus: Resolution summary, root cause, solution applied, verification.
    Investigation complete, case closed with solution.
    """

    case_id: str = Field(description="Case identifier")

    status: Literal[CaseStatus.RESOLVED, CaseStatus.CLOSED] = Field(
        description="Case terminal status: 'resolved' (with solution) or 'closed' (without investigation)",
    )

    title: str = Field(description="Case title", max_length=200)

    current_turn: int = Field(ge=0, description="Current turn counter")

    created_at: datetime = Field(description="When case was created")

    updated_at: datetime = Field(description="Last update timestamp")

    resolved_at: datetime = Field(description="When case was resolved")

    uploaded_files_count: int = Field(default=0, description="Number of uploaded files")

    valid_next_states: List[str] = Field(
        default_factory=list,
        description="Allowed status transitions from current state for user-initiated changes",
    )

    problem_statement: Optional[str] = Field(
        default=None,
        description="Confirmed problem statement carried over from INQUIRY (sourced from case.description).",
        max_length=1000,
    )

    # ============================================================
    # Resolution-Specific Fields
    # ============================================================
    path_selection: Optional[PathSelection] = Field(
        default=None,
        description=(
            "Investigation path that was followed. Lets the terminal UI "
            "display 'Mitigation-first' or 'Root-cause' retrospectively, "
            "and exposes mitigation_completed_at_turn for resolved cases "
            "that detoured through MITIGATION."
        ),
    )

    root_cause: RootCauseSummary = Field(description="What caused the problem")

    solution_applied: SolutionSummary = Field(
        description="Solution that fixed the problem"
    )

    verification_status: VerificationStatus = Field(
        description="How solution effectiveness was verified"
    )

    resolution_summary: ResolutionSummary = Field(
        description="Overall resolution metrics and insights"
    )

    reports_available: List[ReportAvailability] = Field(
        default_factory=list,
        description="Available reports (incident report, post-mortem, runbook)",
    )


# ============================================================
# Union Type for Discriminated Response
# ============================================================

CaseUIResponse = Annotated[
    Union[
        CaseUIResponse_Inquiry, CaseUIResponse_Investigating, CaseUIResponse_Resolved
    ],
    Field(discriminator="status"),
]
"""
Phase-adaptive case response for UI.

The API returns different response schemas based on case status:
- INQUIRY → CaseUIResponse_Inquiry
- INVESTIGATING → CaseUIResponse_Investigating
- RESOLVED → CaseUIResponse_Resolved

This is discriminated by the 'status' field.
"""


# ============================================================
# File Relationship Models (for new uploaded-files endpoints)
# ============================================================


class FileToMilestoneRelationship(BaseModel):
    """Tracks which files contributed to milestone completion."""

    file_id: str = Field(description="Uploaded file identifier")

    milestone_id: str = Field(description="Milestone name (e.g., 'symptom_verified')")

    milestone_name: str = Field(
        description="Human-readable milestone name", max_length=200
    )

    contribution: str = Field(
        description="How this file helped complete the milestone", max_length=1000
    )

    contributed_at: datetime = Field(description="When relationship was established")


class FileToHypothesisRelationship(BaseModel):
    """Tracks evidence supporting/refuting hypotheses."""

    file_id: str = Field(description="Uploaded file identifier")

    hypothesis_id: str = Field(description="Hypothesis identifier")

    hypothesis_text: str = Field(description="Hypothesis statement", max_length=500)

    relationship: Literal["supports", "refutes", "neutral"] = Field(
        description="How this file relates to the hypothesis"
    )

    evidence_strength: float = Field(
        ge=0.0, le=1.0, description="Strength of evidence (0.0-1.0)"
    )


class UploadedFileMetadata(BaseModel):
    """Metadata for uploaded file (list view)."""

    file_id: str = Field(description="File identifier")

    filename: str = Field(description="Original filename", max_length=255)

    mime_type: str = Field(description="MIME type", max_length=100)

    size_bytes: int = Field(ge=0, description="File size in bytes")

    uploaded_at: datetime = Field(description="When file was uploaded")

    uploaded_by_user_id: str = Field(description="User who uploaded")

    # ============================================================
    # AI Analysis Status
    # ============================================================
    analysis_status: Literal["pending", "completed", "failed"] = Field(
        description="AI analysis status"
    )

    ai_insights_summary: Optional[str] = Field(
        default=None,
        description="Brief AI analysis summary (if completed)",
        max_length=500,
    )

    # ============================================================
    # Relationships
    # ============================================================
    related_milestone_ids: List[str] = Field(
        default_factory=list, description="Milestones this file helped complete"
    )

    related_hypothesis_ids: List[str] = Field(
        default_factory=list, description="Hypotheses this file supports/refutes"
    )


class AIInsights(BaseModel):
    """Detailed AI insights for uploaded file."""

    summary: str = Field(
        description="Overall summary of file analysis", max_length=2000
    )

    key_findings: List[str] = Field(
        default_factory=list, description="Key findings extracted from file"
    )

    anomalies_detected: List[str] = Field(
        default_factory=list, description="Anomalies or issues detected"
    )

    confidence_score: float = Field(
        ge=0.0, le=1.0, description="Confidence in analysis accuracy (0.0-1.0)"
    )


class UploadedFileDetailsResponse(BaseModel):
    """Detailed information about uploaded file (detail view)."""

    file_id: str = Field(description="File identifier")

    case_id: str = Field(description="Case this file belongs to")

    filename: str = Field(description="Original filename", max_length=255)

    mime_type: str = Field(description="MIME type", max_length=100)

    size_bytes: int = Field(ge=0, description="File size in bytes")

    uploaded_at: datetime = Field(description="When file was uploaded")

    uploaded_by_user_id: str = Field(description="User who uploaded")

    # ============================================================
    # AI Analysis
    # ============================================================
    analysis_status: Literal["pending", "completed", "failed"] = Field(
        description="AI analysis status"
    )

    ai_insights: Optional[AIInsights] = Field(
        default=None, description="Detailed AI analysis results (if completed)"
    )

    # ============================================================
    # Relationships
    # ============================================================
    related_milestones: List[FileToMilestoneRelationship] = Field(
        default_factory=list, description="Milestones this file helped complete"
    )

    related_hypotheses: List[FileToHypothesisRelationship] = Field(
        default_factory=list, description="Hypotheses this file supports/refutes"
    )


class UploadedFilesListResponse(BaseModel):
    """Response for listing uploaded files."""

    case_id: str = Field(description="Case identifier")

    files: List[UploadedFileMetadata] = Field(
        default_factory=list, description="List of uploaded files with metadata"
    )

    total_count: int = Field(ge=0, description="Total number of files (for pagination)")

    total_size_bytes: int = Field(ge=0, description="Total size of all files in bytes")
