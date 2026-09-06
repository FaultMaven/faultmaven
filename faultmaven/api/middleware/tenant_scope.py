"""Bind the current enterprise to the request context (ADR-010 P2b, ADR-017).

The engine ``begin`` listener (``infrastructure/persistence/database.py``) reads
the current enterprise from the ``config.tenant_context`` contextvar and applies
it to **every transaction** as ``app.current_enterprise_id``, so the PostgreSQL
RLS policies scope reads to that enterprise. This dependency is the request front
door that sets that contextvar per request, from the authenticated user's
verified ``enterprise_id`` claim.

It is registered as a **global dependency** on the FastAPI app (``main.py``) so it
runs for every request, in the request handler's task, before the endpoint opens
any database transaction. A ``BaseHTTPMiddleware`` cannot be used here: Starlette
runs its downstream app in a separate task, so a contextvar it sets would not
reach the endpoint.

Single-tenant (Standalone) is the default; ``multi`` requires
``DEPLOYMENT_MODE=cloud`` (see ``providers/tenancy/factory.py``). In single-tenant
mode this **forces** the Standalone enterprise, ignoring any tenant an attacker
might inject via a forged claim — the permanent re-leak guard ADR-010 requires.

Multi-tenant mode sources the enterprise from the authenticated user's verified
``enterprise_id`` claim and **fails closed** when it is not a usable tenant: a
verified request that reaches a tenanted endpoint without one is rejected, never
silently scoped to the Standalone enterprise (which the contextvar defaults to).
There is deliberately **no fallback to ``users.enterprise_id``** when the claim is
absent (ADR-017, "No data migration, no compatibility layer"): the claim is the
only isolation input from day one, so a token minted before the cutover is
refused rather than honoured. "Usable tenant" is decided by
``config.tenant_context.usable_tenant_id`` — the single place that rule lives —
shared with the route-level ``api/v1/auth_dependencies.require_actor_enterprise``.

Unauthenticated / invalid-token requests are bound to the empty non-tenant
sentinel: they match no enterprise-owned rows, and — unlike the contextvar's
Standalone default — they can never satisfy the platform-tier global-WRITE arm of
the knowledge_items RLS policies (#770), which is keyed on the Standalone
enterprise id. Tenant-free platform-tier READS (``scope='global'``) do not depend
on the bound enterprise and keep working for public endpoints.

The **organization is never read for visibility here.** Under ADR-017 it is a
billing fact; this dependency does bind it, from the same verified claim set, so
that writers have one place to read "who pays for the account this request acts
as" — but nothing downstream may turn it into a predicate. Binding it here rather
than looking it up per write is what keeps both tenancy facts entering a request
in exactly one place.

The organization claim is **validated** before it is bound: it must name a live
organization of the enterprise this same request is bound to, or it is dropped
and the request acts as what it now is — an account in no organization. An access
token lives under thirty minutes, so a stale claim is bounded in time, but inside
that window it is a caller-presented id that selects a turn-cap ledger row and is
stamped on every row the request writes. The check is one indexed lookup on an
authenticated multi-tenant request, run AFTER the enterprise is bound so it is
itself RLS-scoped — an organization of another enterprise simply does not
resolve, and needs no separate predicate. A claim that cannot be validated (no
repository, or a lookup that raised) is dropped rather than trusted: the lookup
has not established the claim is good, and the conservative direction is to
charge the account's own allowance and stamp nothing.

Dropping it never refuses the request. Billing is attribution, not access, so a
lapsed subscription must not read as an outage: the account keeps its enterprise
anchor, and leaving an organization changes what is metered, not what is visible
(ADR-017 D5).
"""

import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request, status

from faultmaven.api.middleware.auth import _extract_token, get_auth_service
from faultmaven.api.v1.dependencies import get_organization_repository
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import (
    UNSCOPED_REQUEST_MSG,
    set_current_billing_organization_id,
    set_current_enterprise_id,
    usable_tenant_id,
)
from faultmaven.models.interfaces_user import IOrganizationRepository
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthenticationError,
    AuthService,
    TokenRevocationError,
)

# Imported as a module, not by name: ``usable_tenant_id`` resolves
# ``requested_tenant_provider`` from this same module attribute, so the arm
# selection below and the sentinel rule always agree on which provider is in
# force — including under a test that overrides it.
from faultmaven.providers.tenancy import factory as tenancy_factory
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI

logger = logging.getLogger(__name__)

# Bound for unauthenticated / invalid-token requests under multi-tenant: matches
# no enterprise's rows AND can never satisfy the RLS write policies'
# single-tenant-sentinel arm (global writes are keyed on the Standalone
# enterprise id). Structurally stronger than leaving the contextvar's Standalone
# default in place.
_UNSCOPED_ENTERPRISE = ""


async def _validated_billing_organization(
    organization_repository: Optional[IOrganizationRepository],
    claimed_organization_id: Optional[str],
) -> Optional[str]:
    """The claimed organization iff it names a live one of the bound enterprise.

    Called with the enterprise already bound, so the lookup is RLS-scoped and an
    organization belonging to another enterprise resolves to ``None`` without a
    predicate of its own. Every other answer — no claim, no repository, a lookup
    that raised, an id that does not resolve — is ``None``, which is the ordinary
    state of an account nobody pays for.
    """
    if not claimed_organization_id:
        return None
    if organization_repository is None:
        # Standalone composes none, and a cloud process can be built before it is
        # wired. Nothing is billed in either case.
        return None
    try:
        organization = await organization_repository.get_organization(
            claimed_organization_id
        )
    except Exception as exc:  # noqa: BLE001 - the direction is what matters
        logger.warning(
            "Dropping the billing organization claim for user: its validation "
            "could not run (%s)",
            type(exc).__name__,
        )
        return None
    if organization is None:
        logger.info(
            "Dropping a stale billing organization claim: %s does not name a "
            "live organization of the bound enterprise",
            claimed_organization_id,
        )
        return None
    return claimed_organization_id


async def bind_request_enterprise_context(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
    organization_repository: Optional[IOrganizationRepository] = Depends(
        get_organization_repository
    ),
) -> None:
    """Scope the request to its enterprise for PostgreSQL RLS.

    Single-tenant: force the Standalone enterprise (ignoring any injected one).
    Multi-tenant: bind the authenticated user's verified ``enterprise_id`` claim,
    failing closed (403) when a verified user carries no usable enterprise.

    Raises:
        HTTPException 403: Multi-tenant mode with a verified user whose token
            carries no usable ``enterprise_id`` claim — refused rather than
            defaulted to the Standalone enterprise or derived from the user row.
    """
    if tenancy_factory.requested_tenant_provider() != BUILTIN_MULTI:
        # Single-tenant: force the Standalone enterprise, ignoring any injected
        # tenant so a forged claim can never re-scope a Standalone deployment.
        # No organization exists in a standalone deployment (ADR-017 D8), so
        # nothing is billed and the attribution stays NULL.
        set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
        set_current_billing_organization_id(None)
        return

    # Multi-tenant: the enterprise comes from the authenticated user's verified
    # claim. We reuse the auth stack's token extraction and verification — no new
    # parsing.
    token = _extract_token(request.headers.get("authorization"), None)
    if not token:
        # Public / unauthenticated request — no enterprise to bind. Tenanted
        # endpoints require auth and will 401; bind the non-tenant sentinel so
        # the session matches no enterprise-owned rows and holds no global-write
        # license (#770).
        set_current_enterprise_id(_UNSCOPED_ENTERPRISE)
        set_current_billing_organization_id(None)
        return

    try:
        claims = await auth_service.verify_token_with_revocation_check(
            token, token_type="access"
        )
    except (AuthenticationError, TokenRevocationError):
        # Invalid / revoked token — let the endpoint's own auth dependency 401;
        # same non-tenant binding as the unauthenticated case.
        set_current_enterprise_id(_UNSCOPED_ENTERPRISE)
        set_current_billing_organization_id(None)
        return

    # Fail closed: a verified user without a usable tenant must never fall
    # through to the contextvar's Standalone default under multi-tenant, and must
    # never be rescued by reading ``users.enterprise_id`` — the claim is the
    # isolation input, and a token without it predates the cutover. What counts
    # as a usable tenant (absent claim, and the Standalone sentinel under multi,
    # which is not a tenant but the identity of the single-tenant deployment) is
    # decided by ``usable_tenant_id`` and nowhere else, so this front door and
    # the route-level ``require_actor_enterprise`` cannot drift apart.
    enterprise_id = usable_tenant_id(claims.get("enterprise_id"))
    if not enterprise_id:
        logger.warning(
            "Rejecting multi-tenant request: token for user %s carries no "
            "enterprise claim",
            claims.get("sub"),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=UNSCOPED_REQUEST_MSG,
        )

    set_current_enterprise_id(enterprise_id)
    # Billing attribution, from the same verified claim set. Deliberately NOT
    # passed through ``usable_tenant_id``: that predicate answers "may this value
    # scope a query?", and this value may never scope one. Absent means nobody
    # pays for this account, which writers stamp as NULL.
    #
    # Validated after the enterprise is bound, so the lookup is scoped by the
    # policy the same way every other read in this request is.
    set_current_billing_organization_id(
        await _validated_billing_organization(
            organization_repository, claims.get("organization_id")
        )
    )
