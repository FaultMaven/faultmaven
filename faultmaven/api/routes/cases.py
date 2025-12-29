"""Case Management API Routes (TASK-014)

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

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, Query, status

from faultmaven.api.dependencies import get_api_case_service
from faultmaven.api.models import (
    CaseCreateRequest,
    CaseListResponse,
    CaseResponse,
    CaseUpdateRequest,
)
from faultmaven.exceptions import NotFoundError
from faultmaven.models.case import CaseSeverity, CaseStatus
from faultmaven.services.case_service import APICaseService


router = APIRouter(prefix="/api/v1/cases", tags=["Cases"])


@router.post("", response_model=CaseResponse, status_code=status.HTTP_201_CREATED)
async def create_case(
    request: CaseCreateRequest,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    user_id: str = Header(..., alias="X-User-ID"),
    case_service: APICaseService = Depends(get_api_case_service),
) -> CaseResponse:
    """Create a new case.

    Creates a new troubleshooting case with the specified details.

    Headers:
        X-Organization-ID: Organization identifier (required)
        X-User-ID: User identifier (required)

    Args:
        request: Case creation request with title, description, severity
        organization_id: Organization creating the case
        user_id: User creating the case
        case_service: Injected case service

    Returns:
        Created case details

    Raises:
        422: Validation error (missing headers or invalid request)
        500: Internal server error
    """
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
    organization_id: str = Header(..., alias="X-Organization-ID"),
    case_service: APICaseService = Depends(get_api_case_service),
) -> CaseResponse:
    """Get case by ID.

    Retrieves a specific case by its ID. The case must belong to the
    organization specified in the header.

    Headers:
        X-Organization-ID: Organization identifier (required)

    Args:
        case_id: Unique case identifier
        organization_id: Organization requesting the case
        case_service: Injected case service

    Returns:
        Case details if found and authorized

    Raises:
        404: Case not found or not accessible
        422: Missing required headers
    """
    case = await case_service.get_case(case_id, organization_id)

    if not case:
        raise NotFoundError("Case", case_id)

    return CaseResponse.from_domain(case)


@router.get("", response_model=CaseListResponse)
async def list_cases(
    organization_id: str = Header(..., alias="X-Organization-ID"),
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

    Headers:
        X-Organization-ID: Organization identifier (required)

    Query Parameters:
        status: Filter by case status (consulting, investigating, resolved, closed)
        severity: Filter by severity (low, medium, high, critical)
        assigned_to: Filter by assigned user ID
        limit: Maximum number of results (1-100, default 50)
        offset: Pagination offset (default 0)

    Args:
        organization_id: Organization requesting cases
        status_filter: Optional status filter
        severity: Optional severity filter
        assigned_to: Optional assignee filter
        limit: Page size
        offset: Pagination offset
        case_service: Injected case service

    Returns:
        Paginated list of cases with total count
    """
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
    organization_id: str = Header(..., alias="X-Organization-ID"),
    case_service: APICaseService = Depends(get_api_case_service),
) -> CaseResponse:
    """Update case.

    Updates specified fields of an existing case. Only provided
    fields will be updated; others remain unchanged.

    Headers:
        X-Organization-ID: Organization identifier (required)

    Args:
        case_id: Unique case identifier
        request: Fields to update
        organization_id: Organization owning the case
        case_service: Injected case service

    Returns:
        Updated case details

    Raises:
        404: Case not found
        403: Not authorized to update case
        422: Validation error
    """
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
    organization_id: str = Header(..., alias="X-Organization-ID"),
    case_service: APICaseService = Depends(get_api_case_service),
) -> None:
    """Delete case.

    Permanently deletes a case and all associated data:
    - All investigation sessions
    - All agent executions
    - All tool calls
    - All evidence artifacts

    This operation cannot be undone.

    Headers:
        X-Organization-ID: Organization identifier (required)

    Args:
        case_id: Unique case identifier
        organization_id: Organization owning the case
        case_service: Injected case service

    Raises:
        403: Not authorized to delete case
        404: Case not found
    """
    deleted = await case_service.delete_case(case_id, organization_id)

    if not deleted:
        raise NotFoundError("Case", case_id)


@router.post("/{case_id}/assign", response_model=CaseResponse)
async def assign_case(
    case_id: str,
    assigned_to: str = Body(..., embed=True),
    organization_id: str = Header(..., alias="X-Organization-ID"),
    case_service: APICaseService = Depends(get_api_case_service),
) -> CaseResponse:
    """Assign case to user.

    Assigns the case to a specific user for investigation.

    Headers:
        X-Organization-ID: Organization identifier (required)

    Body:
        assigned_to: User ID to assign the case to

    Args:
        case_id: Unique case identifier
        assigned_to: User ID to assign to
        organization_id: Organization owning the case
        case_service: Injected case service

    Returns:
        Updated case with new assignee

    Raises:
        404: Case not found
        403: Not authorized to assign case
        422: Validation error
    """
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
    organization_id: str = Header(..., alias="X-Organization-ID"),
    case_service: APICaseService = Depends(get_api_case_service),
) -> CaseResponse:
    """Close case with resolution.

    Closes the case with a resolution description. The case status
    will be set to RESOLVED if a solution was found.

    This action can be reversed using the reopen endpoint.

    Headers:
        X-Organization-ID: Organization identifier (required)

    Body:
        resolution: Resolution description explaining how the issue was resolved

    Args:
        case_id: Unique case identifier
        resolution: Resolution description
        organization_id: Organization owning the case
        case_service: Injected case service

    Returns:
        Updated case with closed status

    Raises:
        404: Case not found
        403: Not authorized to close case
        409: Case already closed
        422: Validation error
    """
    case = await case_service.close_case(
        case_id=case_id,
        organization_id=organization_id,
        resolution=resolution,
    )

    return CaseResponse.from_domain(case)


@router.post("/{case_id}/reopen", response_model=CaseResponse)
async def reopen_case(
    case_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    case_service: APICaseService = Depends(get_api_case_service),
) -> CaseResponse:
    """Reopen closed case.

    Reopens a previously closed case for continued investigation.
    The case status will be reset to CONSULTING.

    Headers:
        X-Organization-ID: Organization identifier (required)

    Args:
        case_id: Unique case identifier
        organization_id: Organization owning the case
        case_service: Injected case service

    Returns:
        Updated case with active status

    Raises:
        404: Case not found
        403: Not authorized to reopen case
        409: Case not closed
    """
    case = await case_service.reopen_case(
        case_id=case_id,
        organization_id=organization_id,
    )

    return CaseResponse.from_domain(case)


@router.get("/{case_id}/statistics")
async def get_case_statistics(
    case_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    case_service: APICaseService = Depends(get_api_case_service),
) -> Dict[str, Any]:
    """Get case-specific statistics.

    Returns statistics for a specific case including session counts,
    evidence counts, and investigation metrics.

    Headers:
        X-Organization-ID: Organization identifier (required)

    Args:
        case_id: Unique case identifier
        organization_id: Organization owning the case
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
