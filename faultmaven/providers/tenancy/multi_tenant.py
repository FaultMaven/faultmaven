"""MultiTenantProvider for Cloud deployments.

The Cloud arm of the tenancy strategy: many isolated enterprises, no ambient
default. It holds no per-request logic — the request's enterprise is bound by
``api/middleware/tenant_scope`` from the verified claim.

In-core and config-selected by ``TENANT_PROVIDER=multi`` (ADR-010). Multi-tenancy
requires the cloud stack (PostgreSQL + RLS, OAuth/RS256, Redis); the deployment
coherence gate enforces that ``multi`` is only used with ``DEPLOYMENT_MODE=cloud``.

**Membership is ``users.enterprise_id``** (ADR-017, "what the inventory
settled"): the isolation membership needs no roster table, and asking one would
be asking a billing question. ``organization_members`` is the billing roster and
is not consulted here.

It is established at TOKEN MINT and re-established on every rotation, not on
every request. This class used to re-check it per request (a
``get_current_enterprise`` that compared ``current_user.enterprise_id`` with the
requested tenant and raised on a mismatch); both sides came from the same request
binding, so the comparison was tautological and the branch unreachable — a guard
that cannot fail reads like one that can. It was removed (#1891) once nothing
called it. What holds the boundary instead is upstream and is a fact about a row:
the claim is minted from ``users.enterprise_id`` at token time, the request front
door refuses a token without it, and refresh rotation re-reads the row, so a
re-anchored or removed account loses its claim within one rotation (under thirty
minutes). Below that, PostgreSQL RLS scopes every read to the bound enterprise
regardless of what any object believes. A route that genuinely needs a *fresh*
membership answer must read ``users.enterprise_id`` itself and say why; none does.
"""

from faultmaven.models.interfaces_user import IEnterpriseRepository
from faultmaven.providers.tenancy.base import TenantProvider


class MultiTenantProvider(TenantProvider):
    """Multi-tenant provider for cloud deployments.

    Behavior:
    - Reports itself multi-tenant; there is no default enterprise to fall back to
    - Holds the enterprise repository for the Cloud composition's wiring

    Use Cases:
    - Cloud SaaS deployment (many isolated enterprises)
    - Production environments serving many tenants

    Design Notes:
        The enterprise id reaches a request through the verified ``enterprise_id``
        JWT claim, bound by ``api/middleware/tenant_scope`` and read back from
        ``config.tenant_context``.
    """

    def __init__(self, enterprise_repository: IEnterpriseRepository):
        """Initialize multi-tenant provider.

        Args:
            enterprise_repository: Repository for enterprise persistence
        """
        self.enterprise_repository = enterprise_repository

    async def is_multi_tenant(self) -> bool:
        """Multi-tenant mode."""
        return True
