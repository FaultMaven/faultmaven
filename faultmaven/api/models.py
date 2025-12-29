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

from faultmaven.models.case import CaseSeverity, CaseStatus
from faultmaven.models.investigation_session import SessionStatus
from faultmaven.models.evidence_artifact import EvidenceArtifactType


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
    assigned_to: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    closed_at: Optional[datetime] = None
    resolution: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_domain(cls, case: Any, severity: Optional[CaseSeverity] = None) -> "CaseResponse":
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
            if hasattr(case, 'problem_verification') and case.problem_verification:
                try:
                    case_severity = CaseSeverity.from_string(case.problem_verification.severity)
                except (ValueError, AttributeError):
                    case_severity = CaseSeverity.MEDIUM
            else:
                case_severity = CaseSeverity.MEDIUM

        # Get resolution from closure_reason if available
        resolution = None
        if hasattr(case, 'closure_reason') and case.closure_reason:
            resolution = case.closure_reason

        return cls(
            case_id=case.case_id,
            organization_id=case.organization_id,
            reporter_user_id=case.user_id,
            title=case.title,
            description=case.description,
            severity=case_severity,
            status=case.status,
            assigned_to=getattr(case, 'assigned_to', None),
            created_at=case.created_at,
            updated_at=case.updated_at,
            closed_at=getattr(case, 'closed_at', None),
            resolution=resolution,
            metadata=getattr(case, 'metadata', None),
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
        """Create EvidenceResponse from domain EvidenceArtifact model.

        Args:
            evidence: Domain EvidenceArtifact object

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
