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

Under multi-tenant (Cloud) the login also has to decide *which tenant* it lands
in (#869). The IdP's organization is resolved through the operator-provisioned
``sso_org_mappings`` table before any user lookup, bound as the request's
organization for the rest of the callback, and carried on the completion code so
the minted tokens claim it. An IdP organization this deployment has no mapping
for means no login: a company is onboarded deliberately, never by whoever signs
in first. Single-tenant runs none of this — there is one organization and the
behaviour is unchanged.

An identity that carries **no** IdP organization at all is a different question,
and the answer is a configuration switch (#1045, ADR-016 D5 amending ADR-015).
Off — the default — it is refused exactly as an unmapped one is. On, its first
sign-in provisions a **personal tenant**: an IdP organization holding that one
member, and the FaultMaven enterprise, organization, default team and
``sso_personal_orgs`` row that make it a real, distinct tenant. Returning
individuals resolve it through that subject-keyed, untenanted lookup, because at
callback time no tenant is bound and membership is therefore unreadable. Nothing
about the unmapped-organization branch changes in either switch state.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import structlog
from pydantic import EmailStr, TypeAdapter, ValidationError

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.exceptions import ConflictError
from faultmaven.infrastructure.persistence.account_anchor import (
    AnchorKind,
    move_account_anchor,
    read_anchor,
)
from faultmaven.infrastructure.persistence.enterprise_liveness import (
    enterprise_is_usable,
)
from faultmaven.infrastructure.persistence.user_repository import (
    User as RepositoryUser,
)
from faultmaven.models.interfaces_user import AuditCategory, AuditEventType
from faultmaven.modules.auth.contracts import ISSOIdentityProvider, SSOIdentity
from faultmaven.modules.auth.domain.personal_tenant import (
    PERSONAL_ORG_NAME,
    personal_org_slug,
    personal_tenant_key,
)
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    capture_state_read_at,
)
from faultmaven.modules.auth.exceptions import (
    SSOAuthenticationError,
    SSOProvisioningError,
)

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
# Multi-tenant only: the IdP reported no organization, or one this deployment
# has no mapping for. Distinct from the generic slug because it is the one
# failure an operator can actually fix (provision the mapping), and it leaks
# nothing — it says only that this deployment does not know that IdP org.
ERROR_ORG_UNMAPPED = "sso_org_unmapped"
ERROR_FAILED = "sso_failed"

# Why an org-less login was refused, keyed on what the account's anchor is.
# Distinct slugs because the remedies are opposite: an employee arriving
# unscoped is fixed at the IdP, a retired subject by re-running the retirement
# with ``--next-login fresh-tenant``, and a dangling anchor by repairing data.
_ANCHOR_REFUSAL_REASONS = {
    AnchorKind.RETIRED_PERSONAL: "personal_tenant_retired",
    AnchorKind.DELETED: "personal_anchor_enterprise_deleted",
    AnchorKind.DANGLING: "personal_anchor_enterprise_missing",
    AnchorKind.LIVE: "personal_account_already_anchored",
}

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


def _is_multi_tenant() -> bool:
    """True when this deployment is multi-tenant (Cloud).

    Deferred import: the tenancy factory pulls in settings, which must not be
    imported at auth-module import time (same reason as
    ``jwt_token_generator.resolve_enterprise_claim``).
    """
    from faultmaven.providers.tenancy.factory import (
        BUILTIN_MULTI,
        requested_tenant_provider,
    )

    return requested_tenant_provider() == BUILTIN_MULTI


def _jit_personal_tenant_enabled() -> bool:
    """True when an org-less identity may provision a personal tenant (#1045).

    Read through ``get_settings()`` at the point of use rather than captured at
    composition time, so the value cannot become a documented knob that nothing
    actually consults. That is **not** a live-reload claim: ``get_settings()``
    is a process singleton, so changing the environment variable takes effect on
    the next process, like every other setting. Deferred import for the same
    reason as :func:`_is_multi_tenant`: settings must not be imported at
    auth-module import time.

    Default false. With the switch off this returns False and the org-less
    branch behaves exactly as it did before this feature existed.
    """
    from faultmaven.config.settings import get_settings

    return bool(get_settings().auth.sso_jit_personal_tenant_enabled)


def _personal_tenant_hourly_ceiling() -> int:
    """How many NEW personal tenants may be provisioned per rolling hour."""
    from faultmaven.config.settings import get_settings

    return int(get_settings().auth.sso_jit_personal_tenant_max_per_hour)


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
    """A freshly minted FaultMaven session, returned from ``exchange``.

    ``idp_logout_url``, when present, is where the client must send the browser
    to end the IdP's own session. Clearing the FaultMaven session does not touch
    it, so without this the next sign-in is answered silently and the account
    cannot be switched. ``None`` where the provider offers no single-logout, or
    in local mode where there is no IdP at all.
    """

    user: Any
    access_token: str
    refresh_token: str
    expires_in: int
    session_id: str
    idp_logout_url: str | None = None


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
        org_mapping_repository: Any | None = None,
        enterprise_repository: Any | None = None,
        personal_enterprise_repository: Any | None = None,
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
        # Multi-tenant enterprise resolution (#869, re-aimed by ADR-017 D9).
        # Optional because single-tenant never consults them; under multi their
        # absence fails the login closed rather than silently skipping the
        # tenant decision.
        self._org_mappings = org_mapping_repository
        self._enterprises = enterprise_repository
        # Subject-keyed personal-tenant lookup (#1045). Consulted only on the
        # no-IdP-organization branch and only with the switch on; its absence
        # fails that branch closed rather than silently skipping the decision.
        self._personal_enterprises = personal_enterprise_repository

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
        # #831: pre-read capture for THIS leg's state reads (org resolution,
        # user provisioning). It rides the login payload so ``exchange`` can
        # stamp the tokens from whichever leg read state first — the
        # completion code itself is not revocable, so it must carry its basis.
        state_read_at = capture_state_read_at()

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

        # Multi-tenant: decide the tenant BEFORE touching the user store, so
        # every read and write below already runs inside that organization's
        # RLS scope. Single-tenant skips this entirely (#869).
        enterprise = None
        if _is_multi_tenant():
            enterprise, org_error = await self._resolve_login_enterprise(identity)
            if org_error is not None:
                return self._dashboard_redirect(error=org_error, return_to=return_to)

        user = await self._users.get_by_sso(
            identity.provider, identity.provider_user_id
        )
        provisioned = user is None
        if user is None:
            # Strict match-by-subject found nothing: provision just-in-time.
            # Never link by email — a conflicting unlinked account fails the
            # login instead (ADR-015 D4).
            user = await self._jit_provision(
                identity,
                enterprise=enterprise,
                client_ip=client_ip,
                user_agent=user_agent,
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
        if enterprise is not None and not await self._ensure_enterprise_anchor(
            user, enterprise
        ):
            # Membership could not be established (or the account belongs to a
            # different enterprise): fail closed rather than let an org-less
            # login through. The next attempt heals — the ensure is idempotent.
            return self._dashboard_redirect(error=ERROR_FAILED, return_to=return_to)
        if not provisioned:
            # Returning subject: mirror the IdP's mutable profile and stamp
            # the login (ADR-015 D4). A just-created user is already current.
            await self._sync_profile(user, identity)

        completion_code = secrets.token_urlsafe(32)
        login_payload: dict[str, Any] = {
            "user_id": user.user_id,
            # Epoch seconds, not isoformat: a number cannot be naive, so the
            # exchange-side parse has no aware/naive failure class (#831).
            "state_read_at": state_read_at.timestamp(),
        }
        if enterprise is not None:
            # Mint-time tenancy rides the completion code. The ENTERPRISE claim
            # is minted from ``users.enterprise_id`` at exchange time (ADR-017
            # D9), which the anchor step above has just written — so what rides
            # here is only the billing organization, and a sign-up creates none
            # (D5). Left absent deliberately rather than filled with the
            # enterprise: a reader that found the tenant in the organization
            # claim would be reading exactly the conflation this campaign undid.
            pass
        if identity.provider_session_id:
            # The IdP session id is known only here, on the callback leg, but is
            # needed by the leg that answers the client. It rides the completion
            # code for the same reason the organization does. Absent for a
            # provider that exposes none — single-logout is then unavailable,
            # which is the pre-existing behaviour, not a failure.
            login_payload["provider_session_id"] = identity.provider_session_id
        await self._store.put_login(
            completion_code, login_payload, LOGIN_CODE_TTL_SECONDS
        )
        logger.info("sso_login_completed", user_id=user.user_id)
        return self._dashboard_redirect(code=completion_code, return_to=return_to)

    # -- multi-tenant organization resolution (#869) ------------------------- #

    async def _resolve_login_enterprise(
        self, identity: SSOIdentity
    ) -> tuple[Any | None, str | None]:
        """Resolve, verify and bind the enterprise this login belongs to.

        Returns ``(enterprise, None)`` on success, or ``(None, error_slug)``.
        On success the enterprise is bound as the current tenant for the rest
        of the callback, which is what makes the RLS-scoped reads and writes
        that follow (user lookup, membership, audit) address the right tenant.

        Two branches decide *which* enterprise, and they then share one tail
        (:meth:`_bind_and_verify_enterprise`) so a personal tenant is subject
        to exactly the checks a mapped one is — the sentinel refusal included,
        which the mapped branch previously lacked.

        Logging carries reason slugs and the IdP's org id only — never the
        subject or email. The IdP org id is not a secret and is exactly what an
        operator needs to provision the missing mapping.
        """
        provider_org_id = identity.organization_id
        if not provider_org_id:
            # The ONLY branch #1045 changes. An identity whose IdP organization
            # exists but is unmapped is handled below and is untouched: a
            # company is onboarded deliberately, never by whoever signs in
            # first.
            if _jit_personal_tenant_enabled():
                return await self._resolve_personal_enterprise(identity)
            logger.warning(
                "sso_org_resolution_failed",
                reason="no_idp_org",
                provider=identity.provider,
            )
            return None, ERROR_ORG_UNMAPPED

        if self._org_mappings is None or self._enterprises is None:
            # Misconfiguration, not a user error: refuse rather than fall
            # through to an org-less login the mint/bind guards would reject
            # later with no explanation.
            logger.error(
                "sso_org_resolution_failed",
                reason="org_repositories_unwired",
                provider=identity.provider,
            )
            return None, ERROR_FAILED

        enterprise_id = await self._org_mappings.get_enterprise_id(
            identity.provider, provider_org_id
        )
        if not enterprise_id:
            logger.warning(
                "sso_org_resolution_failed",
                reason="org_unmapped",
                provider=identity.provider,
                provider_org_id=provider_org_id,
            )
            return None, ERROR_ORG_UNMAPPED

        return await self._bind_and_verify_enterprise(
            enterprise_id, identity=identity, reason_prefix="org"
        )

    async def _bind_and_verify_enterprise(
        self, enterprise_id: str, *, identity: SSOIdentity, reason_prefix: str
    ) -> tuple[Any | None, str | None]:
        """Bind ``enterprise_id`` as the tenant and verify it is usable.

        The one tail both resolution branches end in, so neither can acquire a
        check the other lacks. It does three things, in this order:

        1. **Refuse the Standalone sentinel.** Under multi-tenant that id
           identifies the deployment, not a tenant (fm#850). This applies to the
           mapped branch too, which had no such guard: an operator-created
           mapping row pointing at the sentinel would previously have bound it.
           The guard runs *before* the bind, so the sentinel is never the
           request's scope even momentarily.
        2. **Bind**, before reading the organization row: ``organizations`` is
           RLS-tenanted (migration 018), so the read only succeeds inside its
           own tenant scope. This rebind lands mid-flow and works only because
           each repository call opens its own session, and so its own
           transaction — the engine's ``begin`` listener samples the contextvar
           at BEGIN and never again (#935).
        3. **Verify** the organization exists, is not soft-deleted and is
           active. A tenant that is missing or disabled is an operator problem,
           not something to tell the browser about.
        """
        if enterprise_id == STANDALONE_ENTERPRISE_ID:
            logger.error(
                "sso_org_resolution_failed",
                reason=f"{reason_prefix}_is_sentinel",
                provider=identity.provider,
            )
            return None, ERROR_FAILED

        set_current_enterprise_id(enterprise_id)

        enterprise = await self._enterprises.get_enterprise(enterprise_id)
        if not enterprise_is_usable(enterprise):
            logger.warning(
                "sso_org_resolution_failed",
                reason=f"{reason_prefix}_unavailable",
                provider=identity.provider,
                enterprise_id=enterprise_id,
            )
            return None, ERROR_FAILED

        return enterprise, None

    # -- personal tenants (#1045, ADR-016 D5) -------------------------------- #

    async def _resolve_personal_enterprise(
        self, identity: SSOIdentity
    ) -> tuple[Any | None, str | None]:
        """Resolve — provisioning on first sign-in — this individual's tenant.

        Reached only from the no-IdP-organization branch, and only with
        ``SSO_JIT_PERSONAL_TENANT_ENABLED`` on. Ends in the same
        :meth:`_bind_and_verify_enterprise` tail as the mapped branch.

        **The lookup is keyed on the subject, not on membership.** Organization
        resolution runs before the user lookup precisely so the RLS scope is
        right for everything after it, and ``organization_members`` is itself
        RLS-tenanted — at this moment no tenant is bound, so membership is
        unreadable. The subject is the one identifier every login carries, which
        is why ``sso_personal_orgs`` is keyed on it and untenanted, for the same
        reason ``sso_org_mappings`` is.

        **A refused login writes nothing.** Every refusal this callback can
        still make is evaluated by :meth:`_personal_preflight_refusal` *before*
        any provisioning write, on either side. Before #1045 an offboarded user,
        an email-conflict subject or an employee arriving unscoped were each
        refused with zero writes; provisioning ahead of those checks would have
        left each of them a permanent stray tenant plus an IdP organization, and
        then ``sso_failed`` forever.
        """
        if self._enterprises is None or self._personal_enterprises is None:
            logger.error(
                "sso_org_resolution_failed",
                reason="personal_org_repository_unwired",
                provider=identity.provider,
            )
            return None, ERROR_FAILED

        subject = identity.provider_user_id
        if not subject:
            logger.warning(
                "sso_org_resolution_failed",
                reason="personal_no_subject",
                provider=identity.provider,
            )
            return None, ERROR_FAILED

        try:
            record = await self._personal_enterprises.get(identity.provider, subject)
            if record is None:
                refusal = await self._personal_preflight_refusal(identity)
                if refusal is not None:
                    return None, refusal
                enterprise_id = await self._provision_personal_tenant(identity)
            else:
                enterprise_id = record.enterprise_id
                if not record.membership_confirmed:
                    # A previous attempt committed the tenant and stopped before
                    # the IdP membership. Finish it now: the ensure is
                    # idempotent, and this is the only path that can.
                    await self._finish_personal_membership(identity, record)
        except SSOProvisioningError:
            # Already logged, without provider detail, at the adapter boundary.
            return None, ERROR_FAILED
        except Exception:
            logger.exception("sso_personal_tenant_failed", provider=identity.provider)
            return None, ERROR_FAILED

        return await self._bind_and_verify_enterprise(
            enterprise_id, identity=identity, reason_prefix="personal_org"
        )

    async def _personal_preflight_refusal(self, identity: SSOIdentity) -> str | None:
        """Every refusal this login can still make, evaluated before any write.

        Returns an error slug to refuse with, or None to proceed. Runs with **no
        tenant bound**, which is what makes it possible at all: ``users`` carries
        no ``organization_id`` and is not enrolled in migration 018's policy, so
        the subject and email lookups below are readable here. (A check that
        needed a tenanted table could not be pulled forward, and would have to
        stay a post-provisioning refusal with the stray-tenant consequence.)

        The checks mirror, in order, exactly what the rest of the callback would
        refuse for later: an existing account that is deactivated or deleted;
        an account anchored to an enterprise this login cannot enter; and, for a
        subject with no account, the two things ``_jit_provision`` refuses — an
        unusable email and an email a different account already owns.
        """
        user = await self._users.get_by_sso(
            identity.provider, identity.provider_user_id
        )

        if user is not None:
            if getattr(user, "deleted_at", None) is not None or not getattr(
                user, "is_active", True
            ):
                logger.info(
                    "sso_personal_refused",
                    reason="personal_user_inactive",
                    user_id=user.user_id,
                )
                return ERROR_USER_INACTIVE
            anchor = await read_anchor(getattr(user, "enterprise_id", None))
            if not anchor.releases_provisioning:
                # An account that is anchored to something arriving with no IdP
                # organization. Provisioning would either trip the enterprise
                # guard later (a stray tenant plus a refused login) or, worse,
                # anchor an employee to a personal enterprise and lock them out
                # of their company. ADR-016 D5: refuse, and never provision.
                #
                # Exactly one state releases it — no anchor at all — which is
                # what a ``--next-login fresh-tenant`` retirement leaves.
                # ``users.enterprise_id`` is nullable since migration 052 for
                # this reason: the verdict is a column, not a parsed marker, so
                # an unreadable or unexpected value cannot answer "released".
                logger.warning(
                    "sso_personal_refused",
                    reason=_ANCHOR_REFUSAL_REASONS.get(
                        anchor.kind, "personal_account_already_anchored"
                    ),
                    provider=identity.provider,
                    user_id=user.user_id,
                )
                return ERROR_ORG_UNMAPPED
            return None

        if not _is_usable_email(identity.email):
            logger.warning(
                "sso_personal_refused",
                reason="personal_unusable_email",
                provider=identity.provider,
            )
            return ERROR_FAILED
        if await self._users.get_by_email(identity.email) is not None:
            # A pre-existing, unlinked account owns this email. ADR-015 D4:
            # subject-match-or-create, never email-link — the login fails, and
            # now it fails before a tenant exists for it to strand.
            logger.warning(
                "sso_personal_refused",
                reason="personal_email_conflict",
                provider=identity.provider,
            )
            return ERROR_FAILED
        return None

    async def _provision_personal_tenant(self, identity: SSOIdentity) -> str:
        """Create this subject's IdP organization and FaultMaven tenant.

        Returns the FaultMaven organization id. Raises on any failure, which the
        caller turns into a refused login — never a partial tenant.

        **The order is IdP organization → database commit → IdP membership**,
        and every step of it is chosen so that each partial state is recoverable
        by simply signing in again:

        * *stopped after the IdP organization* — nobody is a member of it, so the
          IdP still reports no organization on the next callback, which re-enters
          this branch and finds the organization again by its derived external
          id. Invisible, self-healing, no duplicate.
        * *stopped after the commit* — the tenant exists with
          ``membership_confirmed`` false; the next callback resolves it from the
          subject row and finishes the membership.
        * *stopped after the membership, before the flag* — the IdP may now echo
          the organization, which sends the next login down the **mapped**
          branch; that resolves, because the mapping row committed one step
          earlier.

        The membership is last for exactly that reason: it is the IdP-visible
        change, and an echoed organization whose mapping has NOT committed sends
        every later login to a permanent ``sso_org_unmapped`` with no path back.
        Creating it before the commit is the cheaper order and the unrecoverable
        one.
        """
        await self._refuse_over_provisioning_ceiling(identity)

        key = personal_tenant_key(identity.provider, identity.provider_user_id)
        slug = personal_org_slug(key)

        # 1. The IdP organization. The provider port is sync, like the code
        #    exchange, so keep the network round-trips off the event loop. The
        #    slug IS the external id: one derivation, so a tenant is recognisable
        #    as the same thing from either side.
        provider_org_id = await asyncio.to_thread(
            lambda: self._provider.provision_personal_organization(
                provider_user_id=identity.provider_user_id,
                external_id=slug,
                name=PERSONAL_ORG_NAME,
            )
        )

        # 2. The FaultMaven tenant, in one transaction. The repository generates
        #    and binds the enterprise id itself — the binding is an
        #    implementation detail of writing rows under the RLS-scoped role, not
        #    something a caller should have to know or could forget.
        enterprise_id = await self._personal_enterprises.provision(
            provider=identity.provider,
            provider_user_id=identity.provider_user_id,
            provider_org_id=provider_org_id,
            name=PERSONAL_ORG_NAME,
            slug=slug,
        )

        # 3. The IdP membership, last. See the docstring.
        await self._confirm_personal_membership(identity)
        logger.info(
            "sso_personal_tenant_resolved",
            provider=identity.provider,
            enterprise_id=enterprise_id,
        )
        return enterprise_id

    async def _refuse_over_provisioning_ceiling(self, identity: SSOIdentity) -> None:
        """Refuse a NEW tenant once the hourly ceiling is reached.

        The switch alone gates nothing about volume: every subject the IdP
        vouches for would mint an IdP organization and five rows, so a scripted
        sign-up loop exhausts the provider's organization quota. This bounds
        **provisioning only** — an existing tenant resolves from its subject row
        without ever reaching here, so tripping the ceiling cannot lock out the
        people already using the product.
        """
        ceiling = _personal_tenant_hourly_ceiling()
        since = datetime.now(UTC) - timedelta(hours=1)
        minted = await self._personal_enterprises.count_created_since(
            identity.provider, since
        )
        if minted >= ceiling:
            logger.error(
                "sso_personal_refused",
                reason="personal_provisioning_ceiling",
                provider=identity.provider,
                minted_last_hour=minted,
                ceiling=ceiling,
            )
            raise SSOProvisioningError("Personal tenant provisioning ceiling reached")

    async def _finish_personal_membership(
        self, identity: SSOIdentity, record: Any
    ) -> None:
        """Complete the IdP half for a tenant whose commit outran it."""
        logger.info(
            "sso_personal_membership_resuming",
            provider=identity.provider,
            enterprise_id=record.enterprise_id,
        )
        await self._confirm_personal_membership(identity)

    async def _confirm_personal_membership(self, identity: SSOIdentity) -> None:
        """Ensure the IdP membership, then record that it is done.

        The ensure is idempotent at the adapter (it confirms an existing
        membership in any state rather than trusting a refusal), so this is safe
        to run on every unconfirmed login. Marking the flag is a separate write
        on purpose: if it is the step that fails, the next login simply repeats
        an ensure that is already satisfied.
        """
        key = personal_tenant_key(identity.provider, identity.provider_user_id)
        await asyncio.to_thread(
            lambda: self._provider.provision_personal_organization(
                provider_user_id=identity.provider_user_id,
                external_id=personal_org_slug(key),
                name=PERSONAL_ORG_NAME,
            )
        )
        await self._personal_enterprises.confirm_membership(
            identity.provider, identity.provider_user_id
        )

    async def _anchor_account_to(self, user: Any, enterprise: Any) -> bool:
        """Set or move ``user``'s anchor onto the enterprise this login resolved.

        The login's **only** anchor write, and it delegates the rule to
        ``infrastructure.persistence.account_anchor`` — one mover, shared with
        the operator command, so the two cannot drift into different ideas of
        which moves are legitimate.

        What this method contributes is the one fact the rule cannot read for
        itself: whether the destination is **this subject's own personal
        tenant**, and whether the account's current live anchor is. Both are
        established from the untenanted ``sso_personal_orgs`` row keyed on *this*
        subject — never from an enterprise's name or slug — so no
        operator-provisioned company organization can satisfy either.

        The directions that follow from the rule:

        * no anchor → anywhere: a *set*, which is how a freshly provisioned
          personal tenant and a first mapped login both land;
        * a retired personal anchor → a company: the move a retired subject
          makes when a company invites them (ADR-016 D8 R6);
        * this subject's own **live** personal anchor → a company: the switch
          #1320 shipped, unchanged, and the one case that also retires the
          binding;
        * an already-anchored account → a personal enterprise: **refused**. No
          login may drag an account back onto a personal tenant, which is what a
          half-finished re-anchor previously allowed.

        Returns True when the account may proceed, False for an ordinary
        enterprise mismatch the caller refuses.
        """
        provider = getattr(user, "sso_provider", None)
        subject = getattr(user, "sso_provider_id", None)
        destination_is_personal = False
        own_live_personal = False
        if self._personal_enterprises is not None and provider and subject:
            try:
                destination_is_personal = (
                    await self._personal_enterprises.find_by_enterprise(
                        provider, subject, enterprise.enterprise_id
                    )
                )
                current = getattr(user, "enterprise_id", None)
                if current:
                    own_live_personal = (
                        await self._personal_enterprises.find_by_enterprise(
                            provider, subject, current
                        )
                    )
            except Exception:
                logger.exception(
                    "sso_personal_anchor_lookup_failed", user_id=user.user_id
                )
                return False

        moved = await move_account_anchor(
            self._users,
            user,
            to_enterprise_id=enterprise.enterprise_id,
            destination_is_personal=destination_is_personal,
            own_live_personal=own_live_personal,
        )
        if not moved:
            return False

        if own_live_personal and not destination_is_personal:
            # #1320's personal → company switch. Retire the binding only after
            # the move is durable: the reverse order would leave an account
            # anchored to a personal enterprise whose subject row no longer
            # names it, and no login could repair that. The personal tenant
            # itself is not migrated — the cases stay where they are.
            await self._personal_enterprises.retire(provider, subject)
            logger.info(
                "sso_personal_tenant_reanchored",
                user_id=user.user_id,
                enterprise_id=enterprise.enterprise_id,
            )
        return True

    async def _ensure_enterprise_anchor(self, user: Any, enterprise: Any) -> bool:
        """Anchor ``user`` to ``enterprise``; False means fail closed.

        Under ADR-017 D3 this is the ONLY membership a login establishes. The
        account's isolation membership is ``users.enterprise_id`` — one column,
        no roster table — and it is what every later read is scoped by.

        No ``organization_members`` row is written any more, and its absence is
        the design rather than an omission: an organization is a billing target
        created by payment (D5), so a sign-in cannot know of one and must not
        invent one. An account signs in, lands in its enterprise, and is in no
        organization until somebody pays for it.

        Runs for JIT-provisioned and returning users alike, so the two cannot
        drift.
        """
        user_enterprise = getattr(user, "enterprise_id", None)
        if user_enterprise == enterprise.enterprise_id:
            return True

        # The account belongs to a different enterprise. Moving an account
        # between enterprises is a deliberate operator action, never an implicit
        # consequence of an IdP claim — with exactly one exception, inside
        # ``_anchor_account_to``.
        if not await self._anchor_account_to(user, enterprise):
            logger.warning(
                "sso_login_rejected",
                reason="enterprise_mismatch",
                user_id=user.user_id,
            )
            return False
        return True

    # -- leg 3: dashboard -> session ---------------------------------------- #

    async def exchange(self, code: str) -> SSOExchangeResult | None:
        """Trade a completion code for a minted FaultMaven session.

        Returns None on any failure (unknown/expired/replayed code, user gone
        or deactivated since the callback) — the router maps None to a uniform
        401 so the endpoint cannot be used to distinguish failure causes.
        """
        # #831: before this leg's reads (login payload, user row).
        state_read_at = capture_state_read_at()

        payload = await self._store.consume_login(code)
        if payload is None:
            return None

        # The claims also derive from state the CALLBACK leg read (the org
        # resolution stored in this payload), up to LOGIN_CODE_TTL_SECONDS
        # earlier — so the stamp must be the older of the two legs' captures,
        # or a revoke-all landing between the legs would be survived by a pair
        # carrying the pre-revocation tenant (#831). Absent only for payloads
        # written by a pre-#831 process during a rolling deploy; the 60s TTL
        # bounds that window and this leg's capture still applies.
        #
        # ``is not None``, not truthiness: a present-but-unusable value must
        # take the warn-and-degrade branch, never a silent skip. The broad
        # except is deliberate — an escape here would 500 the exchange after
        # ``consume_login`` already burned the single-use code, and
        # ``fromtimestamp`` can raise TypeError (non-numeric), ValueError /
        # OverflowError / OSError (out-of-range) depending on the garbage.
        callback_read_at = payload.get("state_read_at")
        if callback_read_at is not None:
            try:
                state_read_at = min(
                    state_read_at,
                    datetime.fromtimestamp(callback_read_at, UTC),
                )
            except (TypeError, ValueError, OverflowError, OSError):
                logger.warning(
                    "sso_exchange_bad_state_read_at", user_id=payload.get("user_id")
                )

        user = await self._users.get(payload["user_id"])
        if (
            user is None
            or getattr(user, "deleted_at", None) is not None
            or not getattr(user, "is_active", True)
        ):
            logger.warning("sso_exchange_user_unavailable", user_id=payload["user_id"])
            return None

        # Mint-time tenancy. The ISOLATION claim needs nothing attached here:
        # ``resolve_enterprise_claim`` mints it from ``users.enterprise_id``,
        # which the callback's anchor step wrote (ADR-017 D9). The BILLING claim
        # is attached from the completion code when the callback resolved one —
        # a sign-up resolves none (D5), so this is normally absent and the token
        # carries no organization at all, which is the correct statement about
        # an account nobody is paying for.
        organization_id = payload.get("organization_id")
        if organization_id:
            user.organization_id = organization_id

        access_token = await self._tokens.generate_access_token(
            user, state_read_at=state_read_at
        )
        refresh_token = await self._tokens.generate_refresh_token(
            user, state_read_at=state_read_at
        )

        provider_session_id = payload.get("provider_session_id")
        session_metadata: dict[str, Any] = {
            "login_method": "sso",
            "sso_provider": self._provider.provider_name,
            "username": user.username,
        }
        if isinstance(provider_session_id, str) and provider_session_id:
            # Persisted so the IdP session can be ended WITHOUT the browser.
            # The logout URL only works if the browser completes a third-party
            # navigation after local teardown; a closed tab leaves the IdP
            # session alive with nothing able to reach it. This is what
            # ``end_idp_session`` reads.
            session_metadata["provider_session_id"] = provider_session_id

        session, _resumed = await self._sessions.create_session(
            user_id=user.user_id,
            metadata=session_metadata,
        )
        session_id = getattr(session, "session_id", str(session))

        return SSOExchangeResult(
            user=user,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self._access_token_expires_in,
            session_id=session_id,
            idp_logout_url=self._idp_logout_url(provider_session_id),
        )

    async def end_idp_session(self, session_id: str) -> bool:
        """End the IdP session behind a FaultMaven session. True if it ended.

        The only path that does not need the browser: logout can end the IdP
        session even if the user closes the tab before the redirect runs.

        Never raises, and never reports a problem the caller can act on — it is
        invoked from a logout that has already revoked the local token, and the
        sign-out is not less successful because the IdP could not be reached.
        """
        if not session_id:
            return False
        try:
            # validate=False: an expired session must still surrender its IdP
            # handle. Expiry is exactly when the IdP session most needs ending,
            # and the validating read deletes the row before we can look.
            session = await self._sessions.get_session(session_id, validate=False)
        except Exception as exc:
            logger.warning("sso_session_read_failed", error=type(exc).__name__)
            return False
        if session is None:
            return False

        provider_session_id = (getattr(session, "metadata", None) or {}).get(
            "provider_session_id"
        )
        if not isinstance(provider_session_id, str) or not provider_session_id:
            return False

        try:
            # The SDK is synchronous; off the event loop, same as exchange.
            return await asyncio.to_thread(
                self._provider.revoke_session,
                provider_session_id=provider_session_id,
            )
        except Exception as exc:
            logger.warning("sso_revoke_session_failed", error=type(exc).__name__)
            return False

    def _idp_logout_url(self, provider_session_id: Any) -> str | None:
        """Build the IdP logout URL for this login, or None if unavailable.

        Never raises. This runs on the success path of a login that has already
        happened — refusing to hand back a session because its *logout* link
        could not be built would trade a working sign-in for a cosmetic field.
        """
        if not isinstance(provider_session_id, str) or not provider_session_id:
            return None
        try:
            # Name the destination rather than inheriting the IdP's default
            # Logout URI, which is a dashboard setting this deployment cannot
            # see: an unset or stale default lands the user somewhere arbitrary
            # immediately after a successful sign-out. The dashboard origin must
            # be registered under the provider's logout redirects — see
            # docs/operations/sso-org-provisioning.md.
            return self._provider.build_logout_url(
                provider_session_id=provider_session_id,
                return_to=self._dashboard_url or None,
            )
        except Exception as exc:
            logger.warning("sso_logout_url_failed", error=type(exc).__name__)
            return None

    # -- provisioning (ADR-015 D4/D5) ----------------------------------------- #

    async def _jit_provision(
        self,
        identity: SSOIdentity,
        *,
        enterprise: Any | None = None,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> Any | None:
        """Create a FaultMaven user for a first-time SSO subject, or None.

        Returns None on any non-provisionable identity (missing/oversized/
        invalid email, or an existing unlinked account already owning the
        email — linking is deliberately out of scope). Failures are logged
        with the provider only, never the subject or email.

        ``enterprise`` is the tenant this login resolved to under multi-tenant
        (None in single-tenant). The new account is anchored to it — one column,
        ``users.enterprise_id`` (ADR-017 D3) — instead of the standalone default
        the repository would otherwise fall back to, which under multi would
        pool every JIT account into the deployment's sentinel tenant.
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
                enterprise_id=(
                    enterprise.enterprise_id if enterprise is not None else None
                ),
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
