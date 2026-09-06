"""Break-glass grant management (ADR-012 D9, #815).

The operator surface for the time-boxed licenses that authorise reading a Cloud
tenant's case content: mint one, see which are live, end one early. The reads
those grants authorise live on the admin *cases* router; the durable record of
accesses taken under them is ``operator_access_audit``.

Design: ``docs/architecture/security/break-glass-content-access.md``.

Grants are managed in both deployments so there is one code path rather than
two, but they only *gate* anything in Cloud — Standalone content reads are
audited, not gated (D9), because the operator and the data controller are the
same party. A Standalone operator therefore never needs to mint one.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from faultmaven.api.middleware.auth import require_platform_admin
from faultmaven.api.operator_grants import (
    build_grant,
    get_operator_grant_repository,
    resolved_deployment_mode,
    validate_identifier,
)
from faultmaven.models.api_models import (
    BreakGlassGrant,
    BreakGlassGrantListResponse,
    BreakGlassGrantRequest,
)
from faultmaven.models.interfaces_operator_grant import IOperatorGrantRepository
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/grants",
    tags=["Admin - Break-glass"],
)


@router.post("", response_model=BreakGlassGrant, status_code=status.HTTP_201_CREATED)
async def create_grant(
    payload: BreakGlassGrantRequest,
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    grant_repo: IOperatorGrantRepository = Depends(get_operator_grant_repository),
) -> BreakGlassGrant:
    """Mint a break-glass grant over one case (ADR-012 D9).

    The grant covers exactly the case named, expires after ``ttl_minutes``, and
    cannot be extended — needing longer means minting a new one with a fresh
    reason, so a grant can never converge on standing access.

    An operator mints their own grant, and it is live immediately. The control
    is the justification, the window, and an immutable trail of every read taken
    under it — not a second party's consent. Customer-initiated approval is the
    stronger posture ADR-012 D9 describes as the ideal; the grant carries the
    ``approval_state`` machine that will drive it, so adding it later is a new
    transition rather than a reshaping of this endpoint or of the read gate.

    Nothing here touches tenant data: the request is not validated against the
    case it names. See ``build_grant`` for why that is the more secure choice.
    """
    grant = build_grant(
        operator=current_user,
        payload=payload,
        deployment_mode=resolved_deployment_mode(),
    )
    await grant_repo.create_grant(grant)

    # Operational visibility. The grant row itself is the system of record, and
    # the audit trail records what is actually *read* under it — minting a grant
    # discloses nothing on its own.
    logger.info(
        "break_glass_grant_created",
        extra={
            "grant_id": grant.grant_id,
            "operator_user_id": current_user.user_id,
            "target_case_id": grant.target_case_id,
            "target_enterprise_id": grant.target_enterprise_id,
            "expires_at": grant.expires_at.isoformat(),
        },
    )
    return BreakGlassGrant.from_domain(grant)


@router.get("", response_model=BreakGlassGrantListResponse)
async def list_grants(
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    grant_repo: IOperatorGrantRepository = Depends(get_operator_grant_repository),
    operator_user_id: Optional[str] = Query(
        None, description="Filter by the operator holding the grant"
    ),
    case_id: Optional[str] = Query(None, description="Filter by the case granted"),
    enterprise_id: Optional[str] = Query(
        None, description="Filter by the enterprise whose case was granted"
    ),
    live_only: bool = Query(
        False, description="Only grants that authorise a read right now"
    ),
    limit: int = Query(100, ge=1, le=500, description="Items per page"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
) -> BreakGlassGrantListResponse:
    """List break-glass grants, newest first.

    Deliberately **not** scoped to the calling operator. Who holds access to a
    tenant's content, and until when, is the governance question this surface
    exists to answer; an operator who could only see their own grants could not
    review anyone else's. Grants carry no case content — a reason, a case id and
    a window — so reading them needs no grant of its own, the same reasoning
    that lets the audit trail be read in Cloud.
    """
    grants, total = await grant_repo.list_grants(
        operator_user_id=operator_user_id,
        target_case_id=case_id,
        target_enterprise_id=enterprise_id,
        live_only=live_only,
        limit=limit,
        offset=offset,
    )
    return BreakGlassGrantListResponse(
        grants=[BreakGlassGrant.from_domain(g) for g in grants],
        total_count=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )


@router.post("/{grant_id}/revoke", response_model=BreakGlassGrant)
async def revoke_grant(
    grant_id: str,
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    grant_repo: IOperatorGrantRepository = Depends(get_operator_grant_repository),
) -> BreakGlassGrant:
    """End a grant before its TTL lapses.

    Any operator may revoke any grant, including one they do not hold: shortening
    someone's access is the safe direction, and requiring ownership would mean a
    grant could outlive the only person able to withdraw it.

    Idempotent — revoking an already-revoked grant leaves the original
    ``revoked_at`` in place rather than moving the record of when access ended.
    """
    validate_identifier(grant_id, "grant_id")

    grant = await grant_repo.revoke_grant(
        grant_id=grant_id, revoked_by=current_user.user_id
    )
    if grant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Grant not found"
        )

    logger.info(
        "break_glass_grant_revoked",
        extra={
            "grant_id": grant.grant_id,
            "revoked_by": current_user.user_id,
            "held_by": grant.operator_user_id,
        },
    )
    return BreakGlassGrant.from_domain(grant)
