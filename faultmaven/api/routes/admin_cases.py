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
from faultmaven.api.operator_grants import (
    OperatorContentAccess,
    authorize_content_read,
    bind_grant_org_scope,
    get_operator_grant_repository,
    resolved_deployment_mode,
    validate_identifier,
)
from faultmaven.config.settings import get_settings
from faultmaven.models.api_models import (
    AdminCaseContentResponse,
    AdminCaseListResponse,
    AdminCaseListResult,
    AdminCaseMessagesResponse,
    AdminCaseMetadata,
    AdminCaseMetadataListResponse,
    BreakGlassGrant,
    CaseDetail,
    CaseListFilter,
    OperatorAccessAuditEntry,
    OperatorAccessAuditListResponse,
)
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.models.interfaces_operator_audit import (
    IOperatorAuditRepository,
    OperatorAction,
)
from faultmaven.models.interfaces_operator_grant import IOperatorGrantRepository
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
        deployment_mode=resolved_deployment_mode(),
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


@router.get("/cases/{case_id}", response_model=AdminCaseContentResponse)
async def open_case_content(
    case_id: str,
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    case_service: ICaseService = Depends(get_case_service),
    audit_repo: IOperatorAuditRepository = Depends(get_operator_audit_repository),
    grant_repo: IOperatorGrantRepository = Depends(get_operator_grant_repository),
) -> AdminCaseContentResponse:
    """Open one case's **content** as an operator (ADR-012 D9).

    Standalone serves it under standing access, recorded but not gated. Cloud
    requires a live break-glass grant naming this case.

    This is a separate endpoint from ``GET /api/v1/cases/{case_id}`` rather than
    an operator arm on that route's owner ∪ shared-to-my-teams check. That check
    is the single-case gate transitively guarding reports, exports, analytics and
    messages, and it runs for every ordinary user request — widening it would
    widen all of those at once. Keeping the elevated path separate is also what
    makes the Standalone All Cases view openable (#846) without touching the
    user-facing route.
    """
    access = await _authorize_and_record_content_read(
        case_id=case_id,
        operator=current_user,
        audit_repo=audit_repo,
        grant_repo=grant_repo,
        details={"surface": "case_detail"},
    )

    # ``user_id=None`` drops the owner ∪ shared check in the service: this is the
    # operator read, authorized above and already recorded. It is the only place
    # that is true.
    case = await case_service.get_case(case_id)
    if not case:
        # Also the honest answer when a grant names a case that does not exist,
        # or one that belongs to a different organization than the grant claimed:
        # after the rebind the row is simply not visible. The failed attempt is
        # already in the trail.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Case not found"
        )

    detail = CaseDetail.from_case(case)
    detail.shared_team_ids = await case_service.get_case_team_ids(case_id)
    return AdminCaseContentResponse(
        access=access.access,
        grant=BreakGlassGrant.from_domain(access.grant) if access.grant else None,
        case=detail,
    )


@router.get("/cases/{case_id}/messages", response_model=AdminCaseMessagesResponse)
async def open_case_transcript(
    case_id: str,
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    case_service: ICaseService = Depends(get_case_service),
    audit_repo: IOperatorAuditRepository = Depends(get_operator_audit_repository),
    grant_repo: IOperatorGrantRepository = Depends(get_operator_grant_repository),
    limit: int = Query(50, ge=1, le=100, description="Messages per page"),
    offset: int = Query(0, ge=0, description="Number of messages to skip"),
) -> AdminCaseMessagesResponse:
    """Open one case's **transcript** as an operator (ADR-012 D9).

    Same gate, same audit action and same envelope as the case-detail read: a
    transcript is content by any reading of D9, and splitting the two surfaces
    across different rules would let one of them drift.
    """
    access = await _authorize_and_record_content_read(
        case_id=case_id,
        operator=current_user,
        audit_repo=audit_repo,
        grant_repo=grant_repo,
        details={"surface": "transcript", "limit": limit, "offset": offset},
    )

    # Existence is re-established through the same operator read the detail
    # endpoint uses, so a transcript cannot be fetched for a case the rebound
    # scope cannot see.
    case = await case_service.get_case(case_id)
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Case not found"
        )

    messages = await case_service.get_case_messages_enhanced(
        case_id=case_id, limit=limit, offset=offset, include_debug=False
    )
    return AdminCaseMessagesResponse(
        access=access.access,
        grant=BreakGlassGrant.from_domain(access.grant) if access.grant else None,
        messages=messages,
    )


async def _authorize_and_record_content_read(
    case_id: str,
    operator: AuthenticatedUser,
    audit_repo: IOperatorAuditRepository,
    grant_repo: IOperatorGrantRepository,
    details: dict,
) -> OperatorContentAccess:
    """Gate, record, and re-scope — in that order — for one content read.

    The order is the point:

    1. **Validate** the path id, so an over-long value is rejected rather than
       truncated into an immutable audit row naming a different, real case.
    2. **Authorize**, so an ungranted read never reaches the trail as though it
       had happened.
    3. **Record**, before any content is served, failing the request closed if
       the record cannot be written.
    4. **Rebind** the RLS scope to the granted organization, so the read that
       follows is bound to that tenant rather than escaping the policy.
    """
    validate_identifier(case_id, "case_id")

    access = await authorize_content_read(
        grant_repo=grant_repo, operator=operator, case_id=case_id
    )

    grant = access.grant
    await record_operator_access(
        audit_repo=audit_repo,
        operator=operator,
        action=OperatorAction.CONTENT_OPEN,
        deployment_mode=resolved_deployment_mode(),
        target_organization_id=access.target_organization_id,
        target_case_id=case_id,
        # Denormalised from the grant rather than left as a join: the audit row
        # is the evidence, and it must stay complete and readable even if the
        # grant row is ever lost.
        reason=grant.reason if grant else None,
        grant_id=grant.grant_id if grant else None,
        expires_at=grant.expires_at if grant else None,
        details={**details, "access": access.access},
    )

    bind_grant_org_scope(access)
    return access


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
