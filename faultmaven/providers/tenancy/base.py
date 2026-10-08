"""TenantProvider (abstract base class).

The deployment-mode strategy for tenancy: it says whether this deployment is
single- or multi-tenant and, for Standalone, seeds the one enterprise and team.
It does NOT resolve the request's enterprise. That is bound exactly once per
request by ``api/middleware/tenant_scope.bind_request_enterprise_context`` (the
verified ``enterprise_id`` claim under multi, the Standalone sentinel under
single) and read back from ``config.tenant_context``; PostgreSQL RLS scopes every
tenant-scoped table to it. A ``get_current_enterprise`` method used to live here and was
removed (#1891) once nothing called it.

Under ADR-017 the tenant is the **enterprise**: it is what isolates, and it is
the only thing a visibility question may resolve within. The organization is a
billing target and is deliberately absent from this interface — a service that
needs to know who pays reads the actor's organization, not the request's tenant.
"""

from abc import ABC, abstractmethod


class TenantProvider(ABC):
    """Abstract base class for the deployment's tenancy strategy.

    Implementations (both in-core, config-selected by ``TENANT_PROVIDER``, ADR-010):
    - SingleTenantProvider: Standalone — one seeded enterprise and team
    - MultiTenantProvider: Cloud — many isolated enterprises

    Design Pattern:
        This follows the Strategy pattern, allowing deployment mode to be
        determined at runtime via dependency injection rather than compile-time
        conditional logic.
    """

    @abstractmethod
    async def is_multi_tenant(self) -> bool:
        """Check if this provider operates in multi-tenant mode.

        Returns:
            bool: True if multi-tenant, False if single-tenant
        """
