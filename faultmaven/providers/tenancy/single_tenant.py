"""SingleTenantProvider for standalone (self-hosted) deployments.

Owns the single default **enterprise** and team of a standalone deployment and
seeds them at startup. All accounts belong to the enterprise, which is what makes
standalone a one-tenant deployment (ADR-017 D8). The per-request binding is not
here: ``api/middleware/tenant_scope`` forces the Standalone sentinel and discards
any claim, so a forged claim cannot re-scope a single-tenant deployment.

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
from faultmaven.models.interfaces_user import (
    Enterprise,
    EnterprisePlanTier,
    IEnterpriseRepository,
    ITeamRepository,
    Team,
)
from faultmaven.providers.tenancy.base import TenantProvider


class SingleTenantProvider(TenantProvider):
    """Single-tenant provider for standalone (self-hosted) deployments.

    Behavior:
    - Seeds a single default enterprise and team
    - All accounts belong to that enterprise
    - Simplifies local development and standalone deployments

    Use Cases:
    - Local development (git clone → python main.py)
    - Standalone (self-hosted, single tenant)
    - Testing and CI/CD

    Design Notes:
        The default enterprise is seeded by the migration baseline and
        re-ensured by the startup bootstrapper (see faultmaven/bootstrap/
        startup.py).
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
                migration baseline's own seed is the source of truth).
            team_repository: Repository for team persistence. Optional — when
                absent, ensure_default_team_exists() is a no-op. Used only to
                seed the default team row (schema/relationship completeness);
                team collaboration stays inert in standalone.
        """
        self.enterprise_repository = enterprise_repository
        self.team_repository = team_repository

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

        existing = await self.team_repository.get_team(
            self.DEFAULT_ENTERPRISE_ID, self.DEFAULT_TEAM_ID
        )
        if existing:
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
        return created_team
