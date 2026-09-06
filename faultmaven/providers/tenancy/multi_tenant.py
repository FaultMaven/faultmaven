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
"""

from typing import Optional

from faultmaven.exceptions import AuthorizationError, NotFoundError, ValidationException
from faultmaven.models.interfaces_user import Enterprise, IEnterpriseRepository
from faultmaven.modules.auth.domain.models.user import User
from faultmaven.providers.tenancy.base import TenantProvider


class MultiTenantProvider(TenantProvider):
    """Multi-tenant provider for cloud deployments.

    Behavior:
    - Requires an explicit enterprise id for each request
    - Validates that the account is anchored to that enterprise
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
        self, current_user: User, enterprise_id: Optional[str] = None
    ) -> Enterprise:
        """Get the enterprise, validating that the account is anchored to it.

        Args:
            current_user: Authenticated user
            enterprise_id: Required in multi-tenant mode

        Returns:
            Enterprise: The enterprise the account belongs to

        Raises:
            ValidationException: If enterprise_id not provided
            NotFoundError: If the enterprise doesn't exist
            AuthorizationError: If the account is anchored elsewhere

        Design Notes:
            The anchor check compares ``users.enterprise_id`` — the account's one
            isolation membership (ADR-017 D3) — against the requested tenant.
            An account with no anchor is refused rather than admitted: absence is
            not membership.
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

        anchor = getattr(current_user, "enterprise_id", None)
        if anchor != enterprise_id:
            raise AuthorizationError(
                f"User {current_user.user_id} is not anchored to enterprise "
                f"{enterprise.name}"
            )

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
