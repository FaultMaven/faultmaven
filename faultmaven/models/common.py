"""Common models shared across FaultMaven.

This module contains foundational models used throughout the application:
- SessionContext: Session management and state tracking
- AgentState: Agent workflow and execution state
- API Response models: DataInsightsResponse, TroubleshootingResponse
- Search models: SearchRequest, SearchResult

Note: Datetime utility functions (utc_timestamp, parse_utc_timestamp) have been
moved to faultmaven.utils.datetime to avoid circular dependencies.
"""

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict


class AgentStateEnum(str, Enum):
    """Enumeration of agent states for testing and status tracking"""

    IDLE = "idle"
    RUNNING = "running"
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentState(TypedDict):
    """State representation for the LangGraph agent"""

    session_id: str
    user_query: str
    current_phase: str
    case_context: dict[str, Any]
    findings: list[dict[str, Any]]
    recommendations: list[str]
    confidence_score: float
    tools_used: list[str]
    awaiting_user_input: bool
    user_feedback: str


class SessionContext(BaseModel):
    """Session context for maintaining state across requests"""

    # Core session fields (spec-compliant: authentication only)
    session_id: str = Field(..., description="Unique session identifier")
    user_id: str = Field(
        ..., description="User identifier - REQUIRED for authorization"
    )
    organization_id: str = Field(
        default="00000000-0000-0000-0000-000000000001",
        description="Organization ID for multi-tenant isolation",
    )

    # Multi-device support fields (spec lines 263-269)
    client_id: str | None = Field(
        None, description="Client/device identifier for session resumption"
    )
    session_resumed: bool = Field(
        False, description="Whether this session was resumed from existing"
    )

    # Timestamps
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Session creation timestamp",
    )
    last_activity: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Last activity timestamp",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Last update timestamp",
    )
    expires_at: datetime | None = Field(
        None, description="Session expiration time (TTL-based)"
    )

    # Session metadata (authentication context only)
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional session metadata"
    )

    @property
    def active(self) -> bool:
        """Check if session is considered active based on last activity (24 hours default)"""
        from datetime import timedelta

        inactive_threshold = timedelta(hours=24)
        time_since_activity = datetime.now(UTC) - self.last_activity
        return time_since_activity < inactive_threshold

    # json_encoders removed in Pydantic V2 - datetime serialization handled by default
    model_config = ConfigDict()


class DataInsightsResponse(BaseModel):
    """Response model for data insights"""

    data_id: str = Field(..., description="Identifier of the processed data")
    data_type: str = Field(
        ..., description="Type of the processed data"
    )  # Changed from DataType enum to string
    insights: dict[str, Any] = Field(
        ..., description="Extracted insights from the data"
    )
    confidence_score: float = Field(
        ..., description="Confidence in the insights (0.0-1.0)"
    )
    processing_time_ms: int = Field(..., description="Time taken to process the data")
    anomalies_detected: list[dict[str, Any]] = Field(
        default_factory=list, description="List of detected anomalies"
    )
    recommendations: list[str] = Field(
        default_factory=list, description="Initial recommendations based on insights"
    )

    # json_encoders removed in Pydantic V2 - datetime serialization handled by default
    model_config = ConfigDict()


class TroubleshootingResponse(BaseModel):
    """Response model for troubleshooting results"""

    session_id: str = Field(..., description="Session identifier")
    case_id: str = Field(..., description="Unique case identifier")
    status: str = Field(..., description="Status of the case")
    findings: list[dict[str, Any]] = Field(
        ..., description="Detailed findings from the case"
    )
    root_cause: str | None = Field(None, description="Identified root cause")
    recommendations: list[str] = Field(..., description="Actionable recommendations")
    confidence_score: float = Field(
        ..., description="Confidence in the analysis (0.0-1.0)"
    )
    estimated_mttr: str | None = Field(
        None, description="Estimated Mean Time To Resolution"
    )
    next_steps: list[str] = Field(
        default_factory=list, description="Recommended next steps"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Case creation timestamp",
    )
    completed_at: datetime | None = Field(None, description="Case completion timestamp")

    # json_encoders removed in Pydantic V2 - datetime serialization handled by default
    model_config = ConfigDict()


class SearchRequest(BaseModel):
    """Request model for knowledge base search"""

    query: str = Field(..., description="Search query", min_length=1)
    document_type: str | None = Field(None, description="Filter by document type")
    category: str | None = Field(None, description="Filter by document category")
    tags: str | None = Field(None, description="Filter by tags (comma-separated)")
    filters: dict[str, Any] | None = Field(
        None, description="Advanced filters for search"
    )
    similarity_threshold: float | None = Field(
        None, description="Minimum similarity score threshold (0.0-1.0)", ge=0.0, le=1.0
    )
    rank_by: str | None = Field(
        None, description="Field to rank results by (e.g., priority)"
    )
    limit: int = Field(
        default=10, description="Maximum number of results", gt=0, le=100
    )


class SearchResult(BaseModel):
    """Model for search result item"""

    document_id: str = Field(..., description="Document identifier")
    title: str = Field(..., description="Document title")
    document_type: str = Field(..., description="Document type")
    tags: list[str] = Field(..., description="Document tags")
    score: float = Field(..., description="Search relevance score")
    snippet: str = Field(..., description="Relevant content snippet")
