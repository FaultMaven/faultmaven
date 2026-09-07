"""MultiTenantProvider for Cloud deployments.

Requires an explicit enterprise id for each request and validates that the
account is anchored to it. Enforces multi-tenant isolation with strict
authorization checks.

In-core and config-selected by ``TENANT_PROVIDER=multi`` (ADR-010). Multi-tenancy
requires the cloud stack (PostgreSQL + RLS, OAuth/RS256, Redis); the deployment
coherence gate enforces that ``multi`` is only used with ``DEPLOYMENT_MODE=cloud``.

**Membership is ``users.enterprise_id``** (ADR-017, "what the inventory
settled"): the isolation membership needs no roster table, and asking one would
be asking a billing question. ``organization_members`` is the billing roster and
is not consulted here.

It is established at TOKEN MINT and re-established on every rotation, not on
every request — see :meth:`MultiTenantProvider.get_current_enterprise` for why
the per-request comparison this class used to make could not fail, and what does
hold the boundary instead.
"""

from typing import Optional

from faultmaven.exceptions import NotFoundError, ValidationException
from faultmaven.models.interfaces_user import Enterprise, IEnterpriseRepository
from faultmaven.providers.tenancy.base import TenantProvider, TenantUser


class MultiTenantProvider(TenantProvider):
    """Multi-tenant provider for cloud deployments.

    Behavior:
    - Requires an explicit enterprise id for each request
    - Resolves it to a live ``enterprises`` row, refusing an absent one
    - Enforces multi-tenant isolation

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

    async def get_current_enterprise(
        self, current_user: TenantUser, enterprise_id: Optional[str] = None
    ) -> Enterprise:
        """Resolve the bound enterprise to its row, refusing an absent one.

        Args:
            current_user: Authenticated user
            enterprise_id: Required in multi-tenant mode

        Returns:
            Enterprise: The enterprise the account belongs to

        Raises:
            ValidationException: If enterprise_id not provided
            NotFoundError: If the enterprise doesn't exist

        Design Notes:
            **Per-request DB membership is NOT re-checked here, by design.** This
            used to compare ``current_user.enterprise_id`` against the requested
            tenant and raise ``AuthorizationError`` on a mismatch. Both sides come
            from the same place — the request binding — so the comparison was
            tautological and the branch unreachable: the ``enterprise_id``
            argument is the bound enterprise, and ``AuthenticatedUser`` fills its
            own from that same binding (see
            ``AuthenticatedUser.from_jwt_claims``). A guard that cannot fail is
            worse than none, because it reads like one that can.

            What actually establishes membership is upstream and is a fact about
            a row, not about this call: the claim is **minted from**
            ``users.enterprise_id`` at token time, the request front door refuses
            a token without it, and refresh rotation re-reads the row — so a
            re-anchored or removed account loses its claim within one rotation
            (under thirty minutes). Below that, PostgreSQL RLS scopes every read
            to the bound enterprise regardless of what this object believes.

            A route that genuinely needs a *fresh* membership answer — one that
            cannot wait out a token lifetime — has to read ``users.enterprise_id``
            itself and say why. None does today.
        """
        if not enterprise_id:
            raise ValidationException(
                "enterprise_id is required in multi-tenant mode. "
                "Provide via the verified JWT claim.",
                details={
                    "tenant_provider": "multi",
                    "user_id": current_user.user_id,
                    "hint": "Add enterprise_id to request context",
                },
            )

        enterprise = await self.enterprise_repository.get_enterprise(enterprise_id)
        if not enterprise:
            raise NotFoundError(resource_type="Enterprise", resource_id=enterprise_id)

        return enterprise

    async def get_default_enterprise(self) -> Enterprise:
        """Not supported in multi-tenant mode.

        Raises:
            NotImplementedError: Multi-tenant mode requires an explicit enterprise

        Design Notes:
            This method exists for interface compatibility but should never
            be called in multi-tenant deployments. Services should always
            provide an explicit enterprise id.
        """
        raise NotImplementedError(
            "Multi-tenant mode does not have a default enterprise. "
            "Provide enterprise_id explicitly via request context."
        )

    async def is_multi_tenant(self) -> bool:
        """Multi-tenant mode."""
        return True
