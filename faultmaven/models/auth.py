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
from typing import Optional
from enum import Enum
from faultmaven.models import parse_utc_timestamp
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
    """
    user_id: str
    username: str
    email: str
    display_name: str
    created_at: datetime
    is_dev_user: bool = True
    is_active: bool = True
    roles: list[str] = None  # Will be set to ['admin'] by default in __post_init__

    def __post_init__(self):
        """Set default roles if not provided"""
        if self.roles is None:
            # Default: all dev users are admins for development
            # In production, this should default to ['user']
            self.roles = ['admin']

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
            "roles": self.roles if self.roles else ['admin']
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'DevUser':
        """Create from dictionary (JSON deserialization)"""
        return cls(
            user_id=data["user_id"],
            username=data["username"],
            email=data["email"],
            display_name=data["display_name"],
            created_at=parse_utc_timestamp(data["created_at"]),
            is_dev_user=data.get("is_dev_user", True),
            is_active=data.get("is_active", True),
            roles=data.get("roles", ['admin'])  # Default to admin for dev users
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
            "last_used_at": to_json_compatible(self.last_used_at) if self.last_used_at else None,
            "is_revoked": self.is_revoked
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'AuthToken':
        """Create from dictionary (JSON deserialization)"""
        return cls(
            token_id=data["token_id"],
            user_id=data["user_id"],
            token_hash=data["token_hash"],
            expires_at=parse_utc_timestamp(data["expires_at"]),
            created_at=parse_utc_timestamp(data["created_at"]),
            last_used_at=parse_utc_timestamp(data["last_used_at"]) if data.get("last_used_at") else None,
            is_revoked=data.get("is_revoked", False)
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