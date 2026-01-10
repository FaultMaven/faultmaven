"""Auth Module Contracts

This module defines the public interfaces (contracts) for the Auth vertical module.
Other modules should import from here, not from infrastructure or domain directly.

Following the design in module-organization-design.md:
- Vertical modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication
"""

from typing import Protocol, Optional, List, TYPE_CHECKING
from abc import ABC

if TYPE_CHECKING:
    from faultmaven.modules.auth.domain.models.user import User
    from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser, TokenPair


# ============================================================
# Repository Contracts
# ============================================================

class IUserRepository(Protocol):
    """Repository interface for User persistence operations."""
    
    async def save(self, user: 'User') -> 'User':
        """Save user to persistence layer."""
        ...
    
    async def get(self, user_id: str) -> Optional['User']:
        """Retrieve user by ID."""
        ...
    
    async def get_by_username(self, username: str) -> Optional['User']:
        """Retrieve user by username."""
        ...
    
    async def get_by_email(self, email: str) -> Optional['User']:
        """Retrieve user by email."""
        ...
    
    async def list(self, limit: int = 50, offset: int = 0) -> tuple[List['User'], int]:
        """List users with pagination."""
        ...
    
    async def delete(self, user_id: str) -> bool:
        """Delete user by ID."""
        ...


class IUserQuery(Protocol):
    """Read-only user query interface (for high fan-in scenarios)."""
    
    async def get_user(self, user_id: str) -> Optional['User']:
        """Get user by ID (read-only)."""
        ...
    
    async def get_by_email(self, email: str) -> Optional['User']:
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


# ============================================================
# Re-export concrete interfaces from repositories
# ============================================================

# Import and re-export UserRepository as IUserRepository for convenience
from faultmaven.modules.auth.infrastructure.repositories.user_repository import UserRepository as _UserRepository
IUserRepository = _UserRepository  # Alias for consistency


# ============================================================
# DTOs (Data Transfer Objects)
# ============================================================

# User domain model can be used directly as DTO
# Additional DTOs can be added here if needed
