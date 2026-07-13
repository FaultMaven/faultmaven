"""Admin cross-tenant case listing (ADR-012 D9).

A platform-admin read path that lists cases across ALL users/orgs, so an
operator can see Copilot- and Slack-originated cases in one place instead of
logging in as each user. It is gated by:

  - ``require_admin`` (platform-admin role), and
  - deployment mode: served in **standalone**; **403 in cloud** until an
    audited break-glass override exists (ADR-012 D7/D8). In cloud/Postgres,
    Row-Level Security would also scope the result to the operator's own org,
    so a cloud response would be misleadingly partial — fail closed instead.

Every access emits a structured audit log line (ADR-012 D8, "boundary now";
the durable audit table + break-glass workflow are deferred as "tooling later").
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from starlette.requests import Request

from faultmaven.api.middleware.auth import require_admin
from faultmaven.config.settings import get_settings
from faultmaven.models.api_models import CaseListFilter, CaseListResponse
from faultmaven.models.interfaces_case import ICaseService
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
    current_user: AuthenticatedUser = Depends(require_admin),
    case_service: ICaseService = Depends(get_case_service),
    state: Optional[CaseState] = Query(None, description="Filter by state"),
    source: Optional[str] = Query(
        None, description="Filter by case source (copilot | slack | api)"
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

    filters = CaseListFilter(state=state, source=source, limit=limit, offset=offset)
    summaries, total = await case_service.list_all_cases(filters)

    # Audit the privileged access (ADR-012 D8 "boundary now").
    logger.info(
        "admin_case_list_access",
        extra={
            "admin_user_id": current_user.user_id,
            "deployment_mode": str(settings.deployment_mode),
            "result_count": len(summaries),
            "total_count": total,
            "state_filter": state.value if state else None,
            "limit": limit,
            "offset": offset,
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
