"""TenantProvider Protocol (Abstract Base Class).

Defines the interface for tenant context resolution to enable deployment-neutral
services that work in both single-tenant and multi-tenant environments.

Design Reference: docs/working/TASK-023-TENANT-PROVIDER.md
"""

from abc import ABC, abstractmethod
from typing import Optional

from faultmaven.models.interfaces_user import Organization
from faultmaven.models.user import User


class TenantProvider(ABC):
    """Abstract base class for tenant context resolution.

    Enables deployment-neutral services by abstracting organization context.

    Implementations:
    - SingleTenantProvider: Returns default organization (local deployment)
    - MultiTenantProvider: Extracts organization from request context (cloud)

    Design Pattern:
        This follows the Strategy pattern, allowing deployment mode to be
        determined at runtime via dependency injection rather than compile-time
        conditional logic.
    """

    @abstractmethod
    async def get_current_organization(
        self, current_user: User, organization_id: Optional[str] = None
    ) -> Organization:
        """Resolve the current organization context.

        Args:
            current_user: Authenticated user from JWT
            organization_id: Optional explicit organization ID (for multi-tenant)

        Returns:
            Organization: The organization context for this request

        Raises:
            NotFoundError: If organization doesn't exist
            AuthorizationError: If user not a member of organization
            ValidationException: If required parameters missing (multi-tenant)
        """
        pass

    @abstractmethod
    async def get_default_organization(self) -> Organization:
        """Get the default organization (used for local/single-tenant mode).

        Returns:
            Organization: The default organization

        Raises:
            NotFoundError: If default organization doesn't exist
            NotImplementedError: If not supported (multi-tenant mode)
        """
        pass

    @abstractmethod
    async def is_multi_tenant(self) -> bool:
        """Check if this provider operates in multi-tenant mode.

        Returns:
            bool: True if multi-tenant, False if single-tenant
        """
        pass
