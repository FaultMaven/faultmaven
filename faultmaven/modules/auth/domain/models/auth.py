"""Authentication Data Models

Purpose: Define data structures for users and authentication tokens

This module provides the core data models for the FaultMaven authentication system.
These models are designed to be simple, testable, and easily replaceable when
migrating to production authentication providers.

Key Components:
- DevUser: Represents a development user account
- AuthToken: Represents an authentication token with metadata
- TokenStatus: Enum for token validation states
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.modules.auth.domain.models.rbac import PLATFORM_ADMIN_ROLE
from faultmaven.utils.datetime import parse_utc_timestamp
from faultmaven.utils.serialization import to_json_compatible


class TokenStatus(Enum):
    """Token validation status"""

    VALID = "valid"
    EXPIRED = "expired"
    INVALID = "invalid"
    REVOKED = "revoked"


@dataclass
class DevUser:
    """Development user account

    Represents a user in the development authentication system.
    Designed to be compatible with future production user models.

    Attributes:
        user_id: Unique identifier (UUID format)
        username: Unique username for login
        email: User email address
        display_name: Human-readable display name
        created_at: Account creation timestamp
        is_dev_user: Flag indicating development account
        is_active: Account active status
        roles: List of user roles for access control (e.g., ['user'],
            ['user', 'admin', 'platform_admin'])
        enterprise_id: Enterprise UUID — the ISOLATION boundary (ADR-017 D1).
            Defaults to ``config.constants.STANDALONE_ENTERPRISE_ID``, which is
            the standalone deployment's one enterprise.
        organization_id: Organization UUID, or ``None`` — BILLING attribution
            only (ADR-017 D2). An account may be in no organization; ``None`` is
            the ordinary answer, and it grants and denies nothing.
        account_kind: ADR-012 account kind — 'individual' (human) or 'slack'
            (service account owning a workspace's cases). Carried here because
            the user store round-trips users through DevUser on update; dropping
            it would rewrite a service account as 'individual' and change the
            derived ``cases.source`` for everything it later creates.
    """

    user_id: str
    username: str
    email: str
    display_name: str
    created_at: datetime
    is_dev_user: bool = True
    is_active: bool = True
    roles: list[str] = None  # Will be set to ['user'] by default in __post_init__
    # Isolation. Defaults to the standalone sentinel in __post_init__.
    enterprise_id: str = None
    # Billing. ``None`` is a legitimate steady state (no organization pays for
    # this account) and is NOT defaulted to anything — a sentinel here would be
    # a value some later reader mistakes for a tenant.
    organization_id: Optional[str] = None
    account_kind: str = "individual"

    def __post_init__(self):
        """Set default roles and enterprise_id if not provided"""
        if self.roles is None:
            # Least privilege. Any construction path that forgets to pass
            # roles gets a baseline account, never a privileged one — a
            # privileged default silently promotes every such caller,
            # including service accounts. Callers wanting elevation say so.
            self.roles = ["user"]
        if self.enterprise_id is None:
            # Implicit single-tenant enterprise (standalone); see
            # config.constants. Under ``multi`` this sentinel is refused as a
            # tenant by ``usable_tenant_id``, so defaulting here cannot widen
            # anything — it only keeps single-tenant construction sites total.
            self.enterprise_id = STANDALONE_ENTERPRISE_ID

    def is_platform_admin(self) -> bool:
        """Check if user holds the cross-tenant operator role.

        See :meth:`AuthenticatedUser.is_platform_admin` — same predicate, for
        the other user representation.
        """
        return PLATFORM_ADMIN_ROLE in (self.roles or [])

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "email": self.email,
            "display_name": self.display_name,
            "created_at": to_json_compatible(self.created_at),
            "is_dev_user": self.is_dev_user,
            "is_active": self.is_active,
            "roles": self.roles if self.roles else ["user"],
            "enterprise_id": self.enterprise_id,
            "organization_id": self.organization_id,
            "account_kind": self.account_kind,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DevUser":
        """Create from dictionary (JSON deserialization)"""
        return cls(
            user_id=data["user_id"],
            username=data["username"],
            email=data["email"],
            display_name=data["display_name"],
            created_at=parse_utc_timestamp(data["created_at"]),
            is_dev_user=data.get("is_dev_user", True),
            is_active=data.get("is_active", True),
            roles=data.get("roles", ["user"]),  # Least privilege when absent
            enterprise_id=data.get("enterprise_id"),
            organization_id=data.get("organization_id"),
            account_kind=data.get("account_kind", "individual"),
        )


@dataclass
class AuthToken:
    """Authentication token with metadata

    Represents an authentication token in the system.
    Contains metadata for security and auditing purposes.

    Attributes:
        token_id: Unique token identifier
        user_id: Associated user identifier
        token_hash: SHA-256 hash of the actual token
        expires_at: Token expiration timestamp
        created_at: Token creation timestamp
        last_used_at: Last usage timestamp (optional)
        is_revoked: Token revocation status
    """

    token_id: str
    user_id: str
    token_hash: str
    expires_at: datetime
    created_at: datetime
    last_used_at: Optional[datetime] = None
    is_revoked: bool = False

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        return {
            "token_id": self.token_id,
            "user_id": self.user_id,
            "token_hash": self.token_hash,
            "expires_at": to_json_compatible(self.expires_at),
            "created_at": to_json_compatible(self.created_at),
            "last_used_at": (
                to_json_compatible(self.last_used_at) if self.last_used_at else None
            ),
            "is_revoked": self.is_revoked,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuthToken":
        """Create from dictionary (JSON deserialization)"""
        return cls(
            token_id=data["token_id"],
            user_id=data["user_id"],
            token_hash=data["token_hash"],
            expires_at=parse_utc_timestamp(data["expires_at"]),
            created_at=parse_utc_timestamp(data["created_at"]),
            last_used_at=(
                parse_utc_timestamp(data["last_used_at"])
                if data.get("last_used_at")
                else None
            ),
            is_revoked=data.get("is_revoked", False),
        )

    @property
    def is_expired(self) -> bool:
        """Check if token is expired"""
        return datetime.now(timezone.utc) > self.expires_at

    @property
    def is_valid(self) -> bool:
        """Check if token is valid (not expired and not revoked)"""
        return not self.is_expired and not self.is_revoked


@dataclass
class TokenValidationResult:
    """Result of token validation operation

    Contains the validation status and associated user if valid.
    Used by token managers to return structured validation results.

    Attributes:
        status: Validation status (TokenStatus enum)
        user: Associated user if token is valid
        error_message: Error description if invalid
    """

    status: TokenStatus
    user: Optional[DevUser] = None
    error_message: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        """Check if validation was successful"""
        return self.status == TokenStatus.VALID and self.user is not None

    @property
    def is_expired(self) -> bool:
        """Check if token was expired"""
        return self.status == TokenStatus.EXPIRED


# ============================================================
# JWT Authentication Models (TASK-017)
# ============================================================


@dataclass
class AuthenticatedUser:
    """Represents an authenticated user from a JWT token.

    This is the standard user representation extracted from a validated
    JWT access token. It contains all claims necessary for authorization.

    Attributes:
        user_id: User UUID (from 'sub' claim)
        enterprise_id: Enterprise UUID (from the 'enterprise_id' claim) — the
            isolation boundary (ADR-017 D1). Every tenant-scoped resolution
            keys on this.
        organization_id: Organization UUID (from the 'organization_id' claim),
            or ``""`` when the account is in none. BILLING only (ADR-017 D2):
            it must never be read as a visibility predicate.
        email: User email address
        roles: List of organization-level roles (admin, member, viewer)
        permissions: List of granular permissions (cases:read, sessions:execute, etc.)
        token_jti: JWT ID for token revocation tracking (optional)
    """

    user_id: str
    enterprise_id: str
    email: str
    roles: list[str]
    permissions: list[str]
    #: Billing, and DEFAULTED — an account in no organization is the ordinary
    #: case (ADR-017 D5), so a construction site that says nothing about billing
    #: is saying "nobody pays for this one" rather than forgetting a field. The
    #: isolation key above is deliberately NOT defaulted: a caller that failed to
    #: resolve a tenant must not be able to build this object at all.
    organization_id: str = ""
    token_jti: Optional[str] = None

    def has_permission(self, permission: str) -> bool:
        """Check if user has a specific permission.

        Args:
            permission: Permission string (e.g., "cases:read")

        Returns:
            True if user has the permission
        """
        return permission in self.permissions

    def has_role(self, role: str) -> bool:
        """Check if user has a specific role.

        Args:
            role: Role name (e.g., "admin")

        Returns:
            True if user has the role
        """
        return role in self.roles

    def is_platform_admin(self) -> bool:
        """Check if user holds the cross-tenant operator role.

        The org-scoped ``Role.ADMIN`` does not satisfy this; see
        ``PLATFORM_ADMIN_ROLE`` for why the two axes are separate.
        """
        return PLATFORM_ADMIN_ROLE in self.roles

    def has_any_permission(self, permissions: list[str]) -> bool:
        """Check if user has any of the specified permissions.

        Args:
            permissions: List of permission strings

        Returns:
            True if user has at least one of the permissions
        """
        return any(perm in self.permissions for perm in permissions)

    def has_all_permissions(self, permissions: list[str]) -> bool:
        """Check if user has all of the specified permissions.

        Args:
            permissions: List of permission strings

        Returns:
            True if user has all of the permissions
        """
        return all(perm in self.permissions for perm in permissions)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "user_id": self.user_id,
            "enterprise_id": self.enterprise_id,
            "organization_id": self.organization_id,
            "email": self.email,
            "roles": self.roles,
            "permissions": self.permissions,
            "token_jti": self.token_jti,
        }

    @classmethod
    def from_jwt_claims(cls, claims: dict) -> "AuthenticatedUser":
        """Create AuthenticatedUser from JWT claims.

        Args:
            claims: Decoded JWT claims dictionary

        Returns:
            AuthenticatedUser instance
        """
        return cls(
            user_id=claims.get("sub", ""),
            enterprise_id=claims.get("enterprise_id", ""),
            organization_id=claims.get("organization_id", ""),
            email=claims.get("email", ""),
            roles=claims.get("roles", []),
            permissions=claims.get("permissions", []),
            token_jti=claims.get("jti"),
        )


@dataclass
class TokenPair:
    """Access and refresh token pair.

    Returned after successful authentication (login) or token refresh.

    Attributes:
        access_token: JWT access token (short-lived, typically 15 minutes)
        refresh_token: JWT refresh token (long-lived, typically 7 days)
        token_type: Token type for Authorization header ("Bearer")
        expires_in: Access token expiration time in seconds
    """

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = 900  # 15 minutes in seconds

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
        }


@dataclass
class TokenClaims:
    """JWT token claims structure.

    Represents all claims that should be included in a JWT token.

    Standard Claims:
        sub: Subject (user ID)
        iss: Issuer
        aud: Audience
        iat: Issued at (Unix timestamp)
        exp: Expiration (Unix timestamp)
        jti: JWT ID (unique token identifier for revocation)

    Custom Claims:
        enterprise_id: Enterprise ID — the isolation claim the binder reads
            (ADR-017 D9). A token without it is refused.
        organization_id: Organization ID (billing context; ``""`` when none)
        email: User email
        roles: User roles in organization
        permissions: Granular permissions
        token_type: Python field name — serialised as "type" in the JWT payload
                    (to_dict translates the field name to the claim key).
    """

    sub: str  # user_id
    enterprise_id: str
    organization_id: str
    email: str
    roles: list[str]
    permissions: list[str]
    iss: str
    aud: str
    iat: int
    exp: int
    jti: str
    token_type: str = "access"

    def to_dict(self) -> dict:
        """Serialize to a JWT payload dict. Field ``token_type`` maps to claim ``"type"``."""
        return {
            "sub": self.sub,
            "enterprise_id": self.enterprise_id,
            "organization_id": self.organization_id,
            "email": self.email,
            "roles": self.roles,
            "permissions": self.permissions,
            "iss": self.iss,
            "aud": self.aud,
            "iat": self.iat,
            "exp": self.exp,
            "jti": self.jti,
            "type": self.token_type,
        }
