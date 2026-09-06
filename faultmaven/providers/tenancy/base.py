"""TenantProvider Protocol (Abstract Base Class).

Defines the interface for tenant context resolution to enable deployment-neutral
services that work in both single-tenant and multi-tenant environments.

Under ADR-017 the tenant is the **enterprise**: it is what isolates, and it is
the only thing a visibility question may resolve within. The organization is a
billing target and is deliberately absent from this interface — a service that
needs to know who pays reads the actor's organization, not the request's tenant.
"""

from abc import ABC, abstractmethod
from typing import Optional

from faultmaven.models.interfaces_user import Enterprise
from faultmaven.modules.auth.domain.models.user import User


class TenantProvider(ABC):
    """Abstract base class for tenant context resolution.

    Enables deployment-neutral services by abstracting enterprise context.

    Implementations (both in-core, config-selected by ``TENANT_PROVIDER``, ADR-010):
    - SingleTenantProvider: Returns the default enterprise (Standalone)
    - MultiTenantProvider: Resolves and validates the enterprise from request
      context (Cloud / multi-tenant)

    Design Pattern:
        This follows the Strategy pattern, allowing deployment mode to be
        determined at runtime via dependency injection rather than compile-time
        conditional logic.
    """

    @abstractmethod
    async def get_current_enterprise(
        self, current_user: User, enterprise_id: Optional[str] = None
    ) -> Enterprise:
        """Resolve the current enterprise context.

        Args:
            current_user: Authenticated user from JWT
            enterprise_id: Optional explicit enterprise ID (for multi-tenant)

        Returns:
            Enterprise: The enterprise context for this request

        Raises:
            NotFoundError: If the enterprise doesn't exist
            AuthorizationError: If the user is not anchored to the enterprise
            ValidationException: If required parameters missing (multi-tenant)
        """

    @abstractmethod
    async def get_default_enterprise(self) -> Enterprise:
        """Get the default enterprise (used for local/single-tenant mode).

        Returns:
            Enterprise: The default enterprise

        Raises:
            NotFoundError: If the default enterprise doesn't exist
            NotImplementedError: If not supported (multi-tenant mode)
        """

    @abstractmethod
    async def is_multi_tenant(self) -> bool:
        """Check if this provider operates in multi-tenant mode.

        Returns:
            bool: True if multi-tenant, False if single-tenant
        """
