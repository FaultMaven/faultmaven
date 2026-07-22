"""SSO hosted-login endpoints (ADR-015, WorkOS AuthKit).

Three legs of the cloud sign-in flow, orchestrated by ``SSOLoginService``:

* ``GET  /auth/sso/login``    — 302 to the IdP hosted login page.
* ``GET  /auth/sso/callback`` — IdP redirect target; 302 to the dashboard with a
  single-use completion code (or a sanitized error slug).
* ``POST /auth/sso/exchange`` — dashboard trades the completion code for a
  minted FaultMaven session (standard ``AuthTokenResponse``).

The router is mounted only when SSO is fully configured (``sso_configured``),
mirroring the OAuth router gate. All three endpoints are unauthenticated by
nature (they ARE the login) and rate-limited per IP.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from faultmaven.modules.auth.api.rate_limiting import (
    require_sso_rate_limit_callback,
    require_sso_rate_limit_exchange,
    require_sso_rate_limit_login,
)
from faultmaven.modules.auth.domain.models.api_auth import (
    AuthTokenResponse,
    UserProfile,
)
from faultmaven.modules.auth.domain.services.sso_login_service import SSOLoginService
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/sso", tags=["sso"])

# Login-flow responses must never be cached or stored by intermediaries: the
# callback URL carries a single-use code and the exchange response carries tokens.
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

# Binds the login flow to the initiating browser (login-CSRF defense): set on
# /login, required + verified + cleared on /callback. SameSite=Lax is still sent
# on the top-level IdP -> callback navigation; Secure is safe because SSO only
# exists on cloud (https) deployments. Scoped to this router's path.
_STATE_COOKIE = "fm_sso_state"
_STATE_COOKIE_PATH = "/api/v1/auth/sso"


class SSOExchangeRequest(BaseModel):
    """Completion-code exchange request from the dashboard."""

    code: str = Field(..., min_length=16, max_length=128)


def get_sso_login_service(request: Request) -> SSOLoginService:
    """Resolve the SSO login service from app.state (Composition Root).

    The router only mounts when SSO is configured, so a missing service means
    the composition root failed part-way — surface as 503, not 404.
    """
    service = getattr(request.app.state, "sso_login_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "temporarily_unavailable",
                "message": "SSO is not available on this deployment.",
            },
        )
    return service


@router.get(
    "/login",
    status_code=302,
    dependencies=[Depends(require_sso_rate_limit_login)],
    response_class=RedirectResponse,
)
async def sso_login(
    return_to: str | None = Query(
        default=None,
        description="Dashboard path to return to after login (same-origin path only)",
    ),
    service: SSOLoginService = Depends(get_sso_login_service),
) -> RedirectResponse:
    """Start the hosted-login flow: mint state, redirect to the IdP."""
    start = await service.begin_login(return_to)
    response = RedirectResponse(
        start.authorization_url, status_code=302, headers=_NO_STORE
    )
    response.set_cookie(
        _STATE_COOKIE,
        start.state,
        max_age=600,
        path=_STATE_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get(
    "/callback",
    status_code=302,
    dependencies=[Depends(require_sso_rate_limit_callback)],
    response_class=RedirectResponse,
)
async def sso_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    service: SSOLoginService = Depends(get_sso_login_service),
) -> RedirectResponse:
    """IdP redirect target: verify state, exchange the code, hand off to the dashboard.

    Never fails with an error page — every outcome is a 302 to the dashboard
    SSO callback, carrying either a completion ``code`` or a sanitized ``error``.
    The state cookie set at /login must accompany and match the ``state`` query
    param (browser binding), and is cleared here either way.
    """
    # Transport metadata for the JIT-provisioning audit trail. Behind the
    # ingress, request.client is the direct peer (the proxy) — uvicorn does not
    # trust forwarding headers here; recorded as-is until the infra-wide
    # proxy-IP fix (shared with the OAuth rate limiter).
    redirect_url = await service.complete_callback(
        code=code,
        state=state,
        error=error,
        browser_state=request.cookies.get(_STATE_COOKIE),
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    response = RedirectResponse(redirect_url, status_code=302, headers=_NO_STORE)
    response.delete_cookie(_STATE_COOKIE, path=_STATE_COOKIE_PATH)
    return response


@router.post(
    "/exchange",
    response_model=AuthTokenResponse,
    status_code=200,
    dependencies=[Depends(require_sso_rate_limit_exchange)],
)
async def sso_exchange(
    request_body: SSOExchangeRequest,
    response: Response,
    service: SSOLoginService = Depends(get_sso_login_service),
) -> AuthTokenResponse:
    """Trade the single-use completion code for a FaultMaven session.

    Any failure (expired, replayed, or unknown code; user removed or
    deactivated since the callback) returns the same 401 — the endpoint must
    not distinguish causes for an unauthenticated caller.
    """
    # Token response: no-store per RFC 6749 §5.1 (applies to the 200 body;
    # error raises below carry no tokens).
    response.headers.update(_NO_STORE)
    result = await service.exchange(request_body.code)
    if result is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_grant",
                "message": "Invalid or expired login code.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = result.user
    user_profile = UserProfile(
        user_id=user.user_id,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        created_at=to_json_compatible(user.created_at),
        is_dev_user=getattr(user, "is_dev_user", False),
        roles=user.roles if user.roles else ["user"],
    )
    logger.info("SSO exchange successful for user %s", user.user_id)
    return AuthTokenResponse(
        access_token=result.access_token,
        token_type="bearer",
        expires_in=result.expires_in,
        refresh_token=result.refresh_token,
        session_id=result.session_id,
        user=user_profile,
    )
