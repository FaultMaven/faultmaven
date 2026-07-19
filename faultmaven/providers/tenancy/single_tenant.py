"""SingleTenantProvider for standalone (self-hosted) deployments.

Returns a single default enterprise + organization for all requests.
All users belong to the same enterprise (and the same organization
within it), simplifying local development and standalone deployments.
"""

from datetime import datetime, timezone
from typing import Optional

from faultmaven.config.constants import (
    STANDALONE_ENTERPRISE_ID,
    STANDALONE_ENTERPRISE_NAME,
    STANDALONE_ENTERPRISE_SLUG,
    STANDALONE_ORG_ID,
    STANDALONE_ORG_NAME,
    STANDALONE_ORG_SLUG,
    STANDALONE_TEAM_ID,
    STANDALONE_TEAM_NAME,
)
from faultmaven.exceptions import NotFoundError
from faultmaven.models.interfaces_user import (
    Enterprise,
    EnterprisePlanTier,
    IEnterpriseRepository,
    IOrganizationRepository,
    ITeamRepository,
    Organization,
    OrgPlanTier,
    Team,
)
from faultmaven.modules.auth.domain.models.user import User
from faultmaven.providers.tenancy.base import TenantProvider

# Re-exported for callers that import the module-level symbol.
DEFAULT_ENTERPRISE_ID = STANDALONE_ENTERPRISE_ID


class SingleTenantProvider(TenantProvider):
    """Single-tenant provider for standalone (self-hosted) deployments.

    Behavior:
    - Returns a single default organization for all requests
    - All users belong to the same organization
    - Simplifies local development and standalone deployments

    Use Cases:
    - Local development (git clone → python main.py)
    - Standalone (self-hosted, single tenant)
    - Testing and CI/CD

    Design Notes:
        The default organization is created by the startup bootstrapper
        (see faultmaven/bootstrap/startup.py) and cached for performance.
    """

    DEFAULT_ORG_ID = STANDALONE_ORG_ID
    DEFAULT_ORG_SLUG = STANDALONE_ORG_SLUG
    DEFAULT_ORG_NAME = STANDALONE_ORG_NAME

    DEFAULT_ENTERPRISE_ID = STANDALONE_ENTERPRISE_ID
    DEFAULT_ENTERPRISE_SLUG = STANDALONE_ENTERPRISE_SLUG
    DEFAULT_ENTERPRISE_NAME = STANDALONE_ENTERPRISE_NAME

    DEFAULT_TEAM_ID = STANDALONE_TEAM_ID
    DEFAULT_TEAM_NAME = STANDALONE_TEAM_NAME

    def __init__(
        self,
        organization_repository: IOrganizationRepository,
        enterprise_repository: Optional[IEnterpriseRepository] = None,
        team_repository: Optional[ITeamRepository] = None,
    ):
        """Initialize single-tenant provider.

        Args:
            organization_repository: Repository for organization persistence
            enterprise_repository: Repository for enterprise persistence.
                Optional during the rollout window — when absent,
                ensure_default_enterprise_exists() is a no-op (the
                migration's own backfill is the source of truth).
            team_repository: Repository for team persistence. Optional — when
                absent, ensure_default_team_exists() is a no-op. Used only to
                seed the default team row (schema/relationship completeness);
                team collaboration stays inert in standalone.
        """
        self.organization_repository = organization_repository
        self.enterprise_repository = enterprise_repository
        self.team_repository = team_repository
        self._default_enterprise: Optional[Enterprise] = None
        self._default_org: Optional[Organization] = None
        self._default_team: Optional[Team] = None

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

    async def ensure_default_enterprise_exists(self) -> Optional[Enterprise]:
        """Create default enterprise if it doesn't exist.

        Called by startup bootstrapper during application initialization,
        before ensure_default_organization_exists(). The migration's
        own backfill seeds this row idempotently, so this method is a
        belt-and-braces guard for fresh DBs that somehow skip the
        migration.

        Returns:
            Enterprise: The default enterprise, or None if no
            enterprise_repository is wired (rollout fallback).

        Design Notes:
            - Uses fixed UUID for predictability and testing
            - Grants PRO tier features for local mode (no billing needed)
            - Idempotent: safe to call multiple times
        """
        if self.enterprise_repository is None:
            return None

        existing = await self.enterprise_repository.get_enterprise(
            self.DEFAULT_ENTERPRISE_ID
        )
        if existing:
            self._default_enterprise = existing
            return existing

        now = datetime.now(timezone.utc)
        default_enterprise = Enterprise(
            enterprise_id=self.DEFAULT_ENTERPRISE_ID,
            slug=self.DEFAULT_ENTERPRISE_SLUG,
            name=self.DEFAULT_ENTERPRISE_NAME,
            plan_tier=EnterprisePlanTier.PRO,
            max_members=100,
            max_cases=None,
            settings={},
            created_at=now,
            updated_at=now,
        )
        created = await self.enterprise_repository.create_enterprise(default_enterprise)
        self._default_enterprise = created
        return created

    async def ensure_default_organization_exists(self) -> Organization:
        """Create default organization if it doesn't exist.

        Called by startup bootstrapper during application initialization.

        Returns:
            Organization: The default organization (existing or newly created)

        Design Notes:
            - Uses fixed UUID for predictability and testing
            - Grants PRO tier features for local mode (no billing needed)
            - Idempotent: safe to call multiple times
            - Always anchors the default org to DEFAULT_ENTERPRISE_ID
              so the FK relationship is satisfied once the column is
              tightened to NOT NULL.
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
            enterprise_id=self.DEFAULT_ENTERPRISE_ID,
            slug=self.DEFAULT_ORG_SLUG,
            name=self.DEFAULT_ORG_NAME,
            description="Default organization for standalone deployment",
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

    async def ensure_default_team_exists(self) -> Optional[Team]:
        """Create the default team if it doesn't exist.

        Called by the startup bootstrapper after the default organization
        (the team's FK organization_id → organizations is NOT NULL). Seeds a
        single default team so the Enterprise = Organization = Team collapse
        chain (ADR-013 D1) is materialized for the standalone tenant.

        This seeds the team ROW only — no memberships. In standalone there is
        no membership-population path (team management is the Cloud module), so
        team-scoped sharing stays inert regardless. Membership resolution
        (build_kb_scope_filter's team arm) therefore returns an empty set.

        Returns:
            Team: the default team, or None if no team_repository is wired
            (rollout fallback — the row is not required for standalone to run).

        Design Notes:
            - Uses a fixed UUID for predictability and testing.
            - Idempotent: safe to call multiple times.
        """
        if self.team_repository is None:
            return None

        existing = await self.team_repository.get_team(self.DEFAULT_TEAM_ID)
        if existing:
            self._default_team = existing
            return existing

        now = datetime.now(timezone.utc)
        default_team = Team(
            team_id=self.DEFAULT_TEAM_ID,
            organization_id=self.DEFAULT_ORG_ID,
            name=self.DEFAULT_TEAM_NAME,
            description="Default team for standalone deployment",
            created_at=now,
            updated_at=now,
        )
        created_team = await self.team_repository.create_team(default_team)
        self._default_team = created_team
        return created_team
