"""SingleTenantProvider for standalone (self-hosted) deployments.

Returns a single default **enterprise** for every request. All accounts belong to
it, which is what makes standalone a one-tenant deployment (ADR-017 D8).

There is deliberately no default *organization*: the organization is a billing
target, and nobody is billed for a self-hosted deployment. ``organization_id`` on
its rows stays NULL.
"""

from datetime import datetime, timezone
from typing import Optional

from faultmaven.config.constants import (
    STANDALONE_ENTERPRISE_ID,
    STANDALONE_ENTERPRISE_NAME,
    STANDALONE_ENTERPRISE_SLUG,
    STANDALONE_TEAM_ID,
    STANDALONE_TEAM_NAME,
)
from faultmaven.exceptions import NotFoundError
from faultmaven.models.interfaces_user import (
    Enterprise,
    EnterprisePlanTier,
    IEnterpriseRepository,
    ITeamRepository,
    Team,
)
from faultmaven.modules.auth.domain.models.user import User
from faultmaven.providers.tenancy.base import TenantProvider

# Re-exported for callers that import the module-level symbol.
DEFAULT_ENTERPRISE_ID = STANDALONE_ENTERPRISE_ID


class SingleTenantProvider(TenantProvider):
    """Single-tenant provider for standalone (self-hosted) deployments.

    Behavior:
    - Returns a single default enterprise for all requests
    - All accounts belong to that enterprise
    - Simplifies local development and standalone deployments

    Use Cases:
    - Local development (git clone → python main.py)
    - Standalone (self-hosted, single tenant)
    - Testing and CI/CD

    Design Notes:
        The default enterprise is seeded by the migration baseline and
        re-ensured by the startup bootstrapper (see faultmaven/bootstrap/
        startup.py), then cached for performance.
    """

    DEFAULT_ENTERPRISE_ID = STANDALONE_ENTERPRISE_ID
    DEFAULT_ENTERPRISE_SLUG = STANDALONE_ENTERPRISE_SLUG
    DEFAULT_ENTERPRISE_NAME = STANDALONE_ENTERPRISE_NAME

    DEFAULT_TEAM_ID = STANDALONE_TEAM_ID
    DEFAULT_TEAM_NAME = STANDALONE_TEAM_NAME

    def __init__(
        self,
        enterprise_repository: Optional[IEnterpriseRepository] = None,
        team_repository: Optional[ITeamRepository] = None,
    ):
        """Initialize single-tenant provider.

        Args:
            enterprise_repository: Repository for enterprise persistence. When
                absent, ensure_default_enterprise_exists() is a no-op (the
                migration baseline's own seed is the source of truth) and
                get_default_enterprise() raises NotFoundError.
            team_repository: Repository for team persistence. Optional — when
                absent, ensure_default_team_exists() is a no-op. Used only to
                seed the default team row (schema/relationship completeness);
                team collaboration stays inert in standalone.
        """
        self.enterprise_repository = enterprise_repository
        self.team_repository = team_repository
        self._default_enterprise: Optional[Enterprise] = None
        self._default_team: Optional[Team] = None

    async def get_current_enterprise(
        self, current_user: User, enterprise_id: Optional[str] = None
    ) -> Enterprise:
        """Always returns the default enterprise (ignores ``enterprise_id``).

        Ignoring the argument is the standalone re-leak guard: a forged claim
        cannot re-scope a single-tenant deployment.

        Args:
            current_user: Authenticated user (not used in single-tenant)
            enterprise_id: Ignored in single-tenant mode

        Returns:
            Enterprise: The default enterprise

        Raises:
            NotFoundError: If the default enterprise doesn't exist
        """
        return await self.get_default_enterprise()

    async def get_default_enterprise(self) -> Enterprise:
        """Get the default enterprise, from cache or from the database.

        Returns:
            Enterprise: The default enterprise

        Raises:
            NotFoundError: If the default enterprise is not found (indicates the
                migration baseline's seed never ran)
        """
        if self._default_enterprise is None:
            enterprise = None
            if self.enterprise_repository is not None:
                enterprise = await self.enterprise_repository.get_enterprise(
                    self.DEFAULT_ENTERPRISE_ID
                )
            if enterprise is None:
                raise NotFoundError(
                    resource_type="Enterprise", resource_id=self.DEFAULT_ENTERPRISE_ID
                )
            self._default_enterprise = enterprise
        return self._default_enterprise

    async def is_multi_tenant(self) -> bool:
        """Single-tenant mode."""
        return False

    async def ensure_default_enterprise_exists(self) -> Optional[Enterprise]:
        """Create the default enterprise if it doesn't exist.

        Called by the startup bootstrapper during application initialization.
        The migration baseline seeds this row idempotently, so this is a
        belt-and-braces guard for a database that somehow skipped it.

        Returns:
            Enterprise: The default enterprise, or None if no
            enterprise_repository is wired.

        Design Notes:
            - Uses a fixed UUID for predictability and testing
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

    async def ensure_default_team_exists(self) -> Optional[Team]:
        """Create the default team if it doesn't exist.

        Called by the startup bootstrapper after the default enterprise (the
        team's FK enterprise_id → enterprises is NOT NULL). Seeds a single
        default team so the sharing substrate has a scope inside the standalone
        enterprise.

        This seeds the team ROW only — no memberships. In standalone there is
        no membership-population path (team management is the Cloud module), so
        team-scoped sharing stays inert regardless. Membership resolution
        (build_kb_scope_filter's team arm) therefore returns an empty set.

        Returns:
            Team: the default team, or None if no team_repository is wired.

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
            enterprise_id=self.DEFAULT_ENTERPRISE_ID,
            name=self.DEFAULT_TEAM_NAME,
            description="Default team for standalone deployment",
            created_at=now,
            updated_at=now,
        )
        created_team = await self.team_repository.create_team(default_team)
        self._default_team = created_team
        return created_team
