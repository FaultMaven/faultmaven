"""API Request/Response Models for Case Management.

These models provide a clean API layer separate from the domain Case model.
They handle:
- Request validation
- Response serialization
- API versioning
- Backward compatibility
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from faultmaven.models.case import Case, CaseStatus, InvestigationStage


# ============================================================
# Case Creation and Updates
# ============================================================

class CaseCreateRequest(BaseModel):
    """Request to create a new case (v2.0).

    User identity is derived from authentication token, not request body.
    This ensures security and prevents user_id spoofing.
    """

    title: Optional[str] = Field(
        default=None,
        description="Case title (optional, auto-generated if not provided)",
        max_length=200
    )

    description: Optional[str] = Field(
        default="",
        description="Initial problem description",
        max_length=2000
    )

    initial_message: Optional[str] = Field(
        default=None,
        description="First user message (for CONSULTING phase)",
        max_length=4000
    )

    session_id: Optional[str] = Field(
        default=None,
        description="Session ID for authentication and case association (restored from old implementation)"
    )

    # Note: user_id and organization_id are derived from authentication context
    # They are NOT part of the request body to prevent spoofing


class CaseUpdateRequest(BaseModel):
    """Request to update an existing case."""

    title: Optional[str] = Field(
        default=None,
        description="Updated title",
        max_length=200
    )

    description: Optional[str] = Field(
        default=None,
        description="Updated description",
        max_length=2000
    )

    status: Optional[CaseStatus] = Field(
        default=None,
        description="Updated status (admin only)"
    )


# ============================================================
# Case Responses (for API)
# ============================================================

class CaseSummary(BaseModel):
    """Minimal case information for list views."""

    case_id: str
    title: str
    status: CaseStatus
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    user_id: str
    organization_id: str

    # Progress indicators
    current_turn: int
    milestones_completed: int
    total_milestones: int = 8

    # Computed fields
    is_stuck: bool
    is_terminal: bool

    @classmethod
    def from_case(cls, case: Case) -> "CaseSummary":
        """Convert Case domain model to API summary."""
        return cls(
            case_id=case.case_id,
            title=case.title,
            status=case.status,
            created_at=case.created_at,
            updated_at=case.updated_at,
            last_activity_at=case.last_activity_at,
            user_id=case.user_id,
            organization_id=case.organization_id,
            current_turn=case.current_turn,
            milestones_completed=len(case.progress.completed_milestones),
            total_milestones=8,
            is_stuck=case.is_stuck,
            is_terminal=case.is_terminal
        )


class CaseDetail(BaseModel):
    """Detailed case information for single case view."""

    case_id: str
    title: str
    description: str
    status: CaseStatus
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    resolved_at: Optional[datetime]
    closed_at: Optional[datetime]

    user_id: str
    organization_id: str

    # Progress
    current_turn: int
    turns_without_progress: int
    current_stage: Optional[InvestigationStage]

    # Milestones
    milestones_completed: List[str]
    pending_milestones: List[str]
    completion_percentage: float

    # Counts
    evidence_count: int
    hypothesis_count: int
    solution_count: int

    # Flags
    is_stuck: bool
    is_terminal: bool
    degraded_mode_active: bool
    escalated: bool

    @classmethod
    def from_case(cls, case: Case) -> "CaseDetail":
        """Convert Case domain model to API detail view."""
        return cls(
            case_id=case.case_id,
            title=case.title,
            description=case.description,
            status=case.status,
            created_at=case.created_at,
            updated_at=case.updated_at,
            last_activity_at=case.last_activity_at,
            resolved_at=case.resolved_at,
            closed_at=case.closed_at,
            user_id=case.user_id,
            organization_id=case.organization_id,
            current_turn=case.current_turn,
            turns_without_progress=case.turns_without_progress,
            current_stage=case.current_stage,
            milestones_completed=case.progress.completed_milestones,
            pending_milestones=case.progress.pending_milestones,
            completion_percentage=case.progress.completion_percentage,
            evidence_count=len(case.evidence),
            hypothesis_count=len(case.hypotheses),
            solution_count=len(case.solutions),
            is_stuck=case.is_stuck,
            is_terminal=case.is_terminal,
            degraded_mode_active=case.degraded_mode.is_active if case.degraded_mode else False,
            escalated=case.escalation_state.is_active if case.escalation_state else False
        )


# ============================================================
# List and Filter
# ============================================================

class CaseListFilter(BaseModel):
    """Filter criteria for listing cases."""

    user_id: Optional[str] = Field(
        default=None,
        description="Filter by user ID"
    )

    organization_id: Optional[str] = Field(
        default=None,
        description="Filter by organization ID"
    )

    status: Optional[CaseStatus] = Field(
        default=None,
        description="Filter by status"
    )

    is_stuck: Optional[bool] = Field(
        default=None,
        description="Filter stuck cases"
    )

    created_after: Optional[datetime] = Field(
        default=None,
        description="Cases created after this date"
    )

    created_before: Optional[datetime] = Field(
        default=None,
        description="Cases created before this date"
    )

    limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Maximum results to return"
    )

    offset: int = Field(
        default=0,
        ge=0,
        description="Pagination offset"
    )

    include_empty: bool = Field(
        default=True,
        description="Include cases with no conversation (current_turn == 0)"
    )

    include_archived: bool = Field(
        default=False,
        description="Include archived/closed cases"
    )


class CaseListResponse(BaseModel):
    """Response for case listing."""

    cases: List[CaseSummary]
    total_count: int
    limit: int
    offset: int
    has_more: bool

    @classmethod
    def from_cases(
        cls,
        cases: List[Case],
        total_count: int,
        limit: int,
        offset: int
    ) -> "CaseListResponse":
        """Convert list of Cases to API response."""
        return cls(
            cases=[CaseSummary.from_case(case) for case in cases],
            total_count=total_count,
            limit=limit,
            offset=offset,
            has_more=(offset + len(cases)) < total_count
        )


# ============================================================
# Search
# ============================================================

class CaseSearchRequest(BaseModel):
    """Request to search cases."""

    query: str = Field(
        description="Search query",
        min_length=1,
        max_length=500
    )

    user_id: Optional[str] = Field(
        default=None,
        description="Limit to user's cases"
    )

    organization_id: Optional[str] = Field(
        default=None,
        description="Limit to organization's cases"
    )

    status: Optional[CaseStatus] = Field(
        default=None,
        description="Filter by status"
    )

    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum results"
    )


class CaseSearchResponse(BaseModel):
    """Response for case search."""

    cases: List[CaseSummary]
    total_count: int
    query: str

    @classmethod
    def from_cases(
        cls,
        cases: List[Case],
        total_count: int,
        query: str
    ) -> "CaseSearchResponse":
        """Convert search results to API response."""
        return cls(
            cases=[CaseSummary.from_case(case) for case in cases],
            total_count=total_count,
            query=query
        )


# ============================================================
# Case Query Submission
# ============================================================

class CaseQueryRequest(BaseModel):
    """Request to submit a query to a case investigation.

    Used by POST /cases/{case_id}/queries endpoint.
    """

    message: str = Field(
        description="User message",
        min_length=1,
        max_length=4000
    )

    attachments: Optional[List[dict]] = Field(
        default=None,
        description="File attachments (file_id, filename, data_type, size, summary, s3_uri)"
    )


class CaseQueryResponse(BaseModel):
    """Response for case query submission.

    Returned by POST /cases/{case_id}/queries endpoint.
    """

    agent_response: str
    turn_number: int
    milestones_completed: List[str]
    case_status: CaseStatus
    progress_made: bool
    is_stuck: bool


# Legacy aliases for backward compatibility (deprecated)
CreateTurnRequest = CaseQueryRequest
CreateTurnResponse = CaseQueryResponse


# ============================================================
# Messages and Conversation History
# ============================================================

class CaseMessage(BaseModel):
    """A single message in case conversation.

    Schema matches case-storage-design.md Section 4.7 (case_messages table).
    """

    message_id: str
    case_id: str
    turn_number: int
    role: str  # "user" | "assistant" | "system"
    content: str
    created_at: datetime = Field(description="Message creation time (matches SQL schema)")

    # Optional fields
    author_id: Optional[str] = Field(None, description="User who created the message")
    token_count: Optional[int] = Field(None, description="Number of tokens in content")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Sources, tools used, etc.")

    # Legacy/extension fields
    attachments: Optional[List[dict]] = None


class CaseConversationResponse(BaseModel):
    """Conversation history for a case."""

    case_id: str
    messages: List[CaseMessage]
    total_messages: int


# ============================================================
# Participants (for future collaboration features)
# ============================================================

class CaseParticipant(BaseModel):
    """Participant in a case (for future use)."""

    user_id: str
    role: str  # "owner", "collaborator", "viewer"
    added_at: datetime
    added_by: str


class AddParticipantRequest(BaseModel):
    """Request to add participant to case."""

    user_id: str
    role: str = Field(default="viewer", pattern="^(owner|collaborator|viewer)$")


# ============================================================
# Analytics and Metrics
# ============================================================

class CaseMetrics(BaseModel):
    """Metrics for a single case."""

    case_id: str
    total_turns: int
    time_to_resolution_minutes: Optional[int]
    evidence_collected: int
    hypotheses_tested: int
    milestones_completed: int
    stuck_turn_count: int


class OrganizationCaseMetrics(BaseModel):
    """Aggregate metrics for an organization."""

    organization_id: str
    total_cases: int
    active_cases: int
    resolved_cases: int
    stuck_cases: int
    avg_resolution_time_minutes: Optional[float]


# ============================================================
# Uploaded Files / Evidence API Models
# ============================================================

class UploadedFileMetadata(BaseModel):
    """Metadata for uploaded files (evidence) - List view."""

    file_id: str = Field(description="Evidence/File identifier")
    filename: str = Field(description="Original or generated filename")
    size_bytes: int = Field(description="File size in bytes", ge=0)
    size_display: str = Field(description="Human-readable size (e.g., '2.3 MB')")
    uploaded_at_turn: int = Field(description="Turn when file was uploaded", ge=0)
    uploaded_at: datetime = Field(description="Upload timestamp")
    source_type: str = Field(description="file_upload | paste | screenshot | page_injection | agent_generated")
    analysis_status: str = Field(description="pending | processing | completed | failed")
    summary: Optional[str] = Field(default=None, description="AI-generated summary (1-2 sentences)")
    source_metadata: Optional[dict] = Field(default=None, description="Additional metadata for page injections")

    @classmethod
    def from_uploaded_file(cls, uploaded_file) -> "UploadedFileMetadata":
        """Convert UploadedFile model to UploadedFileMetadata."""
        from faultmaven.models.case import UploadedFile

        # Calculate human-readable size
        size_bytes = uploaded_file.size_bytes
        if size_bytes < 1024:
            size_display = f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            size_display = f"{size_bytes / 1024:.1f} KB"
        else:
            size_display = f"{size_bytes / (1024 * 1024):.1f} MB"

        return cls(
            file_id=uploaded_file.file_id,
            filename=uploaded_file.filename,  # Use actual filename
            size_bytes=size_bytes,
            size_display=size_display,
            uploaded_at_turn=uploaded_file.uploaded_at_turn,
            uploaded_at=uploaded_file.uploaded_at,
            source_type=uploaded_file.source_type,
            analysis_status="completed",  # Always completed after preprocessing
            summary=uploaded_file.preprocessing_summary,
            source_metadata=None  # Could be added in future
        )

    @classmethod
    def from_evidence(cls, evidence) -> "UploadedFileMetadata":
        """Convert Evidence model to UploadedFileMetadata (legacy - should use from_uploaded_file)."""
        from faultmaven.models.case import Evidence

        # Calculate human-readable size
        size_bytes = evidence.content_size_bytes
        if size_bytes < 1024:
            size_display = f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            size_display = f"{size_bytes / 1024:.1f} KB"
        else:
            size_display = f"{size_bytes / (1024 * 1024):.1f} MB"

        return cls(
            file_id=evidence.evidence_id,
            filename=f"{evidence.source_type.value}_{evidence.evidence_id}.txt",  # Generate filename
            size_bytes=size_bytes,
            size_display=size_display,
            uploaded_at_turn=evidence.collected_at_turn,
            uploaded_at=evidence.collected_at,
            source_type=evidence.source_type.value,
            analysis_status="completed",  # Always completed for now
            summary=evidence.summary,
            source_metadata=None  # Could extract from evidence if needed
        )


class HypothesisRelationship(BaseModel):
    """How a file relates to a hypothesis."""

    hypothesis_id: str
    hypothesis_description: str
    stance: str = Field(description="strongly_supports | supports | neutral | contradicts | strongly_contradicts | irrelevant")
    reasoning: str


class TimelineEvent(BaseModel):
    """Timeline event extracted from file."""

    timestamp: datetime
    event: str


class FileAnalysis(BaseModel):
    """Detailed AI analysis of file."""

    key_findings: List[str] = Field(default_factory=list)
    timeline_events: List[TimelineEvent] = Field(default_factory=list)
    relevance: Optional[str] = None


class UploadedFileDetails(UploadedFileMetadata):
    """Detailed file information including analysis."""

    full_analysis: Optional[FileAnalysis] = Field(default=None, description="Detailed AI analysis")
    hypothesis_relationships: Optional[List[HypothesisRelationship]] = Field(
        default=None,
        description="How this file relates to hypotheses (investigating phase only)"
    )

    @classmethod
    def from_uploaded_file(cls, uploaded_file, case_id: str) -> "UploadedFileDetails":
        """Convert UploadedFile to UploadedFileDetails (CONSULTING phase - no hypotheses yet)."""
        # Start with base metadata
        base = UploadedFileMetadata.from_uploaded_file(uploaded_file)

        # Build minimal analysis (just preprocessing summary)
        full_analysis = FileAnalysis(
            key_findings=[uploaded_file.preprocessing_summary] if uploaded_file.preprocessing_summary else [],
            relevance=None  # No analysis yet in CONSULTING phase
        )

        return cls(
            **base.model_dump(),
            full_analysis=full_analysis,
            hypothesis_relationships=None  # No hypotheses in CONSULTING phase
        )

    @classmethod
    def from_evidence(cls, evidence, case_id: str, hypotheses: Optional[dict] = None) -> "UploadedFileDetails":
        """Convert Evidence to UploadedFileDetails with full analysis (INVESTIGATING phase)."""
        # Start with base metadata
        base = UploadedFileMetadata.from_evidence(evidence)

        # Build full analysis
        full_analysis = FileAnalysis(
            key_findings=[evidence.summary] if evidence.summary else [],
            relevance=evidence.analysis if evidence.analysis else None
        )

        # Build hypothesis relationships if hypotheses provided
        hypothesis_relationships = None
        if hypotheses and evidence.primary_purpose:
            # Find hypotheses related to this evidence
            relationships = []
            for hyp_id, hypothesis in hypotheses.items():
                if hasattr(hypothesis, 'evidence_links') and evidence.evidence_id in hypothesis.evidence_links:
                    link = hypothesis.evidence_links[evidence.evidence_id]
                    relationships.append(HypothesisRelationship(
                        hypothesis_id=hyp_id,
                        hypothesis_description=hypothesis.statement,
                        stance=link.stance.value,
                        reasoning=link.reasoning
                    ))
            if relationships:
                hypothesis_relationships = relationships

        return cls(
            **base.model_dump(),
            full_analysis=full_analysis,
            hypothesis_relationships=hypothesis_relationships
        )


class UploadedFilesList(BaseModel):
    """Paginated list of uploaded files."""

    files: List[UploadedFileMetadata]
    total_count: int = Field(description="Total number of files")
    limit: int
    offset: int


# ============================================================
# Evidence-to-File Linkage API Models (Phase 2)
# ============================================================

class DerivedEvidenceSummary(BaseModel):
    """Summary of evidence derived from an uploaded file."""

    evidence_id: str
    summary: str = Field(max_length=500)
    category: str = Field(description="SYMPTOM_EVIDENCE | CAUSAL_EVIDENCE | RESOLUTION_EVIDENCE | OTHER")
    collected_at_turn: int
    related_hypothesis_ids: List[str] = Field(default_factory=list)


class UploadedFileDetailsResponse(BaseModel):
    """Detailed information about an uploaded file with evidence linkage."""

    file_id: str
    filename: str
    size_bytes: int
    size_display: str
    uploaded_at_turn: int
    uploaded_at: datetime
    source_type: str
    data_type: str
    summary: Optional[str] = None

    # Evidence linkage
    derived_evidence: List[DerivedEvidenceSummary] = Field(default_factory=list)
    evidence_count: int = Field(ge=0)


class UploadedFilesListResponse(BaseModel):
    """List of uploaded files with evidence counts."""

    case_id: str
    total_count: int
    files: List[UploadedFileMetadata]


class SourceFileReference(BaseModel):
    """Reference to source file that evidence was derived from."""

    file_id: str
    filename: str
    uploaded_at_turn: int


class RelatedHypothesis(BaseModel):
    """Hypothesis linked to this evidence."""

    hypothesis_id: str
    statement: str
    stance: str = Field(description="SUPPORTS | REFUTES | NEUTRAL")


class EvidenceDetailsResponse(BaseModel):
    """Detailed evidence information with source and hypothesis linkage."""

    evidence_id: str
    case_id: str
    summary: str = Field(max_length=500)
    category: str
    primary_purpose: str

    collected_at_turn: int
    collected_at: datetime
    collected_by: str

    # Source file linkage
    source_file: Optional[SourceFileReference] = Field(
        None,
        description="Source file this evidence was derived from (null if from user input)"
    )

    # Hypothesis linkage
    related_hypotheses: List[RelatedHypothesis] = Field(default_factory=list)

    # Content
    preprocessed_content: str
    content_size_bytes: int
    analysis: Optional[str] = None
