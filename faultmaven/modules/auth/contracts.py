"""
Auth Module Public Contracts

This file defines the public interface of the auth module.
Other modules MUST only import from this file, never from
domain/ or infrastructure/ directories.

Contracts include:
- DTOs: Data transfer objects for cross-module communication
- Protocols: Interface definitions for dependency injection
- Exceptions: Re-exported domain exceptions
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol, runtime_checkable


# ============================================
# Data Transfer Objects (DTOs)
# ============================================


@dataclass(frozen=True)
class UserDTO:
    """Public representation of a user.

    This is the cross-module representation of a user entity.
    Does not expose internal details like password hashes.
    """

    user_id: str
    username: str
    email: str
    display_name: Optional[str]
    is_active: bool
    created_at: datetime


@dataclass(frozen=True)
class SessionDTO:
    """Public representation of a session.

    Represents an authenticated user session for cross-module use.
    """

    session_id: str
    user_id: str
    device_id: Optional[str]
    created_at: datetime
    expires_at: Optional[datetime]


@dataclass(frozen=True)
class AuthenticationResultDTO:
    """Result of authentication attempt.

    Returned by authentication operations to indicate success/failure.
    """

    success: bool
    user: Optional[UserDTO]
    session: Optional[SessionDTO]
    error_message: Optional[str] = None


@dataclass(frozen=True)
class TokenPairDTO:
    """Access and refresh token pair.

    Returned after successful authentication or token refresh.
    """

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = 900  # 15 minutes in seconds


# ============================================
# Service Protocols (Interfaces)
# ============================================


@runtime_checkable
class IAuthQuery(Protocol):
    """Read-only auth operations for cross-module use.

    Provides read access to user and session data for other modules.
    """

    async def get_user(self, user_id: str) -> Optional[UserDTO]:
        """Get user by ID.

        Args:
            user_id: The user's unique identifier

        Returns:
            UserDTO if found, None otherwise
        """
        ...

    async def get_users_by_ids(self, user_ids: list[str]) -> list[UserDTO]:
        """Bulk user lookup - prevents N+1 queries.

        Args:
            user_ids: List of user IDs to fetch

        Returns:
            List of UserDTO objects (may be fewer than requested if some not found)
        """
        ...

    async def get_user_by_email(self, email: str) -> Optional[UserDTO]:
        """Get user by email address.

        Args:
            email: The user's email address

        Returns:
            UserDTO if found, None otherwise
        """
        ...

    async def get_session(self, session_id: str) -> Optional[SessionDTO]:
        """Get session by ID.

        Args:
            session_id: The session's unique identifier

        Returns:
            SessionDTO if found, None otherwise
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


@runtime_checkable
class IAuthCommand(Protocol):
    """Write operations for auth (typically internal use).

    Provides write access to auth data. Use with caution from other modules.
    """

    async def create_session(
        self, user_id: str, device_id: Optional[str] = None
    ) -> SessionDTO:
        """Create new session for user.

        Args:
            user_id: The user's unique identifier
            device_id: Optional device identifier for multi-device support

        Returns:
            Created SessionDTO
        """
        ...

    async def invalidate_session(self, session_id: str) -> None:
        """Invalidate/logout a session.

        Args:
            session_id: The session's unique identifier
        """
        ...


# ============================================
# Re-exports for convenience
# ============================================

# Export exceptions from module exceptions
# (allows: from auth.contracts import UserNotFoundError)
from faultmaven.modules.auth.exceptions import (
    AuthException,
    AuthenticationError,
    OrganizationError,
    SessionError,
    TokenError,
    TokenExpiredError,
    TokenInvalidError,
    UserNotFoundError,
    UserStoreError,
)

__all__ = [
    # DTOs
    "UserDTO",
    "SessionDTO",
    "AuthenticationResultDTO",
    "TokenPairDTO",
    # Protocols
    "IAuthQuery",
    "IAuthCommand",
    # Exceptions
    "AuthException",
    "AuthenticationError",
    "TokenError",
    "TokenExpiredError",
    "TokenInvalidError",
    "SessionError",
    "UserNotFoundError",
    "UserStoreError",
    "OrganizationError",
]
