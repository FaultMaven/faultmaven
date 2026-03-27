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
from datetime import UTC, datetime
from enum import Enum

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
        roles: List of user roles for access control (e.g., ['admin'], ['user'])
        organization_id: Organization UUID (defaults to SingleTenantProvider.DEFAULT_ORG_ID)
    """

    user_id: str
    username: str
    email: str
    display_name: str
    created_at: datetime
    is_dev_user: bool = True
    is_active: bool = True
    roles: list[str] = None  # Will be set to ['admin'] by default in __post_init__
    organization_id: str = None  # Will be set to DEFAULT_ORG_ID in __post_init__

    def __post_init__(self):
        """Set default roles and organization_id if not provided"""
        if self.roles is None:
            # Default: all dev users are admins for development
            # In production, this should default to ['user']
            self.roles = ["admin"]
        if self.organization_id is None:
            # Default to SingleTenantProvider.DEFAULT_ORG_ID for local mode
            self.organization_id = "00000000-0000-0000-0000-000000000001"

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
            "roles": self.roles if self.roles else ["admin"],
            "organization_id": self.organization_id,
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
            roles=data.get("roles", ["admin"]),  # Default to admin for dev users
            organization_id=data.get("organization_id"),
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
    last_used_at: datetime | None = None
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
        return datetime.now(UTC) > self.expires_at

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
    user: DevUser | None = None
    error_message: str | None = None

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
        organization_id: Organization UUID (from 'organization_id' claim)
        email: User email address
        roles: List of organization-level roles (admin, member, viewer)
        permissions: List of granular permissions (cases:read, sessions:execute, etc.)
        token_jti: JWT ID for token revocation tracking (optional)
    """

    user_id: str
    organization_id: str
    email: str
    roles: list[str]
    permissions: list[str]
    token_jti: str | None = None

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

    def is_admin(self) -> bool:
        """Check if user has admin role.

        Returns:
            True if user is an admin
        """
        return "admin" in self.roles

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
        organization_id: Organization ID
        email: User email
        roles: User roles in organization
        permissions: Granular permissions
        token_type: "access" or "refresh"
    """

    sub: str  # user_id
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
        """Convert to dictionary for JWT encoding."""
        return {
            "sub": self.sub,
            "organization_id": self.organization_id,
            "email": self.email,
            "roles": self.roles,
            "permissions": self.permissions,
            "iss": self.iss,
            "aud": self.aud,
            "iat": self.iat,
            "exp": self.exp,
            "jti": self.jti,
            "token_type": self.token_type,
        }
