"""Auth Module Contracts

This module defines the public interfaces (contracts) for the Auth vertical module.
Other modules should import from here, not from infrastructure or domain directly.

Following the design in module-organization-design.md:
- Vertical modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Protocol, runtime_checkable

from faultmaven.modules.auth.domain.models.auth import (
    AuthenticatedUser,
    DevUser,
    TokenPair,
)
from faultmaven.modules.auth.domain.models.rbac import (
    BASE_USER_ROLE,
    OPERATOR_GRANTED_ROLES,
    ORG_ADMIN_ROLE,
    PLATFORM_ADMIN_ROLE,
    PLATFORM_ADMIN_ROLE_SET,
)

if TYPE_CHECKING:
    from faultmaven.modules.auth.domain.models.user import User


# ============================================================
# DTOs (Data Transfer Objects) for Cross-Module Use
# ============================================================


@dataclass
class UserDTO:
    """Public user representation for cross-module use.

    This DTO exposes only the fields needed by other modules,
    hiding internal auth implementation details.
    """

    user_id: str
    username: str
    email: str
    display_name: str
    is_active: bool = True
    roles: Optional[List[str]] = None


@dataclass
class SessionDTO:
    """Public session representation for cross-module use."""

    session_id: str
    user_id: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    is_valid: bool = True


@dataclass
class OAuthAuthorizationDTO:
    """Data Transfer Object for OAuth authorization request.

    Used in the Dashboard-centric authentication flow where the Dashboard
    acts as IdP and issues authorization codes for the Extension.
    """

    client_id: str
    redirect_uri: str
    state: str
    code_challenge: str
    code_challenge_method: str = "S256"
    scope: str = "openid profile email"


@dataclass
class OAuthTokenDTO:
    """Data Transfer Object for OAuth token response."""

    access_token: str
    refresh_token: str
    user_id: str
    username: str
    token_type: str = "Bearer"
    expires_in: int = 900  # 15 minutes
    refresh_expires_in: int = 604800  # 7 days


@dataclass
class OAuthCodeDTO:
    """Internal representation of authorization code.

    This is stored temporarily (10 minutes) during the OAuth flow
    and includes PKCE challenge for verification.
    """

    code: str
    user_id: str
    redirect_uri: str
    code_challenge: str
    expires_at: datetime
    used: bool = False
    #: Organization the authorizing session was bound to (#872). The user row
    #: carries no organization, so under multi-tenant this is the only place the
    #: tenant survives the hop from the authenticated authorize request to the
    #: unauthenticated token exchange. Under single-tenant it holds the Standalone
    #: sentinel (what the request was bound to), which is also what
    #: ``resolve_billing_organization`` would supply on its own — so it changes
    #: nothing there. Optional because a code issued before this field existed
    #: carries none, and an absent value must mint an unusable claim rather than a
    #: guessed one.
    organization_id: Optional[str] = None
    #: The authorize leg's pre-read capture, as epoch seconds (#831). The code
    #: is a non-revocable hand-off artifact, so it carries the basis of the
    #: state it was minted from; the exchange stamps ``iat`` from the older of
    #: this and its own capture, so a revoke-all landing between the legs
    #: kills the minted pair. Epoch seconds, not an isoformat string — a
    #: number cannot be naive, and it rides the generic JSON round-trip
    #: unaided. Optional for the same rolling-deploy reason as
    #: ``organization_id``; absent falls back to the exchange leg's capture.
    state_read_at: Optional[float] = None


@dataclass
class AuthTokenDTO:
    """Data Transfer Object for authentication token response.

    Used by both Local and Cloud modes for uniform token format.
    Per iam-design.md, both modes use JWT tokens with identical structure.
    """

    access_token: str
    refresh_token: str
    user_id: str
    username: str
    email: str
    display_name: str
    roles: List[str]
    session_id: Optional[str] = None
    token_type: str = "Bearer"
    expires_in: int = 900  # 15 minutes (in seconds)
    refresh_expires_in: int = 604800  # 7 days (in seconds)
    auth_mode: str = "local"  # "local" or "oauth"


# ============================================================
# Repository Contracts
# ============================================================


class IUserRepository(Protocol):
    """Repository interface for User persistence operations."""

    async def save(self, user: "User") -> "User":
        """Save user to persistence layer."""
        ...

    async def get(self, user_id: str) -> Optional["User"]:
        """Retrieve user by ID."""
        ...

    async def get_by_username(self, username: str) -> Optional["User"]:
        """Retrieve user by username."""
        ...

    async def get_by_email(self, email: str) -> Optional["User"]:
        """Retrieve user by email."""
        ...

    async def get_by_sso(self, provider: str, provider_id: str) -> Optional["User"]:
        """Retrieve user by external SSO subject (provider + provider_id).

        Returns the user linked to this IdP subject, or None if no user is
        linked. The (provider, provider_id) pair is unique (users_sso_unique).
        Returns None if either argument is empty — a password user has NULL sso
        fields and must never resolve from an empty subject.
        """
        ...

    async def list(self, limit: int = 50, offset: int = 0) -> tuple[List["User"], int]:
        """List users with pagination."""
        ...

    async def delete(self, user_id: str) -> bool:
        """Delete user by ID."""
        ...


class IUserQuery(Protocol):
    """Read-only user query interface (for high fan-in scenarios)."""

    async def get_user(self, user_id: str) -> Optional["User"]:
        """Get user by ID (read-only)."""
        ...

    async def get_by_email(self, email: str) -> Optional["User"]:
        """Get user by email (read-only)."""
        ...


# ============================================================
# Service Contracts
# ============================================================


class IAuthService(ABC):
    """Interface for authentication business logic."""

    pass


class IOAuthService(ABC):
    """Contract for OAuth authentication operations.

    This interface defines the boundary between the auth module
    and the rest of the system for OAuth-based authentication.
    All OAuth operations must go through this abstraction.

    Implements OAuth 2.0 Authorization Code Flow with PKCE for
    Dashboard-centric authentication (Dashboard acts as IdP for Extension).
    """

    # @abstractmethod where its neighbours have none, deliberately. The other
    # members of this ABC fail LOUDLY when an implementation omits them — an
    # inherited `...` returns None and `code = None` breaks the flow at once.
    # This one fails SILENTLY: the route's gate would pass, `create_authorization
    # _code` would keep working, and #1053 would be back with no error and no
    # failing test. Absence must be refused at construction, not at request time.
    # Do not "harmonise" this decorator away.
    @abstractmethod
    async def validate_authorization_request(
        self,
        request: OAuthAuthorizationDTO,
        user_id: Optional[str] = None,
    ) -> None:
        """Check an authorization request against OAuth policy.

        Exposed so the consent leg of the flow can refuse a request *before*
        rendering a consent screen for it (#1053), rather than leaving both
        checks to ``create_authorization_code``. That method still calls this,
        so minting a code cannot bypass the policy no matter which route runs.

        Args:
            request: OAuth authorization request parameters
            user_id: Authenticated user, for the audit log only

        Raises:
            InvalidRequestError: If client_id, redirect_uri, or
                code_challenge_method is not permitted.
        """
        ...

    async def create_authorization_code(
        self,
        user_id: str,
        request: OAuthAuthorizationDTO,
        organization_id: Optional[str] = None,
    ) -> str:
        """Generate authorization code for OAuth flow.

        Args:
            user_id: Authenticated user's ID from Dashboard session
            request: OAuth authorization request parameters (includes PKCE challenge)
            organization_id: Organization the authorizing session is bound to
                (#872). Captured here because the token exchange that follows is
                unauthenticated and the user row carries no organization.

        Returns:
            Authorization code (short-lived, single-use, 10 minutes)

        Raises:
            InvalidRequestError: If request parameters invalid
        """
        ...

    async def exchange_code_for_token(
        self, code: str, code_verifier: str, redirect_uri: str
    ) -> OAuthTokenDTO:
        """Exchange authorization code for access token.

        Args:
            code: Authorization code from authorization endpoint
            code_verifier: PKCE code verifier (proves client owns code_challenge)
            redirect_uri: Must match original redirect_uri

        Returns:
            Access token and user information

        Raises:
            InvalidGrantError: If code invalid, expired, or already used
            PKCEVerificationError: If code_verifier doesn't match code_challenge
        """
        ...

    async def validate_token(self, token: str) -> Optional[str]:
        """Validate access token and return user_id.

        Args:
            token: Access token from Authorization header

        Returns:
            user_id if token valid, None otherwise
        """
        ...

    async def refresh_access_token(
        self, refresh_token: str, client_id: str
    ) -> OAuthTokenDTO:
        """Refresh access token using refresh token.

        Args:
            refresh_token: Valid refresh token
            client_id: OAuth client ID

        Returns:
            New access token and rotated refresh token

        Raises:
            InvalidGrantError: If refresh token invalid, expired, or revoked
        """
        ...

    async def revoke_token(self, token: str) -> None:
        """Revoke access token (logout).

        Args:
            token: Access token to revoke
        """
        ...

    async def revoke_refresh_token(self, refresh_token: str) -> None:
        """Revoke refresh token (prevents future token refresh).

        Args:
            refresh_token: Refresh token to revoke
        """
        ...


class ILocalAuthService(ABC):
    """Contract for Local Mode authentication operations.

    Per iam-design.md, this interface handles authentication for
    self-hosted/single-user deployments using simple username/password.

    Local mode uses JWT tokens (same as OAuth mode) for middleware uniformity.
    The only difference is the signing algorithm (HS256 vs RS256).
    """

    async def login(
        self, username: str, password: Optional[str] = None
    ) -> AuthTokenDTO:
        """Authenticate user with username/password.

        Args:
            username: User's username
            password: Optional password (for enhanced security)

        Returns:
            JWT access token and user information

        Raises:
            AuthenticationError: If credentials invalid
        """
        ...

    async def register(
        self,
        username: str,
        email: str,
        display_name: str,
        password: Optional[str] = None,
    ) -> AuthTokenDTO:
        """Register new user account.

        Args:
            username: Desired username
            email: User's email address
            display_name: User's display name
            password: Optional password

        Returns:
            JWT access token and user information

        Raises:
            UserExistsError: If username or email already taken
        """
        ...

    async def validate_token(self, token: str) -> Optional[str]:
        """Validate access token and return user_id.

        Args:
            token: Access token from Authorization header

        Returns:
            user_id if token valid, None otherwise
        """
        ...

    async def refresh_access_token(self, refresh_token: str) -> AuthTokenDTO:
        """Refresh access token using refresh token.

        Args:
            refresh_token: Valid refresh token

        Returns:
            New access token and rotated refresh token

        Raises:
            InvalidGrantError: If refresh token invalid or expired
        """
        ...


class IPermissionChecker(Protocol):
    """Interface for permission checking (for high fan-in scenarios)."""

    async def can_access(self, user_id: str, resource: str) -> bool:
        """Check if user can access a resource."""
        ...


class IOAuthCodeRepository(ABC):
    """Storage abstraction for OAuth authorization codes.

    This repository handles persistence of short-lived authorization codes
    during the OAuth flow. Implementation can use Redis, PostgreSQL, or
    in-memory storage depending on deployment configuration.

    The storage is owned by the auth module - no other modules should
    access OAuth codes directly.
    """

    async def save_code(self, code_data: OAuthCodeDTO) -> None:
        """Store authorization code with PKCE challenge.

        Args:
            code_data: Authorization code and associated metadata

        The code should expire automatically after 10 minutes (TTL).
        """
        ...

    async def get_code(self, code: str) -> Optional[OAuthCodeDTO]:
        """Retrieve authorization code data.

        Args:
            code: The authorization code

        Returns:
            Code data if found and not expired, None otherwise
        """
        ...

    async def claim_code(self, code: str) -> bool:
        """Atomically claim the code for single use.

        Returns True for exactly one caller, however many redeem the same code
        concurrently; False for every other caller and for a code that is
        already claimed, expired, or unknown.

        Atomicity is the contract, not an implementation detail: a read-then-
        write split across two calls lets two callers both observe an unclaimed
        code and both mint a token pair, which is the replay RFC 6749 §4.1.2
        requires the server to prevent. Implementations must delegate the
        arbitration to their store (``SET NX``, a conditional ``UPDATE``, a held
        lock) rather than deciding it in Python between two round trips.

        Callers must treat a False return as "someone else redeemed this" and
        refuse, and must call this only at the point they are otherwise
        committed to minting — a claim spent before fallible work burns the
        user's code on a transient failure.

        Args:
            code: The authorization code to claim

        Returns:
            True if this caller won the claim, False otherwise.
        """
        ...

    async def delete_expired_codes(self) -> int:
        """Clean up expired codes (maintenance operation).

        Returns:
            Count of codes deleted
        """
        ...


@runtime_checkable
class ISessionService(Protocol):
    """Session service interface for cross-module use.

    Provides session operations needed by other modules (e.g., case module).
    This is the public contract for session management.
    """

    async def get_session(
        self, session_id: str, validate: bool = True
    ) -> Optional[SessionDTO]:
        """Get session by ID with optional validation.

        Args:
            session_id: The session's unique identifier
            validate: Whether to validate session is active and not expired

        Returns:
            SessionDTO if found (and valid if validate=True), None otherwise
        """
        ...

    async def validate_session(self, session_id: str) -> bool:
        """Check if session is valid and not expired.

        Args:
            session_id: The session's unique identifier

        Returns:
            True if session is valid and active, False otherwise
        """
        ...


# ============================================================
# SSO (external IdP) identity port — ADR-015
# ============================================================


@dataclass(frozen=True)
class SSOIdentity:
    """A normalized identity returned by an external IdP after authentication.

    ``provider`` + ``provider_user_id`` is the stable subject FaultMaven uses to
    look up or provision a user (never the email — emails change). ``email`` and
    ``email_verified`` come straight from the IdP. ``organization_id`` is the
    IdP's own organization identifier (e.g. a WorkOS ``org_...``); under
    multi-tenant it is resolved through ``ISSOOrgMappingRepository`` to the
    FaultMaven organization the login lands in (ADR-010 P2). Single-tenant
    ignores it — there is one organization.

    ``provider_session_id`` is the IdP's own session identifier, needed to end
    that session at logout. It is the IdP's session, not FaultMaven's: clearing
    a FaultMaven session leaves the IdP's alive, so the next sign-in is answered
    silently and "log out" does not mean logged out. Optional because not every
    provider exposes one, and a provider that does not simply cannot offer
    single-logout.
    """

    provider: str
    provider_user_id: str
    email: str
    email_verified: bool
    display_name: str | None = None
    organization_id: str | None = None
    provider_session_id: str | None = None


class ISSOIdentityProvider(ABC):
    """Hosted-login SSO provider port: build an authorization URL, exchange a code.

    Implemented by an infrastructure adapter (e.g. WorkOS AuthKit). Concrete
    adapters are the only code that imports a vendor SDK; this port stays
    vendor-free so it is import-safe in every deployment.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Stable provider key persisted on the user (e.g. ``"workos"``)."""

    @abstractmethod
    def build_authorization_url(self, *, state: str) -> str:
        """Return the IdP hosted-login URL to redirect the browser to.

        Args:
            state: an opaque CSRF token the caller mints and later verifies when
                the IdP redirects back to the callback.
        """

    @abstractmethod
    def exchange_code(self, code: str) -> SSOIdentity:
        """Exchange an authorization code for a normalized identity.

        Raises:
            SSOAuthenticationError: if the IdP rejects the code, or the exchange
                fails for any reason.
        """

    def build_logout_url(
        self, *, provider_session_id: str, return_to: str | None = None
    ) -> str | None:
        """Return the IdP URL that ends ``provider_session_id``, or None.

        Ending the FaultMaven session is not a logout on its own: the IdP holds
        its own session in the browser, so the next authorization request is
        answered without a prompt and the account cannot be switched. Visiting
        this URL is what actually ends it.

        ``return_to`` names where the IdP should send the browser afterwards.
        Providers generally require it to be **pre-registered** with them, so an
        unregistered value is a failed logout rather than a redirect elsewhere —
        it is a deployment setting, not a per-request choice.

        Returning ``None`` is a supported answer, not a failure — a provider may
        offer no single-logout, and callers degrade to today's behaviour rather
        than failing the logout. Implementations must not raise: a logout that
        blows up leaves the caller more signed-in than before.
        """
        return None

    def revoke_session(self, *, provider_session_id: str) -> bool:
        """End ``provider_session_id`` at the IdP directly. True if it ended.

        The server-side counterpart to :meth:`build_logout_url`, and the reason
        both exist: the URL only works if the browser completes a third-party
        navigation *after* local state is gone. A closed tab, a dropped network
        or a blocked request leaves the IdP session alive with nothing able to
        end it. This path does not depend on the browser at all.

        ``False`` means "not ended" for any reason — no single-logout support, a
        provider error, an already-dead session. Like the URL builder, it must
        not raise: this runs inside a logout that has already revoked the local
        token, and failing here would report a failed sign-out for one that
        largely succeeded.
        """
        return False

    @abstractmethod
    def provision_personal_organization(
        self, *, provider_user_id: str, external_id: str, name: str
    ) -> str:
        """Return the IdP organization id holding ``provider_user_id`` alone.

        The IdP half of a personal tenant (#1045, ADR-016 D5): an organization
        that exists to hold exactly one member, so an individual who signs up
        with no company still has an organization-scoped identity at the IdP.

        **Must be idempotent in ``external_id``.** The caller derives that value
        deterministically from the subject and calls this before it writes
        anything of its own, so a retry after a failed database commit has to
        find the organization the previous attempt created rather than mint a
        second one. Adding the member is idempotent for the same reason, and
        must tolerate every membership state a previous attempt could have left.

        Returns the IdP's organization id. Raises
        :class:`~faultmaven.modules.auth.exceptions.SSOProvisioningError` on any
        failure, with no provider detail attached — this runs inside an
        unauthenticated callback, which must not become an error oracle.

        **Abstract rather than a raising default.** A default that raised would
        be indistinguishable at the call site from a provider outage, and would
        let a provider silently ship without the capability; making it abstract
        moves that from a runtime refusal to a construction-time one.
        """


class ISSOOrgMappingRepository(ABC):
    """IdP organization → FaultMaven ENTERPRISE lookup port (ADR-017 D9).

    Read on the **unauthenticated** SSO callback, before any tenant is bound,
    which is why the backing table is deliberately not RLS-tenanted: every
    tenant-scoped table is unreadable at that point. A mapping row carries only
    an identifier equivalence, never tenant data.

    Operators create the mapping out of band
    (the ``fm-provision-sso-org`` command); there is no self-service path, so
    an unmapped IdP organization is a fail-closed login, not a JIT tenant.
    """

    @abstractmethod
    async def get_enterprise_id(
        self, provider: str, provider_org_id: str
    ) -> Optional[str]:
        """Return the mapped FaultMaven enterprise id, or None if unmapped.

        Args:
            provider: SSO provider key (e.g. ``"workos"``).
            provider_org_id: The IdP's organization identifier.
        """


#: The operator's ``--next-login`` choice, as
#: ``sso_personal_enterprises.retirement_state`` stores it. A retired subject's
#: next org-less sign-in is refused.
RETIREMENT_POLICY_REFUSE = "refuse"

#: The retirement releases the account, so the next org-less sign-in provisions
#: a brand-new personal enterprise for the same subject.
#:
#: Under ADR-017 D3 ``users.enterprise_id`` is NOT NULL, so the release is this
#: **recorded value** rather than the cleared anchor it used to be: the account
#: stays anchored to the enterprise the retirement fenced, and
#: ``account_anchor.releases_provisioning`` reads the policy off the subject row
#: to decide. A positive value is the safer spelling in any case — an absence
#: can be produced by a half-finished retirement, and this cannot.
RETIREMENT_POLICY_FRESH_TENANT = "fresh_tenant"


@dataclass(frozen=True)
class RetiredIdPOrganization:
    """What the IdP half of a retirement actually removed.

    Every field reports what happened, never what was intended: an organization
    that was already gone answers ``organization_deleted=False`` and
    ``organization_absent=True``. A delete that swallowed a "not found" and
    reported success is a command claiming work it did not do.
    """

    organization_absent: bool
    memberships_deleted: int
    organization_deleted: bool


class ISSOTenantRetirementProvider(ABC):
    """IdP-side teardown of a personal tenant — the **operator** path only.

    Deliberately a port of its own rather than three more methods on
    :class:`ISSOIdentityProvider`. Nothing in the login flow may take an IdP
    organization down, so putting the capability on the login port would hand
    every login-path double a destructive method it must never call, and would
    make every existing fake declare it. An adapter implements both ports; the
    operator command asks for this one.
    """

    @abstractmethod
    def retire_personal_organization(
        self, *, provider_org_id: str
    ) -> "RetiredIdPOrganization":
        """Remove the memberships and the IdP organization named by its **id**.

        Addressed by the ``provider_org_id`` the tenant's own mapping row
        records — **never** by an id re-derived from the subject. The derived
        ``external_id`` is a function of the subject, so a *later* tenant of the
        same subject answers to it too, and a re-run aimed at a retired
        predecessor would delete the live successor's organization. The recorded
        id names one organization and stops naming anything once it is gone.

        Memberships are scoped by organization: a personal organization holds
        one member by construction, and taking the organization down removes any
        the listing did not name. They go first, so an interrupted run never
        leaves a member of an organization nothing points at.

        Idempotent: an organization that is already gone is a completed step,
        reported as ``organization_absent`` rather than raised. Only a lookup or
        a delete that FAILED raises — the caller must be able to tell "nothing
        left to remove" from "this step did not run".

        Raises:
            SSOProvisioningError: the IdP could not be asked, or refused.
        """


@dataclass(frozen=True)
class PersonalEnterpriseRecord:
    """One subject's personal-tenant binding, as the untenanted table holds it.

    The tenant is an ENTERPRISE (ADR-017 D9): that is what isolates, and it is
    what a login has to bind. ``membership_confirmed`` is why this is a record
    rather than a bare id: a login that resolved an existing tenant still has to
    know whether the IdP half was finished, and that answer cannot come from the
    enterprise row.
    """

    enterprise_id: str
    provider_org_id: str
    membership_confirmed: bool


class ISSOPersonalEnterpriseRepository(ABC):
    """IdP subject → the personal FaultMaven enterprise it owns (#1045, D9).

    The sibling of :class:`ISSOOrgMappingRepository`, and untenanted for exactly
    the same reason: both are read on the **unauthenticated** SSO callback,
    before a tenant is bound, and binding the tenant is what the lookup decides.

    It cannot be folded into that port. ``sso_org_mappings`` is keyed on the
    IdP's *organization* id, which a returning individual's login need not
    carry at all; and it is 1:1 per organization, so a personal tenant's row
    there is already spent on the IdP organization that holds the member.
    Membership cannot serve either — every membership table is RLS-tenanted and
    invisible at callback time.

    Writes are hostile-input-facing: the caller is a login, not an operator.
    """

    @abstractmethod
    async def get(
        self, provider: str, provider_user_id: str
    ) -> Optional["PersonalEnterpriseRecord"]:
        """Return the subject's **live** personal-tenant record, or None.

        A retired binding answers ``None``. The row is kept — it carries the
        operator's next-login policy, which is what releases or refuses the next
        sign-in — but answering with it here would resolve the subject straight
        back into the tenant the retirement fenced them out of.
        """

    @abstractmethod
    async def find_by_enterprise(
        self, provider: str, provider_user_id: str, enterprise_id: str
    ) -> bool:
        """Whether this subject's personal tenant lives in ``enterprise_id``.

        Read on the **mapped** branch, where the session is bound to the company
        tenant. It is what lets a company login tell "this account is anchored
        to a personal enterprise I may re-anchor" from "this account belongs to
        a different company" (ADR-016 D5 as amended by ADR-017 D9).
        """

    @abstractmethod
    async def count_created_since(self, provider: str, since: datetime) -> int:
        """How many personal tenants this provider has minted since ``since``.

        Backs the provisioning ceiling: the switch alone bounds nothing, so an
        unauthenticated sign-up loop could exhaust the IdP's organization quota.
        Counting is deliberately global rather than per-subject — the abuse
        shape is many subjects, not one subject retrying.
        """

    @abstractmethod
    async def provision(
        self,
        *,
        provider: str,
        provider_user_id: str,
        provider_org_id: str,
        name: str,
        slug: str,
    ) -> str:
        """Create the tenant for one subject, atomically, and return its
        ENTERPRISE id.

        Writes **three rows in one transaction**: the enterprise, the
        ``sso_org_mappings`` row binding ``provider_org_id`` to it, and the
        ``sso_personal_enterprises`` row binding the subject. No organization
        and no team — those are a billing fact and a consent fact (ADR-017
        D5/D4), and a sign-in knows neither.

        Implementations must be idempotent and race-safe: a second call for the
        same subject, whether sequential or concurrent, returns the enterprise
        the first one created and writes no second tenant. A failure part-way
        must leave nothing behind for a later login to adopt.

        A subject whose previous enterprise was retired with
        ``fresh_tenant`` has its existing row **re-pointed** rather than a
        second one inserted: ``subject`` is the primary key, so there is exactly
        one, and the retirement columns are cleared as part of the move because
        the policy has by then been honoured.

        No tenant context is bound: every table this writes is outside RLS —
        ``enterprises`` is the tenant, and the two SSO tables are read on the
        unauthenticated callback before one exists.
        """

    @abstractmethod
    async def confirm_membership(self, provider: str, provider_user_id: str) -> None:
        """Record that the IdP-side membership for this subject now exists.

        The IdP membership is established *after* the tenant commits, because a
        membership is what makes the IdP start echoing the organization — and an
        echoed organization with no committed mapping sends every later login
        down the mapped branch to a permanent ``sso_org_unmapped``. This marks
        the IdP half finished so a returning login does not pay a provider
        round-trip to re-confirm it.
        """

    @abstractmethod
    async def retire(self, provider: str, provider_user_id: str) -> bool:
        """Drop the subject's personal binding outright; True if a row was removed.

        Called when a mapped (company) login re-anchors an account that was
        anchored to a personal enterprise. The personal enterprise and its cases
        are deliberately left alone — this is not a migration (#1045 non-goal) —
        but the *binding* must go, or a later unscoped login would resolve the
        user back into a tenant they can no longer enter.

        Deliberately a delete rather than the stamp an operator retirement
        writes: the account no longer lives in a personal tenant at all, so
        there is no next-login policy to record, and a stamped row would tell
        the anchor check the opposite.
        """


# ============================================================
# Team Membership Policy
# ============================================================


async def is_team_member(team_service, user_id: Optional[str], team_id: str) -> bool:
    """Whether ``user_id`` belongs to ``team_id``. Fail-closed.

    The single membership predicate behind every surface that lets a caller
    target a team they name (case share, KB team publish) — extracted so the
    surfaces cannot drift on the rule "you may only share/publish into a team
    you belong to". Each surface raises its own exception type on refusal;
    what is shared is the resolution and its fail-closed semantics:
    no team service wired (standalone), a missing id, or a resolution error
    all answer ``False`` — never a silent allow.

    ``team_service`` is the duck-typed injected service exposing
    ``list_all_user_team_ids`` (RLS-scoped to the caller's org, so a
    foreign-org team id can never resolve as a membership).
    """
    if not team_service or not user_id or not team_id:
        return False
    try:
        return team_id in await team_service.list_all_user_team_ids(user_id)
    except Exception as exc:  # noqa: BLE001 — fail closed on any resolution error
        # Log before answering False: to the caller a resolver outage is
        # indistinguishable from a genuine non-membership refusal, so this
        # line is the only signal that the refusal was infrastructural.
        logging.getLogger(__name__).warning(
            "Team membership resolution failed for user %s / team %s: %s",
            user_id,
            team_id,
            exc,
        )
        return False


# ============================================================
# Module Exports
# ============================================================

__all__ = [
    # Roles
    "PLATFORM_ADMIN_ROLE",
    "PLATFORM_ADMIN_ROLE_SET",
    "OPERATOR_GRANTED_ROLES",
    "ORG_ADMIN_ROLE",
    "BASE_USER_ROLE",
    # DTOs
    "UserDTO",
    "SessionDTO",
    "OAuthAuthorizationDTO",
    "OAuthTokenDTO",
    "OAuthCodeDTO",
    "AuthTokenDTO",
    "SSOIdentity",
    "PersonalEnterpriseRecord",
    "RetiredIdPOrganization",
    # Personal-tenant retirement vocabulary
    "RETIREMENT_POLICY_REFUSE",
    "RETIREMENT_POLICY_FRESH_TENANT",
    # Repository Protocols
    "IUserRepository",
    "IUserQuery",
    "IOAuthCodeRepository",
    # Policy helpers
    "is_team_member",
    # Service Protocols
    "IAuthService",
    "IOAuthService",
    "ILocalAuthService",
    "IPermissionChecker",
    "ISessionService",
    "ISSOIdentityProvider",
    "ISSOOrgMappingRepository",
    "ISSOPersonalEnterpriseRepository",
    "ISSOTenantRetirementProvider",
]
