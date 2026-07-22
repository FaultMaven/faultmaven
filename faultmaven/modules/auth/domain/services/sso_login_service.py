"""SSO hosted-login orchestration (ADR-015, WorkOS AuthKit).

Drives the three-legged cloud sign-in flow:

1. ``begin_login`` — mint a single-use CSRF ``state``, remember the caller's
   ``return_to`` path, and build the IdP hosted-login URL to redirect to.
2. ``complete_callback`` — the IdP redirected back: verify + consume the
   ``state``, exchange the authorization code for a normalized identity, resolve
   the FaultMaven user by stable SSO subject (provisioning one just-in-time on
   first login), and hand the dashboard a 60-second single-use completion
   code. Every failure maps to a sanitized error slug in the dashboard
   redirect — IdP detail is never echoed (no error oracle).
3. ``exchange`` — the dashboard posts the completion code back and receives a
   freshly minted FaultMaven session (RS256 access + refresh tokens). Tokens are
   minted here, at exchange time, so they never rest in Redis and never appear
   in a URL.

FaultMaven mints its own session; the IdP is an authentication front-end only.
User resolution is strict match-by-subject (``get_by_sso``); an unknown subject
is provisioned just-in-time (ADR-015 D4): username derived from the email
local-part, NULL password, never admin. There is deliberately NO email-based
linking of an SSO login to a pre-existing unlinked account — an account that
already owns the identity's email is a hard conflict, not a link target.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import structlog
from pydantic import EmailStr, TypeAdapter, ValidationError

from faultmaven.exceptions import ConflictError
from faultmaven.infrastructure.persistence.user_repository import (
    User as RepositoryUser,
)
from faultmaven.models.interfaces_user import AuditCategory, AuditEventType
from faultmaven.modules.auth.contracts import ISSOIdentityProvider, SSOIdentity
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
ERROR_USER_INACTIVE = "sso_user_inactive"
ERROR_ACCESS_DENIED = "sso_access_denied"
ERROR_FAILED = "sso_failed"

_MAX_RETURN_TO_LENGTH = 512

# Our state/completion tokens are 43-char token_urlsafe values; anything far
# beyond that (or an oversized IdP code) is garbage — bound it before it
# reaches Redis or the IdP exchange.
_MAX_STATE_LENGTH = 256
_MAX_CODE_LENGTH = 512

# JIT provisioning bounds. The username base is capped well under the column
# limit (100) so collision suffixes always fit; suffix probing is bounded and
# falls back to a random tail so provisioning always terminates.
_USERNAME_BASE_MAX = 64
_USERNAME_SUFFIX_ATTEMPTS = 30
_MAX_EMAIL_LENGTH = 255
_MAX_DISPLAY_NAME_LENGTH = 200

# The JIT path validates emails via User model construction; the profile-sync
# path assigns onto an existing model (no validate_assignment), so it runs the
# same EmailStr validation explicitly before applying an IdP email change.
_EMAIL_VALIDATOR: TypeAdapter[str] = TypeAdapter(EmailStr)


def _is_usable_email(email: str) -> bool:
    """True when the IdP-supplied email is present, bounded, and well-formed."""
    if not email or len(email) > _MAX_EMAIL_LENGTH:
        return False
    try:
        _EMAIL_VALIDATOR.validate_python(email)
    except ValidationError:
        return False
    return True


def derive_username(email: str) -> str:
    """Derive a username candidate from an email local-part (ADR-015 D4).

    Lowercased, restricted to ``[a-z0-9._-]``, trimmed of edge punctuation,
    length-capped; falls back to ``"user"`` when nothing survives.
    """
    local_part = email.split("@", 1)[0].lower()
    base = re.sub(r"[^a-z0-9._-]", "", local_part).strip("._-")
    return (base or "user")[:_USERNAME_BASE_MAX]


@dataclass(frozen=True)
class SSOLoginStart:
    """A begun login: where to send the browser, and the state that binds it.

    ``state`` is returned so the transport layer can ALSO bind it to the
    initiating browser (an HttpOnly cookie) — the server-side single-use store
    alone does not prove the callback arrived from the browser that started
    the flow (login-CSRF/session-fixation defense).
    """

    authorization_url: str
    state: str


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
        audit_log: Any | None = None,
    ) -> None:
        self._provider = identity_provider
        self._store = ephemeral_store
        self._users = user_repository
        self._tokens = token_generator
        self._sessions = session_service
        self._dashboard_url = dashboard_url.rstrip("/")
        self._access_token_expires_in = access_token_expires_in
        # IAuditRepository (or None): records the JIT account-creation trail
        # (ADR-015 PR 7). Optional so unit setups without a DB still work.
        self._audit = audit_log

    # -- leg 1: browser -> IdP ---------------------------------------------- #

    async def begin_login(self, return_to: str | None = None) -> SSOLoginStart:
        """Mint a single-use state and return the IdP URL + state to bind."""
        state = secrets.token_urlsafe(32)
        payload = {}
        safe_return_to = sanitize_return_to(return_to)
        if safe_return_to:
            payload["return_to"] = safe_return_to
        await self._store.put_state(state, payload, STATE_TTL_SECONDS)
        return SSOLoginStart(
            authorization_url=self._provider.build_authorization_url(state=state),
            state=state,
        )

    # -- leg 2: IdP -> callback -> dashboard -------------------------------- #

    async def complete_callback(
        self,
        *,
        code: str | None,
        state: str | None,
        error: str | None,
        browser_state: str | None,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> str:
        """Handle the IdP redirect; always return a dashboard redirect URL.

        ``browser_state`` is the state echoed by the initiating browser (the
        cookie set alongside ``begin_login``); it must match the ``state`` query
        param, or the callback did not come from the browser that started the
        flow (login-CSRF/session-fixation attempt).

        ``client_ip``/``user_agent`` are transport metadata recorded on the
        audit trail when this callback provisions an account; they play no part
        in any authentication decision.

        This method never raises — the browser is mid-redirect and must land
        somewhere. Every failure, including unexpected infrastructure errors,
        resolves to the dashboard login callback with a sanitized ``error`` slug.
        """
        try:
            return await self._complete_callback(
                code=code,
                state=state,
                error=error,
                browser_state=browser_state,
                client_ip=client_ip,
                user_agent=user_agent,
            )
        except Exception:
            # Redis/DB/provider infrastructure failure mid-redirect: never leak
            # a raw 500 page at the API origin — land on the dashboard.
            logger.exception("sso_callback_failed")
            return self._dashboard_redirect(error=ERROR_FAILED)

    async def _complete_callback(
        self,
        *,
        code: str | None,
        state: str | None,
        error: str | None,
        browser_state: str | None,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> str:
        if not state or len(state) > _MAX_STATE_LENGTH:
            logger.warning("sso_callback_rejected", reason="state_invalid")
            return self._dashboard_redirect(error=ERROR_STATE_INVALID)

        # Verify-and-consume the state FIRST, even on IdP-reported errors: the
        # stored payload must not survive for a second attempt, and an unsolicited
        # callback (no valid state) must not be able to probe anything.
        state_payload = await self._store.consume_state(state)
        if state_payload is None:
            logger.warning("sso_callback_rejected", reason="state_invalid")
            return self._dashboard_redirect(error=ERROR_STATE_INVALID)

        # Browser binding: the server-side store proves the state is one WE
        # minted, but only the cookie proves THIS browser started the flow. An
        # attacker replaying their own unconsumed callback URL in a victim's
        # browser fails here (state already burned above, so no retry either).
        if not browser_state or not secrets.compare_digest(state, browser_state):
            logger.warning("sso_callback_rejected", reason="browser_binding_missing")
            return self._dashboard_redirect(error=ERROR_STATE_INVALID)

        return_to = state_payload.get("return_to")

        if error:
            # RFC 6749 error param from the IdP. Map to our sanitized enum;
            # never echo `error`/`error_description` content.
            slug = ERROR_ACCESS_DENIED if error == "access_denied" else ERROR_FAILED
            logger.warning("sso_callback_rejected", reason="idp_error")
            return self._dashboard_redirect(error=slug, return_to=return_to)

        if not code or len(code) > _MAX_CODE_LENGTH:
            logger.warning("sso_callback_rejected", reason="missing_or_oversized_code")
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
        provisioned = user is None
        if user is None:
            # Strict match-by-subject found nothing: provision just-in-time.
            # Never link by email — a conflicting unlinked account fails the
            # login instead (ADR-015 D4).
            user = await self._jit_provision(
                identity, client_ip=client_ip, user_agent=user_agent
            )
            if user is None:
                return self._dashboard_redirect(error=ERROR_FAILED, return_to=return_to)
        if getattr(user, "deleted_at", None) is not None or not getattr(
            user, "is_active", True
        ):
            logger.info("sso_login_inactive_user", user_id=user.user_id)
            return self._dashboard_redirect(
                error=ERROR_USER_INACTIVE, return_to=return_to
            )
        if not provisioned:
            # Returning subject: mirror the IdP's mutable profile and stamp
            # the login (ADR-015 D4). A just-created user is already current.
            await self._sync_profile(user, identity)

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
        if (
            user is None
            or getattr(user, "deleted_at", None) is not None
            or not getattr(user, "is_active", True)
        ):
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

    # -- provisioning (ADR-015 D4/D5) ----------------------------------------- #

    async def _jit_provision(
        self,
        identity: SSOIdentity,
        *,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> Any | None:
        """Create a FaultMaven user for a first-time SSO subject, or None.

        Returns None on any non-provisionable identity (missing/oversized/
        invalid email, or an existing unlinked account already owning the
        email — linking is deliberately out of scope). Failures are logged
        with the provider only, never the subject or email.
        """
        if not _is_usable_email(identity.email):
            logger.warning(
                "sso_jit_rejected", reason="unusable_email", provider=identity.provider
            )
            return None
        if await self._users.get_by_email(identity.email) is not None:
            # A pre-existing, unlinked account owns this email. ADR-015 D4:
            # subject-match-or-create, never email-link — fail the login.
            logger.warning(
                "sso_jit_rejected", reason="email_conflict", provider=identity.provider
            )
            return None

        now = datetime.now(UTC)
        username = await self._free_username(derive_username(identity.email))
        display_name = (identity.display_name or username)[:_MAX_DISPLAY_NAME_LENGTH]
        try:
            user = RepositoryUser(
                user_id=str(uuid.uuid4()),
                username=username,
                email=identity.email,
                display_name=display_name,
                hashed_password=None,  # SSO-only account, no password ever
                is_active=True,
                is_email_verified=identity.email_verified,
                email_verified_at=now if identity.email_verified else None,
                sso_provider=identity.provider,
                sso_provider_id=identity.provider_user_id,
                created_at=now,
                updated_at=now,
                last_login_at=now,
                # Never admin (ADR-015 D5): the first cloud admin is promoted
                # out-of-band, no login path grants elevated roles.
                roles=["user"],
            )
        except ValidationError:
            # Keep pydantic's message (which echoes the input email) out of
            # the logs — reject with a reason slug only.
            logger.warning(
                "sso_jit_rejected",
                reason="invalid_identity",
                provider=identity.provider,
            )
            return None

        try:
            created = await self._users.create(user)
        except ConflictError:
            # Lost a create race. If the same subject was provisioned by a
            # concurrent callback, that row is ours to log in with.
            existing = await self._users.get_by_sso(
                identity.provider, identity.provider_user_id
            )
            if existing is not None:
                return existing
            logger.warning(
                "sso_jit_rejected",
                reason="create_conflict",
                provider=identity.provider,
            )
            return None
        logger.info(
            "sso_user_provisioned",
            user_id=created.user_id,
            provider=identity.provider,
        )
        await self._audit_jit_created(
            created, identity, client_ip=client_ip, user_agent=user_agent
        )
        return created

    async def _audit_jit_created(
        self,
        created: Any,
        identity: SSOIdentity,
        *,
        client_ip: str | None,
        user_agent: str | None,
    ) -> None:
        """Record the JIT account creation on the audit trail (ADR-015 PR 7).

        Fail-open by design: the account exists and the login is legitimate, so
        an audit-store outage must not turn a successful first sign-in into an
        error — it logs loudly instead. Details carry the provider and username
        only; the IdP subject and email stay out of the audit row (the user row
        already holds the email under its own lifecycle).
        """
        if self._audit is None:
            return
        try:
            await self._audit.log_event(
                user_id=created.user_id,
                event_type=AuditEventType.ACCOUNT_CREATED,
                event_category=AuditCategory.AUTHENTICATION,
                resource_type="user",
                resource_id=created.user_id,
                details={
                    "provider": identity.provider,
                    "method": "sso_jit",
                    "username": created.username,
                },
                ip_address=client_ip,
                user_agent=user_agent,
                success=True,
            )
        except Exception:
            logger.exception(
                "sso_jit_audit_write_failed",
                user_id=created.user_id,
                provider=identity.provider,
            )

    async def _free_username(self, base: str) -> str:
        """Pick an unused username: base, then numeric suffixes, then random."""
        candidate = base
        for suffix in range(2, 2 + _USERNAME_SUFFIX_ATTEMPTS):
            if await self._users.get_by_username(candidate) is None:
                return candidate
            candidate = f"{base}-{suffix}"
        # Pathological collision run: a random tail terminates the search; the
        # create's own uniqueness check still backstops a race.
        return f"{base}-{secrets.token_hex(4)}"

    async def _sync_profile(self, user: Any, identity: SSOIdentity) -> None:
        """Mirror the IdP's mutable profile onto a returning user + stamp login.

        A profile-sync conflict (the IdP-reported email now belongs to another
        account) must not fail the login — the stored profile simply stays as
        it was.
        """
        now = datetime.now(UTC)
        if (
            _is_usable_email(identity.email)
            and identity.email.lower() != (user.email or "").lower()
        ):
            user.email = identity.email
            user.is_email_verified = identity.email_verified
            user.email_verified_at = now if identity.email_verified else None
        elif identity.email_verified and not getattr(user, "is_email_verified", False):
            user.is_email_verified = True
            user.email_verified_at = now
        if identity.display_name and identity.display_name != getattr(
            user, "display_name", None
        ):
            user.display_name = identity.display_name[:_MAX_DISPLAY_NAME_LENGTH]
        user.last_login_at = now
        try:
            await self._users.update(user)
        except ConflictError:
            logger.warning("sso_profile_sync_conflict", user_id=user.user_id)

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
