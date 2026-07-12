"""API Request/Response Models for Case Management.

These models provide a clean API layer separate from the domain Case model.
They handle:
- Request validation
- Response serialization
- API versioning
- Backward compatibility
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    InvestigationStage,
)
from faultmaven.modules.case.domain.services.case_action_manager import (
    CaseActionManager,
)

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
        max_length=200,
    )

    description: Optional[str] = Field(
        default="", description="Initial problem description", max_length=2000
    )

    initial_message: Optional[str] = Field(
        default=None,
        description="First user message (for INQUIRY phase)",
        max_length=4000,
    )

    session_id: Optional[str] = Field(
        default=None,
        description="Session ID for authentication and case association (restored from old implementation)",
    )

    # Note: user_id and organization_id are derived from authentication context
    # They are NOT part of the request body to prevent spoofing


class CaseUpdateRequest(BaseModel):
    """Request to update an existing case."""

    title: Optional[str] = Field(
        default=None, description="Updated title", max_length=200
    )

    description: Optional[str] = Field(
        default=None, description="Updated description", max_length=2000
    )

    state: Optional[CaseState] = Field(
        default=None, description="Updated state (admin only)"
    )


# ============================================================
# Case Responses (for API)
# ============================================================


class CaseSummary(BaseModel):
    """Minimal case information for list views."""

    case_id: str
    title: str
    description: str
    state: CaseState
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    resolved_at: Optional[datetime]
    closed_at: Optional[datetime]
    user_id: str
    organization_id: str
    closure_reason: Optional[str]

    # Progress indicators
    current_turn: int
    milestones_completed: int
    total_milestones: int = 8

    # Computed fields
    is_terminal: bool

    # Status transitions
    valid_next_states: List[str] = Field(
        default_factory=list,
        description="Allowed state transitions from current state for user-initiated changes",
    )

    @classmethod
    def from_case(cls, case: Case) -> "CaseSummary":
        """Convert Case domain model to API summary."""
        return cls(
            case_id=case.case_id,
            title=case.title,
            description=case.description,
            state=case.state,
            created_at=case.created_at,
            updated_at=case.updated_at,
            last_activity_at=case.last_activity_at,
            resolved_at=case.resolved_at,
            closed_at=case.closed_at,
            user_id=case.user_id,
            organization_id=case.organization_id,
            closure_reason=case.closure_reason,
            current_turn=case.current_turn,
            milestones_completed=len(case.progress.completed_milestones),
            total_milestones=8,
            is_terminal=case.is_terminal,
            valid_next_states=[
                status.value
                for status in CaseActionManager.get_allowed_transitions(case.state)
            ],
        )


class CaseDetail(BaseModel):
    """Detailed case information for single case view."""

    case_id: str
    title: str
    description: str
    state: CaseState
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    resolved_at: Optional[datetime]
    closed_at: Optional[datetime]

    user_id: str
    organization_id: str
    closure_reason: Optional[str]

    # Progress
    current_turn: int
    turns_without_progress: int
    current_stage: Optional[InvestigationStage]

    # Milestones
    milestones_completed: List[str]
    pending_milestones: List[str]

    # Counts
    evidence_count: int
    hypothesis_count: int
    solution_count: int

    # Flags
    is_terminal: bool
    escalated: bool

    # Status transitions
    valid_next_states: List[str] = Field(
        default_factory=list,
        description="Allowed state transitions from current state for user-initiated changes",
    )

    @classmethod
    def from_case(cls, case: Case) -> "CaseDetail":
        """Convert Case domain model to API detail view."""
        return cls(
            case_id=case.case_id,
            title=case.title,
            description=case.description,
            state=case.state,
            created_at=case.created_at,
            updated_at=case.updated_at,
            last_activity_at=case.last_activity_at,
            resolved_at=case.resolved_at,
            closed_at=case.closed_at,
            user_id=case.user_id,
            organization_id=case.organization_id,
            closure_reason=case.closure_reason,
            current_turn=case.current_turn,
            turns_without_progress=case.turns_without_progress,
            current_stage=case.current_stage,
            milestones_completed=case.progress.completed_milestones,
            pending_milestones=case.progress.pending_milestones,
            evidence_count=len(case.evidence),
            hypothesis_count=len(case.hypotheses),
            solution_count=len(case.solutions),
            is_terminal=case.is_terminal,
            escalated=(
                case.escalation_state.is_active if case.escalation_state else False
            ),
            valid_next_states=[
                status.value
                for status in CaseActionManager.get_allowed_transitions(case.state)
            ],
        )


# ============================================================
# List and Filter
# ============================================================


class CaseListFilter(BaseModel):
    """Filter criteria for listing cases."""

    user_id: Optional[str] = Field(default=None, description="Filter by user ID")

    organization_id: Optional[str] = Field(
        default=None, description="Filter by organization ID"
    )

    state: Optional[CaseState] = Field(default=None, description="Filter by state")

    created_after: Optional[datetime] = Field(
        default=None, description="Cases created after this date"
    )

    created_before: Optional[datetime] = Field(
        default=None, description="Cases created before this date"
    )

    limit: int = Field(
        default=50, ge=1, le=200, description="Maximum results to return"
    )

    offset: int = Field(default=0, ge=0, description="Pagination offset")

    include_empty: bool = Field(
        default=True,
        description="Include cases with no conversation (current_turn == 0)",
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
        cls, cases: List[Case], total_count: int, limit: int, offset: int
    ) -> "CaseListResponse":
        """Convert list of Cases to API response."""
        return cls(
            cases=[CaseSummary.from_case(case) for case in cases],
            total_count=total_count,
            limit=limit,
            offset=offset,
            has_more=(offset + len(cases)) < total_count,
        )


# ============================================================
# Search
# ============================================================


class CaseSearchRequest(BaseModel):
    """Request to search cases."""

    query: str = Field(description="Search query", min_length=1, max_length=500)

    user_id: Optional[str] = Field(default=None, description="Limit to user's cases")

    organization_id: Optional[str] = Field(
        default=None, description="Limit to organization's cases"
    )

    state: Optional[CaseState] = Field(default=None, description="Filter by state")

    limit: int = Field(default=20, ge=1, le=100, description="Maximum results")


class CaseSearchResponse(BaseModel):
    """Response for case search."""

    cases: List[CaseSummary]
    total_count: int
    query: str

    @classmethod
    def from_cases(
        cls, cases: List[Case], total_count: int, query: str
    ) -> "CaseSearchResponse":
        """Convert search results to API response."""
        return cls(
            cases=[CaseSummary.from_case(case) for case in cases],
            total_count=total_count,
            query=query,
        )


# ============================================================
# Case Query Submission with Intent-Based Routing
# ============================================================


class IntentType(str, Enum):
    """Intent types for query routing.

    Enables reliable intent detection without keyword matching.
    Each type routes to specialized handling logic.
    """

    CONVERSATION = "conversation"  # Natural language query - use LLM
    STATUS_TRANSITION = "status_transition"  # Explicit state transition (resolve/close)
    HYPOTHESIS_ACTION = "hypothesis_action"  # Validate/refute/retire hypothesis
    EVIDENCE_NEED = "evidence_need"  # User-initiated action on a persistent EvidenceNeed (stays NOT_IMPLEMENTED until a frontend feature requires it)
    CONFIRMATION = "confirmation"  # Yes/No confirmation response
    GREETING = "greeting"  # Heuristic greeting response
    FILE_RECLASSIFICATION = "file_reclassification"  # Resolve classification_failed: re-run preprocessing on an UploadedFile under a user-chosen DataType


class QueryIntent(BaseModel):
    """Structured intent metadata for programmatic query handling.

    Enables reliable intent detection without keyword matching.
    All queries must specify their intent type for proper routing.

    Design Reference: Intent-based routing eliminates ambiguity in pattern matching
    and provides single code path for all interactions (conversation history unified).
    """

    type: IntentType = Field(
        description="Intent type for routing - determines which handler processes this query"
    )

    # Additional intent-specific fields (vary by type)
    from_state: Optional[CaseState] = Field(
        default=None, description="For status_transition: source state (validation)"
    )
    to_state: Optional[CaseState] = Field(
        default=None,
        description="For status_transition: target state (REQUIRED for status_transition)",
    )
    user_confirmed: Optional[bool] = Field(
        default=None,
        description="User explicitly confirmed action via UI button/dialog",
    )
    hypothesis_id: Optional[str] = Field(
        default=None, description="For hypothesis_action: target hypothesis ID"
    )
    action: Optional[str] = Field(
        default=None, description="Action to perform: validate | refute | retire"
    )
    evidence_need_id: Optional[str] = Field(
        default=None,
        description=(
            "For evidence_need: target EvidenceNeed ID "
            "(format: ``eneed_xxxxxxxxxxxx``)."
        ),
    )
    confirmation_value: Optional[bool] = Field(
        default=None, description="For confirmation: yes/no value"
    )
    file_id: Optional[str] = Field(
        default=None,
        description=(
            "For file_reclassification: target UploadedFile ID "
            "(format: ``file_xxxxxxxxxxxx``)."
        ),
    )
    data_type: Optional[str] = Field(
        default=None,
        description=(
            "For file_reclassification: target DataType enum value "
            "(e.g. 'logs_and_errors', 'structured_config')."
        ),
    )

    @model_validator(mode="after")
    def validate_intent_fields(self):
        """Ensure required fields present for each intent type."""
        if self.type == IntentType.STATUS_TRANSITION:
            if not self.to_state:
                raise ValueError("to_state required for status_transition intent")
        elif self.type == IntentType.HYPOTHESIS_ACTION:
            if not self.hypothesis_id or not self.action:
                raise ValueError(
                    "hypothesis_id and action required for hypothesis_action intent"
                )
        elif self.type == IntentType.EVIDENCE_NEED:
            if not self.evidence_need_id:
                raise ValueError("evidence_need_id required for evidence_need intent")
        elif self.type == IntentType.CONFIRMATION:
            if self.confirmation_value is None:
                raise ValueError("confirmation_value required for confirmation intent")
        elif self.type == IntentType.FILE_RECLASSIFICATION:
            if not self.file_id or not self.data_type:
                raise ValueError(
                    "file_id and data_type required for file_reclassification intent"
                )
        return self


# ============================================================
# Unified Turn Response (v4.1)
# ============================================================


class AttachmentResult(BaseModel):
    """Result of preprocessing a single attachment.

    Post-010 strict evidence model: file uploads no longer create an
    Evidence row at intake — they create an UploadedFile row.
    ``file_id`` is the uploaded_files row identifier (frontend uses it
    to reference the attachment; Evidence rows that later reference
    this file via ``source_file_id`` are born during INVESTIGATING
    when the LLM extracts claim-anchored slices).
    """

    file_id: str = Field(
        description="UploadedFile row identifier for this attachment.",
    )
    filename: str
    file_size: int
    processing_status: str
    uploaded_at: str = Field(
        default="",
        description="ISO 8601 timestamp of when the attachment was processed",
    )
    source_type: str = Field(
        description=(
            "Data-type classification from preprocessing: logs | metrics | "
            "configuration | code | text | image"
        ),
    )
    upload_source: str = Field(
        default="file_upload",
        description="Input origin: file_upload | paste | page_capture | screenshot | agent_generated",
    )
    duplicate_of: Optional[str] = Field(
        default=None,
        description=(
            "If set, this attachment was a per-case content-hash duplicate "
            "of an earlier upload. No new UploadedFile was created; this "
            "field carries the existing file_id. Frontend should render a "
            "non-blocking toast."
        ),
    )
    duplicate_turn: Optional[int] = Field(
        default=None,
        description="Turn number where the original was uploaded (for toast text).",
    )


class SuggestedActionResponse(BaseModel):
    """A follow-up suggestion returned with agent responses."""

    label: str
    type: str  # "DECIDE" | "RUN" | "EVIDENCE" | "FREE_SPEECH"
    payload: Optional[str] = (
        None  # DECIDE: text a click sends; RUN: command a click copies
    )
    body: Optional[str] = None
    hints: Optional[List[str]] = None  # FREE_SPEECH: short framework tags
    intent: Optional[Dict[str, Any]] = (
        None  # QueryIntent metadata for deterministic routing
    )
    # Phase 6: EVIDENCE-type suggestions derived from an open EvidenceNeed
    # carry the source need ID for frontend visual linkage (highlight /
    # dismiss / group). The engine resolves any same-turn ``new_index_N``
    # placeholders against ``metadata["evidence_needs_updated"]`` before
    # this field is populated; unresolvable refs are dropped silently.
    evidence_need_id: Optional[str] = None


class ProgressTransparencyInfo(BaseModel):
    """Progress transparency state for the current turn.

    When active, the investigation has stalled (N investigative turns
    without milestone progress) and the agent is surfacing what evidence
    is needed to advance. The frontend should visually highlight the
    pending milestone.

    See: docs/architecture/investigation-engine/progress-transparency.md
    """

    active: bool = Field(description="Whether transparent mode is active this turn")
    pending_milestone: Optional[str] = Field(
        default=None,
        description="Milestone that progress is stalled on (e.g., 'root_cause_identified')",
    )
    milestone_description: Optional[str] = Field(
        default=None,
        description="Human-readable description of what the pending milestone requires",
    )
    repair_type: Optional[str] = Field(
        default=None,
        description="Agent state repair pattern detected, if any: "
        "hypothesis_anchoring, hypothesis_deadlock, exhausted, "
        "fix_failure_cycle, action_loop",
    )
    verification_status: Optional[str] = Field(
        default=None,
        description="Engine-derived verification status this turn — the "
        "grounding × progress join (healthy | treatment_blocked | open | "
        "not_yet_productive | insufficient_evidence). Lets the frontend show the "
        "honest partial outcome (e.g. insufficient_evidence) alongside the "
        "stalled-milestone surfacing.",
    )
    cause_assurance: Optional[str] = Field(
        default=None,
        description="Engine-derived assurance grade behind the identified cause "
        "(no_root | mechanistic | confirmed). Lets the frontend label a "
        "lower-assurance conclusion — e.g. an identified-but-unconfirmed cause — "
        "instead of presenting every conclusion at equal certainty.",
    )


class TurnResponse(BaseModel):
    """Response for POST /cases/{id}/turns."""

    agent_response: str
    turn_number: int
    milestones_completed: List[str]
    case_state: CaseState
    progress_made: bool
    attachments_processed: List[AttachmentResult] = Field(default_factory=list)
    suggested_actions: List[SuggestedActionResponse] = Field(default_factory=list)
    progress_transparency: Optional[ProgressTransparencyInfo] = Field(
        default=None,
        description="Progress transparency state. Present when investigation "
        "has stalled and agent is surfacing milestone dependencies.",
    )


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
    created_at: datetime = Field(
        description="Message creation time (matches SQL schema)"
    )

    # Optional fields
    author_id: Optional[str] = Field(None, description="User who created the message")
    token_count: Optional[int] = Field(None, description="Number of tokens in content")
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Sources, tools used, etc."
    )

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
    source_type: str = Field(
        description="file_upload | paste | screenshot | page_injection | agent_generated"
    )
    analysis_status: str = Field(
        description="pending | processing | completed | failed"
    )
    summary: Optional[str] = Field(
        default=None, description="AI-generated summary (1-2 sentences)"
    )
    source_metadata: Optional[dict] = Field(
        default=None, description="Source origin metadata (e.g. page capture URL)"
    )

    @classmethod
    def from_uploaded_file(cls, uploaded_file) -> "UploadedFileMetadata":
        """Convert UploadedFile model to UploadedFileMetadata."""
        from faultmaven.modules.case.contracts import UploadedFile

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
            source_type=uploaded_file.upload_source,
            analysis_status="completed",  # Always completed after preprocessing
            # summary is the case-scoped Evidence.summary, not on the upload
            # row anymore. Callers with case context can populate it via
            # case.find_evidence_for_file(...) (or by traversing case.evidence
            # for source_file_id == file_id) and pass it via a follow-up
            # field-level update.
            summary=None,
            source_metadata=None,
        )

    # `from_evidence` (legacy) was removed: it relied on the dropped
    # `evidence.content_size_bytes` and `evidence.collected_at`-as-
    # `uploaded_at` shim. Callers should always go through
    # `from_uploaded_file` now (Evidence and UploadedFile are distinct
    # entities post-redesign).


class HypothesisRelationship(BaseModel):
    """How a file relates to a hypothesis."""

    hypothesis_id: str
    hypothesis_description: str
    stance: str = Field(
        description="strongly_supports | supports | neutral | contradicts | strongly_contradicts | irrelevant"
    )
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

    full_analysis: Optional[FileAnalysis] = Field(
        default=None, description="Detailed AI analysis"
    )
    hypothesis_relationships: Optional[List[HypothesisRelationship]] = Field(
        default=None,
        description="How this file relates to hypotheses (investigating phase only)",
    )

    @classmethod
    def from_uploaded_file(cls, uploaded_file, case_id: str) -> "UploadedFileDetails":
        """Convert UploadedFile to UploadedFileDetails (INQUIRY phase - no hypotheses yet)."""
        # Start with base metadata
        base = UploadedFileMetadata.from_uploaded_file(uploaded_file)

        # No file-level summary on the upload row anymore; key_findings
        # for the INQUIRY-phase response is just empty until analysis runs.
        # The case-scoped summary lives on the linked Evidence row.
        full_analysis = FileAnalysis(
            key_findings=[],
            relevance=None,  # No analysis yet in INQUIRY phase
        )

        return cls(
            **base.model_dump(),
            full_analysis=full_analysis,
            hypothesis_relationships=None,  # No hypotheses in INQUIRY phase
        )

    @classmethod
    def from_evidence(
        cls, evidence, case_id: str, hypotheses: Optional[dict] = None
    ) -> "UploadedFileDetails":
        """Convert Evidence to UploadedFileDetails with full analysis (INVESTIGATING phase)."""
        # Start with base metadata
        base = UploadedFileMetadata.from_evidence(evidence)

        # Build full analysis
        full_analysis = FileAnalysis(
            key_findings=[evidence.summary] if evidence.summary else [],
            relevance=evidence.analysis if evidence.analysis else None,
        )

        # Build hypothesis relationships if hypotheses provided
        hypothesis_relationships = None
        if hypotheses and evidence.primary_purpose:
            # Find hypotheses related to this evidence
            relationships = []
            for hyp_id, hypothesis in hypotheses.items():
                if not hasattr(hypothesis, "evidence_links"):
                    continue
                link = next(
                    (
                        l
                        for l in hypothesis.evidence_links
                        if l.evidence_id == evidence.evidence_id
                    ),
                    None,
                )
                if link is not None:
                    relationships.append(
                        HypothesisRelationship(
                            hypothesis_id=hyp_id,
                            hypothesis_description=hypothesis.statement,
                            stance=link.stance.value,
                            reasoning=link.reasoning,
                        )
                    )
            if relationships:
                hypothesis_relationships = relationships

        return cls(
            **base.model_dump(),
            full_analysis=full_analysis,
            hypothesis_relationships=hypothesis_relationships,
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
    category: str = Field(
        description="SYMPTOM_EVIDENCE | CAUSAL_EVIDENCE | SYMPTOM_ABSENCE_EVIDENCE | CAUSAL_ABSENCE_EVIDENCE | OTHER"
    )
    collected_at_turn: int
    source_type: str = Field(
        description="LOGS | METRICS | CONFIGURATION | CODE | TEXT | IMAGE"
    )
    primary_purpose: Optional[str] = None
    related_hypothesis_ids: List[str] = Field(default_factory=list)


class UploadedFileDetailsResponse(BaseModel):
    """Detailed information about an uploaded file with evidence linkage."""

    file_id: str
    filename: str
    size_bytes: int
    size_display: str
    content_type: Optional[str] = Field(
        default=None, description="MIME type as reported on upload"
    )
    content_hash: Optional[str] = Field(
        default=None, description="SHA-256 of file contents (storage-backend dedup)"
    )
    uploaded_at_turn: int
    uploaded_at: datetime
    upload_source: str = Field(
        description="Provenance: file_upload | paste | screenshot | page_capture | agent_generated | conversion_source"
    )
    summary: Optional[str] = Field(
        default=None,
        description="File-level preprocessing summary, set by the ingestion pipeline.",
    )

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
        description=(
            "Source file this evidence was derived from. NULL only when "
            "the evidence is a verbatim quote extracted from the user's "
            "chat message (source_type=USER_DESCRIPTION)."
        ),
    )

    # Hypothesis linkage
    related_hypotheses: List[RelatedHypothesis] = Field(default_factory=list)

    # Content
    extract: Optional[str] = Field(
        default=None,
        description=(
            "Optional verbatim quote backing the summary. NULL when the "
            "LLM omitted it (the summary is self-contained)."
        ),
    )
    analysis: Optional[str] = None


class CaseEvidenceListResponse(BaseModel):
    """All evidence records for a case, each with source and hypothesis linkage."""

    case_id: str
    total_count: int
    evidence: List[EvidenceDetailsResponse]
