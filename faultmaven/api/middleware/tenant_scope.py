"""Bind the current organization to the request context (ADR-010 P2b).

The engine ``begin`` listener (``infrastructure/persistence/database.py``) reads
the current organization from the ``config.tenant_context`` contextvar and applies
it to **every transaction** as ``app.current_org_id``, so the PostgreSQL RLS
policies (migration 018) scope reads to that organization. This dependency is the
request front door that sets that contextvar per request, from the authenticated
user's verified ``organization_id`` claim.

It is registered as a **global dependency** on the FastAPI app (``main.py``) so it
runs for every request, in the request handler's task, before the endpoint opens
any database transaction. A ``BaseHTTPMiddleware`` cannot be used here: Starlette
runs its downstream app in a separate task, so a contextvar it sets would not
reach the endpoint.

Single-tenant (Standalone) is the default; ``multi`` requires
``DEPLOYMENT_MODE=cloud`` (see ``providers/tenancy/factory.py``). In single-tenant
mode this **forces** the Standalone org, ignoring any org an attacker might
inject via a forged claim — the permanent re-leak guard ADR-010 requires.

Multi-tenant mode sources the org from the authenticated user's verified
``organization_id`` claim and **fails closed** when it is not a usable tenant: a
verified request that reaches a tenanted endpoint without one is rejected, never
silently scoped to the Standalone org (which the contextvar defaults to). "Usable
tenant" is decided by ``config.tenant_context.usable_tenant_id`` — the single
place that rule lives — shared with the route-level
``api/v1/auth_dependencies.require_actor_organization``. Unauthenticated /
invalid-token requests are bound to the empty non-org sentinel: they match no
org-owned rows, and — unlike the contextvar's Standalone default — they can never
satisfy the platform-tier global-WRITE arm of the knowledge_items RLS policies
(migration 033, #770), which is keyed on the Standalone org id. Org-free
platform-tier READS (``scope='global'``) do not depend on the bound org and keep
working for public endpoints.
"""

import logging
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from faultmaven.api.middleware.auth import _extract_token, get_auth_service
from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.tenant_context import (
    UNSCOPED_REQUEST_MSG,
    set_current_org_id,
    usable_tenant_id,
)
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
# no organization's rows AND can never satisfy the RLS write policies'
# single-tenant-sentinel arm (migration 033 keys global writes on the Standalone
# org id). Structurally stronger than leaving the contextvar's Standalone
# default in place.
_UNSCOPED_ORG = ""


async def bind_request_org_context(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    auth_service: AuthService = Depends(get_auth_service),
) -> None:
    """Scope the request to its organization for PostgreSQL RLS.

    Single-tenant: force the Standalone org (ignoring any injected org).
    Multi-tenant: bind the authenticated user's verified ``organization_id`` claim,
    failing closed (403) when a verified user has no org.

    Raises:
        HTTPException 403: Multi-tenant mode with a verified user that carries no
            organization — refused rather than defaulted to the Standalone org.
    """
    if tenancy_factory.requested_tenant_provider() != BUILTIN_MULTI:
        # Single-tenant: force the Standalone org, ignoring any injected org so a
        # forged claim can never re-scope a Standalone deployment.
        set_current_org_id(STANDALONE_ORG_ID)
        return

    # Multi-tenant: the org comes from the authenticated user's verified claim. We
    # reuse the auth stack's token extraction and verification — no new parsing.
    token = _extract_token(authorization, None)
    if not token:
        # Public / unauthenticated request — no org to bind. Tenanted endpoints
        # require auth and will 401; bind the non-org sentinel so the session
        # matches no org-owned rows and holds no global-write license (#770).
        set_current_org_id(_UNSCOPED_ORG)
        return

    try:
        user = await auth_service.extract_user_from_token_with_revocation_check(token)
    except (AuthenticationError, TokenRevocationError):
        # Invalid / revoked token — let the endpoint's own auth dependency 401;
        # same non-org binding as the unauthenticated case.
        set_current_org_id(_UNSCOPED_ORG)
        return

    # Fail closed: a verified user without a usable tenant must never fall through
    # to the contextvar's Standalone default under multi-tenant. The decision of
    # what counts as a usable tenant — absent claim, and the Standalone sentinel
    # under multi, which is not a tenant but the identity of the single-tenant
    # deployment (migration 033 keys the global-KB write policy on it) — is made
    # by ``usable_tenant_id`` and nowhere else, so this front door and the
    # route-level ``require_actor_organization`` cannot drift apart. Enforcing it
    # here as well as at mint time keeps the guarantee independent of every
    # token-minting path, present and future.
    tenant_id = usable_tenant_id(user.organization_id)
    if not tenant_id:
        logger.warning(
            "Rejecting multi-tenant request: user %s carries no organization",
            user.user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=UNSCOPED_REQUEST_MSG,
        )

    set_current_org_id(tenant_id)
