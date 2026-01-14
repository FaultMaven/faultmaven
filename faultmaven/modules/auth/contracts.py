"""Auth Module Contracts

This module defines the public interfaces (contracts) for the Auth vertical module.
Other modules should import from here, not from infrastructure or domain directly.

Following the design in module-organization-design.md:
- Vertical modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication
"""

from abc import ABC
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser, TokenPair
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


class IPermissionChecker(Protocol):
    """Interface for permission checking (for high fan-in scenarios)."""

    async def can_access(self, user_id: str, resource: str) -> bool:
        """Check if user can access a resource."""
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
# Re-export concrete interfaces from repositories
# ============================================================

# Import and re-export UserRepository as IUserRepository for convenience
from faultmaven.modules.auth.infrastructure.repositories.user_repository import (
    UserRepository as _UserRepository,
)

IUserRepository = _UserRepository  # Alias for consistency


# ============================================================
# Module Exports
# ============================================================

__all__ = [
    # DTOs
    "UserDTO",
    "SessionDTO",
    # Repository Protocols
    "IUserRepository",
    "IUserQuery",
    # Service Protocols
    "IAuthService",
    "IPermissionChecker",
    "ISessionService",
]
