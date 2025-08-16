# File: faultmaven/models/api.py

from pydantic import BaseModel, Field, model_validator
from typing import List, Optional, Dict, Any, Literal
from enum import Enum

# --- Enumerations for Explicit Contracts ---

class ResponseType(str, Enum):
    """Defines the agent's primary intent for this turn."""
    ANSWER = "ANSWER"
    PLAN_PROPOSAL = "PLAN_PROPOSAL"
    CLARIFICATION_REQUEST = "CLARIFICATION_REQUEST"
    CONFIRMATION_REQUEST = "CONFIRMATION_REQUEST"
    SOLUTION_READY = "SOLUTION_READY"
    NEEDS_MORE_DATA = "NEEDS_MORE_DATA"
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"

class SourceType(str, Enum):
    """Defines the origin of a piece of evidence."""
    KNOWLEDGE_BASE = "knowledge_base"
    LOG_FILE = "log_file"
    WEB_SEARCH = "web_search"

# --- Core Data Structures ---

class Source(BaseModel):
    """Represents a single piece of citable evidence to build user trust."""
    type: SourceType
    name: str  # e.g., "database_runbook.md"
    snippet: str

class PlanStep(BaseModel):
    """Represents one step in a multi-step plan."""
    description: str

class UploadedData(BaseModel):
    """A strongly-typed model for data uploaded by the user."""
    id: str
    name: str
    type: str

class ViewState(BaseModel):
    """
    A minimal, read-only snapshot of the investigation's state, sent to the
    frontend to keep its view in sync with the backend.
    """
    session_id: str
    case_id: str
    running_summary: str
    uploaded_data: List[UploadedData]

# --- Main Payloads ---

class QueryRequest(BaseModel):
    """The JSON payload sent from the frontend when the user asks a question."""
    session_id: str
    query: str
    context: Optional[Dict[str, Any]] = None

class AgentResponse(BaseModel):
    """The single, unified JSON payload returned from the backend."""
    
    schema_version: Literal["3.1.0"] = "3.1.0"
    content: str
    response_type: ResponseType
    view_state: ViewState
    sources: List[Source] = Field(default_factory=list)
    plan: Optional[List[PlanStep]] = None

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

class ErrorDetail(BaseModel):
    """A detailed error message."""
    code: str # e.g., "SESSION_NOT_FOUND"
    message: str

class ErrorResponse(BaseModel):
    """The standard JSON payload returned from the backend on failure."""
    schema_version: Literal["3.1.0"] = "3.1.0"
    error: ErrorDetail