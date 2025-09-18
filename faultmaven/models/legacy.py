"""Legacy models to maintain backward compatibility.

This module contains models that were originally in models_original.py
but need to be maintained for backward compatibility during the refactoring.
"""

from datetime import datetime
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

    session_id: str = Field(..., description="Unique session identifier")
    user_id: Optional[str] = Field(None, description="User identifier if authenticated")
    created_at: datetime = Field(
        default_factory=datetime.utcnow, description="Session creation timestamp"
    )
    last_activity: datetime = Field(
        default_factory=datetime.utcnow, description="Last activity timestamp"
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow, description="Last update timestamp"
    )
    data_uploads: List[str] = Field(
        default_factory=list, description="List of uploaded data IDs"
    )
    case_history: List[Dict[str, Any]] = Field(
        default_factory=list, description="History of cases (conversation threads)"
    )
    current_case_id: Optional[str] = Field(None, description="Current active case/conversation thread ID")
    agent_state: Optional[Dict[str, Any]] = Field(None, description="Current agent state and context")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional session metadata")

    @property
    def active(self) -> bool:
        """Check if session is considered active based on last activity (24 hours default)"""
        from datetime import timedelta
        inactive_threshold = timedelta(hours=24)
        time_since_activity = datetime.utcnow() - self.last_activity
        return time_since_activity < inactive_threshold

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat() + 'Z'}


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
        json_encoders = {datetime: lambda v: v.isoformat() + 'Z'}


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
        default_factory=datetime.utcnow, description="Case creation timestamp"
    )
    completed_at: Optional[datetime] = Field(
        None, description="Case completion timestamp"
    )

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat() + 'Z'}


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
    return datetime.utcnow().isoformat() + 'Z'


def parse_utc_timestamp(timestamp_str: str) -> datetime:
    """Parse UTC timestamp string into timezone-naive datetime object.
    
    Handles both 'Z' suffix format and regular ISO format consistently,
    returning timezone-naive datetime objects to avoid comparison issues.
    
    Args:
        timestamp_str: UTC timestamp string (with or without 'Z' suffix)
        
    Returns:
        datetime: Timezone-naive datetime object in UTC
    """
    if timestamp_str.endswith('Z'):
        # Remove 'Z' suffix and parse as naive datetime (already UTC)
        return datetime.fromisoformat(timestamp_str[:-1])
    else:
        # Parse regular ISO format
        return datetime.fromisoformat(timestamp_str)