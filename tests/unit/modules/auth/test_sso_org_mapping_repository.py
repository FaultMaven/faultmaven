"""The ``sso_org_mappings`` table and its lookup adapter (#869, ADR-017 D9).

Exercised against a real in-memory SQLite engine built from the ORM metadata,
so the constraints under test are the ones the schema actually declares: the
``(provider, provider_org_id)`` primary key (an IdP organization resolves to at
most one tenant) and the ``(provider, enterprise_id)`` unique constraint (a
tenant is claimed by at most one IdP organization per provider).

ADR-017 D9 moved the target of the map one tier up: an IdP organization names
the **enterprise** its members land in, never an organization. The two
constraints are unchanged in shape and unchanged in what they are for — the
relation is still 1:1 per provider — but they now guard the isolation boundary
rather than a billing group, which is what makes a broken one a cross-tenant
defect rather than a metering one.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    SSOOrgMappingModel,
)
from faultmaven.modules.auth.infrastructure.repositories.sso_org_mapping_repository import (
    SSOOrgMappingRepository,
)

ENTERPRISE_A = "33333333-3333-3333-3333-333333333333"
ENTERPRISE_B = "55555555-5555-5555-5555-555555555555"


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
        # Two enterprises and no organization at all: under ADR-017 D5 a tenant
        # may own none, and nothing on this path reads one.
        for enterprise_id, slug in ((ENTERPRISE_A, "acme"), (ENTERPRISE_B, "globex")):
            s.add(
                EnterpriseModel(
                    enterprise_id=enterprise_id,
                    name=f"Enterprise {slug}",
                    slug=slug,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
        await s.commit()
        yield s


@pytest.fixture
def repo(session):
    return SSOOrgMappingRepository(session)


async def _add_mapping(session, provider, provider_org_id, enterprise_id):
    session.add(
        SSOOrgMappingModel(
            provider=provider,
            provider_org_id=provider_org_id,
            enterprise_id=enterprise_id,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await session.commit()


@pytest.mark.unit
async def test_lookup_returns_the_mapped_enterprise(repo, session):
    await _add_mapping(session, "workos", "org_01H", ENTERPRISE_A)

    assert await repo.get_enterprise_id("workos", "org_01H") == ENTERPRISE_A


@pytest.mark.unit
async def test_lookup_of_an_unmapped_org_returns_none(repo, session):
    await _add_mapping(session, "workos", "org_01H", ENTERPRISE_A)

    assert await repo.get_enterprise_id("workos", "org_UNKNOWN") is None


@pytest.mark.unit
async def test_lookup_is_scoped_by_provider(repo, session):
    """The same provider org id under a different provider is a different key."""
    await _add_mapping(session, "workos", "org_01H", ENTERPRISE_A)

    assert await repo.get_enterprise_id("okta", "org_01H") is None


@pytest.mark.unit
async def test_the_table_carries_no_organization_column():
    """The column ADR-017 D9 removed, asserted on the mapper rather than
    remembered.

    Its survival would be the compatibility arm the campaign forbids: two
    columns naming a tenant, one of them the retired one, and no way to tell
    from a call site which was read.
    """
    columns = set(SSOOrgMappingModel.__table__.columns.keys())
    assert "enterprise_id" in columns
    assert "organization_id" not in columns


@pytest.mark.unit
async def test_an_idp_org_cannot_map_to_two_enterprises(session):
    """Primary key: one IdP organization resolves to at most one tenant.

    The failure this forbids is the sharpest one on this path: a second row
    would make "which enterprise do this IdP org's members land in?" ambiguous,
    and the answer decides what they can see.
    """
    await _add_mapping(session, "workos", "org_01H", ENTERPRISE_A)

    with pytest.raises(IntegrityError):
        await _add_mapping(session, "workos", "org_01H", ENTERPRISE_B)
    await session.rollback()


@pytest.mark.unit
async def test_an_enterprise_cannot_be_claimed_by_two_idp_orgs(session):
    """Unique constraint: 'which IdP org owns this tenant' has one answer."""
    await _add_mapping(session, "workos", "org_01H", ENTERPRISE_A)

    with pytest.raises(IntegrityError):
        await _add_mapping(session, "workos", "org_01J", ENTERPRISE_A)
    await session.rollback()


@pytest.mark.unit
async def test_the_same_enterprise_may_be_claimed_per_provider(session):
    """The uniqueness is per provider, not global — a second provider is fine.

    The positive control for the two refusals above: without it they would also
    pass if the table refused every second row for any reason.
    """
    await _add_mapping(session, "workos", "org_01H", ENTERPRISE_A)
    await _add_mapping(session, "okta", "0oa123", ENTERPRISE_A)

    repo = SSOOrgMappingRepository(session)
    assert await repo.get_enterprise_id("workos", "org_01H") == ENTERPRISE_A
    assert await repo.get_enterprise_id("okta", "0oa123") == ENTERPRISE_A
