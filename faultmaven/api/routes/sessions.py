"""Investigation Session API Routes (TASK-014, TASK-017, TASK-020)

Purpose: FastAPI routes for investigation session management operations.

Endpoints:
- POST   /api/v1/cases/{case_id}/sessions           - Create session
- GET    /api/v1/cases/{case_id}/sessions           - List sessions for case
- GET    /api/v1/cases/{case_id}/sessions/active    - Get active session
- GET    /api/v1/cases/{case_id}/sessions/{session_id} - Get session by ID
- PATCH  /api/v1/cases/{case_id}/sessions/{session_id} - Update session
- POST   /api/v1/cases/{case_id}/sessions/{session_id}/pause    - Pause session
- POST   /api/v1/cases/{case_id}/sessions/{session_id}/resume   - Resume session
- POST   /api/v1/cases/{case_id}/sessions/{session_id}/complete - Complete session

Authentication:
- JWT Bearer token: Authorization: Bearer <token>

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

from typing import List, Optional

from fastapi import APIRouter, Body, Depends, Query, status

from faultmaven.api.dependencies import get_investigation_session_service
from faultmaven.api.middleware.auth import get_current_user
from faultmaven.api.models import (
    SessionCreateRequest,
    SessionListResponse,
    SessionResponse,
    SessionUpdateRequest,
)
from faultmaven.exceptions import NotFoundError
from faultmaven.models.auth import AuthenticatedUser
from faultmaven.models.investigation_session import SessionStatus
from faultmaven.modules.case.domain.services.investigation_session_service import (
    APIInvestigationSessionService,
)

router = APIRouter(prefix="/api/v1/cases/{case_id}/sessions", tags=["Sessions"])


# ============================================================
# Session Endpoints
# ============================================================


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    case_id: str,
    request: SessionCreateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session_service: APIInvestigationSessionService = Depends(
        get_investigation_session_service
    ),
) -> SessionResponse:
    """Create investigation session for case.

    Creates a new investigation session for the specified case.
    Only one active session is allowed per case at a time.

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>

    Args:
        case_id: Case to create session for
        request: Session creation request
        current_user: Authenticated user from JWT
        session_service: Injected session service

    Returns:
        Created session details

    Raises:
        401: Authentication required
        404: Case not found
        409: Active session already exists
        422: Validation error
    """
    session = await session_service.create_session(
        case_id=case_id,
        organization_id=current_user.organization_id,
        user_id=current_user.user_id,
        session_goal=request.session_goal,
        token_budget_limit=request.token_budget_limit,
        metadata=request.metadata,
    )

    return SessionResponse.from_domain(session)


@router.get("/active", response_model=Optional[SessionResponse])
async def get_active_session(
    case_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session_service: APIInvestigationSessionService = Depends(
        get_investigation_session_service
    ),
) -> Optional[SessionResponse]:
    """Get currently active session for case.

    Returns the currently active investigation session for a case,
    if one exists. Each case can have at most one active session.

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>

    Args:
        case_id: Case to get active session for
        current_user: Authenticated user from JWT
        session_service: Injected session service

    Returns:
        Active session if exists, null otherwise

    Raises:
        401: Authentication required
        404: Case not found
    """
    session = await session_service.get_active_session(
        case_id=case_id,
        organization_id=current_user.organization_id,
    )

    if not session:
        return None

    return SessionResponse.from_domain(session)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    case_id: str,
    session_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session_service: APIInvestigationSessionService = Depends(
        get_investigation_session_service
    ),
) -> SessionResponse:
    """Get session by ID.

    Retrieves a specific investigation session by its ID.
    The session must belong to a case owned by the organization.

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>

    Args:
        case_id: Case the session belongs to
        session_id: Unique session identifier
        current_user: Authenticated user from JWT
        session_service: Injected session service

    Returns:
        Session details if found and authorized

    Raises:
        401: Authentication required
        404: Session not found or case not found
    """
    session = await session_service.get_session(
        session_id, current_user.organization_id
    )

    if not session:
        raise NotFoundError("Session", session_id)

    # Verify session belongs to the specified case
    if session.case_id != case_id:
        raise NotFoundError("Session", session_id)

    return SessionResponse.from_domain(session)


@router.get("", response_model=List[SessionResponse])
async def list_sessions(
    case_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    status_filter: Optional[SessionStatus] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session_service: APIInvestigationSessionService = Depends(
        get_investigation_session_service
    ),
) -> List[SessionResponse]:
    """List sessions for case.

    Retrieves all investigation sessions for a case with optional filtering.

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>

    Query Parameters:
        status: Filter by session status (active, paused, completed, abandoned)
        limit: Maximum number of results (1-100, default 50)
        offset: Pagination offset (default 0)

    Args:
        case_id: Case to list sessions for
        current_user: Authenticated user from JWT
        status_filter: Optional status filter
        limit: Page size
        offset: Pagination offset
        session_service: Injected session service

    Returns:
        List of sessions for the case

    Raises:
        401: Authentication required
        404: Case not found
    """
    sessions = await session_service.list_sessions(
        case_id=case_id,
        organization_id=current_user.organization_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )

    return [SessionResponse.from_domain(session) for session in sessions]


@router.patch("/{session_id}", response_model=SessionResponse)
async def update_session(
    case_id: str,
    session_id: str,
    request: SessionUpdateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session_service: APIInvestigationSessionService = Depends(
        get_investigation_session_service
    ),
) -> SessionResponse:
    """Update session.

    Updates specified fields of an investigation session.
    Only session_goal, token_budget_limit, and metadata can be updated.

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>

    Args:
        case_id: Case the session belongs to
        session_id: Unique session identifier
        request: Fields to update
        current_user: Authenticated user from JWT
        session_service: Injected session service

    Returns:
        Updated session details

    Raises:
        401: Authentication required
        404: Session not found
        422: Validation error
    """
    # Build updates dict from non-None fields
    updates = {}
    if request.session_goal is not None:
        updates["session_goal"] = request.session_goal
    if request.token_budget_limit is not None:
        updates["token_budget_limit"] = request.token_budget_limit
    if request.metadata is not None:
        updates["metadata"] = request.metadata

    if not updates:
        # If no updates provided, just return current session
        session = await session_service.get_session(
            session_id, current_user.organization_id
        )
        if not session:
            raise NotFoundError("Session", session_id)
        if session.case_id != case_id:
            raise NotFoundError("Session", session_id)
        return SessionResponse.from_domain(session)

    session = await session_service.update_session(
        session_id=session_id,
        organization_id=current_user.organization_id,
        updates=updates,
    )

    # Verify session belongs to the specified case
    if session.case_id != case_id:
        raise NotFoundError("Session", session_id)

    return SessionResponse.from_domain(session)


@router.post("/{session_id}/pause", response_model=SessionResponse)
async def pause_session(
    case_id: str,
    session_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session_service: APIInvestigationSessionService = Depends(
        get_investigation_session_service
    ),
) -> SessionResponse:
    """Pause active session.

    Pauses an active investigation session. Only active sessions
    can be paused. Paused sessions can be resumed later.

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>

    Args:
        case_id: Case the session belongs to
        session_id: Unique session identifier
        current_user: Authenticated user from JWT
        session_service: Injected session service

    Returns:
        Updated session with paused status

    Raises:
        401: Authentication required
        404: Session not found
        400: Session not active (cannot pause)
    """
    session = await session_service.pause_session(
        session_id=session_id,
        organization_id=current_user.organization_id,
    )

    # Verify session belongs to the specified case
    if session.case_id != case_id:
        raise NotFoundError("Session", session_id)

    return SessionResponse.from_domain(session)


@router.post("/{session_id}/resume", response_model=SessionResponse)
async def resume_session(
    case_id: str,
    session_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session_service: APIInvestigationSessionService = Depends(
        get_investigation_session_service
    ),
) -> SessionResponse:
    """Resume paused session.

    Resumes a paused investigation session. Only paused sessions
    can be resumed.

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>

    Args:
        case_id: Case the session belongs to
        session_id: Unique session identifier
        current_user: Authenticated user from JWT
        session_service: Injected session service

    Returns:
        Updated session with active status

    Raises:
        401: Authentication required
        404: Session not found
        400: Session not paused (cannot resume)
    """
    session = await session_service.resume_session(
        session_id=session_id,
        organization_id=current_user.organization_id,
    )

    # Verify session belongs to the specified case
    if session.case_id != case_id:
        raise NotFoundError("Session", session_id)

    return SessionResponse.from_domain(session)


@router.post("/{session_id}/complete", response_model=SessionResponse)
async def complete_session(
    case_id: str,
    session_id: str,
    findings_summary: str = Body(..., embed=True),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session_service: APIInvestigationSessionService = Depends(
        get_investigation_session_service
    ),
) -> SessionResponse:
    """Complete session with findings.

    Completes an investigation session with a findings summary.
    This is a terminal action - completed sessions cannot be modified.

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>

    Body:
        findings_summary: Summary of investigation findings

    Args:
        case_id: Case the session belongs to
        session_id: Unique session identifier
        findings_summary: Summary of investigation findings
        current_user: Authenticated user from JWT
        session_service: Injected session service

    Returns:
        Updated session with completed status

    Raises:
        401: Authentication required
        404: Session not found
        400: Session already in terminal state
        422: Missing findings summary
    """
    session = await session_service.complete_session(
        session_id=session_id,
        organization_id=current_user.organization_id,
        findings_summary=findings_summary,
    )

    # Verify session belongs to the specified case
    if session.case_id != case_id:
        raise NotFoundError("Session", session_id)

    return SessionResponse.from_domain(session)
