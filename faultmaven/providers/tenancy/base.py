"""TenantProvider Protocol (Abstract Base Class).

Defines the interface for tenant context resolution to enable deployment-neutral
services that work in both single-tenant and multi-tenant environments.
"""

from abc import ABC, abstractmethod
from typing import Optional, Protocol

from faultmaven.models.interfaces_user import Organization


class TenantUser(Protocol):
    """What tenancy needs of an authenticated user, and nothing more.

    Structural, so the auth module's ``User`` satisfies it without this package
    importing it. That import was a real boundary violation — a cross-cutting
    provider reaching into another module's DOMAIN models — and because every
    module reaches tenancy through ``config.tenant_context``, it made all five
    of them transitively importers of ``auth.domain``. It went unseen because
    ``faultmaven/providers`` had no ``__init__.py``, so grimp never put it in
    the import graph and the contracts naming it could not fail.

    Only these two attributes are ever read (``multi_tenant`` uses both; the
    other implementations ignore the argument), so this is the whole dependency.
    """

    user_id: str
    email: str


class TenantProvider(ABC):
    """Abstract base class for tenant context resolution.

    Enables deployment-neutral services by abstracting organization context.

    Implementations (both in-core, config-selected by ``TENANT_PROVIDER``, ADR-010):
    - SingleTenantProvider: Returns the default organization (Standalone)
    - MultiTenantProvider: Resolves and validates the organization from request
      context (Cloud / multi-tenant)

    Design Pattern:
        This follows the Strategy pattern, allowing deployment mode to be
        determined at runtime via dependency injection rather than compile-time
        conditional logic.
    """

    @abstractmethod
    async def get_current_organization(
        self, current_user: TenantUser, organization_id: Optional[str] = None
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
