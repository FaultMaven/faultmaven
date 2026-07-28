"""The ``sso_org_mappings`` table and its lookup adapter (#869).

Exercised against a real in-memory SQLite engine built from the ORM metadata,
so the constraints under test are the ones the schema actually declares: the
``(provider, provider_org_id)`` primary key (an IdP organization resolves to at
most one tenant) and the ``(provider, organization_id)`` unique constraint (a
tenant is claimed by at most one IdP organization per provider).
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    OrganizationModel,
    SSOOrgMappingModel,
)
from faultmaven.modules.auth.infrastructure.repositories.sso_org_mapping_repository import (
    SSOOrgMappingRepository,
)

ENTERPRISE_ID = "33333333-3333-3333-3333-333333333333"
ORG_A = "22222222-2222-2222-2222-222222222222"
ORG_B = "55555555-5555-5555-5555-555555555555"


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture(scope="function")
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add(
            EnterpriseModel(
                enterprise_id=ENTERPRISE_ID,
                name="Acme",
                slug="acme",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        for org_id, slug in ((ORG_A, "acme-a"), (ORG_B, "acme-b")):
            s.add(
                OrganizationModel(
                    organization_id=org_id,
                    enterprise_id=ENTERPRISE_ID,
                    name=f"Acme {slug}",
                    slug=slug,
                    is_active=True,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
        await s.commit()
        yield s


@pytest.fixture
def repo(session):
    return SSOOrgMappingRepository(session)


async def _add_mapping(session, provider, provider_org_id, organization_id):
    session.add(
        SSOOrgMappingModel(
            provider=provider,
            provider_org_id=provider_org_id,
            organization_id=organization_id,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await session.commit()


@pytest.mark.unit
async def test_lookup_returns_the_mapped_organization(repo, session):
    await _add_mapping(session, "workos", "org_01H", ORG_A)

    assert await repo.get_organization_id("workos", "org_01H") == ORG_A


@pytest.mark.unit
async def test_lookup_of_an_unmapped_org_returns_none(repo, session):
    await _add_mapping(session, "workos", "org_01H", ORG_A)

    assert await repo.get_organization_id("workos", "org_UNKNOWN") is None


@pytest.mark.unit
async def test_lookup_is_scoped_by_provider(repo, session):
    """The same provider org id under a different provider is a different key."""
    await _add_mapping(session, "workos", "org_01H", ORG_A)

    assert await repo.get_organization_id("okta", "org_01H") is None


@pytest.mark.unit
async def test_an_idp_org_cannot_map_to_two_organizations(session):
    """Primary key: one IdP organization resolves to at most one tenant."""
    await _add_mapping(session, "workos", "org_01H", ORG_A)

    with pytest.raises(IntegrityError):
        await _add_mapping(session, "workos", "org_01H", ORG_B)
    await session.rollback()


@pytest.mark.unit
async def test_an_organization_cannot_be_claimed_by_two_idp_orgs(session):
    """Unique constraint: 'which IdP org owns this tenant' has one answer."""
    await _add_mapping(session, "workos", "org_01H", ORG_A)

    with pytest.raises(IntegrityError):
        await _add_mapping(session, "workos", "org_01J", ORG_A)
    await session.rollback()


@pytest.mark.unit
async def test_the_same_organization_may_be_claimed_per_provider(session):
    """The uniqueness is per provider, not global — a second provider is fine."""
    await _add_mapping(session, "workos", "org_01H", ORG_A)
    await _add_mapping(session, "okta", "0oa123", ORG_A)

    repo = SSOOrgMappingRepository(session)
    assert await repo.get_organization_id("workos", "org_01H") == ORG_A
    assert await repo.get_organization_id("okta", "0oa123") == ORG_A
