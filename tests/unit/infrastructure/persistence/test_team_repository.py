"""Unit tests for PostgreSQLTeamRepository.

Exercises the team repository against a real in-memory SQLite engine (via the
ORM's Base.metadata.create_all), mirroring test_enterprise_repository. The focus
is the KB scope resolver ``list_all_user_team_ids`` (join-through-``teams``) and
the membership/team CRUD substrate.

FK enforcement is left OFF (SQLite default), so rows can be inserted without
seeding parent org/user rows — these tests assert query/join behavior, not FK
integrity.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.team_repository import (
    PostgreSQLTeamRepository,
)
from faultmaven.models.interfaces_user import Team

ORG_A = "org-a"
ORG_B = "org-b"


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
    """PostgreSQLTeamRepository bound to the test session."""
    return PostgreSQLTeamRepository(session)


def make_team(team_id: str, organization_id: str = ORG_A, name: str = "") -> Team:
    now = datetime.now(timezone.utc)
    return Team(
        team_id=team_id,
        organization_id=organization_id,
        # Default the name to the id — the (organization_id, name) UNIQUE
        # constraint rejects duplicate names within one org.
        name=name or f"Team {team_id}",
        description=None,
        created_at=now,
        updated_at=now,
    )


# =============================================================================
# list_all_user_team_ids — the KB scope resolver
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_all_user_team_ids_returns_all_memberships(repo):
    """A user in multiple teams resolves to all their team ids."""
    await repo.create_team(make_team("t1", name="Team One"))
    await repo.create_team(make_team("t2", name="Team Two"))
    await repo.add_member("t1", "user-1")
    await repo.add_member("t2", "user-1")

    ids = await repo.list_all_user_team_ids("user-1")

    assert sorted(ids) == ["t1", "t2"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_all_user_team_ids_excludes_non_member_teams(repo):
    """Only teams the user actually belongs to are returned."""
    await repo.create_team(make_team("t1"))
    await repo.create_team(make_team("t2"))
    await repo.add_member("t1", "user-1")

    ids = await repo.list_all_user_team_ids("user-1")

    assert ids == ["t1"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_all_user_team_ids_excludes_soft_deleted_teams(repo):
    """Soft-deleted teams drop out — the join filters teams.deleted_at."""
    await repo.create_team(make_team("t1"))
    await repo.add_member("t1", "user-1")

    assert await repo.list_all_user_team_ids("user-1") == ["t1"]

    await repo.delete_team("t1")

    assert await repo.list_all_user_team_ids("user-1") == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_all_user_team_ids_empty_when_no_memberships(repo):
    """The standalone-inert case: a user with no memberships resolves to []."""
    await repo.create_team(make_team("t1"))  # exists but nobody joined

    assert await repo.list_all_user_team_ids("user-1") == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_all_user_team_ids_isolated_per_user(repo):
    """User A's resolution never leaks user B's memberships."""
    await repo.create_team(make_team("t1"))
    await repo.create_team(make_team("t2"))
    await repo.add_member("t1", "user-a")
    await repo.add_member("t2", "user-b")

    assert await repo.list_all_user_team_ids("user-a") == ["t1"]
    assert await repo.list_all_user_team_ids("user-b") == ["t2"]


# =============================================================================
# Team + membership CRUD substrate
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_create_and_get_team_roundtrip(repo):
    await repo.create_team(make_team("t1", organization_id=ORG_A, name="Team One"))

    got = await repo.get_team("t1")

    assert got is not None
    assert got.team_id == "t1"
    assert got.organization_id == ORG_A
    assert got.name == "Team One"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_team_returns_none_for_missing_or_deleted(repo):
    assert await repo.get_team("nope") is None

    await repo.create_team(make_team("t1"))
    await repo.delete_team("t1")

    assert await repo.get_team("t1") is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_add_member_is_idempotent_upsert(repo):
    """add_member upserts — re-adding updates role, never duplicates."""
    await repo.create_team(make_team("t1"))
    await repo.add_member("t1", "user-1", team_role="member")
    await repo.add_member("t1", "user-1", team_role="lead")

    members = await repo.list_team_members("t1")

    assert len(members) == 1
    assert members[0].user_id == "user-1"
    assert members[0].team_role == "lead"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_is_team_member_and_remove_member(repo):
    await repo.create_team(make_team("t1"))
    await repo.add_member("t1", "user-1")

    assert await repo.is_team_member("t1", "user-1") is True

    assert await repo.remove_member("t1", "user-1") is True
    assert await repo.is_team_member("t1", "user-1") is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_organization_teams_scopes_to_org(repo):
    await repo.create_team(make_team("t1", organization_id=ORG_A))
    await repo.create_team(make_team("t2", organization_id=ORG_B))

    teams_a = await repo.list_organization_teams(ORG_A)

    assert [t.team_id for t in teams_a] == ["t1"]
