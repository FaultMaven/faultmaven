"""TenantProvider Protocol (Abstract Base Class).

Defines the interface for tenant context resolution to enable deployment-neutral
services that work in both single-tenant and multi-tenant environments.

Under ADR-017 the tenant is the **enterprise**: it is what isolates, and it is
the only thing a visibility question may resolve within. The organization is a
billing target and is deliberately absent from this interface — a service that
needs to know who pays reads the actor's organization, not the request's tenant.
"""

from abc import ABC, abstractmethod
from typing import Optional, Protocol

from faultmaven.models.interfaces_user import Enterprise


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
        self, current_user: TenantUser, enterprise_id: Optional[str] = None
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
