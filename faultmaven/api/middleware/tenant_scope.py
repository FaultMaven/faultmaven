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
``organization_id`` claim and **fails closed** when it is missing: a verified
request that reaches a tenanted endpoint without an org is rejected, never silently
scoped to the Standalone org (which the contextvar defaults to). Unauthenticated
requests to public endpoints carry no org and are left at the default — they never
read tenanted data.
"""

import logging
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from faultmaven.api.middleware.auth import _extract_token, get_auth_service
from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.tenant_context import set_current_org_id
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthenticationError,
    AuthService,
    TokenRevocationError,
)
from faultmaven.providers.tenancy.factory import (
    BUILTIN_MULTI,
    requested_tenant_provider,
)

logger = logging.getLogger(__name__)


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
    if requested_tenant_provider() != BUILTIN_MULTI:
        # Single-tenant: force the Standalone org, ignoring any injected org so a
        # forged claim can never re-scope a Standalone deployment.
        set_current_org_id(STANDALONE_ORG_ID)
        return

    # Multi-tenant: the org comes from the authenticated user's verified claim. We
    # reuse the auth stack's token extraction and verification — no new parsing.
    token = _extract_token(authorization, None)
    if not token:
        # Public / unauthenticated request — no org to bind. Tenanted endpoints
        # require auth and will 401; leave the contextvar at its default.
        return

    try:
        user = await auth_service.extract_user_from_token_with_revocation_check(token)
    except (AuthenticationError, TokenRevocationError):
        # Invalid / revoked token — let the endpoint's own auth dependency 401.
        return

    if not user.organization_id:
        # Fail closed: a verified user without an org must never fall through to
        # the contextvar's Standalone default under multi-tenant.
        logger.warning(
            "Rejecting multi-tenant request: user %s carries no organization",
            user.user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request is not scoped to an organization.",
        )

    set_current_org_id(user.organization_id)
