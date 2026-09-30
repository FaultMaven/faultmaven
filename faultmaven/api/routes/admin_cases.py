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

  - tenancy, which decides which read can answer truthfully. The web process
    connects as the RLS-enforcing ``faultmaven_app`` role, so under
    ``TENANT_PROVIDER=multi`` an ordinary case query is scoped to the one
    enterprise the request is bound to — a list that would claim to span every
    tenant and show one. That deployment is served instead from the
    cross-enterprise metadata read (``ICaseMetadataReader``): ``SECURITY
    DEFINER`` functions whose result has no column sourced from a title, a
    description or any other user text, executable by the runtime role only.
    Under ``single`` every row carries the Standalone enterprise, so the
    RLS-scoped case query IS the complete list.

Every access is recorded in the durable, append-only ``operator_access_audit``
table before any case data is returned; see ``api/operator_audit.py`` for that
policy and why it fails closed. The recorded ``details`` name which view was
served, so the trail distinguishes a metadata read from a full one.
"""

import logging
from typing import List, Literal, Optional, Union

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
    bind_grant_enterprise_scope,
    get_operator_grant_repository,
    refuse_retired_filter,
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
from faultmaven.modules.case.contracts import (
    CaseMetadata,
    CaseMetadataNotGrantedError,
    CaseMetadataUnavailableError,
    ICaseMetadataReader,
)
from faultmaven.modules.case.domain.models.lifecycle import CaseState
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


async def get_case_metadata_reader(request: Request) -> Optional[ICaseMetadataReader]:
    """The cross-enterprise metadata read, or ``None`` where none is composed.

    Composed only under ``TENANT_PROVIDER=multi`` — the one deployment where an
    ordinary case query cannot list every tenant. Its absence there is refused
    by the route, never answered from the RLS-scoped list.
    """
    return getattr(request.app.state, "case_metadata_reader", None)


router = APIRouter(
    prefix="/api/v1/admin",
    tags=["Admin - Cases"],
)

#: Fixed text, never the database error: the operator needs to know which fix
#: applies, not the driver's message.
_NOT_GRANTED_DETAIL = (
    "Cross-enterprise case listing is not available: the application's database "
    "role lacks EXECUTE on the case metadata functions"
)


@router.get("/cases", response_model=AdminCaseListResult)
async def list_all_cases(
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    case_service: ICaseService = Depends(get_case_service),
    metadata_reader: Optional[ICaseMetadataReader] = Depends(get_case_metadata_reader),
    audit_repo: IOperatorAuditRepository = Depends(get_operator_audit_repository),
    state: Optional[CaseState] = Query(None, description="Filter by state"),
    source: Optional[Literal["copilot", "slack", "api"]] = Query(
        None, description="Filter by case source"
    ),
    limit: int = Query(50, ge=1, le=200, description="Items per page"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
) -> Union[AdminCaseListResponse, AdminCaseMetadataListResponse]:
    """List cases across all users and enterprises for a platform-admin (ADR-012 D9).

    Standalone serves full summaries; cloud serves metadata-only rows. Under
    multi-tenancy the rows come from the cross-enterprise metadata read. See the
    module docstring for why the split falls where it does.
    """
    settings = get_settings()
    # Keyed on tenancy alone, not on `is_cloud`: `multi` cannot boot outside
    # cloud today (``create_tenant_provider`` refuses), so the two are the same
    # condition — and if that ever changed, the metadata-only read is the
    # direction to be wrong in.
    cross_enterprise = requested_tenant_provider() == BUILTIN_MULTI
    metadata_only = cross_enterprise or settings.is_cloud

    if cross_enterprise and metadata_reader is None:
        # Fail closed. The RLS-scoped case query is right there, and it would
        # answer — with one enterprise's cases, under a list that claims to span
        # them all. Refused before recording: nothing was read.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cross-enterprise case listing is not available",
        )

    # Record the privileged access BEFORE serving it (ADR-012 D8/D9). Ordered
    # this way so a crash between recording and responding leaves evidence of an
    # attempted access rather than none — the safe direction to be wrong in.
    # target_enterprise_id stays NULL: this list spans every tenant.
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

    if cross_enterprise:
        try:
            metadata, total = await metadata_reader.list_case_metadata(
                state=state, source=source, limit=limit, offset=offset
            )
        except CaseMetadataNotGrantedError:
            # The functions exist but this role may not execute them: EXECUTE
            # is granted to the runtime role explicitly, never to PUBLIC. Same
            # fail-closed answer as a missing function, with a different fix.
            logger.error(
                "admin_case_list_unavailable: the database role lacks EXECUTE on "
                "the cross-enterprise case metadata functions"
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_NOT_GRANTED_DETAIL,
            )
        except CaseMetadataUnavailableError:
            # The database has not been migrated to the read. Same answer as an
            # unwired reader, and for the same reason: never the RLS-scoped list.
            logger.error(
                "admin_case_list_unavailable: the cross-enterprise case metadata "
                "functions are missing from the database"
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Cross-enterprise case listing is not available",
            )
        rows = _project_case_metadata(metadata)
        result_count = len(rows)
    else:
        # RLS-scoped to the bound enterprise, which under `single` every row
        # carries. A row stamped with another enterprise (an out-of-band write,
        # a rolled-back flip to `multi`) would be dropped silently, and no check
        # over the served rows can notice: RLS has already removed the evidence.
        # Detecting it would need a count from outside the policy — which the
        # metadata count function could give — and is not attempted here.
        filters = CaseListFilter(state=state, source=source, limit=limit, offset=offset)
        summaries, total = await case_service.list_all_cases(filters)
        result_count = len(summaries)

    # Operational visibility only — the audit row above is the system of record.
    # Carries just the result sizes, which are known only after the query.
    logger.info(
        "admin_case_list_access",
        extra={
            "admin_user_id": current_user.user_id,
            "result_count": result_count,
            "total_count": total,
        },
    )

    # Robust to best-effort conversion drops: base "more pages?" on the
    # requested window vs. the repository's true total, not the rendered count.
    has_more = (offset + limit) < total

    if cross_enterprise:
        return AdminCaseMetadataListResponse(
            cases=rows,
            total_count=total,
            limit=limit,
            offset=offset,
            has_more=has_more,
        )

    if metadata_only:
        # Projected at the boundary: the rows never leave this function
        # un-projected. This single-tenant read and the cross-enterprise one
        # above are two paths to the same row, kept in step by a parity test on
        # PostgreSQL that serves one fixture set through both.
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


def _project_case_metadata(metadata: List[CaseMetadata]) -> List[AdminCaseMetadata]:
    """Project the cross-enterprise rows, best-effort per case.

    The same per-case policy as ``CaseService.list_all_cases``, which drops a
    case whose summary does not validate: today that is a case whose owner
    account was deleted (``user_id`` is ``NULL``). Mirrored rather than
    improved on so the two paths serve the same rows; ``has_more`` is computed
    from the true total, so a dropped row cannot end pagination early.
    """
    rows: List[AdminCaseMetadata] = []
    for case in metadata:
        try:
            rows.append(AdminCaseMetadata.from_case_metadata(case))
        except ValueError as exc:
            logger.error(
                "Failed to project case %s to operator metadata: %s",
                case.case_id,
                exc,
            )
    return rows


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
        target_enterprise_id=access.target_enterprise_id,
        target_case_id=case_id,
        # Denormalised from the grant rather than left as a join: the audit row
        # is the evidence, and it must stay complete and readable even if the
        # grant row is ever lost.
        reason=grant.reason if grant else None,
        grant_id=grant.grant_id if grant else None,
        expires_at=grant.expires_at if grant else None,
        details={**details, "access": access.access},
    )

    bind_grant_enterprise_scope(access)
    return access


@router.get("/audit/operator-access", response_model=OperatorAccessAuditListResponse)
async def list_operator_access_audit(
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    audit_repo: IOperatorAuditRepository = Depends(get_operator_audit_repository),
    operator_user_id: Optional[str] = Query(
        None, description="Filter by the operator who performed the access"
    ),
    target_enterprise_id: Optional[str] = Query(
        None, description="Filter by the enterprise accessed"
    ),
    target_organization_id: Optional[str] = Query(
        None,
        include_in_schema=False,
        description=(
            "Retired (ADR-017). Declared only so it can be REFUSED: undeclared, "
            "it would be dropped and the caller handed the whole trail."
        ),
    ),
    target_case_id: Optional[str] = Query(None, description="Filter by case accessed"),
    action: Optional[OperatorAction] = Query(
        None, description="Filter by access kind (list | content_open)"
    ),
    grant_id: Optional[str] = Query(
        None, description="Filter to accesses taken under one break-glass grant"
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
    refuse_retired_filter(
        {"target_organization_id": target_organization_id}, "target_enterprise_id"
    )

    entries, total = await audit_repo.list_access(
        operator_user_id=operator_user_id,
        target_enterprise_id=target_enterprise_id,
        target_case_id=target_case_id,
        action=action,
        grant_id=grant_id,
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
