"""models.py

Purpose: Pydantic data models and enums

Requirements:
--------------------------------------------------------------------------------
• Define DataType enum
• Define SessionContext model
• Define API response models

Key Components:
--------------------------------------------------------------------------------
  class DataType(str, Enum): ...
  class SessionContext(BaseModel): ...

Technology Stack:
--------------------------------------------------------------------------------
Pydantic, Enum

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Cost-Efficiency: Use semantic caching
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from typing_extensions import TypedDict


class DataType(str, Enum):
    """Enumeration of supported data types for classification"""

    LOG_FILE = "log_file"
    ERROR_MESSAGE = "error_message"
    STACK_TRACE = "stack_trace"
    METRICS_DATA = "metrics_data"
    CONFIG_FILE = "config_file"
    DOCUMENTATION = "documentation"
    UNKNOWN = "unknown"


class AgentState(TypedDict):
    """State representation for the LangGraph agent"""

    session_id: str
    user_query: str
    current_phase: str
    investigation_context: Dict[str, Any]
    findings: List[Dict[str, Any]]
    recommendations: List[str]
    confidence_score: float
    tools_used: List[str]


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
    investigation_history: List[Dict[str, Any]] = Field(
        default_factory=list, description="History of investigations"
    )
    agent_state: Optional[AgentState] = Field(None, description="Current agent state")

    @property
    def active(self) -> bool:
        """Check if session is considered active based on last activity (24 hours default)"""
        from datetime import timedelta
        inactive_threshold = timedelta(hours=24)
        time_since_activity = datetime.utcnow() - self.last_activity
        return time_since_activity < inactive_threshold

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class UploadedData(BaseModel):
    """Model for uploaded data processing"""

    data_id: str = Field(..., description="Unique identifier for the uploaded data")
    session_id: str = Field(..., description="Session this data belongs to")
    data_type: DataType = Field(..., description="Classified type of the data")
    content: str = Field(..., description="Raw content of the uploaded data")
    file_name: Optional[str] = Field(
        None, description="Original filename if applicable"
    )
    file_size: Optional[int] = Field(None, description="File size in bytes")
    uploaded_at: datetime = Field(
        default_factory=datetime.utcnow, description="Upload timestamp"
    )
    processing_status: str = Field(default="pending", description="Processing status")
    insights: Optional[Dict[str, Any]] = Field(
        None, description="Extracted insights from the data"
    )

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class DataInsightsResponse(BaseModel):
    """Response model for data insights"""

    data_id: str = Field(..., description="Identifier of the processed data")
    data_type: DataType = Field(..., description="Type of the processed data")
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
        json_encoders = {datetime: lambda v: v.isoformat()}


class TroubleshootingResponse(BaseModel):
    """Response model for troubleshooting results"""

    session_id: str = Field(..., description="Session identifier")
    investigation_id: str = Field(..., description="Unique investigation identifier")
    status: str = Field(..., description="Status of the investigation")
    findings: List[Dict[str, Any]] = Field(
        ..., description="Detailed findings from the investigation"
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
        default_factory=datetime.utcnow, description="Investigation creation timestamp"
    )
    completed_at: Optional[datetime] = Field(
        None, description="Investigation completion timestamp"
    )

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class QueryRequest(BaseModel):
    """Request model for troubleshooting queries"""

    session_id: str = Field(..., description="Session identifier")
    query: str = Field(..., description="User's troubleshooting query")
    context: Optional[Dict[str, Any]] = Field(
        None, description="Additional context for the query"
    )
    priority: str = Field(default="normal", description="Query priority level")


class KnowledgeBaseDocument(BaseModel):
    """Model for knowledge base documents"""

    document_id: str = Field(..., description="Unique document identifier")
    title: str = Field(..., description="Document title")
    content: str = Field(..., description="Document content")
    document_type: str = Field(
        ..., description="Type of document (e.g., troubleshooting guide, FAQ)"
    )
    category: Optional[str] = Field(None, description="Document category for organization")
    status: str = Field(default="processed", description="Document processing status")
    tags: List[str] = Field(default_factory=list, description="Tags for categorization")
    source_url: Optional[str] = Field(None, description="Source URL if applicable")
    created_at: datetime = Field(
        default_factory=datetime.utcnow, description="Document creation timestamp"
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow, description="Last update timestamp"
    )
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional document metadata")

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


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
