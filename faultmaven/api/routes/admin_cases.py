"""Admin cross-tenant case listing (ADR-012 D9).

A platform-admin read path that lists cases across ALL users/orgs, so an
operator can see Copilot- and Slack-originated cases in one place instead of
logging in as each user. It is gated by:

  - ``require_platform_admin`` (platform-admin role), and
  - deployment mode: served in **standalone**; **403 in cloud** until an
    audited break-glass override exists (ADR-012 D7/D8). In cloud/Postgres,
    Row-Level Security would also scope the result to the operator's own org,
    so a cloud response would be misleadingly partial — fail closed instead.

Every access is recorded in the durable, append-only ``operator_access_audit``
table before any case data is returned; see ``api/operator_audit.py`` for that
policy and why it fails closed.
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from starlette.requests import Request

from faultmaven.api.middleware.auth import require_platform_admin
from faultmaven.api.operator_audit import (
    get_operator_audit_repository,
    record_operator_access,
)
from faultmaven.config.settings import get_settings
from faultmaven.models.api_models import (
    CaseListFilter,
    CaseListResponse,
    OperatorAccessAuditEntry,
    OperatorAccessAuditListResponse,
)
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.models.interfaces_operator_audit import (
    IOperatorAuditRepository,
    OperatorAction,
)
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.case.domain.models import CaseState

logger = logging.getLogger(__name__)


async def get_case_service(request: Request) -> ICaseService:
    """Get the CaseService from app.state (Composition Root)."""
    case_service = getattr(request.app.state, "case_service", None)
    if case_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Case service not available",
        )
    return case_service


router = APIRouter(
    prefix="/api/v1/admin",
    tags=["Admin - Cases"],
)


@router.get("/cases", response_model=CaseListResponse)
async def list_all_cases(
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    case_service: ICaseService = Depends(get_case_service),
    audit_repo: IOperatorAuditRepository = Depends(get_operator_audit_repository),
    state: Optional[CaseState] = Query(None, description="Filter by state"),
    source: Optional[Literal["copilot", "slack", "api"]] = Query(
        None, description="Filter by case source"
    ),
    limit: int = Query(50, ge=1, le=200, description="Items per page"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
) -> CaseListResponse:
    """List cases across all users/orgs for a platform-admin (ADR-012 D9)."""
    settings = get_settings()

    if settings.is_cloud:
        # Cross-tenant admin reads in cloud require an audited break-glass
        # override (ADR-012 D7/D8), not yet built. RLS would also scope the
        # result to the operator's own org, so fail closed rather than return
        # a misleading partial list.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Admin cross-tenant case listing is not available in cloud "
                "deployments yet (requires audited break-glass; ADR-012 D7/D9)."
            ),
        )

    # Record the privileged access BEFORE serving it (ADR-012 D8/D9). Ordered
    # this way so a crash between recording and responding leaves evidence of an
    # attempted access rather than none — the safe direction to be wrong in.
    # target_organization_id stays NULL: this list spans every tenant.
    await record_operator_access(
        audit_repo=audit_repo,
        operator=current_user,
        action=OperatorAction.LIST,
        deployment_mode=str(settings.deployment_mode),
        details={
            "state_filter": state.value if state else None,
            "source_filter": source,
            "limit": limit,
            "offset": offset,
        },
    )

    filters = CaseListFilter(state=state, source=source, limit=limit, offset=offset)
    summaries, total = await case_service.list_all_cases(filters)

    # Operational visibility only — the audit row above is the system of record.
    # Carries just the result sizes, which are known only after the query.
    logger.info(
        "admin_case_list_access",
        extra={
            "admin_user_id": current_user.user_id,
            "result_count": len(summaries),
            "total_count": total,
        },
    )

    return CaseListResponse(
        cases=summaries,
        total_count=total,
        limit=limit,
        offset=offset,
        # Robust to best-effort conversion drops: base "more pages?" on the
        # requested window vs. the repository's true total, not the rendered count.
        has_more=(offset + limit) < total,
    )


@router.get("/audit/operator-access", response_model=OperatorAccessAuditListResponse)
async def list_operator_access_audit(
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    audit_repo: IOperatorAuditRepository = Depends(get_operator_audit_repository),
    operator_user_id: Optional[str] = Query(
        None, description="Filter by the operator who performed the access"
    ),
    target_organization_id: Optional[str] = Query(
        None, description="Filter by the organization accessed"
    ),
    target_case_id: Optional[str] = Query(None, description="Filter by case accessed"),
    action: Optional[OperatorAction] = Query(
        None, description="Filter by access kind (list | content_open)"
    ),
    limit: int = Query(100, ge=1, le=500, description="Items per page"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
) -> OperatorAccessAuditListResponse:
    """Read the operator access trail (ADR-012 D8/D9).

    The review path over ``operator_access_audit`` — what an internal reviewer
    or a SOC 2 / ISO 27001 auditor reads to answer "who reached tenant data,
    when, and under what justification".

    Reading the trail is itself operator-only but is deliberately NOT recorded
    as an access: it returns no tenant content, and self-recording every read
    would make the table grow under its own review without adding evidence.

    Unlike the case list, this is served in cloud as well as standalone. It
    carries identifiers, an action and counts — never case titles or content —
    so no break-glass grant is required to read it, and withholding the trail
    in cloud would remove the governance record precisely where it matters most.
    """
    entries, total = await audit_repo.list_access(
        operator_user_id=operator_user_id,
        target_organization_id=target_organization_id,
        target_case_id=target_case_id,
        action=action,
        limit=limit,
        offset=offset,
    )

    return OperatorAccessAuditListResponse(
        # model_validate rather than a field-by-field copy, so a column added
        # by #815 surfaces automatically instead of being silently dropped.
        entries=[OperatorAccessAuditEntry.model_validate(e) for e in entries],
        total_count=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )
