"""SSO hosted-login orchestration (ADR-015, WorkOS AuthKit).

Drives the three-legged cloud sign-in flow:

1. ``begin_login`` — mint a single-use CSRF ``state``, remember the caller's
   ``return_to`` path, and build the IdP hosted-login URL to redirect to.
2. ``complete_callback`` — the IdP redirected back: verify + consume the
   ``state``, exchange the authorization code for a normalized identity, resolve
   the FaultMaven user by stable SSO subject, and hand the dashboard a 60-second
   single-use completion code. Every failure maps to a sanitized error slug in
   the dashboard redirect — IdP detail is never echoed (no error oracle).
3. ``exchange`` — the dashboard posts the completion code back and receives a
   freshly minted FaultMaven session (RS256 access + refresh tokens). Tokens are
   minted here, at exchange time, so they never rest in Redis and never appear
   in a URL.

FaultMaven mints its own session; the IdP is an authentication front-end only.
User resolution is strict match-by-subject (``get_by_sso``) — no email linking.
An unknown subject is an error in this phase (JIT provisioning arrives in the
next phase and flips that branch to a create).
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import structlog

from faultmaven.modules.auth.contracts import ISSOIdentityProvider
from faultmaven.modules.auth.exceptions import SSOAuthenticationError

logger = structlog.get_logger(__name__)

# Lifetime of the login CSRF state: covers the user completing the hosted login
# page (typing credentials, MFA) without leaving a long replay window.
STATE_TTL_SECONDS = 600

# Lifetime of the completion code: one immediate browser redirect plus one
# dashboard POST. Anything longer only widens the interception window.
LOGIN_CODE_TTL_SECONDS = 60

# Sanitized error slugs surfaced to the dashboard login page. These are the ONLY
# error values the callback may emit — never raw IdP error text.
ERROR_STATE_INVALID = "sso_state_invalid"
ERROR_EXCHANGE_FAILED = "sso_exchange_failed"
ERROR_USER_UNKNOWN = "sso_user_unknown"
ERROR_USER_INACTIVE = "sso_user_inactive"
ERROR_ACCESS_DENIED = "sso_access_denied"
ERROR_FAILED = "sso_failed"

_MAX_RETURN_TO_LENGTH = 512


@dataclass(frozen=True)
class SSOExchangeResult:
    """A freshly minted FaultMaven session, returned from ``exchange``."""

    user: Any
    access_token: str
    refresh_token: str
    expires_in: int
    session_id: str


def sanitize_return_to(value: str | None) -> str | None:
    """Validate ``return_to`` as a same-origin dashboard path, else None.

    Accepts only an absolute path within the dashboard origin: must start with
    a single ``/`` (``//host`` is a scheme-relative URL and is rejected), no
    backslashes (browsers normalize ``\\`` to ``/``), no control characters or
    whitespace, bounded length. Anything else — full URLs, traversal to another
    origin — is dropped rather than rejected loudly: the login still proceeds,
    just without the redirect hint.
    """
    if not value:
        return None
    if len(value) > _MAX_RETURN_TO_LENGTH:
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    if "\\" in value:
        return None
    if any(ord(ch) < 0x20 or ch in (" ", "\x7f") for ch in value):
        return None
    return value


class SSOLoginService:
    """Orchestrates the hosted-login flow against the SSO seam.

    Collaborators are injected (Composition Root); the service holds no vendor
    or transport knowledge beyond the ``ISSOIdentityProvider`` port.
    """

    def __init__(
        self,
        *,
        identity_provider: ISSOIdentityProvider,
        ephemeral_store: Any,
        user_repository: Any,
        token_generator: Any,
        session_service: Any,
        dashboard_url: str,
        access_token_expires_in: int,
    ) -> None:
        self._provider = identity_provider
        self._store = ephemeral_store
        self._users = user_repository
        self._tokens = token_generator
        self._sessions = session_service
        self._dashboard_url = dashboard_url.rstrip("/")
        self._access_token_expires_in = access_token_expires_in

    # -- leg 1: browser -> IdP ---------------------------------------------- #

    async def begin_login(self, return_to: str | None = None) -> str:
        """Mint a single-use state and return the IdP hosted-login URL."""
        state = secrets.token_urlsafe(32)
        payload = {}
        safe_return_to = sanitize_return_to(return_to)
        if safe_return_to:
            payload["return_to"] = safe_return_to
        await self._store.put_state(state, payload, STATE_TTL_SECONDS)
        return self._provider.build_authorization_url(state=state)

    # -- leg 2: IdP -> callback -> dashboard -------------------------------- #

    async def complete_callback(
        self,
        *,
        code: str | None,
        state: str | None,
        error: str | None,
    ) -> str:
        """Handle the IdP redirect; always return a dashboard redirect URL.

        This method never raises for flow failures — the browser is mid-redirect
        and must land somewhere. Every failure path resolves to the dashboard
        login callback with a sanitized ``error`` slug.
        """
        # Verify-and-consume the state FIRST, even on IdP-reported errors: the
        # stored payload must not survive for a second attempt, and an unsolicited
        # callback (no valid state) must not be able to probe anything.
        state_payload = await self._store.consume_state(state) if state else None
        if state_payload is None:
            logger.warning("sso_callback_rejected", reason="state_invalid")
            return self._dashboard_redirect(error=ERROR_STATE_INVALID)

        return_to = state_payload.get("return_to")

        if error:
            # RFC 6749 error param from the IdP. Map to our sanitized enum;
            # never echo `error`/`error_description` content.
            slug = ERROR_ACCESS_DENIED if error == "access_denied" else ERROR_FAILED
            logger.warning("sso_callback_rejected", reason="idp_error")
            return self._dashboard_redirect(error=slug, return_to=return_to)

        if not code:
            logger.warning("sso_callback_rejected", reason="missing_code")
            return self._dashboard_redirect(error=ERROR_FAILED, return_to=return_to)

        try:
            # The provider port is sync (vendor SDKs are); the exchange is a
            # network round-trip, so keep it off the event loop.
            identity = await asyncio.to_thread(self._provider.exchange_code, code)
        except SSOAuthenticationError:
            # Already logged (without detail) at the adapter boundary.
            return self._dashboard_redirect(
                error=ERROR_EXCHANGE_FAILED, return_to=return_to
            )

        user = await self._users.get_by_sso(
            identity.provider, identity.provider_user_id
        )
        if user is None:
            # Strict match-by-subject: unknown subject is a dead end in this
            # phase. The JIT-provisioning phase replaces this branch with a
            # create. Log the provider only — never the subject or email.
            logger.info("sso_login_unknown_subject", provider=identity.provider)
            return self._dashboard_redirect(
                error=ERROR_USER_UNKNOWN, return_to=return_to
            )
        if not getattr(user, "is_active", True):
            logger.info("sso_login_inactive_user", user_id=user.user_id)
            return self._dashboard_redirect(
                error=ERROR_USER_INACTIVE, return_to=return_to
            )

        completion_code = secrets.token_urlsafe(32)
        await self._store.put_login(
            completion_code, {"user_id": user.user_id}, LOGIN_CODE_TTL_SECONDS
        )
        logger.info("sso_login_completed", user_id=user.user_id)
        return self._dashboard_redirect(code=completion_code, return_to=return_to)

    # -- leg 3: dashboard -> session ---------------------------------------- #

    async def exchange(self, code: str) -> SSOExchangeResult | None:
        """Trade a completion code for a minted FaultMaven session.

        Returns None on any failure (unknown/expired/replayed code, user gone
        or deactivated since the callback) — the router maps None to a uniform
        401 so the endpoint cannot be used to distinguish failure causes.
        """
        payload = await self._store.consume_login(code)
        if payload is None:
            return None

        user = await self._users.get(payload["user_id"])
        if user is None or not getattr(user, "is_active", True):
            logger.warning("sso_exchange_user_unavailable", user_id=payload["user_id"])
            return None

        access_token = await self._tokens.generate_access_token(user)
        refresh_token = await self._tokens.generate_refresh_token(user)

        session, _resumed = await self._sessions.create_session(
            user_id=user.user_id,
            metadata={
                "login_method": "sso",
                "sso_provider": self._provider.provider_name,
                "username": user.username,
            },
        )
        session_id = getattr(session, "session_id", str(session))

        return SSOExchangeResult(
            user=user,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self._access_token_expires_in,
            session_id=session_id,
        )

    # -- internals ----------------------------------------------------------- #

    def _dashboard_redirect(
        self,
        *,
        code: str | None = None,
        error: str | None = None,
        return_to: str | None = None,
    ) -> str:
        """Build the dashboard SSO-callback URL with exactly the given params."""
        params: dict[str, str] = {}
        if code:
            params["code"] = code
        if error:
            params["error"] = error
        if return_to:
            params["return_to"] = return_to
        query = urlencode(params)
        base = f"{self._dashboard_url}/auth/sso/callback"
        return f"{base}?{query}" if query else base
