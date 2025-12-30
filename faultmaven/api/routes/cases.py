"""Case Management API Routes (TASK-014, TASK-017)

Purpose: FastAPI routes for case management operations.

Endpoints:
- POST   /api/v1/cases           - Create new case
- GET    /api/v1/cases           - List cases for organization
- GET    /api/v1/cases/{case_id} - Get case by ID
- PATCH  /api/v1/cases/{case_id} - Update case
- DELETE /api/v1/cases/{case_id} - Delete case
- POST   /api/v1/cases/{case_id}/assign - Assign case
- POST   /api/v1/cases/{case_id}/close  - Close case
- POST   /api/v1/cases/{case_id}/reopen - Reopen case
- GET    /api/v1/cases/{case_id}/statistics - Get case statistics

Authentication:
- JWT Bearer token (preferred): Authorization: Bearer <token>
- Legacy headers (deprecated): X-Organization-ID, X-User-ID

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, Query, status

from faultmaven.api.dependencies import get_api_case_service
from faultmaven.api.middleware.auth import get_current_user_optional
from faultmaven.api.models import (
    CaseCreateRequest,
    CaseListResponse,
    CaseResponse,
    CaseUpdateRequest,
)
from faultmaven.exceptions import NotFoundError
from faultmaven.models.auth import AuthenticatedUser
from faultmaven.models.case import CaseSeverity, CaseStatus
from faultmaven.services.case_service import APICaseService


router = APIRouter(prefix="/api/v1/cases", tags=["Cases"])


# ============================================================
# Backwards-Compatible Authentication Helper
# ============================================================


async def get_auth_context(
    current_user: Optional[AuthenticatedUser] = Depends(get_current_user_optional),
    legacy_org_id: Optional[str] = Header(None, alias="X-Organization-ID"),
    legacy_user_id: Optional[str] = Header(None, alias="X-User-ID"),
) -> tuple[str, str]:
    """Get authentication context from JWT or legacy headers.

    Supports both JWT authentication and legacy header-based auth.
    JWT takes precedence when provided.

    Args:
        current_user: Authenticated user from JWT (optional)
        legacy_org_id: Legacy X-Organization-ID header
        legacy_user_id: Legacy X-User-ID header

    Returns:
        Tuple of (organization_id, user_id)

    Raises:
        HTTPException 401: If neither JWT nor legacy headers provided
    """
    if current_user:
        # JWT authentication - preferred
        return current_user.organization_id, current_user.user_id

    # Fall back to legacy headers
    if legacy_org_id and legacy_user_id:
        return legacy_org_id, legacy_user_id

    # At least org_id must be provided for read operations
    if legacy_org_id:
        return legacy_org_id, legacy_user_id or ""

    # No authentication provided
    from fastapi import HTTPException
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required. Provide Bearer token or X-Organization-ID header.",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ============================================================
# Case Endpoints
# ============================================================


@router.post("", response_model=CaseResponse, status_code=status.HTTP_201_CREATED)
async def create_case(
    request: CaseCreateRequest,
    auth_context: tuple[str, str] = Depends(get_auth_context),
    case_service: APICaseService = Depends(get_api_case_service),
) -> CaseResponse:
    """Create a new case.

    Creates a new troubleshooting case with the specified details.

    Authentication:
        - JWT Bearer token (preferred): Authorization: Bearer <token>
        - Legacy headers (deprecated): X-Organization-ID, X-User-ID

    Args:
        request: Case creation request with title, description, severity
        auth_context: Authentication context (organization_id, user_id)
        case_service: Injected case service

    Returns:
        Created case details

    Raises:
        401: Authentication required
        422: Validation error (invalid request)
        500: Internal server error
    """
    organization_id, user_id = auth_context

    if not user_id:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User ID required for case creation",
        )

    case = await case_service.create_case(
        user_id=user_id,
        organization_id=organization_id,
        title=request.title,
        description=request.description,
        severity=request.severity,
        metadata=request.metadata,
    )

    return CaseResponse.from_domain(case, severity=request.severity)


@router.get("/{case_id}", response_model=CaseResponse)
async def get_case(
    case_id: str,
    auth_context: tuple[str, str] = Depends(get_auth_context),
    case_service: APICaseService = Depends(get_api_case_service),
) -> CaseResponse:
    """Get case by ID.

    Retrieves a specific case by its ID. The case must belong to the
    authenticated user's organization.

    Authentication:
        - JWT Bearer token (preferred): Authorization: Bearer <token>
        - Legacy headers (deprecated): X-Organization-ID

    Args:
        case_id: Unique case identifier
        auth_context: Authentication context (organization_id, user_id)
        case_service: Injected case service

    Returns:
        Case details if found and authorized

    Raises:
        401: Authentication required
        404: Case not found or not accessible
    """
    organization_id, _ = auth_context

    case = await case_service.get_case(case_id, organization_id)

    if not case:
        raise NotFoundError("Case", case_id)

    return CaseResponse.from_domain(case)


@router.get("", response_model=CaseListResponse)
async def list_cases(
    auth_context: tuple[str, str] = Depends(get_auth_context),
    status_filter: Optional[CaseStatus] = Query(None, alias="status"),
    severity: Optional[CaseSeverity] = Query(None),
    assigned_to: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    case_service: APICaseService = Depends(get_api_case_service),
) -> CaseListResponse:
    """List cases for organization with filters.

    Retrieves a paginated list of cases belonging to the organization.
    Supports filtering by status, severity, and assignee.

    Authentication:
        - JWT Bearer token (preferred): Authorization: Bearer <token>
        - Legacy headers (deprecated): X-Organization-ID

    Query Parameters:
        status: Filter by case status (consulting, investigating, resolved, closed)
        severity: Filter by severity (low, medium, high, critical)
        assigned_to: Filter by assigned user ID
        limit: Maximum number of results (1-100, default 50)
        offset: Pagination offset (default 0)

    Args:
        auth_context: Authentication context (organization_id, user_id)
        status_filter: Optional status filter
        severity: Optional severity filter
        assigned_to: Optional assignee filter
        limit: Page size
        offset: Pagination offset
        case_service: Injected case service

    Returns:
        Paginated list of cases with total count
    """
    organization_id, _ = auth_context

    cases = await case_service.list_cases(
        organization_id=organization_id,
        status=status_filter,
        severity=severity,
        assigned_to=assigned_to,
        limit=limit,
        offset=offset,
    )

    items = [CaseResponse.from_domain(case) for case in cases]

    return CaseListResponse(
        items=items,
        total=len(items),  # Note: Full count would need service enhancement
        limit=limit,
        offset=offset,
    )


@router.patch("/{case_id}", response_model=CaseResponse)
async def update_case(
    case_id: str,
    request: CaseUpdateRequest,
    auth_context: tuple[str, str] = Depends(get_auth_context),
    case_service: APICaseService = Depends(get_api_case_service),
) -> CaseResponse:
    """Update case.

    Updates specified fields of an existing case. Only provided
    fields will be updated; others remain unchanged.

    Authentication:
        - JWT Bearer token (preferred): Authorization: Bearer <token>
        - Legacy headers (deprecated): X-Organization-ID

    Args:
        case_id: Unique case identifier
        request: Fields to update
        auth_context: Authentication context (organization_id, user_id)
        case_service: Injected case service

    Returns:
        Updated case details

    Raises:
        404: Case not found
        403: Not authorized to update case
        422: Validation error
    """
    organization_id, _ = auth_context

    # Build updates dict from non-None fields
    updates = {}
    if request.title is not None:
        updates["title"] = request.title
    if request.description is not None:
        updates["description"] = request.description
    if request.severity is not None:
        updates["severity"] = request.severity.value
    if request.status is not None:
        updates["status"] = request.status
    if request.assigned_to is not None:
        updates["assigned_to"] = request.assigned_to

    if not updates:
        # If no updates provided, just return current case
        case = await case_service.get_case(case_id, organization_id)
        if not case:
            raise NotFoundError("Case", case_id)
        return CaseResponse.from_domain(case)

    case = await case_service.update_case(
        case_id=case_id,
        organization_id=organization_id,
        updates=updates,
    )

    return CaseResponse.from_domain(case)


@router.delete("/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_case(
    case_id: str,
    auth_context: tuple[str, str] = Depends(get_auth_context),
    case_service: APICaseService = Depends(get_api_case_service),
) -> None:
    """Delete case.

    Permanently deletes a case and all associated data:
    - All investigation sessions
    - All agent executions
    - All tool calls
    - All evidence artifacts

    This operation cannot be undone.

    Authentication:
        - JWT Bearer token (preferred): Authorization: Bearer <token>
        - Legacy headers (deprecated): X-Organization-ID

    Args:
        case_id: Unique case identifier
        auth_context: Authentication context (organization_id, user_id)
        case_service: Injected case service

    Raises:
        403: Not authorized to delete case
        404: Case not found
    """
    organization_id, _ = auth_context

    deleted = await case_service.delete_case(case_id, organization_id)

    if not deleted:
        raise NotFoundError("Case", case_id)


@router.post("/{case_id}/assign", response_model=CaseResponse)
async def assign_case(
    case_id: str,
    assigned_to: str = Body(..., embed=True),
    auth_context: tuple[str, str] = Depends(get_auth_context),
    case_service: APICaseService = Depends(get_api_case_service),
) -> CaseResponse:
    """Assign case to user.

    Assigns the case to a specific user for investigation.

    Authentication:
        - JWT Bearer token (preferred): Authorization: Bearer <token>
        - Legacy headers (deprecated): X-Organization-ID

    Body:
        assigned_to: User ID to assign the case to

    Args:
        case_id: Unique case identifier
        assigned_to: User ID to assign to
        auth_context: Authentication context (organization_id, user_id)
        case_service: Injected case service

    Returns:
        Updated case with new assignee

    Raises:
        404: Case not found
        403: Not authorized to assign case
        422: Validation error
    """
    organization_id, _ = auth_context

    case = await case_service.assign_case(
        case_id=case_id,
        organization_id=organization_id,
        assigned_to=assigned_to,
    )

    return CaseResponse.from_domain(case)


@router.post("/{case_id}/close", response_model=CaseResponse)
async def close_case(
    case_id: str,
    resolution: str = Body(..., embed=True),
    auth_context: tuple[str, str] = Depends(get_auth_context),
    case_service: APICaseService = Depends(get_api_case_service),
) -> CaseResponse:
    """Close case with resolution.

    Closes the case with a resolution description. The case status
    will be set to RESOLVED if a solution was found.

    This action can be reversed using the reopen endpoint.

    Authentication:
        - JWT Bearer token (preferred): Authorization: Bearer <token>
        - Legacy headers (deprecated): X-Organization-ID

    Body:
        resolution: Resolution description explaining how the issue was resolved

    Args:
        case_id: Unique case identifier
        resolution: Resolution description
        auth_context: Authentication context (organization_id, user_id)
        case_service: Injected case service

    Returns:
        Updated case with closed status

    Raises:
        404: Case not found
        403: Not authorized to close case
        409: Case already closed
        422: Validation error
    """
    organization_id, _ = auth_context

    case = await case_service.close_case(
        case_id=case_id,
        organization_id=organization_id,
        resolution=resolution,
    )

    return CaseResponse.from_domain(case)


@router.post("/{case_id}/reopen", response_model=CaseResponse)
async def reopen_case(
    case_id: str,
    auth_context: tuple[str, str] = Depends(get_auth_context),
    case_service: APICaseService = Depends(get_api_case_service),
) -> CaseResponse:
    """Reopen closed case.

    Reopens a previously closed case for continued investigation.
    The case status will be reset to CONSULTING.

    Authentication:
        - JWT Bearer token (preferred): Authorization: Bearer <token>
        - Legacy headers (deprecated): X-Organization-ID

    Args:
        case_id: Unique case identifier
        auth_context: Authentication context (organization_id, user_id)
        case_service: Injected case service

    Returns:
        Updated case with active status

    Raises:
        404: Case not found
        403: Not authorized to reopen case
        409: Case not closed
    """
    organization_id, _ = auth_context

    case = await case_service.reopen_case(
        case_id=case_id,
        organization_id=organization_id,
    )

    return CaseResponse.from_domain(case)


@router.get("/{case_id}/statistics")
async def get_case_statistics(
    case_id: str,
    auth_context: tuple[str, str] = Depends(get_auth_context),
    case_service: APICaseService = Depends(get_api_case_service),
) -> Dict[str, Any]:
    """Get case-specific statistics.

    Returns statistics for a specific case including session counts,
    evidence counts, and investigation metrics.

    Authentication:
        - JWT Bearer token (preferred): Authorization: Bearer <token>
        - Legacy headers (deprecated): X-Organization-ID

    Args:
        case_id: Unique case identifier
        auth_context: Authentication context (organization_id, user_id)
        case_service: Injected case service

    Returns:
        Dictionary containing case statistics:
        - session_count: Number of investigation sessions
        - evidence_count: Number of evidence artifacts
        - total_token_usage: Total tokens used across sessions
        - investigation_duration_ms: Total investigation time

    Raises:
        404: Case not found
        403: Not authorized to view case
    """
    organization_id, _ = auth_context

    # First verify the case exists and is accessible
    case = await case_service.get_case(case_id, organization_id)
    if not case:
        raise NotFoundError("Case", case_id)

    # Get detailed case information with related entities
    case_details = await case_service.get_case_with_details(
        case_id=case_id,
        organization_id=organization_id,
        include_sessions=True,
        include_evidence=True,
        include_executions=True,
    )

    if not case_details:
        raise NotFoundError("Case", case_id)

    sessions = case_details.get("sessions", [])
    evidence = case_details.get("evidence", [])
    executions = case_details.get("executions", [])

    # Calculate statistics
    total_token_usage = sum(
        getattr(session, "total_token_usage", 0)
        for session in sessions
    )
    total_duration_ms = sum(
        getattr(session, "total_duration_ms", 0) or 0
        for session in sessions
    )

    return {
        "case_id": case_id,
        "session_count": len(sessions),
        "evidence_count": len(evidence),
        "execution_count": len(executions),
        "total_token_usage": total_token_usage,
        "investigation_duration_ms": total_duration_ms,
        "status": case.status.value,
    }
