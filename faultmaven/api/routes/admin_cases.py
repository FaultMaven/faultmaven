"""Admin cross-tenant case listing (ADR-012 D9).

A platform-admin read path that lists cases across ALL users/orgs, so an
operator can see Copilot- and Slack-originated cases in one place instead of
logging in as each user. It is gated by:

  - ``require_platform_admin`` (platform-admin role), and
  - deployment mode, which decides *what a row contains* rather than whether
    the endpoint answers at all (the D9 metadata/content split):

      * **standalone** — full summaries, titles included. The operator and the
        data controller are the same party; content reads are audited, not gated.
      * **cloud** — ambient metadata only (ids, org, state, timestamps, counts).
        Titles and transcripts are content, reachable only through the audited
        break-glass grant (#815).

  - tenancy, which decides whether a cross-tenant answer is *truthful*. The web
    process connects as the RLS-enforcing ``faultmaven_app`` role, so under
    ``TENANT_PROVIDER=multi`` this query is silently scoped to the operator's
    own organization — a list that claims to span every tenant but does not.
    That is refused (403) rather than served: an operator triaging "which tenant
    is stuck" would be misled by a partial answer in exactly the case where the
    endpoint exists. Under ``single`` (what cloud runs today) every row carries
    the Standalone org, so the RLS-scoped result IS the complete list.

Every access is recorded in the durable, append-only ``operator_access_audit``
table before any case data is returned; see ``api/operator_audit.py`` for that
policy and why it fails closed. The recorded ``details`` name which view was
served, so the trail distinguishes a metadata read from a full one.
"""

import logging
from typing import Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, status
from starlette.requests import Request

from faultmaven.api.middleware.auth import require_platform_admin
from faultmaven.api.operator_audit import (
    get_operator_audit_repository,
    record_operator_access,
)
from faultmaven.config.settings import get_settings
from faultmaven.models.api_models import (
    AdminCaseListResponse,
    AdminCaseListResult,
    AdminCaseMetadata,
    AdminCaseMetadataListResponse,
    CaseListFilter,
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
from faultmaven.providers.tenancy.factory import (
    BUILTIN_MULTI,
    requested_tenant_provider,
)

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


@router.get("/cases", response_model=AdminCaseListResult)
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
) -> Union[AdminCaseListResponse, AdminCaseMetadataListResponse]:
    """List cases across all users/orgs for a platform-admin (ADR-012 D9).

    Standalone serves full summaries; cloud serves metadata-only rows. See the
    module docstring for why the split falls where it does.
    """
    settings = get_settings()
    metadata_only = settings.is_cloud

    if requested_tenant_provider() == BUILTIN_MULTI:
        # RLS scopes this query to the operator's own organization, so the
        # "all tenants" list would silently be one tenant's. Refuse rather than
        # mislead; the cross-tenant read under multi-tenancy needs a bounded
        # bypass, which is designed with the break-glass path (#815).
        #
        # Keyed on tenancy alone, not on `is_cloud`: `multi` cannot boot outside
        # cloud today (``create_tenant_provider`` refuses), so the two are the
        # same condition — and if that ever changed, refusing is the direction
        # to be wrong in.
        #
        # It is keyed on CONFIG rather than on the served rows' orgs, which
        # leaves a residual gap: rows carrying a non-Standalone org under
        # `single` (an out-of-band write, a rolled-back flip) would be dropped by
        # RLS and this gate would stay quiet. Inspecting the result does not
        # close it — the rows that came back are RLS-filtered, so every one of
        # them carries the bound org by construction and the missing ones are
        # precisely what the session cannot see. Detecting that needs a count
        # from outside the policy, which is the bounded-bypass work deferred to
        # #815; a check over the visible rows would only look like a defense.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Cross-tenant case listing is not available under multi-tenant "
                "cloud: row-level security would scope the result to a single "
                "organization, so the list would be silently partial "
                "(ADR-012 D9)."
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
        # `deployment_mode` is a plain str on some settings paths and a
        # DeploymentMode member on others, so unwrap it the way `is_cloud`
        # does. A bare `str()` on the enum member yields
        # "DeploymentMode.STANDALONE"; these rows are append-only, so that
        # would leave the system of record permanently holding a value no
        # `deployment_mode = 'standalone'` query ever matches.
        deployment_mode=str(
            getattr(settings.deployment_mode, "value", settings.deployment_mode)
        ),
        details={
            "state_filter": state.value if state else None,
            "source_filter": source,
            "limit": limit,
            "offset": offset,
            # Which of the two D9 shapes the operator actually received. An
            # auditor reading this row needs to know whether case titles were
            # disclosed, and that is not derivable from the action alone.
            "view": "metadata" if metadata_only else "full",
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

    # Robust to best-effort conversion drops: base "more pages?" on the
    # requested window vs. the repository's true total, not the rendered count.
    has_more = (offset + limit) < total

    if metadata_only:
        # One query, one service call, projected at the boundary. A separate
        # metadata-only read path would be a second thing to keep in step with
        # the case model for no gain — the rows never leave this function
        # un-projected.
        return AdminCaseMetadataListResponse(
            cases=[AdminCaseMetadata.from_summary(s) for s in summaries],
            total_count=total,
            limit=limit,
            offset=offset,
            has_more=has_more,
        )

    return AdminCaseListResponse(
        cases=summaries,
        total_count=total,
        limit=limit,
        offset=offset,
        has_more=has_more,
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
        # model_validate rather than a field-by-field copy — one mapping to
        # keep in step instead of twelve. A field #815 adds to the domain
        # object is IGNORED until it is declared here too: Pydantic populates
        # only declared fields, so the API surface never widens by accident.
        entries=[OperatorAccessAuditEntry.model_validate(e) for e in entries],
        total_count=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )
