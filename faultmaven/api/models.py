"""API Request/Response Models (TASK-014)

Purpose: Pydantic models for FastAPI request validation and response serialization.

This module provides:
- Request models for case, session, and evidence operations
- Response models for API responses
- Error response models for consistent error handling

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from faultmaven.models.investigation_session import SessionStatus

# Import from contracts (Principle 2: Vertical Modules with Contracts)
from faultmaven.modules.case.contracts import (
    CaseSeverity,
    CaseStatus,
    EvidenceArtifactType,
    InvestigationProgress,
)

# ============================================================
# Case Models
# ============================================================


class CaseCreateRequest(BaseModel):
    """Request model for creating a case."""

    title: str = Field(..., min_length=1, max_length=512)
    description: str = Field(..., min_length=1)
    severity: CaseSeverity
    metadata: Optional[Dict[str, Any]] = None


class CaseUpdateRequest(BaseModel):
    """Request model for updating a case."""

    title: Optional[str] = Field(None, min_length=1, max_length=512)
    description: Optional[str] = Field(None, min_length=1)
    severity: Optional[CaseSeverity] = None
    status: Optional[CaseStatus] = None
    assigned_to: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class CaseResponse(BaseModel):
    """Response model for a case."""

    case_id: str
    organization_id: str
    reporter_user_id: str
    title: str
    description: str
    severity: CaseSeverity
    status: CaseStatus
    progress: Optional[InvestigationProgress] = None
    assigned_to: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    closed_at: Optional[datetime] = None
    resolution: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_domain(
        cls, case: Any, severity: Optional[CaseSeverity] = None
    ) -> "CaseResponse":
        """Create CaseResponse from domain Case model.

        Args:
            case: Domain Case object
            severity: Optional severity override (extracted from metadata)

        Returns:
            CaseResponse instance
        """
        # Extract severity from problem_verification or metadata
        case_severity = severity
        if case_severity is None:
            if hasattr(case, "problem_verification") and case.problem_verification:
                try:
                    case_severity = CaseSeverity.from_string(
                        case.problem_verification.severity
                    )
                except (ValueError, AttributeError):
                    case_severity = CaseSeverity.MEDIUM
            else:
                case_severity = CaseSeverity.MEDIUM

        # Get resolution from closure_reason if available
        resolution = None
        if hasattr(case, "closure_reason") and case.closure_reason:
            resolution = case.closure_reason

        return cls(
            case_id=case.case_id,
            organization_id=case.organization_id,
            reporter_user_id=case.user_id,
            title=case.title,
            description=case.description,
            severity=case_severity,
            status=case.status,
            progress=getattr(case, "progress", None),
            assigned_to=getattr(case, "assigned_to", None),
            created_at=case.created_at,
            updated_at=case.updated_at,
            closed_at=getattr(case, "closed_at", None),
            resolution=resolution,
            metadata=getattr(case, "metadata", None),
        )


class CaseListResponse(BaseModel):
    """Response model for case list."""

    items: List[CaseResponse]
    total: int
    limit: int
    offset: int


# ============================================================
# Session Models
# ============================================================


class SessionCreateRequest(BaseModel):
    """Request model for creating investigation session."""

    session_goal: Optional[str] = None
    token_budget_limit: Optional[int] = Field(None, ge=0)
    metadata: Optional[Dict[str, Any]] = None


class SessionUpdateRequest(BaseModel):
    """Request model for updating session."""

    session_goal: Optional[str] = None
    token_budget_limit: Optional[int] = Field(None, ge=0)
    metadata: Optional[Dict[str, Any]] = None


class SessionResponse(BaseModel):
    """Response model for investigation session."""

    session_id: str
    case_id: str
    user_id: str
    organization_id: str
    status: SessionStatus
    started_at: datetime
    ended_at: Optional[datetime] = None
    last_activity_at: datetime
    total_duration_ms: Optional[int] = None
    session_goal: Optional[str] = None
    findings_summary: Optional[str] = None
    total_token_usage: int
    total_agent_executions: int
    token_budget_limit: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_domain(cls, session: Any) -> "SessionResponse":
        """Create SessionResponse from domain InvestigationSession model.

        Args:
            session: Domain InvestigationSession object

        Returns:
            SessionResponse instance
        """
        return cls(
            session_id=session.session_id,
            case_id=session.case_id,
            user_id=session.user_id,
            organization_id=session.organization_id,
            status=session.status,
            started_at=session.started_at,
            ended_at=session.ended_at,
            last_activity_at=session.last_activity_at,
            total_duration_ms=session.total_duration_ms,
            session_goal=session.session_goal,
            findings_summary=session.findings_summary,
            total_token_usage=session.total_token_usage,
            total_agent_executions=session.total_agent_executions,
            token_budget_limit=session.token_budget_limit,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )


class SessionListResponse(BaseModel):
    """Response model for session list."""

    items: List[SessionResponse]
    total: int
    limit: int
    offset: int


# ============================================================
# Evidence Models
# ============================================================


class EvidenceUploadRequest(BaseModel):
    """Request model for evidence upload (multipart form).

    Note: This model is used for documentation purposes.
    The actual upload uses FastAPI Form parameters.
    """

    evidence_type: EvidenceArtifactType
    description: Optional[str] = None
    is_primary: bool = False
    metadata: Optional[Dict[str, Any]] = None


class EvidenceUpdateRequest(BaseModel):
    """Request model for updating evidence."""

    description: Optional[str] = None
    is_primary: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None


class EvidenceResponse(BaseModel):
    """Response model for evidence artifact."""

    evidence_id: str
    case_id: str
    user_id: str
    organization_id: str
    original_filename: str
    evidence_type: EvidenceArtifactType
    mime_type: str
    file_size: int
    description: Optional[str] = None
    is_primary: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_domain(cls, evidence: Any) -> "EvidenceResponse":
        """Create EvidenceResponse from a domain evidence object.

        Args:
            evidence: A domain object exposing the fields below.

        Returns:
            EvidenceResponse instance
        """
        return cls(
            evidence_id=evidence.evidence_id,
            case_id=evidence.case_id,
            user_id=evidence.user_id,
            organization_id=evidence.organization_id,
            original_filename=evidence.original_filename,
            evidence_type=evidence.evidence_type,
            mime_type=evidence.mime_type,
            file_size=evidence.file_size,
            description=evidence.description,
            is_primary=evidence.is_primary,
            created_at=evidence.created_at,
            updated_at=evidence.updated_at,
        )


class EvidenceListResponse(BaseModel):
    """Response model for evidence list."""

    items: List[EvidenceResponse]
    total: int
    limit: int
    offset: int


# ============================================================
# Error Models
# ============================================================


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    detail: Optional[str] = None
    status_code: int


class ValidationErrorResponse(BaseModel):
    """Validation error response with field-level details."""

    error: str = "Validation Error"
    detail: Optional[str] = None
    status_code: int = 400
    errors: Optional[List[Dict[str, Any]]] = None


# ============================================================
# Agent Execution Models (TASK-016)
# ============================================================


class AgentExecutionRequest(BaseModel):
    """Request model for executing an AI agent.

    This model defines the input for agent execution requests,
    supporting both streaming and non-streaming modes.
    """

    user_message: str = Field(
        ...,
        min_length=1,
        max_length=10000,
        description="User's question or request for the agent",
    )
    agent_type: str = Field(
        default="investigator",
        description="Type of agent to execute (investigator, debugger, researcher, validator, reporter)",
    )
    stream: bool = Field(
        default=True,
        description="Whether to stream response events (SSE)",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user_message": "What is causing the 500 errors in the API?",
                "agent_type": "investigator",
                "stream": True,
            }
        }
    )


class ToolCallResponse(BaseModel):
    """Response model for a tool call within an execution."""

    tool_call_id: str
    tool_name: str
    arguments: Dict[str, Any]
    result: Optional[str] = None
    status: str

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_domain(cls, tool_call: Any) -> "ToolCallResponse":
        """Create ToolCallResponse from domain AgentToolCall model.

        Args:
            tool_call: Domain AgentToolCall object

        Returns:
            ToolCallResponse instance
        """
        return cls(
            tool_call_id=tool_call.tool_call_id,
            tool_name=tool_call.tool_name,
            arguments=tool_call.tool_input or {},
            result=str(tool_call.tool_output) if tool_call.tool_output else None,
            status=tool_call.status,
        )


class AgentExecutionResponse(BaseModel):
    """Response model for completed agent execution (non-streaming).

    Used when stream=false in the request.
    """

    execution_id: str
    status: str
    agent_response: str
    tokens_used: int
    started_at: datetime
    completed_at: Optional[datetime] = None
    tool_calls: List[ToolCallResponse] = []

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_domain(cls, execution: Any) -> "AgentExecutionResponse":
        """Create AgentExecutionResponse from domain AgentExecution model.

        Args:
            execution: Domain AgentExecution object

        Returns:
            AgentExecutionResponse instance
        """
        return cls(
            execution_id=execution.execution_id,
            status=(
                execution.status.value
                if hasattr(execution.status, "value")
                else str(execution.status)
            ),
            agent_response=execution.response or "",
            tokens_used=(
                execution.get_total_tokens()
                if hasattr(execution, "get_total_tokens")
                else 0
            ),
            started_at=execution.started_at or execution.created_at,
            completed_at=execution.completed_at,
            tool_calls=[
                ToolCallResponse.from_domain(tc) for tc in (execution.tool_calls or [])
            ],
        )


class ExecutionEventSSE(BaseModel):
    """Server-Sent Event model for execution streaming.

    Represents a single SSE event sent during streaming agent execution.
    Events are formatted according to the SSE specification.
    """

    event: str = Field(
        ...,
        description="Event type: started, thinking, tool_call, tool_result, response, error, completed",
    )
    data: Dict[str, Any] = Field(
        default_factory=dict,
        description="Event data payload",
    )

    def to_sse(self) -> str:
        """Convert to SSE format string.

        Returns:
            SSE-formatted string with event and data lines
        """
        import json

        data_json = json.dumps(self.data)
        return f"event: {self.event}\ndata: {data_json}\n\n"

    @classmethod
    def from_execution_event(cls, event: Any) -> "ExecutionEventSSE":
        """Create ExecutionEventSSE from domain ExecutionEvent.

        Args:
            event: Domain ExecutionEvent object

        Returns:
            ExecutionEventSSE instance
        """
        return cls(
            event=(
                event.event_type.value
                if hasattr(event.event_type, "value")
                else str(event.event_type)
            ),
            data={
                "content": event.content,
                "metadata": event.metadata or {},
                "timestamp": (
                    event.timestamp.isoformat()
                    if hasattr(event.timestamp, "isoformat")
                    else str(event.timestamp)
                ),
                "execution_id": event.execution_id,
            },
        )

    @classmethod
    def error_event(cls, error_code: str, message: str) -> "ExecutionEventSSE":
        """Create an error SSE event.

        Args:
            error_code: Error code (not_found, forbidden, conflict, llm_error, internal_error)
            message: Human-readable error message

        Returns:
            ExecutionEventSSE instance for the error
        """
        return cls(
            event="error",
            data={
                "error": error_code,
                "message": message,
            },
        )


# ============================================================
# Admin User Management Models (TASK-019)
# ============================================================


class AdminUserListItem(BaseModel):
    """User list item for admin endpoints (with full info)."""

    user_id: str
    organization_id: str
    email: str
    full_name: str
    roles: List[str]
    is_active: bool
    is_verified: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdminUserListResponse(BaseModel):
    """Admin user list response with pagination."""

    users: List[AdminUserListItem]
    total: int
    limit: int
    offset: int


class UserDetailResponse(BaseModel):
    """Detailed user information (admin only)."""

    user_id: str
    organization_id: str
    email: str
    full_name: str
    roles: List[str]
    permissions: List[str]
    is_active: bool
    is_verified: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)


class UserStatusResponse(BaseModel):
    """User activation/deactivation response."""

    user_id: str
    is_active: bool
    updated_at: datetime
    message: str


class RoleAssignmentRequest(BaseModel):
    """Role assignment request."""

    role: str = Field(
        ...,
        pattern="^(admin|member|viewer)$",
        description="Role to assign (admin, member, or viewer)",
    )


class RoleAssignmentResponse(BaseModel):
    """Role assignment response."""

    user_id: str
    roles: List[str]
    updated_at: datetime
    message: str


class OrganizationUserListItem(BaseModel):
    """User list item for organization user list (limited info)."""

    user_id: str
    email: str
    full_name: str
    roles: List[str]
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class OrganizationUserListResponse(BaseModel):
    """Organization user list response with pagination."""

    users: List[OrganizationUserListItem]
    total: int
    limit: int
    offset: int


# ============================================================
# LLM Configuration Models (Dashboard Phase 1a)
# ============================================================


class LLMProviderDetail(BaseModel):
    """Individual LLM provider status for dashboard display."""

    name: str
    display_name: str
    enabled: bool = Field(
        description="Provider is initialized and in the fallback chain"
    )
    connected: bool = Field(description="Provider responded to last health check")
    has_api_key: bool = Field(description="API key is configured (value never exposed)")
    state: str = Field(
        default="not_configured",
        description="Provider lifecycle state: not_configured, configured, or active",
    )
    models: List[str] = Field(default_factory=list)
    selected_model: Optional[str] = Field(
        None, description="Currently active model for this provider"
    )
    available_models: List[str] = Field(
        default_factory=list,
        description="Models the user can choose from for this provider",
    )
    error_message: Optional[str] = None
    health: str = Field(
        default="unknown", description="HEALTHY, DEGRADED, UNHEALTHY, or UNKNOWN"
    )
    avg_latency_ms: float = 0.0


class LLMConfigResponse(BaseModel):
    """LLM configuration and provider status response."""

    deployment: str = Field(description="Deployment mode: 'local' or 'cloud'")
    config_readonly: bool = Field(
        description="True in local mode (config managed via .env file)"
    )
    primary_provider: str
    strict_mode: bool
    fallback_chain: List[str]
    providers: Dict[str, LLMProviderDetail]
    timestamp: datetime


class LLMConnectionTestRequest(BaseModel):
    """Request to test an LLM provider connection."""

    provider: str = Field(
        ..., description="Provider name to test (e.g. 'anthropic', 'openai')"
    )


class LLMConnectionTestResponse(BaseModel):
    """Result of an LLM provider connection test."""

    model_config = ConfigDict(protected_namespaces=())

    provider: str
    connected: bool
    response_time_ms: int = 0
    error_message: Optional[str] = None
    model_used: Optional[str] = None
    timestamp: datetime


class LLMConfigUpdateRequest(BaseModel):
    """Request to update LLM configuration."""

    primary_provider: Optional[str] = Field(
        None, description="New primary provider name"
    )
    fallback_chain: Optional[List[str]] = Field(
        None, description="New fallback chain order"
    )
    provider_name: Optional[str] = Field(
        None, description="Provider to update API key or model for"
    )
    api_key: Optional[str] = Field(
        None, description="New API key value for the specified provider"
    )
    model: Optional[str] = Field(
        None,
        description="Model to use for the specified provider (requires provider_name)",
    )


class LLMConfigUpdateResponse(BaseModel):
    """Response after updating LLM configuration."""

    updated_keys: List[str] = Field(description="Config keys that were updated")
    message: str
    timestamp: datetime


# ============================================================
# Environment Configuration Status Models (Dashboard Phase 1a)
# ============================================================


class FeatureStatus(BaseModel):
    """Status of an optional feature that depends on configuration."""

    enabled: bool = Field(description="Feature is active and usable")
    has_api_key: bool = Field(
        default=False, description="Required API key is configured"
    )
    description: str = Field(default="", description="Brief explanation of the feature")
    config_hint: str = Field(
        default="",
        description="What the user needs to set to enable this feature",
    )


class EnvConfigStatusResponse(BaseModel):
    """Read-only environment configuration status for dashboard display."""

    auth_mode: str = Field(description="'local' or 'oauth'")
    deployment: str = Field(description="'local' or 'cloud' — derived from auth_mode")
    db_backend: str = Field(description="'sqlite' or 'postgresql'")
    session_storage: str = Field(description="'inmemory' or 'redis'")
    vector_storage: str = Field(description="'inmemory' or 'chromadb'")
    llm_provider: str = Field(description="Primary LLM provider name")
    pii_redaction_enabled: bool
    rate_limit_enabled: bool
    features: Dict[str, FeatureStatus] = Field(
        default_factory=dict,
        description="Optional features and their configuration status",
    )
    timestamp: datetime
