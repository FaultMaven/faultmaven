"""Break-glass authorization — shared API-layer policy (ADR-012 D9, #815).

The gate that decides whether an operator may read a tenant's case **content**,
and what has to happen around that read. It lives outside any one route module
for the same reason ``operator_audit`` does: the case-detail and transcript
endpoints must resolve identically, and a second endpoint added later must
inherit the decision rather than re-derive it.

Design: ``docs/architecture/security/break-glass-content-access.md``.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import HTTPException, status
from starlette.requests import Request

from faultmaven.config.settings import get_settings
from faultmaven.config.tenant_context import get_current_org_id, set_current_org_id
from faultmaven.models.api_models import (
    MAX_IDENTIFIER_LENGTH,
    BreakGlassGrantRequest,
)
from faultmaven.models.interfaces_operator_grant import (
    GrantApprovalState,
    IOperatorGrantRepository,
    OperatorAccessGrant,
)
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.providers.tenancy.factory import (
    BUILTIN_MULTI,
    requested_tenant_provider,
)

logger = logging.getLogger(__name__)


async def get_operator_grant_repository(request: Request) -> IOperatorGrantRepository:
    """Get the break-glass grant repository from app.state (Composition Root)."""
    repo = getattr(request.app.state, "operator_grant_repository", None)
    if repo is None:
        # Fail closed, not open: without the grant store there is no way to
        # establish that a read was authorised, so cloud content must be
        # unreachable rather than ungated.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Break-glass grant store not available",
        )
    return repo


def validate_identifier(value: str, field_name: str) -> str:
    """Reject an over-long identifier rather than letting it be truncated.

    Path parameters reach the audit trail before the request is served. A
    >36-character case id clipped to the column bound could name a *different,
    real* case, and the resulting audit row is immutable — it would permanently
    record an access to a case nobody opened. Failing loudly at the boundary is
    the only outcome that cannot corrupt the record.

    Values derived from a verified JWT are still clipped rather than rejected
    (see ``OperatorAuditRepository._clip``): they cannot be attacker-shaped into
    a collision, and refusing them would fail an otherwise legitimate audited
    read over an unusually long username.
    """
    if not value or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} must not be empty",
        )
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{field_name} exceeds {MAX_IDENTIFIER_LENGTH} characters; "
                "rejected rather than truncated, which could record access to a "
                "different case"
            ),
        )
    return value


@dataclass(frozen=True)
class OperatorContentAccess:
    """How an operator content read was authorised.

    ``standing`` is the self-hosted posture (audited, not gated).
    ``break_glass`` names the live grant that authorised a cloud read.
    """

    access: Literal["standing", "break_glass"]
    grant: Optional[OperatorAccessGrant] = None

    @property
    def target_organization_id(self) -> str:
        """The organization this access is **attributed to** in the audit trail.

        Deliberately not "whatever the grant claims". ``target_organization_id``
        on a grant is written by the operator and is never validated against the
        case (see ``build_grant`` for why that is right for *authorization*) — so
        using it for *attribution* would let the audited party choose which
        tenant their own immutable audit row names. That is the misattribution an
        append-only trail exists to prevent, and migration 036's triggers make it
        uncorrectable.

        The claim is only trustworthy where something else has already forced it
        to be true. Under ``multi``, ``bind_grant_org_scope`` has rebound RLS to
        that organization before the read, so a false claim yields no rows and
        404s — the grant's org is then a fact about the request, not an
        assertion about it. Everywhere else the bound org is what the read
        actually ran under, and that is what gets recorded.
        """
        if self.grant is not None and requested_tenant_provider() == BUILTIN_MULTI:
            return self.grant.target_organization_id
        return get_current_org_id()


async def authorize_content_read(
    grant_repo: IOperatorGrantRepository,
    operator: AuthenticatedUser,
    case_id: str,
) -> OperatorContentAccess:
    """Decide whether this operator may open this case's content.

    Standalone returns standing access: the operator and the data controller are
    the same party, so D9 records the read rather than withholding it.

    Cloud requires a live grant naming exactly this case. Absent, expired or
    revoked, the answer is 403 — never a degraded or partial view, because a
    content endpoint that answers *anything* about an ungranted case has already
    disclosed that it exists.
    """
    if not get_settings().is_cloud:
        return OperatorContentAccess(access="standing")

    grant = await grant_repo.find_live_grant(
        operator_user_id=operator.user_id, target_case_id=case_id
    )
    # Re-check in Python even though the query already filtered: the domain
    # object owns the definition of liveness, and a divergence between the SQL
    # predicate and ``is_live`` can then only ever fail closed.
    if grant is None or not grant.is_live():
        logger.warning(
            "break_glass_denied",
            extra={
                "operator_user_id": operator.user_id,
                "target_case_id": case_id,
                "reason": "no_live_grant",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Reading case content in cloud requires a live break-glass "
                "grant for this case (ADR-012 D9). Request one via "
                "POST /api/v1/admin/grants."
            ),
        )
    return OperatorContentAccess(access="break_glass", grant=grant)


def bind_grant_org_scope(access: OperatorContentAccess) -> None:
    """Re-scope this request's RLS binding to the granted organization.

    The elevated read does not escape row-level security — it is bound
    *somewhere else*. The session stays RLS-enforcing and still sees exactly one
    organization; which one is named by a grant row rather than by call-site
    discipline, which is the property a ``BYPASSRLS`` connection could not offer.

    Applied only under ``TENANT_PROVIDER=multi``. Under ``single`` every row
    carries the Standalone organization, so rebinding to anything else would
    make the read return nothing.
    """
    if access.grant is None:
        return
    if requested_tenant_provider() != BUILTIN_MULTI:
        return
    set_current_org_id(access.grant.target_organization_id)


def build_grant(
    operator: AuthenticatedUser,
    payload: BreakGlassGrantRequest,
    deployment_mode: str,
) -> OperatorAccessGrant:
    """Mint a grant from a validated request.

    Deliberately does **not** verify that the case exists or belongs to the
    named organization. Under ``multi`` such a check cannot work — RLS hides the
    very row it would read — so it would behave differently per tenancy; and a
    validating endpoint is an existence oracle for other tenants' case ids. A
    wrong pair fails closed by itself: the later read rebinds to the named
    organization, finds nothing, and 404s.
    """
    now = datetime.now(timezone.utc)
    return OperatorAccessGrant(
        grant_id=str(uuid.uuid4()),
        operator_user_id=operator.user_id,
        operator_username=operator.email,
        target_case_id=payload.case_id,
        target_organization_id=payload.organization_id,
        reason=payload.reason,
        created_at=now,
        expires_at=now + timedelta(minutes=payload.ttl_minutes),
        # Auto-approved: today's control is reason + TTL + an immutable trail.
        # Customer-initiated approval is a transition in this state machine, and
        # the read gate already accepts only APPROVED_STATES.
        approval_state=GrantApprovalState.AUTO_APPROVED,
        deployment_mode=deployment_mode,
    )


def resolved_deployment_mode() -> str:
    """The deployment mode as a plain string.

    ``settings.deployment_mode`` is a plain ``str`` on some paths and a
    ``DeploymentMode`` member on others. A bare ``str()`` on the enum yields
    ``"DeploymentMode.CLOUD"``, and these values land in append-only audit rows —
    so a stringified enum would permanently store a value no
    ``deployment_mode = 'cloud'`` query ever matches.
    """
    settings = get_settings()
    return str(getattr(settings.deployment_mode, "value", settings.deployment_mode))
