# File: faultmaven/models/api.py

from pydantic import BaseModel, Field, model_validator, field_validator
from typing import List, Optional, Dict, Any, Literal, TYPE_CHECKING
from enum import Enum
import datetime

# Import for type annotations (avoid circular imports)
if TYPE_CHECKING:
    from faultmaven.models.doctor_patient import SuggestedAction

# Import evidence-centric models
from faultmaven.models.evidence import (
    EvidenceRequest,
    InvestigationMode,
    CaseStatus as EvidenceCaseStatus,
)

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
    COMPARISON_TABLE = "COMPARISON_TABLE"  # Markdown table (feature comparisons, pros/cons)

class SourceType(str, Enum):
    """Defines the origin of a piece of evidence."""
    KNOWLEDGE_BASE = "knowledge_base"
    LOG_FILE = "log_file"
    WEB_SEARCH = "web_search"
    DOCUMENTATION = "documentation"
    PREVIOUS_ANALYSIS = "previous_analysis"
    USER_PROVIDED = "user_provided"

class DataType(str, Enum):
    """Defines the type of data uploaded by users."""
    LOG_FILE = "log_file"
    CONFIG_FILE = "config_file"
    ERROR_REPORT = "error_report"
    DOCUMENTATION = "documentation"
    SCREENSHOT = "screenshot"
    OTHER = "other"

class ProcessingStatus(str, Enum):
    """Defines the status of data processing operations."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class SessionStatus(str, Enum):
    """Defines the status of user sessions."""
    ACTIVE = "active"
    EXPIRED = "expired"
    TERMINATED = "terminated"

# --- Core Data Structures ---

class Source(BaseModel):
    """Represents a single piece of citable evidence to build user trust."""
    type: SourceType
    content: str
    confidence: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None

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
    processing_summary: Optional[str] = None
    confidence_score: Optional[float] = None

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
    percentage: Optional[int] = None

class ViewState(BaseModel):
    """
    Comprehensive view state representing the complete frontend rendering state.
    This is the single source of truth for what the frontend should display.
    """
    session_id: str
    user: "User"  # User context for authentication
    active_case: Optional["Case"] = None  # Currently active case
    cases: List["Case"] = Field(default_factory=list)  # All user's cases
    messages: List[Dict[str, Any]] = Field(default_factory=list)  # Messages for active case
    uploaded_data: List[UploadedData] = Field(default_factory=list)  # Data for active case
    show_case_selector: bool = True  # UI hint: show case selector
    show_data_upload: bool = True   # UI hint: show data upload option
    loading_state: Optional[str] = None  # Optional loading message
    memory_context: Optional[Dict[str, Any]] = None  # Agent memory context
    planning_state: Optional[Dict[str, Any]] = None  # Agent planning state

    # OODA Framework Progress (v3.2.0)
    investigation_progress: Optional[Dict[str, Any]] = Field(
        default=None,
        description="OODA investigation progress (phase, iteration, hypotheses)"
    )

# --- Main Payloads ---

class QueryRequest(BaseModel):
    """The JSON payload sent from the frontend when the user asks a question.
    Note: case_id is provided in the URL path, not in the request body.
    """
    session_id: str
    query: str
    context: Optional[Dict[str, Any]] = None
    priority: Literal["low", "normal", "medium", "high", "critical"] = "normal"
    timestamp: str = Field(default_factory=lambda: datetime.datetime.utcnow().isoformat() + 'Z')

class AgentResponse(BaseModel):
    """The single, unified JSON payload returned from the backend (v3.1.0 - Evidence-Centric)."""
    model_config = {"extra": "allow"}  # Allow additional properties for forward compatibility

    schema_version: str = Field(default="3.1.0")
    content: str
    response_type: ResponseType
    session_id: str
    case_id: Optional[str] = None
    confidence_score: Optional[float] = None
    sources: List[Source] = Field(default_factory=list)
    next_action_hint: Optional[str] = None
    view_state: Optional[ViewState] = None
    plan: Optional[List[PlanStep]] = None

    # EVIDENCE-CENTRIC FIELDS (v3.1.0)
    evidence_requests: List[EvidenceRequest] = Field(
        default_factory=list,
        description="Active evidence requests for this turn"
    )
    investigation_mode: InvestigationMode = Field(
        default=InvestigationMode.ACTIVE_INCIDENT,
        description="Current investigation approach (speed vs depth)"
    )
    case_status: EvidenceCaseStatus = Field(
        default=EvidenceCaseStatus.INTAKE,
        description="Current case investigation state"
    )

    # DEPRECATED (backward compatibility)
    suggested_actions: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        deprecated=True,
        description="DEPRECATED in v3.1.0 - Use evidence_requests instead. Always null in new responses."
    )

    @model_validator(mode='before')
    @classmethod
    def check_plan_consistency(cls, values):
        """Ensures the 'plan' field is only present for a PLAN_PROPOSAL."""
        if isinstance(values, dict):
            response_type, plan = values.get('response_type'), values.get('plan')
            if response_type == ResponseType.PLAN_PROPOSAL and not plan:
                raise ValueError("A 'plan' must be provided for a PLAN_PROPOSAL response type.")
            if response_type != ResponseType.PLAN_PROPOSAL and plan is not None:
                raise ValueError("A 'plan' can only be provided for a PLAN_PROPOSAL response type.")
        return values

# --- Title generation contracts ---

class TitleGenerateRequest(BaseModel):
    """Request payload for conversation title generation."""
    session_id: str
    context: Optional[Dict[str, Any]] = None
    # Optional guardrail for word count
    max_words: int = 8

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
    code: str # General error code or SessionErrorCode value
    message: str
    session_id: Optional[str] = None  # Session context for session-related errors
    timeout_info: Optional[Dict[str, Any]] = None  # Additional timeout information

class ErrorResponse(BaseModel):
    """The standard JSON payload returned from the backend on failure."""
    schema_version: Literal["3.1.0"] = "3.1.0"
    error: ErrorDetail

# --- New REST Endpoint Models ---

class SessionRequest(BaseModel):
    """Request payload for creating a new session."""
    user_id: Optional[str] = None
    context: Optional[Dict[str, Any]] = None

class SessionResponse(BaseModel):
    """Response payload for session operations - API spec compliance."""
    schema_version: Literal["3.1.0"] = "3.1.0"
    session_id: str
    user_id: Optional[str] = None
    client_id: Optional[str] = None  # Client/device identifier for session resumption
    status: SessionStatus = SessionStatus.ACTIVE
    created_at: str  # UTC ISO 8601 format
    expires_at: Optional[str] = None  # UTC ISO 8601 format - optional for compliance
    metadata: Optional[Dict[str, Any]] = None
    session_resumed: Optional[bool] = None  # Indicates if this was an existing session resumed

class Case(BaseModel):
    """Represents a troubleshooting case."""
    case_id: str  # Match frontend expectations
    title: str
    description: Optional[str] = None
    status: Literal["active", "resolved", "archived"] = "active"  # Match frontend expectations
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    created_at: str = Field(default_factory=lambda: datetime.datetime.utcnow().isoformat() + 'Z')
    updated_at: str = Field(default_factory=lambda: datetime.datetime.utcnow().isoformat() + 'Z')
    message_count: int = 0
    session_id: Optional[str] = None  # Session linkage for frontend
    owner_id: Optional[str] = None  # Case owner user ID

class CaseRequest(BaseModel):
    """Request payload for creating a new case."""
    title: str
    initial_query: Optional[str] = None
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    context: Optional[Dict[str, Any]] = None

class CaseResponse(BaseModel):
    """Response payload for case creation."""
    schema_version: Literal["3.1.0"] = "3.1.0"
    case: Case

class CaseListResponse(BaseModel):
    """Response payload for listing cases."""
    schema_version: Literal["3.1.0"] = "3.1.0"
    cases: List[Case]

class SessionCasesResponse(BaseModel):
    """Response payload for session cases list."""
    schema_version: Literal["3.1.0"] = "3.1.0"
    cases: List[Case]
    total: int

# --- Authentication Models ---

class User(BaseModel):
    """Represents a user in the system."""
    user_id: str
    email: str
    name: str
    created_at: str = Field(default_factory=lambda: datetime.datetime.utcnow().isoformat() + 'Z')
    last_login: Optional[str] = None

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
    description: Optional[str] = None
    expected_type: Optional[DataType] = None
    context: Optional[Dict[str, Any]] = None

class DataUploadResponse(BaseModel):
    """Response payload for data upload."""
    schema_version: Literal["3.1.0"] = "3.1.0"
    data_id: str
    processing_status: ProcessingStatus
    classification: Optional[Dict[str, Any]] = None
    view_state: ViewState

# --- API Compliance Response Models ---

class KnowledgeBaseDocument(BaseModel):
    """Response model for knowledge base document operations."""
    document_id: str
    title: str
    content: str
    document_type: str
    category: Optional[str] = None
    status: str = "processed"
    tags: List[str] = Field(default_factory=list)
    source_url: Optional[str] = None
    created_at: str
    updated_at: str
    metadata: Optional[Dict[str, Any]] = None

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
    progress: Optional[int] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str

class QueryJobStatus(BaseModel):
    """Case-scoped query job status tracking model."""
    query_id: str
    case_id: str
    status: Literal["pending", "processing", "running", "completed", "failed", "cancelled"]
    progress_percentage: Optional[int] = Field(None, ge=0, le=100, description="Processing progress percentage")
    started_at: Optional[str] = Field(None, description="Job start time (UTC ISO 8601)")
    last_updated_at: str = Field(default_factory=lambda: datetime.datetime.utcnow().isoformat() + 'Z')
    error: Optional[Dict[str, Any]] = Field(None, description="Error details if status is failed")
    result: Optional[AgentResponse] = Field(None, description="Final result if completed")

class CaseQuerySummary(BaseModel):
    """Summary information for case queries."""
    query_id: str
    case_id: str
    status: Literal["pending", "processing", "running", "completed", "failed", "cancelled"]
    created_at: str
    last_updated_at: str = Field(default_factory=lambda: datetime.datetime.utcnow().isoformat() + 'Z')

class CaseSummary(BaseModel):
    """Summary information for cases (used in listings)."""
    case_id: str
    title: str
    description: Optional[str] = None
    status: str
    priority: str
    created_at: str
    updated_at: str
    owner_id: Optional[str] = None
    message_count: Optional[int] = Field(None, description="Number of messages/queries in case")
    last_activity_at: Optional[str] = Field(None, description="Last activity timestamp")
    session_id: Optional[str] = None

class Message(BaseModel):
    """Message model for conversation endpoints."""
    message_id: str
    role: Literal["user", "agent", "assistant", "system"]
    content: str
    created_at: str = Field(..., description="ISO 8601 datetime string")

class MessageRetrievalDebugInfo(BaseModel):
    """Debug information for message retrieval operations."""
    redis_key: str = Field(..., description="Redis key used for message storage")
    redis_operation_time_ms: float = Field(..., description="Time taken for Redis operation")
    storage_errors: List[str] = Field(default_factory=list, description="Any storage-related errors encountered")
    message_parsing_errors: int = Field(default=0, description="Number of messages that failed to parse")

class CaseMessagesResponse(BaseModel):
    """Enhanced response model for case message retrieval with debugging support."""
    messages: List[Message] = Field(..., description="Array of conversation messages")
    total_count: int = Field(..., description="Total number of messages in the case")
    retrieved_count: int = Field(..., description="Number of messages successfully retrieved")
    has_more: bool = Field(..., description="Whether more messages are available for pagination")
    next_offset: Optional[int] = Field(None, description="Offset for next page (null if no more pages)")
    debug_info: Optional[MessageRetrievalDebugInfo] = Field(None, description="Debug information (only when include_debug=true)")

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
    case_id: Optional[str] = None
    confidence_score: Optional[float] = None
    sources: List[Dict[str, Any]] = Field(default_factory=list)
    next_action_hint: Optional[str] = None