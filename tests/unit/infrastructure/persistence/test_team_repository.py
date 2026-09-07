"""Unit tests for PostgreSQLTeamRepository.

Exercises the team repository against a real in-memory SQLite engine (via the
ORM's Base.metadata.create_all), mirroring test_enterprise_repository. The focus
is the KB scope resolver ``list_all_user_team_ids`` (join-through-``teams``) and
the membership/team CRUD substrate.

FK enforcement is left OFF (SQLite default), so rows can be inserted without
seeding parent enterprise rows — these tests assert query/join behavior, not FK
integrity. The ``users`` rows ARE seeded, though, and not for the FK: since
ADR-017 ``add_member`` compares the account's anchor against the team's and
fails closed when either is unresolvable, so an unseeded user is refused
membership rather than silently joined.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.team_repository import (
    PostgreSQLTeamRepository,
)
from faultmaven.models.interfaces_user import Team

ENT_A = "ent-a"
ENT_B = "ent-b"


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
async def repo(session):
    """PostgreSQLTeamRepository bound to the test session.

    Seeds the accounts these tests join to teams, anchored to ``ENT_A``.
    ``add_member`` refuses a user whose anchor it cannot read (ADR-017), so
    without this every membership assertion below would read as an empty
    result and prove nothing about the join it is aimed at.
    """
    for user_id in ("user-1", "user-a", "user-b"):
        await session.execute(
            text(
                "INSERT INTO users (user_id, username, email, display_name, "
                "enterprise_id, is_active, created_at, updated_at) VALUES "
                "(:u, :u, :e, :u, :ent, 1, :now, :now)"
            ),
            {
                "u": user_id,
                "e": f"{user_id}@example.test",
                "ent": ENT_A,
                "now": datetime.now(timezone.utc),
            },
        )
    await session.commit()
    return PostgreSQLTeamRepository(session)


def make_team(team_id: str, enterprise_id: str = ENT_A, name: str = "") -> Team:
    now = datetime.now(timezone.utc)
    return Team(
        team_id=team_id,
        enterprise_id=enterprise_id,
        # Default the name to the id — the (enterprise_id, name) UNIQUE
        # constraint rejects duplicate names within one enterprise.
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
# list_user_teams — object-returning sibling (GET /teams: names for the picker)
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_user_teams_returns_full_objects_with_names(repo):
    """Returns the same membership set as the id resolver, but full Team objects."""
    await repo.create_team(make_team("t1", name="Alpha"))
    await repo.create_team(make_team("t2", name="Beta"))
    await repo.add_member("t1", "user-1")
    await repo.add_member("t2", "user-1")

    teams = await repo.list_user_teams("user-1")

    assert {t.team_id for t in teams} == {"t1", "t2"}
    assert {t.name for t in teams} == {"Alpha", "Beta"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_user_teams_excludes_non_member_and_soft_deleted(repo):
    """Non-member teams are excluded; soft-deleted teams drop out of the join."""
    await repo.create_team(make_team("t1"))
    await repo.create_team(make_team("t2"))  # user is not a member
    await repo.add_member("t1", "user-1")

    assert [t.team_id for t in await repo.list_user_teams("user-1")] == ["t1"]

    await repo.delete_team("t1")

    assert await repo.list_user_teams("user-1") == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_user_teams_empty_when_no_memberships(repo):
    await repo.create_team(make_team("t1"))  # exists but nobody joined

    assert await repo.list_user_teams("user-1") == []


# =============================================================================
# Team + membership CRUD substrate
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_create_and_get_team_roundtrip(repo):
    await repo.create_team(make_team("t1", enterprise_id=ENT_A, name="Team One"))

    got = await repo.get_team("t1")

    assert got is not None
    assert got.team_id == "t1"
    assert got.enterprise_id == ENT_A
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
async def test_list_enterprise_teams_scopes_to_enterprise(repo):
    await repo.create_team(make_team("t1", enterprise_id=ENT_A))
    await repo.create_team(make_team("t2", enterprise_id=ENT_B))

    teams_a = await repo.list_enterprise_teams(ENT_A)

    assert [t.team_id for t in teams_a] == ["t1"]
