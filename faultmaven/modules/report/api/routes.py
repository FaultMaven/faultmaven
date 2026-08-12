"""Report Management API Routes (TASK-024)

Purpose: REST API endpoints for report generation, management, and case closure.

This module provides 7 CRITICAL endpoints for the Report Module:
1. POST   /reports/generate           - Generate report with LLM
2. GET    /reports/{report_id}        - Get report by ID
3. PUT    /reports/{report_id}        - Update report
4. DELETE /reports/{report_id}        - Delete report
5. GET    /reports/case/{case_id}     - List reports for case
6. GET    /reports/{report_id}/versions - Get version history
7. POST   /reports/{report_id}/link-case - Link to case closure

Authentication:
- JWT Bearer token: Authorization: Bearer <token>

Authorization:
- Every endpoint resolves the report's parent case through ``authorize_case_access``
  and 404s when it does not resolve: the caller must own the case or have it shared
  to one of their teams. Sharing an organization with the owner is not enough.

Design Reference: docs/architecture/document-generation-and-closure-design.md
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from faultmaven.api.exception_handlers import (
    is_quota_exhausted_service_error,
    quota_exhausted_http_exception,
)
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import (
    get_case_repository,
    get_case_service,
    get_report_generation_service,
    get_tenant_provider,
)
from faultmaven.config.tenant_context import get_current_org_id
from faultmaven.exceptions import (
    NotFoundError,
    ServiceException,
    ValidationException,
)
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.models.interfaces_case import ICaseService

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.contracts import (
    Case,
    CaseClosureRequest,
    CaseClosureResponse,
    CaseReport,
    ICaseRepository,
    ReportGenerationRequest,
    ReportGenerationResponse,
    ReportStatus,
    ReportType,
)
from faultmaven.providers.tenancy.base import TenantProvider
from faultmaven.utils.serialization import to_json_compatible

# Create router
router = APIRouter(prefix="/reports", tags=["reports"])

# Set up logging
logger = logging.getLogger(__name__)


# ============================================================
# API Request/Response Models
# ============================================================


class ReportResponse(BaseModel):
    """API response model for a report."""

    report_id: str
    case_id: str
    report_type: str
    title: str
    content: str
    format: str
    generation_status: str
    generated_at: str
    generation_time_ms: int
    is_current: bool
    version: int
    linked_to_closure: bool
    metadata: Optional[dict] = None

    @classmethod
    def from_domain(cls, report: CaseReport) -> "ReportResponse":
        """Create ReportResponse from domain CaseReport model."""
        return cls(
            report_id=report.report_id,
            case_id=report.case_id,
            report_type=report.report_type.value,
            title=report.title,
            content=report.content,
            format=report.format,
            generation_status=report.generation_status.value,
            generated_at=report.generated_at,
            generation_time_ms=report.generation_time_ms,
            is_current=report.is_current,
            version=report.version,
            linked_to_closure=report.linked_to_closure,
            metadata=report.metadata.model_dump() if report.metadata else None,
        )


class ReportListResponse(BaseModel):
    """API response for list of reports."""

    reports: List[ReportResponse]
    total: int
    case_id: str


class ReportUpdateRequest(BaseModel):
    """Request model for updating a report."""

    title: Optional[str] = Field(None, min_length=10, max_length=200)
    content: Optional[str] = Field(
        None, description="Updated report content in markdown"
    )


class ReportVersionResponse(BaseModel):
    """Version history entry for a report."""

    report_id: str
    version: int
    title: str
    generated_at: str
    is_current: bool
    linked_to_closure: bool


class ReportVersionListResponse(BaseModel):
    """List of report versions."""

    versions: List[ReportVersionResponse]
    total: int


class LinkCaseRequest(BaseModel):
    """Request to link report to case closure."""

    closure_note: Optional[str] = Field(None, max_length=500)


class LinkCaseResponse(BaseModel):
    """Response after linking report to case closure."""

    status: str
    message: str
    report_id: str
    case_id: str
    linked_at: str


# ============================================================
# Helper Functions
# ============================================================


def check_case_repository_available(
    case_repository: Optional[ICaseRepository],
) -> ICaseRepository:
    """Check if case repository is available and raise appropriate error if not."""
    if case_repository is None:
        raise HTTPException(
            status_code=503,
            detail="Case repository unavailable - report service not available",
        )
    return case_repository


def check_case_service_available(case_service: Optional[ICaseService]) -> ICaseService:
    """Check if case service is available."""
    if case_service is None:
        raise HTTPException(status_code=503, detail="Case service unavailable")
    return case_service


def check_tenant_provider_available(
    tenant_provider: Optional[TenantProvider],
) -> TenantProvider:
    """Check if tenant provider is available."""
    if tenant_provider is None:
        raise HTTPException(status_code=503, detail="Tenant provider unavailable")
    return tenant_provider


async def validate_organization_access(
    tenant_provider: TenantProvider,
    current_user: UserDTO,
    case_organization_id: Optional[str] = None,
) -> None:
    """Validate user has access to the organization context.

    Args:
        tenant_provider: TenantProvider for organization resolution
        current_user: Authenticated user
        case_organization_id: Optional organization ID from case (for validation)

    Raises:
        HTTPException: 403 if user lacks organization access
    """
    try:
        # Resolve the current organization from the request-bound tenant context
        # (tenant_scope middleware -> config.tenant_context). In single-tenant mode
        # the provider ignores the id and returns the default org; in multi-tenant
        # mode it validates the caller's membership in that org. Passing it is what
        # keeps this check working under multi — the provider otherwise raises
        # "organization_id required" and every report call fails closed with 403.
        organization = await tenant_provider.get_current_organization(
            current_user, organization_id=get_current_org_id()
        )

        # If case has organization_id, verify it matches
        if (
            case_organization_id
            and organization.organization_id != case_organization_id
        ):
            logger.warning(
                "Organization access denied",
                extra={
                    "user_id": current_user.user_id,
                    "requested_org": case_organization_id,
                    "user_org": organization.organization_id,
                },
            )
            raise HTTPException(
                status_code=403,
                detail="Access denied - resource belongs to different organization",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Organization validation failed: {e}")
        raise HTTPException(
            status_code=403, detail="Unable to validate organization access"
        )


async def authorize_case_access(
    case_id: Optional[str],
    current_user: UserDTO,
    tenant_provider: Optional[TenantProvider],
    case_service: Optional[ICaseService],
    not_found_detail: str,
) -> Case:
    """Authorize the caller against the case a report hangs off.

    Deny, don't skip (#1044). ``CaseService.get_case`` returns ``None`` for BOTH
    "no such case" and "you may not see this case" — the owner ∪ shared-to-my-teams
    gate that transitively guards reports, exports, analytics and messages. The
    report routes used to read that ``None`` as "nothing to compare organizations
    against" and fall through to a ``case_id``/``report_id``-only repository read,
    leaving the organization as the only boundary: two users in one org could read,
    edit and delete each other's reports. Both meanings must deny.

    The organization check stays second and stays conditional on ``tenant_provider``:
    it can only be absent in a single-tenant deployment wired without an organization
    repository (``TENANT_PROVIDER=multi`` fails closed at container build instead), and
    the case gate above holds on its own there. ``case_service`` absent is a 503 — the
    gate cannot be evaluated, so nothing is served.

    Args:
        case_id: Case the report belongs to (falsy is treated as unauthorized)
        current_user: Authenticated caller
        tenant_provider: Tenant provider for the organization check, when wired
        case_service: Case service carrying the access gate
        not_found_detail: 404 body — phrase it after the resource the caller named
            (the report, not the case) so the response does not confirm existence

    Returns:
        The authorized case

    Raises:
        HTTPException: 404 if the case is missing or not the caller's, 403 if it
            belongs to another organization, 503 if the case service is unavailable
    """
    case_service = check_case_service_available(case_service)

    case = (
        await case_service.get_case(case_id, user_id=current_user.user_id)
        if case_id
        else None
    )
    if not case:
        raise HTTPException(status_code=404, detail=not_found_detail)

    if tenant_provider:
        await validate_organization_access(
            tenant_provider, current_user, case.organization_id
        )

    return case


# ============================================================
# Report Endpoints
# ============================================================


@router.post(
    "/generate",
    response_model=ReportGenerationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate reports for a case",
    description="Generate post-mortem, executive summary, or technical analysis reports using LLM",
)
@trace("api_generate_report")
async def generate_report(
    request: ReportGenerationRequest,
    case_id: str = Query(..., description="Case ID to generate reports for"),
    current_user: UserDTO = Depends(require_authentication),
    tenant_provider: Optional[TenantProvider] = Depends(get_tenant_provider),
    case_service: Optional[ICaseService] = Depends(get_case_service),
    generation_service=Depends(get_report_generation_service),
) -> ReportGenerationResponse:
    """Generate reports for a case.

    Supports terminal summary generation:
    - RESOLUTION_SUMMARY: Auto-generated for resolved cases
    - CLOSURE_SUMMARY: Auto-generated for closed cases

    Args:
        request: Report generation request with report types
        case_id: Case identifier
        current_user: Authenticated user
        case_service: Case service for case validation

    Returns:
        ReportGenerationResponse with generated reports

    Raises:
        401: Authentication required
        404: Case not found
        400: Invalid request or regeneration limit exceeded
        503: Service unavailable
    """
    case_service = check_case_service_available(case_service)

    # Validate tenant context if provider available (multi-tenant mode)
    if tenant_provider:
        await validate_organization_access(tenant_provider, current_user)

    logger.info(
        f"Generating reports for case",
        extra={
            "case_id": case_id,
            "user_id": current_user.user_id,
            "report_types": [t.value for t in request.report_types],
        },
    )

    try:
        # Authorize against the case (owner ∪ shared), then its organization
        case = await authorize_case_access(
            case_id,
            current_user,
            tenant_provider,
            case_service,
            f"Case {case_id} not found",
        )

        # Validate generation service is available
        if not generation_service:
            raise HTTPException(
                status_code=503, detail="Report generation service unavailable"
            )

        # Generate reports
        response = await generation_service.generate_reports(
            case=case, report_types=request.report_types
        )

        logger.info(
            f"Reports generated successfully",
            extra={"case_id": case_id, "report_count": len(response.reports)},
        )

        return response

    except HTTPException:
        raise
    except ValidationException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ServiceException as e:
        # Billing/quota exhaustion → 402 (operator must add credits), same
        # contract as the /turns endpoint. Otherwise a generic 500.
        if is_quota_exhausted_service_error(e):
            raise quota_exhausted_http_exception()
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{report_id}",
    response_model=ReportResponse,
    summary="Get report by ID",
    description="Retrieve a specific report by its UUID",
)
@trace("api_get_report")
async def get_report(
    report_id: str = Path(..., description="Report UUID"),
    current_user: UserDTO = Depends(require_authentication),
    tenant_provider: Optional[TenantProvider] = Depends(get_tenant_provider),
    case_repository: Optional[ICaseRepository] = Depends(get_case_repository),
    case_service: Optional[ICaseService] = Depends(get_case_service),
) -> ReportResponse:
    """Get report by ID.

    Args:
        report_id: Report UUID
        current_user: Authenticated user
        tenant_provider: Tenant provider for multi-tenant isolation
        case_repository: Case repository for report retrieval (TD-001: migrated from IReportStore)
        case_service: Case service for organization validation

    Returns:
        Report details

    Raises:
        401: Authentication required
        403: Access denied (wrong organization)
        404: Report not found
        503: Service unavailable
    """
    case_repository = check_case_repository_available(case_repository)

    logger.info(
        f"Getting report",
        extra={"report_id": report_id, "user_id": current_user.user_id},
    )

    try:
        report = await case_repository.get_report(report_id)

        if not report:
            raise HTTPException(status_code=404, detail=f"Report {report_id} not found")

        # Authorize against the parent case (owner ∪ shared), then its organization
        await authorize_case_access(
            report.case_id,
            current_user,
            tenant_provider,
            case_service,
            f"Report {report_id} not found",
        )

        return ReportResponse.from_domain(report)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting report {report_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve report")


@router.put(
    "/{report_id}",
    response_model=ReportResponse,
    summary="Update report",
    description="Update report title or content",
)
@trace("api_update_report")
async def update_report(
    report_id: str = Path(..., description="Report UUID"),
    request: ReportUpdateRequest = Body(...),
    current_user: UserDTO = Depends(require_authentication),
    tenant_provider: Optional[TenantProvider] = Depends(get_tenant_provider),
    case_repository: Optional[ICaseRepository] = Depends(get_case_repository),
    case_service: Optional[ICaseService] = Depends(get_case_service),
) -> ReportResponse:
    """Update existing report.

    Allows updating:
    - Title
    - Content (markdown)

    Note: Updates create new version in history.

    Args:
        report_id: Report UUID
        request: Update request with new values
        current_user: Authenticated user
        tenant_provider: Tenant provider for multi-tenant isolation
        case_repository: Case repository for report updates (TD-001: migrated from IReportStore)
        case_service: Case service for organization validation

    Returns:
        Updated report

    Raises:
        401: Authentication required
        403: Access denied (wrong organization)
        404: Report not found
        400: Validation error
    """
    case_repository = check_case_repository_available(case_repository)

    logger.info(
        f"Updating report",
        extra={"report_id": report_id, "user_id": current_user.user_id},
    )

    try:
        # Get existing report
        existing_report = await case_repository.get_report(report_id)
        if not existing_report:
            raise HTTPException(status_code=404, detail=f"Report {report_id} not found")

        # Authorize against the parent case (owner ∪ shared), then its organization
        await authorize_case_access(
            existing_report.case_id,
            current_user,
            tenant_provider,
            case_service,
            f"Report {report_id} not found",
        )

        # Update fields
        updated_title = request.title if request.title else existing_report.title
        updated_content = (
            request.content if request.content else existing_report.content
        )

        # Create updated report (new version)
        # Note: When creating a new version via update, generated_at is set to current time
        # updated_at will be set by repository's update_report method
        now = datetime.now(timezone.utc)
        generated_at_str = to_json_compatible(now)
        updated_report = CaseReport(
            report_id=existing_report.report_id,
            case_id=existing_report.case_id,
            report_type=existing_report.report_type,
            title=updated_title,
            content=updated_content,
            format=existing_report.format,
            generation_status=existing_report.generation_status,
            generated_at=generated_at_str,  # New version generated now
            updated_at=None,  # Will be set by repository.update_report to current time
            generation_time_ms=0,  # Not regenerated (manual edit)
            is_current=True,
            version=existing_report.version + 1,
            linked_to_closure=existing_report.linked_to_closure,
            metadata=existing_report.metadata,
        )

        # Save updated report
        updated_report = await case_repository.update_report(updated_report)

        logger.info(
            f"Report updated",
            extra={"report_id": report_id, "new_version": updated_report.version},
        )

        return ReportResponse.from_domain(updated_report)

    except HTTPException:
        raise
    except ValidationException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating report {report_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update report")


@router.delete(
    "/{report_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete report",
    description="Permanently delete a report (runbooks cannot be deleted)",
)
@trace("api_delete_report")
async def delete_report(
    report_id: str = Path(..., description="Report UUID"),
    current_user: UserDTO = Depends(require_authentication),
    tenant_provider: Optional[TenantProvider] = Depends(get_tenant_provider),
    case_repository: Optional[ICaseRepository] = Depends(get_case_repository),
    case_service: Optional[ICaseService] = Depends(get_case_service),
) -> None:
    """Delete report.

    Permanently deletes a report. This operation cannot be undone.

    Note: Runbooks CANNOT be deleted via this endpoint as they persist
    independently in the knowledge base for similarity search.

    Args:
        report_id: Report UUID
        current_user: Authenticated user
        tenant_provider: Tenant provider for multi-tenant isolation
        case_repository: Case repository for report deletion (TD-001: migrated from IReportStore)
        case_service: Case service for organization validation

    Raises:
        401: Authentication required
        403: Access denied (wrong organization) or attempt to delete runbook
        404: Report not found
    """
    case_repository = check_case_repository_available(case_repository)

    logger.info(
        f"Deleting report",
        extra={"report_id": report_id, "user_id": current_user.user_id},
    )

    try:
        # Get report to validate it exists
        report = await case_repository.get_report(report_id)
        if not report:
            raise HTTPException(status_code=404, detail=f"Report {report_id} not found")

        # Authorize against the parent case (owner ∪ shared), then its organization.
        # Ordered ahead of the runbook rule so the 403 below cannot tell an
        # unauthorized caller what type someone else's report is.
        await authorize_case_access(
            report.case_id,
            current_user,
            tenant_provider,
            case_service,
            f"Report {report_id} not found",
        )

        # Check if runbook - runbooks cannot be deleted
        if report.report_type == ReportType.RUNBOOK:
            raise HTTPException(
                status_code=403,
                detail="Runbooks cannot be deleted - they persist independently in the knowledge base",
            )

        # Actually delete the report
        deleted = await case_repository.delete_report(report_id)
        if not deleted:
            raise HTTPException(status_code=500, detail="Failed to delete report")

        logger.info(
            f"Report deleted successfully",
            extra={"report_id": report_id, "case_id": report.case_id},
        )

    except HTTPException:
        raise
    except ServiceException as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error(f"Error deleting report {report_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete report")


@router.get(
    "/case/{case_id}",
    response_model=ReportListResponse,
    summary="List reports for case",
    description="Get all reports associated with a specific case",
)
@trace("api_list_reports_for_case")
async def list_reports_for_case(
    case_id: str = Path(..., description="Case UUID"),
    include_history: bool = Query(
        False, description="Include all versions or only current"
    ),
    report_type: Optional[str] = Query(None, description="Filter by report type"),
    current_user: UserDTO = Depends(require_authentication),
    tenant_provider: Optional[TenantProvider] = Depends(get_tenant_provider),
    case_repository: Optional[ICaseRepository] = Depends(get_case_repository),
    case_service: Optional[ICaseService] = Depends(get_case_service),
) -> ReportListResponse:
    """List all reports for a case.

    Args:
        case_id: Case UUID
        include_history: If True, include all versions; if False, only current
        report_type: Optional filter by report type (resolution_summary, closure_summary, runbook)
        current_user: Authenticated user
        tenant_provider: Tenant provider for multi-tenant isolation
        case_repository: Case repository for report retrieval (TD-001: migrated from IReportStore)
        case_service: Case service for organization validation

    Returns:
        List of reports for the case

    Raises:
        401: Authentication required
        403: Access denied (wrong organization)
        404: Case not found
    """
    case_repository = check_case_repository_available(case_repository)

    # Authorize against the case (owner ∪ shared), then its organization
    await authorize_case_access(
        case_id,
        current_user,
        tenant_provider,
        case_service,
        f"Case {case_id} not found",
    )

    logger.info(
        f"Listing reports for case",
        extra={
            "case_id": case_id,
            "user_id": current_user.user_id,
            "include_history": include_history,
        },
    )

    try:
        # Parse report type filter
        type_filter = None
        if report_type:
            try:
                type_filter = ReportType(report_type)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid report type: {report_type}. Valid types: resolution_summary, closure_summary, runbook",
                )

        # Get reports
        reports = await case_repository.get_reports(
            case_id=case_id, report_type=type_filter, include_history=include_history
        )

        return ReportListResponse(
            reports=[ReportResponse.from_domain(r) for r in reports],
            total=len(reports),
            case_id=case_id,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing reports for case {case_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list reports")


@router.get(
    "/{report_id}/versions",
    response_model=ReportVersionListResponse,
    summary="Get report version history",
    description="Retrieve all versions of a report",
)
@trace("api_get_report_versions")
async def get_report_versions(
    report_id: str = Path(..., description="Report UUID"),
    current_user: UserDTO = Depends(require_authentication),
    tenant_provider: Optional[TenantProvider] = Depends(get_tenant_provider),
    case_repository: Optional[ICaseRepository] = Depends(get_case_repository),
    case_service: Optional[ICaseService] = Depends(get_case_service),
) -> ReportVersionListResponse:
    """Get version history for a report.

    Returns all versions of the report, sorted by version number (descending).

    Args:
        report_id: Report UUID
        current_user: Authenticated user
        tenant_provider: Tenant provider for multi-tenant isolation
        case_repository: Case repository for report retrieval (TD-001: migrated from IReportStore)
        case_service: Case service for organization validation

    Returns:
        List of report versions

    Raises:
        401: Authentication required
        403: Access denied (wrong organization)
        404: Report not found
    """
    case_repository = check_case_repository_available(case_repository)

    logger.info(
        f"Getting report versions",
        extra={"report_id": report_id, "user_id": current_user.user_id},
    )

    try:
        # Get current report to find case_id
        report = await case_repository.get_report(report_id)
        if not report:
            raise HTTPException(status_code=404, detail=f"Report {report_id} not found")

        # Authorize against the parent case (owner ∪ shared), then its organization
        await authorize_case_access(
            report.case_id,
            current_user,
            tenant_provider,
            case_service,
            f"Report {report_id} not found",
        )

        # Get all versions for this report type in this case
        all_reports = await case_repository.get_reports(
            case_id=report.case_id, report_type=report.report_type, include_history=True
        )

        # Build version list
        versions = [
            ReportVersionResponse(
                report_id=r.report_id,
                version=r.version,
                title=r.title,
                generated_at=r.generated_at,
                is_current=r.is_current,
                linked_to_closure=r.linked_to_closure,
            )
            for r in all_reports
        ]

        # Sort by version descending
        versions.sort(key=lambda v: v.version, reverse=True)

        return ReportVersionListResponse(versions=versions, total=len(versions))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error getting versions for report {report_id}: {e}", exc_info=True
        )
        raise HTTPException(status_code=500, detail="Failed to get report versions")


@router.post(
    "/{report_id}/link-case",
    response_model=LinkCaseResponse,
    summary="Link report to case closure",
    description="Mark case as closed and link final report",
)
@trace("api_link_report_to_case")
async def link_report_to_case_closure(
    report_id: str = Path(..., description="Report UUID"),
    request: LinkCaseRequest = Body(default=LinkCaseRequest()),
    current_user: UserDTO = Depends(require_authentication),
    tenant_provider: Optional[TenantProvider] = Depends(get_tenant_provider),
    case_repository: Optional[ICaseRepository] = Depends(get_case_repository),
    case_service: Optional[ICaseService] = Depends(get_case_service),
) -> LinkCaseResponse:
    """Link report to case closure.

    This endpoint:
    1. Marks the report as linked to closure
    2. Gets all current reports for the case
    3. Links them to the case closure

    Args:
        report_id: Report UUID
        request: Optional closure note
        current_user: Authenticated user
        tenant_provider: Tenant provider for multi-tenant isolation
        case_repository: Case repository for report updates (TD-001: migrated from IReportStore)
        case_service: Case service

    Returns:
        Success response with closure details

    Raises:
        401: Authentication required
        403: Access denied (wrong organization)
        404: Report or case not found
        400: Report already linked or case not in valid state
    """
    case_repository = check_case_repository_available(case_repository)
    case_service = check_case_service_available(case_service)

    logger.info(
        f"Linking report to case closure",
        extra={"report_id": report_id, "user_id": current_user.user_id},
    )

    try:
        # Get report
        report = await case_repository.get_report(report_id)
        if not report:
            raise HTTPException(status_code=404, detail=f"Report {report_id} not found")

        # Authorize against the parent case (owner ∪ shared), then its organization
        await authorize_case_access(
            report.case_id,
            current_user,
            tenant_provider,
            case_service,
            f"Report {report_id} not found",
        )

        # Check if already linked
        if report.linked_to_closure:
            raise HTTPException(
                status_code=400, detail="Report already linked to case closure"
            )

        # Get all current reports for the case
        current_reports = await case_repository.get_reports(
            case_id=report.case_id, only_current=True
        )
        report_ids = [r.report_id for r in current_reports]

        # Mark reports as linked by updating each one
        for r in current_reports:
            r.linked_to_closure = True
            await case_repository.update_report(r)

        linked_at = to_json_compatible(datetime.now(timezone.utc))

        logger.info(
            f"Reports linked to case closure",
            extra={"case_id": report.case_id, "report_count": len(report_ids)},
        )

        return LinkCaseResponse(
            status="success",
            message=f"Linked {len(report_ids)} reports to case closure",
            report_id=report_id,
            case_id=report.case_id,
            linked_at=linked_at,
        )

    except HTTPException:
        raise
    except ValidationException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error linking report {report_id} to closure: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to link report to case closure"
        )
