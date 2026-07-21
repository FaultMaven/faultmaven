"""Auth Module Contracts

This module defines the public interfaces (contracts) for the Auth vertical module.
Other modules should import from here, not from infrastructure or domain directly.

Following the design in module-organization-design.md:
- Vertical modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Protocol, runtime_checkable

from faultmaven.modules.auth.domain.models.auth import (
    AuthenticatedUser,
    DevUser,
    TokenPair,
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
    expires_in: int = 3600  # 1 hour
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
    expires_in: int = 3600  # 1 hour (in seconds)
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

    async def create_authorization_code(
        self, user_id: str, request: OAuthAuthorizationDTO
    ) -> str:
        """Generate authorization code for OAuth flow.

        Args:
            user_id: Authenticated user's ID from Dashboard session
            request: OAuth authorization request parameters (includes PKCE challenge)

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

    async def mark_code_used(self, code: str) -> None:
        """Mark code as used (prevents replay attacks).

        Args:
            code: The authorization code to mark as used
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
    ``email_verified`` come straight from the IdP. ``organization_id`` is carried
    for a future org-mapping phase (ADR-010 P2) and is ignored today.
    """

    provider: str
    provider_user_id: str
    email: str
    email_verified: bool
    display_name: str | None = None
    organization_id: str | None = None


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


# ============================================================
# Module Exports
# ============================================================

__all__ = [
    # DTOs
    "UserDTO",
    "SessionDTO",
    "OAuthAuthorizationDTO",
    "OAuthTokenDTO",
    "OAuthCodeDTO",
    "AuthTokenDTO",
    "SSOIdentity",
    # Repository Protocols
    "IUserRepository",
    "IUserQuery",
    "IOAuthCodeRepository",
    # Service Protocols
    "IAuthService",
    "IOAuthService",
    "ILocalAuthService",
    "IPermissionChecker",
    "ISessionService",
    "ISSOIdentityProvider",
]
