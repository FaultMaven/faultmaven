"""User Domain Model (TASK-018)

Purpose: Define the User domain model for user management operations.

This module provides the core User domain model used throughout the
application for user authentication, profile management, and authorization.

Design Reference: TASK-018 User Management Service
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class User:
    """User domain model.

    Represents a user account in the system with authentication
    credentials and profile information.

    Attributes:
        user_id: Unique identifier (UUID)
        email: Unique email address
        hashed_password: bcrypt hash of password
        full_name: User's full display name
        is_active: Whether account is active
        is_verified: Whether email is verified
        created_at: Account creation timestamp
        updated_at: Last update timestamp
        last_login_at: Last successful login timestamp
        metadata: Optional user metadata (JSON)
    """

    user_id: str
    email: str
    hashed_password: str
    full_name: str
    is_active: bool = True
    is_verified: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_login_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None

    def verify_password(self, plain_password: str) -> bool:
        """Verify password against hashed password.

        Args:
            plain_password: Plain text password to verify

        Returns:
            True if password matches, False otherwise
        """
        import bcrypt

        return bcrypt.checkpw(
            plain_password.encode("utf-8"), self.hashed_password.encode("utf-8")
        )

    # NOTE: there is deliberately no update_last_login helper here. Login
    # stamps go through UserRepository.touch_last_login (a targeted UPDATE),
    # because a read-modify-write of this object racing an admin action can
    # revert it (#1127 review). A helper on this model had zero callers and
    # advertised exactly that unsafe shape.

    def deactivate(self) -> None:
        """Deactivate user account (soft delete)."""
        self.is_active = False
        self.updated_at = datetime.now(timezone.utc)

    def activate(self) -> None:
        """Activate user account."""
        self.is_active = True
        self.updated_at = datetime.now(timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization.

        Note: This does NOT include hashed_password for security.

        Returns:
            Dictionary representation of user (without password)
        """
        from faultmaven.utils.serialization import to_json_compatible

        return {
            "user_id": self.user_id,
            "email": self.email,
            "full_name": self.full_name,
            "is_active": self.is_active,
            "is_verified": self.is_verified,
            "created_at": to_json_compatible(self.created_at),
            "updated_at": to_json_compatible(self.updated_at),
            "last_login_at": (
                to_json_compatible(self.last_login_at) if self.last_login_at else None
            ),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "User":
        """Create User from dictionary.

        Args:
            data: Dictionary with user data (must include hashed_password)

        Returns:
            User instance
        """
        from faultmaven.utils.datetime import parse_utc_timestamp

        return cls(
            user_id=data["user_id"],
            email=data["email"],
            hashed_password=data.get("hashed_password", ""),
            full_name=data["full_name"],
            is_active=data.get("is_active", True),
            is_verified=data.get("is_verified", False),
            created_at=parse_utc_timestamp(data["created_at"]),
            updated_at=parse_utc_timestamp(data["updated_at"]),
            last_login_at=(
                parse_utc_timestamp(data["last_login_at"])
                if data.get("last_login_at")
                else None
            ),
            metadata=data.get("metadata"),
        )


@dataclass
class PasswordResetToken:
    """Password reset token data.

    Used to track password reset requests and prevent token reuse.

    Attributes:
        token_id: Unique token identifier (jti)
        user_id: User requesting the reset
        email: Email the reset was sent to
        created_at: When the token was created
        expires_at: When the token expires
        used_at: When the token was used (None if not used)
    """

    token_id: str
    user_id: str
    email: str
    created_at: datetime
    expires_at: datetime
    used_at: Optional[datetime] = None

    @property
    def is_used(self) -> bool:
        """Check if token has been used."""
        return self.used_at is not None

    @property
    def is_expired(self) -> bool:
        """Check if token has expired."""
        return datetime.now(timezone.utc) > self.expires_at

    @property
    def is_valid(self) -> bool:
        """Check if token is valid (not used and not expired)."""
        return not self.is_used and not self.is_expired

    def mark_used(self) -> None:
        """Mark the token as used."""
        self.used_at = datetime.now(timezone.utc)
