"""Common models shared across FaultMaven.

This module contains foundational models used throughout the application:
- SessionContext: Session management and state tracking
- AgentState: Agent workflow and execution state
- API Response models: DataInsightsResponse, TroubleshootingResponse
- Search models: SearchRequest, SearchResult
- Utility functions: utc_timestamp(), parse_utc_timestamp()
"""

from datetime import datetime, timezone
from faultmaven.utils.serialization import to_json_compatible
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
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
    case_context: Dict[str, Any]
    findings: List[Dict[str, Any]]
    recommendations: List[str]
    confidence_score: float
    tools_used: List[str]
    awaiting_user_input: bool
    user_feedback: str


class SessionContext(BaseModel):
    """Session context for maintaining state across requests"""

    # Core session fields (spec-compliant: authentication only)
    session_id: str = Field(..., description="Unique session identifier")
    user_id: str = Field(..., description="User identifier - REQUIRED for authorization")

    # Multi-device support fields (spec lines 263-269)
    client_id: Optional[str] = Field(None, description="Client/device identifier for session resumption")
    session_resumed: bool = Field(False, description="Whether this session was resumed from existing")

    # Timestamps
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), description="Session creation timestamp"
    )
    last_activity: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), description="Last activity timestamp"
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), description="Last update timestamp"
    )
    expires_at: Optional[datetime] = Field(None, description="Session expiration time (TTL-based)")

    # Session metadata (authentication context only)
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional session metadata")

    @property
    def active(self) -> bool:
        """Check if session is considered active based on last activity (24 hours default)"""
        from datetime import timedelta, timezone
        inactive_threshold = timedelta(hours=24)
        time_since_activity = datetime.now(timezone.utc) - self.last_activity
        return time_since_activity < inactive_threshold

    class Config:
        json_encoders = {datetime: lambda v: to_json_compatible(v)}


class DataInsightsResponse(BaseModel):
    """Response model for data insights"""

    data_id: str = Field(..., description="Identifier of the processed data")
    data_type: str = Field(..., description="Type of the processed data")  # Changed from DataType enum to string
    insights: Dict[str, Any] = Field(
        ..., description="Extracted insights from the data"
    )
    confidence_score: float = Field(
        ..., description="Confidence in the insights (0.0-1.0)"
    )
    processing_time_ms: int = Field(..., description="Time taken to process the data")
    anomalies_detected: List[Dict[str, Any]] = Field(
        default_factory=list, description="List of detected anomalies"
    )
    recommendations: List[str] = Field(
        default_factory=list, description="Initial recommendations based on insights"
    )

    class Config:
        json_encoders = {datetime: lambda v: to_json_compatible(v)}


class TroubleshootingResponse(BaseModel):
    """Response model for troubleshooting results"""

    session_id: str = Field(..., description="Session identifier")
    case_id: str = Field(..., description="Unique case identifier")
    status: str = Field(..., description="Status of the case")
    findings: List[Dict[str, Any]] = Field(
        ..., description="Detailed findings from the case"
    )
    root_cause: Optional[str] = Field(None, description="Identified root cause")
    recommendations: List[str] = Field(..., description="Actionable recommendations")
    confidence_score: float = Field(
        ..., description="Confidence in the analysis (0.0-1.0)"
    )
    estimated_mttr: Optional[str] = Field(
        None, description="Estimated Mean Time To Resolution"
    )
    next_steps: List[str] = Field(
        default_factory=list, description="Recommended next steps"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), description="Case creation timestamp"
    )
    completed_at: Optional[datetime] = Field(
        None, description="Case completion timestamp"
    )

    class Config:
        json_encoders = {datetime: lambda v: to_json_compatible(v)}


class SearchRequest(BaseModel):
    """Request model for knowledge base search"""

    query: str = Field(..., description="Search query", min_length=1)
    document_type: Optional[str] = Field(None, description="Filter by document type")
    category: Optional[str] = Field(None, description="Filter by document category")
    tags: Optional[str] = Field(None, description="Filter by tags (comma-separated)")
    filters: Optional[Dict[str, Any]] = Field(None, description="Advanced filters for search")
    similarity_threshold: Optional[float] = Field(None, description="Minimum similarity score threshold (0.0-1.0)", ge=0.0, le=1.0)
    rank_by: Optional[str] = Field(None, description="Field to rank results by (e.g., priority)")
    limit: int = Field(default=10, description="Maximum number of results", gt=0, le=100)


class SearchResult(BaseModel):
    """Model for search result item"""

    document_id: str = Field(..., description="Document identifier")
    title: str = Field(..., description="Document title")
    document_type: str = Field(..., description="Document type")
    tags: List[str] = Field(..., description="Document tags")
    score: float = Field(..., description="Search relevance score")
    snippet: str = Field(..., description="Relevant content snippet")


# Utility functions for timestamp formatting
def utc_timestamp() -> str:
    """Generate UTC timestamp with 'Z' suffix format required by API specification.
    
    Returns:
        str: UTC timestamp in ISO format with 'Z' suffix (e.g. "2024-01-15T14:30:00.123Z")
    """
    return to_json_compatible(datetime.now(timezone.utc))


def parse_utc_timestamp(timestamp_str: str) -> datetime:
    """Parse UTC timestamp string into timezone-aware datetime object.

    Handles multiple ISO 8601 formats:
    - '2025-10-17T04:02:59+00:00' (timezone-aware with +00:00)
    - '2025-10-17T04:02:59Z' (Zulu time suffix)
    - '2025-10-17T04:02:59' (naive, assumed UTC)
    - '2025-10-17T04:02:59+00:00Z' (CORRUPTED - legacy data only, auto-fixes on save)

    Args:
        timestamp_str: UTC timestamp string in various formats

    Returns:
        datetime: Timezone-aware datetime object in UTC

    Note:
        The corrupted format (both +00:00 and Z) is handled for backwards compatibility
        with old data. When cases are re-saved, timestamps are automatically standardized
        to the proper +00:00 format.
    """
    from datetime import timezone

    if timestamp_str.endswith('Z'):
        # Remove 'Z' suffix and parse
        # This also handles corrupted '+00:00Z' format by stripping Z, leaving valid '+00:00'
        dt = datetime.fromisoformat(timestamp_str[:-1])
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    else:
        # Parse ISO format (handles +00:00 automatically)
        dt = datetime.fromisoformat(timestamp_str)
        # If naive, assume UTC
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt