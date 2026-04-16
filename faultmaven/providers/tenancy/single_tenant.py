"""SingleTenantProvider for Local/Community Deployments.

Returns a single default organization for all requests. All users share
the same organization, simplifying local development and community edition.

Design Reference: docs/working/TASK-023-TENANT-PROVIDER.md
"""

from datetime import datetime, timezone
from typing import Optional

from faultmaven.exceptions import NotFoundError
from faultmaven.models.interfaces_user import (
    IOrganizationRepository,
    Organization,
    OrgPlanTier,
)
from faultmaven.modules.auth.domain.models.user import User
from faultmaven.providers.tenancy.base import TenantProvider


class SingleTenantProvider(TenantProvider):
    """Single-tenant provider for local/community deployments.

    Behavior:
    - Returns a single default organization for all requests
    - All users belong to the same organization
    - Simplifies local development and community edition

    Use Cases:
    - Local development (git clone → python main.py)
    - Community edition (self-hosted, single team)
    - Testing and CI/CD

    Design Notes:
        The default organization is created by the startup bootstrapper
        (see faultmaven/bootstrap/startup.py) and cached for performance.
    """

    DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"
    DEFAULT_ORG_SLUG = "default"
    DEFAULT_ORG_NAME = "Default Organization"

    def __init__(self, organization_repository: IOrganizationRepository):
        """Initialize single-tenant provider.

        Args:
            organization_repository: Repository for organization persistence
        """
        self.organization_repository = organization_repository
        self._default_org: Optional[Organization] = None

    async def get_current_organization(
        self, current_user: User, organization_id: Optional[str] = None
    ) -> Organization:
        """Always returns the default organization (ignores organization_id).

        In single-tenant mode, all users share the same organization.

        Args:
            current_user: Authenticated user (not used in single-tenant)
            organization_id: Ignored in single-tenant mode

        Returns:
            Organization: The default organization

        Raises:
            NotFoundError: If default organization doesn't exist
                (should be created by startup bootstrapper)
        """
        return await self.get_default_organization()

    async def get_default_organization(self) -> Organization:
        """Get or create the default organization.

        Returns cached organization if available, otherwise loads from DB.

        Returns:
            Organization: The default organization

        Raises:
            NotFoundError: If default organization not found
                (indicates startup bootstrapper hasn't run)
        """
        if self._default_org is None:
            self._default_org = await self.organization_repository.get_organization(
                self.DEFAULT_ORG_ID
            )
            if self._default_org is None:
                raise NotFoundError(
                    resource_type="Organization", resource_id=self.DEFAULT_ORG_ID
                )
        return self._default_org

    async def is_multi_tenant(self) -> bool:
        """Single-tenant mode."""
        return False

    async def ensure_default_organization_exists(self) -> Organization:
        """Create default organization if it doesn't exist.

        Called by startup bootstrapper during application initialization.

        Returns:
            Organization: The default organization (existing or newly created)

        Design Notes:
            - Uses fixed UUID for predictability and testing
            - Grants PRO tier features for local mode (no billing needed)
            - Idempotent: safe to call multiple times
        """
        existing = await self.organization_repository.get_organization(
            self.DEFAULT_ORG_ID
        )
        if existing:
            # Update cache
            self._default_org = existing
            return existing

        # Create default organization with generous limits for local use
        now = datetime.now(timezone.utc)
        default_org = Organization(
            organization_id=self.DEFAULT_ORG_ID,
            slug=self.DEFAULT_ORG_SLUG,
            name=self.DEFAULT_ORG_NAME,
            description="Default organization for local/community deployment",
            plan_tier=OrgPlanTier.PRO,  # Local mode gets pro features
            max_members=100,  # Generous limit for local teams
            max_cases=None,  # Unlimited cases in local mode
            settings={},
            created_at=now,
            updated_at=now,
        )

        created_org = await self.organization_repository.create_organization(
            default_org
        )

        # Cache the created organization
        self._default_org = created_org

        return created_org
