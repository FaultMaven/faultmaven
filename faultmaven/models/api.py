# File: faultmaven/models/api.py

from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from faultmaven.utils.serialization import to_json_compatible

# Import for type annotations (avoid circular imports)
if TYPE_CHECKING:
    pass

# Import evidence-centric models
from faultmaven.models.llm_schemas import EvidenceRequestToAdd as EvidenceRequest
from faultmaven.modules.case.domain.models import CaseStatus as EvidenceCaseStatus
from faultmaven.modules.case.domain.models import InvestigationStrategy

# --- Enumerations for Explicit Contracts ---


class ResponseType(str, Enum):
    """Defines the agent's primary intent for this turn - v3.0 Response-Format-Driven Design

    9 response formats designed to serve 16 QueryIntent categories (1.8:1 ratio).
    Each format has strict structural requirements for frontend parsing.
    """

    # Core response formats (7 existing)
    ANSWER = "ANSWER"  # Natural prose response
    PLAN_PROPOSAL = "PLAN_PROPOSAL"  # Numbered steps with commands/rationale
    CLARIFICATION_REQUEST = "CLARIFICATION_REQUEST"  # 2-3 specific questions
    CONFIRMATION_REQUEST = "CONFIRMATION_REQUEST"  # Risk warning + yes/no prompt
    SOLUTION_READY = "SOLUTION_READY"  # Root cause + solution sections
    NEEDS_MORE_DATA = "NEEDS_MORE_DATA"  # Diagnostic data request (what, why, how)
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"  # Handoff with summary

    # Visual response formats (2 new in v3.0)
    VISUAL_DIAGRAM = "VISUAL_DIAGRAM"  # Mermaid diagram (architecture, flowcharts)
    COMPARISON_TABLE = (
        "COMPARISON_TABLE"  # Markdown table (feature comparisons, pros/cons)
    )


class SourceType(str, Enum):
    """Defines the origin of a piece of evidence."""

    KNOWLEDGE_BASE = "knowledge_base"
    LOG_FILE = "log_file"
    WEB_SEARCH = "web_search"
    DOCUMENTATION = "documentation"
    PREVIOUS_ANALYSIS = "previous_analysis"
    USER_PROVIDED = "user_provided"


class DataType(str, Enum):
    """12 purpose-driven data classifications for preprocessing pipeline."""

    # Processable types (11)
    LOGS_AND_ERRORS = "logs_and_errors"
    UNSTRUCTURED_TEXT = "unstructured_text"
    STRUCTURED_CONFIG = "structured_config"
    METRICS_AND_PERFORMANCE = "metrics_and_performance"
    SOURCE_CODE = "source_code"
    VISUAL_EVIDENCE = "visual_evidence"

    # New diagnostic data types (5) - aligned with data-classification-strategy.md
    TRACE_DATA = "trace_data"  # Distributed traces (OpenTelemetry, Jaeger, Zipkin)
    PROFILING_DATA = (
        "profiling_data"  # Performance profiling (cProfile, flame graphs, perf)
    )
    ERROR_REPORT = "error_report"  # Standalone exception dumps (Sentry, Bugsnag)
    DOCUMENTATION = "documentation"  # Runbooks, wikis, technical docs
    COMMAND_OUTPUT = (
        "command_output"  # Shell command results (top, ps, iostat, netstat)
    )

    # Reference-only type (1)
    UNANALYZABLE = "unanalyzable"


class ProcessingStatus(str, Enum):
    """Defines the status of data processing operations."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class AuthSessionStatus(str, Enum):
    """Defines the status of authentication sessions (not investigation sessions).

    For investigation session status, see faultmaven.models.investigation_session.SessionStatus
    """

    ACTIVE = "active"
    EXPIRED = "expired"
    TERMINATED = "terminated"


# --- Core Data Structures ---


class Source(BaseModel):
    """Represents a single piece of citable evidence to build user trust."""

    type: SourceType
    content: str
    confidence: float | None = None
    metadata: dict[str, Any] | None = None

    # Verification status for trust badges
    verification_status: Literal["verified", "community", "experimental"] | None = None
    verification_reason: str | None = None  # Tooltip text (e.g., "Reviewed by admin")


class PlanStep(BaseModel):
    """Represents one step in a multi-step plan."""

    description: str


class UploadedData(BaseModel):
    """A strongly-typed model for data uploaded by the user."""

    id: str
    name: str
    type: DataType
    size_bytes: int
    upload_timestamp: str  # UTC ISO 8601 format
    processing_status: ProcessingStatus
    processing_summary: str | None = None
    likelihood: float | None = None


class AvailableAction(BaseModel):
    """Represents an action the user can take in the current context."""

    id: str
    label: str
    description: str
    requires_confirmation: bool = False


class ProgressIndicator(BaseModel):
    """Shows investigation progress to the user."""

    phase: str
    status: Literal["pending", "in_progress", "completed", "failed"]
    description: str
    percentage: int | None = None


class ViewState(BaseModel):
    """
    Comprehensive view state representing the complete frontend rendering state.
    This is the single source of truth for what the frontend should display.
    """

    session_id: str  # Current authentication session
    user: "User"  # User context for authentication
    active_case: Optional["Case"] = None  # Currently active case
    cases: list["Case"] = Field(default_factory=list)  # All user's cases
    messages: list[dict[str, Any]] = Field(
        default_factory=list
    )  # Messages for active case
    uploaded_data: list[UploadedData] = Field(
        default_factory=list
    )  # Data for active case
    show_case_selector: bool = True  # UI hint: show case selector
    show_data_upload: bool = True  # UI hint: show data upload option
    loading_state: str | None = None  # Optional loading message
    memory_context: dict[str, Any] | None = None  # Agent memory context
    planning_state: dict[str, Any] | None = None  # Agent planning state

    # Investigation Progress
    investigation_progress: dict[str, Any] | None = Field(
        default=None,
        description="Investigation progress (milestones, evidence, hypotheses)",
    )


# --- Main Payloads ---


class QueryRequest(BaseModel):
    """The JSON payload sent from the frontend when the user asks a question or submits machine data.
    Note: case_id is provided in the URL path, not in the request body.

    Enhanced in v3.2: Supports both human questions and machine data (logs, errors, alerts)
    """

    session_id: str  # For authentication context
    query: str = Field(
        ...,
        min_length=1,
        max_length=200000,
        description="User query or machine data (max 200KB)",
    )
    context: dict[str, Any] | None = None
    priority: Literal["low", "normal", "medium", "high", "critical"] | None = "normal"
    timestamp: str = Field(
        default_factory=lambda: to_json_compatible(datetime.now(UTC))
    )

    # v3.2 enhancements for machine data support
    query_type: Literal["question", "machine_data"] | None = (
        None  # Hint from UI about content type
    )
    content_type: DataType | None = (
        None  # Optional data type hint (if UI knows it's logs, metrics, etc.)
    )
    is_raw_content: bool | None = (
        False  # True if copy&paste machine data, False if typed question
    )


class AgentResponse(BaseModel):
    """The single, unified JSON payload returned from the backend (v3.1.0 - Evidence-Centric)."""

    model_config = {
        "extra": "allow"
    }  # Allow additional properties for forward compatibility

    schema_version: str = Field(default="3.1.0")
    content: str
    response_type: ResponseType
    session_id: str  # Current authentication session
    case_id: str | None = None
    likelihood: float | None = None
    sources: list[Source] = Field(default_factory=list)
    next_action_hint: str | None = None
    view_state: ViewState | None = None
    plan: list[PlanStep] | None = None

    # EVIDENCE-CENTRIC FIELDS (v3.1.0)
    evidence_requests: list[EvidenceRequest] = Field(
        default_factory=list, description="Active evidence requests for this turn"
    )
    investigation_mode: InvestigationStrategy = Field(
        default=InvestigationStrategy.ACTIVE_INCIDENT,
        description="Current investigation approach (speed vs depth)",
    )
    case_status: EvidenceCaseStatus = Field(
        default=EvidenceCaseStatus.INQUIRY,
        description="Current case investigation state",
    )

    @model_validator(mode="before")
    @classmethod
    def check_plan_consistency(cls, values):
        """Ensures the 'plan' field is only present for a PLAN_PROPOSAL."""
        if isinstance(values, dict):
            response_type, plan = values.get("response_type"), values.get("plan")
            if response_type == ResponseType.PLAN_PROPOSAL and not plan:
                raise ValueError(
                    "A 'plan' must be provided for a PLAN_PROPOSAL response type."
                )
            if response_type != ResponseType.PLAN_PROPOSAL and plan is not None:
                raise ValueError(
                    "A 'plan' can only be provided for a PLAN_PROPOSAL response type."
                )
        return values


# --- Title generation contracts ---


class TitleGenerateRequest(BaseModel):
    """Request payload for conversation title generation."""

    session_id: str
    context: dict[str, Any] | None = None
    # Optional guardrail for word count (accepts null for default)
    max_words: int | None = 8


class TitleResponse(BaseModel):
    """Response payload for title generation."""

    schema_version: Literal["3.1.0"] = "3.1.0"
    title: str
    view_state: ViewState


class SessionErrorCode(str, Enum):
    """Session-specific error codes for better frontend error handling."""

    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_TIMEOUT = "SESSION_TIMEOUT"
    SESSION_INVALID = "SESSION_INVALID"
    SESSION_CREATION_FAILED = "SESSION_CREATION_FAILED"
    INVALID_CLIENT_ID = "INVALID_CLIENT_ID"
    TIMEOUT_OUT_OF_RANGE = "TIMEOUT_OUT_OF_RANGE"


class ErrorDetail(BaseModel):
    """A detailed error message with optional session-specific error codes."""

    code: str  # General error code or SessionErrorCode value
    message: str
    session_id: str | None = None  # Session context for session-related errors
    timeout_info: dict[str, Any] | None = None  # Additional timeout information


class ErrorResponse(BaseModel):
    """The standard JSON payload returned from the backend on failure."""

    schema_version: Literal["3.1.0"] = "3.1.0"
    error: ErrorDetail


# --- New REST Endpoint Models ---


class SessionRequest(BaseModel):
    """Request payload for creating a new session."""

    user_id: str | None = None
    context: dict[str, Any] | None = None


class SessionResponse(BaseModel):
    """Response payload for auth session operations - API spec compliance."""

    schema_version: Literal["3.1.0"] = "3.1.0"
    session_id: str
    user_id: str | None = None
    client_id: str | None = None  # Client/device identifier for session resumption
    status: AuthSessionStatus = AuthSessionStatus.ACTIVE
    created_at: str  # UTC ISO 8601 format
    expires_at: str | None = None  # UTC ISO 8601 format - optional for compliance
    metadata: dict[str, Any] | None = None
    session_resumed: bool | None = (
        None  # Indicates if this was an existing session resumed
    )


class Case(BaseModel):
    """Represents a troubleshooting case."""

    case_id: str  # Match frontend expectations
    title: str
    description: str | None = None
    status: Literal["inquiry", "investigating", "resolved", "closed"] = (
        "inquiry"  # Valid CaseStatus values
    )
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    created_at: str = Field(
        default_factory=lambda: to_json_compatible(datetime.now(UTC))
    )
    updated_at: str = Field(
        default_factory=lambda: to_json_compatible(datetime.now(UTC))
    )
    message_count: int = 0
    owner_id: str  # Case owner user ID - REQUIRED per spec (no session binding)


class CaseRequest(BaseModel):
    """Request payload for creating a new case."""

    title: str
    initial_query: str | None = None
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    context: dict[str, Any] | None = None


class CaseResponse(BaseModel):
    """Response payload for case creation."""

    schema_version: Literal["3.1.0"] = "3.1.0"
    case: Case


class CaseListResponse(BaseModel):
    """Response payload for listing cases."""

    schema_version: Literal["3.1.0"] = "3.1.0"
    cases: list[Case]


class SessionCasesResponse(BaseModel):
    """Response payload for session cases list."""

    schema_version: Literal["3.1.0"] = "3.1.0"
    cases: list[Case]
    total: int


# --- Authentication Models ---


class User(BaseModel):
    """Represents a user in the system."""

    user_id: str
    email: str
    name: str
    created_at: str = Field(
        default_factory=lambda: to_json_compatible(datetime.now(UTC))
    )
    last_login: str | None = None


class DevLoginRequest(BaseModel):
    """Request payload for developer login."""

    username: str


class AuthResponse(BaseModel):
    """Response payload for authentication operations with ViewState."""

    schema_version: Literal["3.1.0"] = "3.1.0"
    success: bool = True
    view_state: ViewState


class DataUploadRequest(BaseModel):
    """Request payload for data upload (multipart form data)."""

    description: str | None = None
    expected_type: DataType | None = None
    context: dict[str, Any] | None = None


# --- API Compliance Response Models ---


class KnowledgeBaseDocument(BaseModel):
    """Response model for knowledge base document operations."""

    document_id: str
    title: str
    content: str
    document_type: str
    category: str | None = None
    status: str = "processed"
    tags: list[str] = Field(default_factory=list)
    source_url: str | None = None
    scope: str = "global"
    owner_id: str | None = None
    team_id: str | None = None
    created_at: str
    updated_at: str
    metadata: dict[str, Any] | None = None

    # Verification status (0=experimental, 1=community, 2=admin_verified)
    verification_level: int = 0
    verification_status: Literal["verified", "community", "experimental"] | None = (
        "experimental"
    )
    verification_reason: str | None = None

    # Lineage tracking
    source_suggestion_id: str | None = None  # Link to originating suggestion


class DocumentSnippetResponse(BaseModel):
    """Response model for document snippet (hover card preview).

    Supports both line-based and semantic snippet extraction.
    """

    document_id: str
    title: str
    snippet: str
    line_range: tuple[int, int] | None = None  # (start, end) line numbers
    total_lines: int
    document_type: str
    verification_status: Literal["verified", "community", "experimental"] = (
        "experimental"
    )
    verification_level: int = 0

    # Semantic context (when query_string is provided)
    relevance_score: float | None = None  # How relevant is this snippet to the query


# --- Knowledge Suggestion Models ---


class SuggestionLineage(BaseModel):
    """Lineage information for knowledge suggestions.

    Displayed in Review Inbox footer:
    "Extracted from Case #402 (prod-db-latency) • 2h ago"
    """

    case_id: str
    case_title: str
    extracted_by: str  # Email or username
    extracted_at: str  # UTC ISO 8601


class KnowledgeSuggestionSummary(BaseModel):
    """Summary view of a knowledge suggestion for list endpoints."""

    suggestion_id: str
    title: str
    content_preview: str  # First ~200 chars
    status: Literal["pending_review", "approved", "rejected", "draft"]
    verification_status: str = "experimental"  # Always experimental until approved
    pii_scan_status: Literal[
        "not_scanned", "scanning", "clean", "pii_detected", "remediated", "scan_failed"
    ]
    suggested_type: str
    created_at: str
    lineage: SuggestionLineage


class KnowledgeSuggestionDetail(BaseModel):
    """Full detail view of a knowledge suggestion."""

    suggestion_id: str
    organization_id: str
    case_id: str
    status: Literal["pending_review", "approved", "rejected", "draft"]

    # Content
    suggested_title: str
    suggested_content: str
    suggested_type: str

    # Extraction info
    extracted_by: str
    extracted_at: str
    include_messages: bool
    include_evidence: bool

    # PII scanning
    pii_scan_status: Literal[
        "not_scanned", "scanning", "clean", "pii_detected", "remediated", "scan_failed"
    ]
    pii_scan_result: dict[str, Any] | None = None
    pii_remediated_by: str | None = None
    pii_remediated_at: str | None = None

    # Lineage
    lineage: SuggestionLineage

    # Review
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    review_notes: str | None = None
    rejection_reason: str | None = None

    # Bidirectional link
    knowledge_item_id: str | None = None

    # Timestamps
    created_at: str
    updated_at: str
    metadata: dict[str, Any] | None = None


class SuggestionListResponse(BaseModel):
    """Paginated list of knowledge suggestions."""

    suggestions: list[KnowledgeSuggestionSummary]
    total_count: int
    limit: int
    offset: int


class KnowledgeExtractionRequest(BaseModel):
    """Request body for extracting knowledge from a case."""

    include_messages: bool = True
    include_evidence: bool = True
    title_suggestion: str | None = None


class KnowledgeExtractionResponse(BaseModel):
    """Response from knowledge extraction endpoint."""

    suggestion_id: str
    case_id: str
    status: str = "pending_review"
    suggested_title: str
    suggested_content: str
    pii_scan_status: str
    extracted_from: dict[str, Any]  # case_title, message_count, evidence_count
    created_at: str


class SuggestionApproveRequest(BaseModel):
    """Request body for approving a suggestion."""

    review_notes: str | None = None


class SuggestionRejectRequest(BaseModel):
    """Request body for rejecting a suggestion."""

    rejection_reason: str
    review_notes: str | None = None


class SuggestionUpdateRequest(BaseModel):
    """Request body for updating a suggestion's content."""

    title: str | None = None
    content: str | None = None
    suggested_type: str | None = None


class StandardErrorResponse(BaseModel):
    """Standard error response model with correlation tracking."""

    detail: str
    error_type: str
    correlation_id: str
    timestamp: str


class JobStatus(BaseModel):
    """Async job status tracking model."""

    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    progress: int | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class QueryJobStatus(BaseModel):
    """Case-scoped query job status tracking model."""

    query_id: str
    case_id: str
    status: Literal[
        "pending", "processing", "running", "completed", "failed", "cancelled"
    ]
    progress_percentage: int | None = Field(
        None, ge=0, le=100, description="Processing progress percentage"
    )
    started_at: str | None = Field(None, description="Job start time (UTC ISO 8601)")
    last_updated_at: str = Field(
        default_factory=lambda: to_json_compatible(datetime.now(UTC))
    )
    error: dict[str, Any] | None = Field(
        None, description="Error details if status is failed"
    )
    result: AgentResponse | None = Field(None, description="Final result if completed")


class CaseQuerySummary(BaseModel):
    """Summary information for case queries."""

    query_id: str
    case_id: str
    status: Literal[
        "pending", "processing", "running", "completed", "failed", "cancelled"
    ]
    created_at: str
    last_updated_at: str = Field(
        default_factory=lambda: to_json_compatible(datetime.now(UTC))
    )


class CaseSummary(BaseModel):
    """Summary information for cases (used in listings)."""

    case_id: str
    title: str
    description: str | None = None
    status: str
    priority: str
    created_at: str
    updated_at: str
    owner_id: str | None = None
    message_count: int | None = Field(
        None, description="Number of messages/queries in case"
    )
    last_activity_at: str | None = Field(None, description="Last activity timestamp")
    session_id: str | None = None


class Message(BaseModel):
    """Message model for conversation endpoints.

    Schema matches case-storage-design.md Section 4.7 (case_messages table).
    """

    message_id: str
    turn_number: int = Field(
        ..., description="Turn number in conversation (user messages increment turn)"
    )
    role: Literal["user", "agent", "assistant", "system"]
    content: str
    created_at: str = Field(
        ..., description="ISO 8601 datetime string (matches SQL schema)"
    )

    # Optional fields
    author_id: str | None = Field(None, description="User who created the message")
    token_count: int | None = Field(None, description="Number of tokens in content")
    metadata: dict[str, Any] | None = Field(
        None, description="Sources, tools used, etc."
    )


class MessageRetrievalDebugInfo(BaseModel):
    """Debug information for message retrieval operations."""

    redis_key: str = Field(..., description="Redis key used for message storage")
    redis_operation_time_ms: float = Field(
        ..., description="Time taken for Redis operation"
    )
    storage_errors: list[str] = Field(
        default_factory=list, description="Any storage-related errors encountered"
    )
    message_parsing_errors: int = Field(
        default=0, description="Number of messages that failed to parse"
    )


class CaseMessagesResponse(BaseModel):
    """Enhanced response model for case message retrieval with debugging support."""

    messages: list[Message] = Field(..., description="Array of conversation messages")
    total_count: int = Field(..., description="Total number of messages in the case")
    retrieved_count: int = Field(
        ..., description="Number of messages successfully retrieved"
    )
    has_more: bool = Field(
        ..., description="Whether more messages are available for pagination"
    )
    next_offset: int | None = Field(
        None, description="Offset for next page (null if no more pages)"
    )
    debug_info: MessageRetrievalDebugInfo | None = Field(
        None, description="Debug information (only when include_debug=true)"
    )


class TitleGenerateResponse(BaseModel):
    """Response model for title generation."""

    case_id: str
    generated_title: str
    success: bool = True
    message: str = "Title generated successfully"


class TitleResponse(BaseModel):
    """Simplified title response schema per API spec."""

    schema_version: str = "3.1.0"
    title: str


class SimpleAgentResponse(BaseModel):
    """Simplified AgentResponse schema per API spec."""

    response_type: ResponseType
    content: str
    session_id: str
    case_id: str | None = None
    likelihood: float | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)
    next_action_hint: str | None = None


# --- Data Preprocessing Models ---


class SourceMetadata(BaseModel):
    """Metadata about data source origin, used for classification boosting."""

    source_type: Literal["file_upload", "text_paste", "page_capture"]
    source_url: str | None = Field(None, description="URL if from page capture")


class ClassificationResult(BaseModel):
    """
    Result of data type classification

    Used by the classification service to return classification decision
    with confidence scoring and metadata about how the classification was made.
    """

    data_type: DataType = Field(..., description="Classified data type")
    confidence: float = Field(
        ..., description="Classification confidence score (0.0-1.0)", ge=0.0, le=1.0
    )
    source: Literal[
        "user_override",
        "agent_hint",
        "browser_context",
        "source_url",
        "rule_based",
        "rule_based_best_effort",
    ] = Field(..., description="How the classification was determined")
    classification_failed: bool = Field(
        default=False,
        description="True if confidence below threshold, triggers user fallback modal",
    )
    suggested_types: list[DataType] | None = Field(
        None, description="Suggested data types for ambiguous cases (user fallback)"
    )
    source_type: str | None = Field(
        None,
        description="Origin of the content: page_capture, user_paste, file_upload. "
        "Propagated from Attachment.source_metadata when available.",
    )


class ExtractionMetadata(BaseModel):
    """
    Metadata about the extraction process

    Tracks how data was extracted, which strategy was used,
    and performance metrics for the extraction operation.
    """

    data_type: DataType = Field(..., description="Data type being extracted")
    extraction_strategy: Literal[
        "crime_scene",
        "map_reduce",
        "direct",
        "vision",
        "statistical",
        "ast_parse",
        "none",
        "classification_failed",
        "trace_correlation",
        "profiling_hotspot",
        "exception_context",
        "documentation_structure",
        "command_parsing",
    ] = Field(..., description="Strategy used for extraction")
    llm_calls_used: int = Field(
        ..., description="Number of LLM calls used during extraction", ge=0
    )
    confidence: float = Field(
        ...,
        description="Classification confidence (from ClassificationResult)",
        ge=0.0,
        le=1.0,
    )
    source: str = Field(
        ..., description="Classification source (from ClassificationResult)"
    )
    processing_time_ms: float = Field(
        ..., description="Time taken for extraction in milliseconds", ge=0.0
    )


class PreprocessedData(BaseModel):
    """
    Output from preprocessing pipeline - LLM-ready format

    This is the bridge between raw uploaded data and LLM analysis.
    The 'content' field contains the extracted, formatted representation
    of the original data, optimized for LLM context consumption.
    """

    # LLM-ready content (KEY OUTPUT)
    content: str = Field(
        ...,
        description="Extracted/formatted content ready for LLM analysis",
        max_length=200000,  # Safety limit - supports up to ~50K tokens for Claude/GPT-4
    )

    # Extraction metadata
    metadata: ExtractionMetadata = Field(
        ..., description="Metadata about extraction process"
    )

    # Size metrics
    original_size: int = Field(..., description="Original content size in bytes")
    processed_size: int = Field(..., description="Processed content size in characters")

    # Security flags
    security_flags: list[str] = Field(
        default_factory=list,
        description="Security issues detected (pii_detected, secrets_found, etc.)",
    )

    # Source information (optional)
    source_metadata: SourceMetadata | None = Field(
        None, description="Metadata about data source (file, text paste, page capture)"
    )

    # Structured insights (optional) - for advanced features like timeline extraction
    insights: dict[str, Any] | None = Field(
        None,
        description="Structured insights extracted during preprocessing (errors, anomalies, metrics, etc.)",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "content": "[ERROR] Connection timeout...\n[CRITICAL] Database failed...",
                "metadata": {
                    "data_type": "logs_and_errors",
                    "extraction_strategy": "crime_scene",
                    "llm_calls_used": 0,
                    "confidence": 0.98,
                    "source": "rule_based",
                    "processing_time_ms": 45.2,
                },
                "original_size": 45000,
                "processed_size": 7800,
                "security_flags": ["api_key_detected"],
                "source_metadata": {"source_type": "file_upload"},
                "insights": {
                    "top_errors": [{"message": "Connection timeout", "count": 15}],
                    "anomalies": ["Spike in error rate at 14:32"],
                },
            }
        }
