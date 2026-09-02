"""Common models shared across FaultMaven.

This module contains foundational models used throughout the application:
- SessionContext: Session management and state tracking
- AgentState: Agent workflow and execution state
- API Response models: DataInsightsResponse, TroubleshootingResponse
- Search models: SearchRequest, SearchResult

Note: Datetime utility functions (utc_timestamp, parse_utc_timestamp) have been
moved to faultmaven.utils.datetime to avoid circular dependencies.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.utils.serialization import to_json_compatible


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
    user_id: str = Field(
        ..., description="User identifier - REQUIRED for authorization"
    )
    organization_id: str = Field(
        default=STANDALONE_ORG_ID,
        description="Implicit single-tenant org; multi-tenant isolation is in-core PostgreSQL RLS (ADR-010)",
    )

    # Multi-device support fields (spec lines 263-269)
    client_id: Optional[str] = Field(
        None, description="Client/device identifier for session resumption"
    )
    session_resumed: bool = Field(
        False, description="Whether this session was resumed from existing"
    )

    # Timestamps
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Session creation timestamp",
    )
    last_activity: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Last activity timestamp",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Last update timestamp",
    )
    expires_at: Optional[datetime] = Field(
        None, description="Session expiration time (TTL-based)"
    )

    # Session metadata (authentication context only)
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Additional session metadata"
    )

    @property
    def active(self) -> bool:
        """Check if session is considered active based on last activity (24 hours default)"""
        from datetime import timedelta, timezone

        inactive_threshold = timedelta(hours=24)
        time_since_activity = datetime.now(timezone.utc) - self.last_activity
        return time_since_activity < inactive_threshold

    # json_encoders removed in Pydantic V2 - datetime serialization handled by default
    model_config = ConfigDict()


class DataInsightsResponse(BaseModel):
    """Response model for data insights"""

    data_id: str = Field(..., description="Identifier of the processed data")
    data_type: str = Field(
        ..., description="Type of the processed data"
    )  # Changed from DataType enum to string
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

    # json_encoders removed in Pydantic V2 - datetime serialization handled by default
    model_config = ConfigDict()


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
        default_factory=lambda: datetime.now(timezone.utc),
        description="Case creation timestamp",
    )
    completed_at: Optional[datetime] = Field(
        None, description="Case completion timestamp"
    )

    # json_encoders removed in Pydantic V2 - datetime serialization handled by default
    model_config = ConfigDict()


class SearchRequest(BaseModel):
    """Request model for knowledge base search"""

    query: str = Field(..., description="Search query", min_length=1)
    document_type: Optional[str] = Field(None, description="Filter by document type")
    category: Optional[str] = Field(None, description="Filter by document category")
    tags: Optional[str] = Field(None, description="Filter by tags (comma-separated)")
    filters: Optional[Dict[str, Any]] = Field(
        None, description="Advanced filters for search"
    )
    similarity_threshold: Optional[float] = Field(
        None, description="Minimum similarity score threshold (0.0-1.0)", ge=0.0, le=1.0
    )
    rank_by: Optional[str] = Field(
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
    tags: List[str] = Field(..., description="Document tags")
    score: float = Field(..., description="Search relevance score")
    snippet: str = Field(..., description="Relevant content snippet")
    parent_document_id: Optional[str] = Field(
        default=None,
        description=(
            "Id of the parent knowledge_items row this hit's chunk belongs to. "
            "None when the hit carries "
            "no parent identity. Distinct from document_id, which for a chunk hit "
            "is the chunk id."
        ),
    )
    total_chunks: Optional[int] = Field(
        default=None,
        description=(
            "How many chunks the parent document was split into at index time, "
            "as stamped on THIS hit's chunk. Lets a consumer read a hit count "
            "relative to the document's own length: one hit on a one-chunk "
            "document is a COMPLETE match, while one hit on a fourteen-chunk "
            "runbook is a marginal one. None when the stamp is absent "
            "(pre-stamp content), which consumers must read as 'unknown', "
            "never as 'small'."
        ),
    )

    rerank_score: Optional[float] = Field(
        default=None,
        description=(
            "The blended reranker score this hit was ORDERED by, when it came "
            "from a hybrid search. Relative to one candidate set, so it is not "
            "comparable across queries and no admission floor may be expressed "
            "in it — that is what ``score`` (raw cosine) is for. None on the "
            "pure-vector path, where ordering IS ``score``."
        ),
    )
