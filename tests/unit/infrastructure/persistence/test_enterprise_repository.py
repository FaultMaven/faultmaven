"""Unit tests for PostgreSQLEnterpriseRepository.

Exercises the enterprise repository against a real in-memory SQLite engine
(via the SQLAlchemy ORM's Base.metadata.create_all), since the repo has no
InMemory variant — it's a small entity used by single-tenant bootstrap +
future cloud-rollout admin flows, not a hot-path domain object.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.enterprise_repository import (
    PostgreSQLEnterpriseRepository,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.models.interfaces_user import Enterprise, EnterprisePlanTier

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="function")
async def engine():
    """In-memory SQLite engine with the full ORM schema."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    """One AsyncSession per test; expire_on_commit=False so we can read attrs."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.fixture
def repo(session):
    """PostgreSQLEnterpriseRepository bound to the test session."""
    return PostgreSQLEnterpriseRepository(session)


def make_enterprise(
    enterprise_id: str = "ent-001",
    name: str = "Acme Corp",
    slug: str = "acme",
    plan_tier: EnterprisePlanTier = EnterprisePlanTier.PRO,
    max_members: int = 100,
    settings: dict | None = None,
) -> Enterprise:
    now = datetime.now(timezone.utc)
    return Enterprise(
        enterprise_id=enterprise_id,
        name=name,
        slug=slug,
        plan_tier=plan_tier,
        max_members=max_members,
        max_cases=None,
        billing_email=None,
        settings=settings or {},
        created_at=now,
        updated_at=now,
    )


# =============================================================================
# create_enterprise
# =============================================================================


class TestCreateEnterprise:
    @pytest.mark.asyncio
    async def test_create_persists_row(self, repo):
        ent = make_enterprise()
        created = await repo.create_enterprise(ent)
        assert created.enterprise_id == ent.enterprise_id
        # Reload via repo to confirm persistence
        loaded = await repo.get_enterprise(ent.enterprise_id)
        assert loaded is not None
        assert loaded.name == "Acme Corp"
        assert loaded.slug == "acme"
        assert loaded.plan_tier == EnterprisePlanTier.PRO
        assert loaded.max_members == 100

    @pytest.mark.asyncio
    async def test_create_serializes_settings_dict(self, repo):
        ent = make_enterprise(settings={"sso_provider": "okta", "feature_x": True})
        await repo.create_enterprise(ent)
        loaded = await repo.get_enterprise(ent.enterprise_id)
        assert loaded.settings == {"sso_provider": "okta", "feature_x": True}

    @pytest.mark.asyncio
    async def test_create_handles_empty_settings(self, repo):
        ent = make_enterprise(settings={})
        await repo.create_enterprise(ent)
        loaded = await repo.get_enterprise(ent.enterprise_id)
        assert loaded.settings == {}


# =============================================================================
# get_enterprise / get_enterprise_by_slug
# =============================================================================


class TestGetEnterprise:
    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing(self, repo):
        result = await repo.get_enterprise("does-not-exist")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_slug_returns_none_for_missing(self, repo):
        result = await repo.get_enterprise_by_slug("nope")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_slug_matches(self, repo):
        ent = make_enterprise(enterprise_id="ent-002", slug="globex")
        await repo.create_enterprise(ent)
        loaded = await repo.get_enterprise_by_slug("globex")
        assert loaded is not None
        assert loaded.enterprise_id == "ent-002"

    @pytest.mark.asyncio
    async def test_get_skips_soft_deleted(self, repo, session):
        """get_enterprise should not return soft-deleted rows.

        Repository has no delete method yet — this test pokes
        deleted_at directly to guard against future regressions.
        """
        from sqlalchemy import update

        from faultmaven.infrastructure.persistence.models import EnterpriseModel

        ent = make_enterprise(enterprise_id="ent-soft-del")
        await repo.create_enterprise(ent)

        await session.execute(
            update(EnterpriseModel)
            .where(EnterpriseModel.enterprise_id == "ent-soft-del")
            .values(deleted_at=datetime.now(timezone.utc))
        )
        await session.commit()

        result = await repo.get_enterprise("ent-soft-del")
        assert result is None
        result_by_slug = await repo.get_enterprise_by_slug(ent.slug)
        assert result_by_slug is None


# =============================================================================
# update_enterprise
# =============================================================================


class TestUpdateEnterprise:
    @pytest.mark.asyncio
    async def test_update_changes_persisted_fields(self, repo):
        ent = make_enterprise()
        await repo.create_enterprise(ent)

        ent.name = "Acme International"
        ent.plan_tier = EnterprisePlanTier.ENTERPRISE
        ent.max_members = 5000
        ent.billing_email = "billing@acme.example"
        ent.settings = {"updated": True}

        ok = await repo.update_enterprise(ent)
        assert ok is True

        loaded = await repo.get_enterprise(ent.enterprise_id)
        assert loaded.name == "Acme International"
        assert loaded.plan_tier == EnterprisePlanTier.ENTERPRISE
        assert loaded.max_members == 5000
        assert loaded.billing_email == "billing@acme.example"
        assert loaded.settings == {"updated": True}

    @pytest.mark.asyncio
    async def test_update_returns_false_for_missing_row(self, repo):
        ent = make_enterprise(enterprise_id="never-created")
        ok = await repo.update_enterprise(ent)
        assert ok is False

    @pytest.mark.asyncio
    async def test_update_bumps_updated_at(self, repo):
        ent = make_enterprise()
        await repo.create_enterprise(ent)
        original = ent.updated_at

        ent.name = "Renamed"
        await repo.update_enterprise(ent)

        loaded = await repo.get_enterprise(ent.enterprise_id)
        # SQLite drops tzinfo on round-trip; normalize before comparing.
        loaded_ts = loaded.updated_at
        if loaded_ts.tzinfo is None:
            loaded_ts = loaded_ts.replace(tzinfo=timezone.utc)
        assert loaded_ts >= original
