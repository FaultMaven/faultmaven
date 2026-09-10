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

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base, TeamModel
from faultmaven.infrastructure.persistence.team_repository import (
    PostgreSQLTeamRepository,
)
from faultmaven.models.interfaces_user import (
    AcceptOutcome,
    LeaveOutcome,
    Team,
    TeamInvitation,
    TeamInvitationStatus,
    TeamNameTakenError,
)

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


async def _retire(repo, team_id: str) -> bool:
    """Soft-delete a team, the only way the port still offers.

    ``delete_team`` is gone: retirement happens inside ``leave_team``'s
    transaction, because a team is retired *because* its last member left and
    the two must not be separable. These tests need the state, not the path, so
    they set it directly.
    """
    from sqlalchemy import update

    result = await repo.db.execute(
        update(TeamModel)
        .where(TeamModel.team_id == team_id, TeamModel.deleted_at.is_(None))
        .values(deleted_at=datetime.now(timezone.utc))
    )
    await repo.db.commit()
    return result.rowcount > 0


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
    await repo.add_member(ENT_A, "t1", "user-1")
    await repo.add_member(ENT_A, "t2", "user-1")

    ids = await repo.list_all_user_team_ids("user-1")

    assert sorted(ids) == ["t1", "t2"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_all_user_team_ids_excludes_non_member_teams(repo):
    """Only teams the user actually belongs to are returned."""
    await repo.create_team(make_team("t1"))
    await repo.create_team(make_team("t2"))
    await repo.add_member(ENT_A, "t1", "user-1")

    ids = await repo.list_all_user_team_ids("user-1")

    assert ids == ["t1"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_all_user_team_ids_excludes_soft_deleted_teams(repo):
    """Soft-deleted teams drop out — the join filters teams.deleted_at."""
    await repo.create_team(make_team("t1"))
    await repo.add_member(ENT_A, "t1", "user-1")

    assert await repo.list_all_user_team_ids("user-1") == ["t1"]

    await _retire(repo, "t1")

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
    await repo.add_member(ENT_A, "t1", "user-a")
    await repo.add_member(ENT_A, "t2", "user-b")

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
    await repo.add_member(ENT_A, "t1", "user-1")
    await repo.add_member(ENT_A, "t2", "user-1")

    teams = await repo.list_user_teams("user-1")

    assert {t.team_id for t in teams} == {"t1", "t2"}
    assert {t.name for t in teams} == {"Alpha", "Beta"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_user_teams_excludes_non_member_and_soft_deleted(repo):
    """Non-member teams are excluded; soft-deleted teams drop out of the join."""
    await repo.create_team(make_team("t1"))
    await repo.create_team(make_team("t2"))  # user is not a member
    await repo.add_member(ENT_A, "t1", "user-1")

    assert [t.team_id for t in await repo.list_user_teams("user-1")] == ["t1"]

    await _retire(repo, "t1")

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

    got = await repo.get_team(ENT_A, "t1")

    assert got is not None
    assert got.team_id == "t1"
    assert got.enterprise_id == ENT_A
    assert got.name == "Team One"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_the_team_reads_are_scoped_by_the_enterprise_they_are_given(repo):
    """A10: the tenant predicate is a parameter, and it is actually applied.

    The signature change alone proves only that a caller cannot *forget* the
    enterprise — it does not prove the value is used. SQLite has no RLS, so if
    the predicate were dropped from the WHERE clause every one of these would
    answer with ENT_A's rows and nothing on this dialect would notice. That is
    exactly the gap the parameter exists to close: RLS covers the deployed path,
    and this covers the two that RLS does not (SQLite, and any owner-role
    connection).

    Every team-addressed read is checked, not a representative one: they are
    five separate WHERE clauses, and a predicate can go missing from any of
    them independently.
    """
    await repo.create_team(make_team("t1", enterprise_id=ENT_A, name="Alpha"))
    await repo.add_member(ENT_A, "t1", "user-1")

    assert await repo.get_team(ENT_B, "t1") is None
    assert await repo.get_team_with_members(ENT_B, "t1") == (None, [])
    assert await repo.get_team_names(ENT_B, ["t1"]) == {}
    assert await repo.list_team_members(ENT_B, "t1") == []

    # The control, on the same rows: every one of those answers for ENT_A, so
    # the Nones above are the predicate and not a broken fixture.
    assert await repo.get_team(ENT_A, "t1") is not None
    assert (await repo.get_team_with_members(ENT_A, "t1"))[0] is not None
    assert await repo.get_team_names(ENT_A, ["t1"]) == {"t1": "Alpha"}
    assert len(await repo.list_team_members(ENT_A, "t1")) == 1
    assert await _retire(repo, "t1") is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_team_returns_none_for_missing_or_deleted(repo):
    assert await repo.get_team(ENT_A, "nope") is None

    await repo.create_team(make_team("t1"))
    await _retire(repo, "t1")

    assert await repo.get_team(ENT_A, "t1") is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_add_member_is_idempotent_upsert(repo):
    """add_member upserts — re-adding updates role, never duplicates."""
    await repo.create_team(make_team("t1"))
    await repo.add_member(ENT_A, "t1", "user-1", team_role="member")
    await repo.add_member(ENT_A, "t1", "user-1", team_role="lead")

    members = await repo.list_team_members(ENT_A, "t1")

    assert len(members) == 1
    assert members[0].user_id == "user-1"
    assert members[0].team_role == "lead"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_membership_is_written_by_add_member_and_removed_by_leave_team(repo):
    """The port has no ``is_team_member`` or ``remove_member`` any more.

    Both were unscoped, bare-id operations kept alive by nothing:
    ``_require_team_admin`` already holds the roster that answers the first, and
    ``leave_team`` performs the second inside the transaction that decides the
    last-admin rule. Three un-transactional ways to do what one transactional
    method exists to do is a standing invitation to use the wrong one.
    """
    await repo.create_team(make_team("t1"))
    await repo.add_member(ENT_A, "t1", "user-1", "admin")

    assert [m.user_id for m in await repo.list_team_members(ENT_A, "t1")] == ["user-1"]

    assert (
        await repo.leave_team(ENT_A, "t1", "user-1", "admin")
        is LeaveOutcome.LEFT_AND_RETIRED
    )
    assert await repo.list_team_members(ENT_A, "t1") == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_enterprise_teams_scopes_to_enterprise(repo):
    await repo.create_team(make_team("t1", enterprise_id=ENT_A))
    await repo.create_team(make_team("t2", enterprise_id=ENT_B))

    teams_a = await repo.list_enterprise_teams(ENT_A)

    assert [t.team_id for t in teams_a] == ["t1"]


# =============================================================================
# D2 — the IntegrityError recoveries, on the dialect that used to defeat them
# =============================================================================
#
# These ran only against PostgreSQL before, and that is precisely why the bug
# survived: the recoveries decided which constraint had been violated by looking
# for the index name in the driver's message. asyncpg supplies it. **SQLite does
# not** — it says ``UNIQUE constraint failed: teams.enterprise_id, teams.name``
# and names no index — so on the single-tenant dialect both recoveries fell
# through to a raw ``IntegrityError``, and `create_team` is on the standalone
# default-team bootstrap path at startup.
#
# The recoveries now re-read to decide, which is dialect-independent. Running
# them here is what keeps that true.


@pytest.mark.asyncio
@pytest.mark.unit
async def test_d2_a_duplicate_team_name_is_typed_on_sqlite_too(repo):
    """The recovery must not depend on a driver naming its index."""
    await repo.create_team(make_team("t1", enterprise_id=ENT_A, name="Payments"))

    with pytest.raises(TeamNameTakenError):
        await repo.create_team(make_team("t2", enterprise_id=ENT_A, name="Payments"))


@pytest.mark.asyncio
@pytest.mark.unit
async def test_d2_the_same_name_in_another_enterprise_is_not_a_conflict(repo):
    """The control: the re-read is scoped, so it cannot over-report.

    Without this, "raise TeamNameTakenError on any IntegrityError" would pass
    the test above — and would report a foreign-key violation as a name
    collision.
    """
    await repo.create_team(make_team("t1", enterprise_id=ENT_A, name="Payments"))

    other = await repo.create_team(
        make_team("t2", enterprise_id=ENT_B, name="Payments")
    )

    assert other.team_id == "t2"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_d2_a_non_unique_integrity_error_is_re_raised(repo):
    """Anything the re-read cannot explain stays what it was.

    Re-inserting an existing team id under a DIFFERENT name violates the primary
    key, not the name index — and the re-read finds no other live team with that
    name, so there is nothing to report but the error itself. Reporting it as
    "the name is taken" would turn a real corruption into an ordinary
    user-caused 409 that somebody would then try to fix by renaming.
    """
    await repo.create_team(make_team("t1", enterprise_id=ENT_A, name="Payments"))

    with pytest.raises(IntegrityError):
        await repo.create_team(make_team("t1", enterprise_id=ENT_A, name="Different"))


@pytest.mark.asyncio
@pytest.mark.unit
async def test_d2_a_duplicate_pending_invitation_returns_the_winner_on_sqlite(repo):
    """A3's recovery, on the dialect where it used to re-raise."""
    await repo.create_team(make_team("t1", enterprise_id=ENT_A))
    now = datetime.now(timezone.utc)

    def offer(invitation_id: str) -> TeamInvitation:
        return TeamInvitation(
            invitation_id=invitation_id,
            enterprise_id=ENT_A,
            team_id="t1",
            email="newcomer@acme.example",
            status=TeamInvitationStatus.PENDING,
            created_at=now,
            expires_at=now + timedelta(days=14),
        )

    winner = await repo.create_invitation(offer("inv-1"))
    loser = await repo.create_invitation(offer("inv-2"))

    assert loser.invitation_id == winner.invitation_id == "inv-1"


# =============================================================================
# D6 / F3 — the lock policy, and the predicates A10 left unit-untested
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_d6_the_lock_is_skipped_only_for_a_positively_identified_sqlite(repo):
    """Fail closed on an unknown dialect, not open.

    The first version asked ``bind is not None and dialect != "sqlite"``, so an
    unresolvable bind skipped ``FOR UPDATE`` — removing the only mechanism
    ``leave_team`` exists to provide, in the situation where least is known.
    Asserted through the policy rather than by running the statement, because
    the failure is a decision, not an outcome: on SQLite the locked and unlocked
    statements execute identically, so no behavioural assertion here could tell
    them apart.
    """
    from unittest.mock import patch

    assert repo._dialect() == "sqlite"
    assert "FOR UPDATE" not in str(repo._with_row_lock(select(TeamModel)))

    with patch.object(type(repo), "_dialect", lambda self: None):
        assert "FOR UPDATE" in str(repo._with_row_lock(select(TeamModel)))
    with patch.object(type(repo), "_dialect", lambda self: "postgresql"):
        assert "FOR UPDATE" in str(repo._with_row_lock(select(TeamModel)))


@pytest.mark.asyncio
@pytest.mark.unit
async def test_f3_the_invitation_reads_and_leave_are_scoped_by_enterprise(repo):
    """The three predicates the A10 test claimed to cover and did not.

    ``find_pending_invitation`` and ``list_team_invitations`` are the two
    invitation reads A10 actually changed, and ``leave_team``'s predicate had no
    unit coverage at all — deleting any of the three left the suite green on
    SQLite, which is the dialect with no RLS to fall back on.
    """
    await repo.create_team(make_team("t1", enterprise_id=ENT_A))
    await repo.add_member(ENT_A, "t1", "user-1", "admin")
    now = datetime.now(timezone.utc)
    await repo.create_invitation(
        TeamInvitation(
            invitation_id="inv-scope",
            enterprise_id=ENT_A,
            team_id="t1",
            email="scoped@acme.example",
            status=TeamInvitationStatus.PENDING,
            created_at=now,
            expires_at=now + timedelta(days=14),
        )
    )

    assert (
        await repo.find_pending_invitation(ENT_B, "t1", "scoped@acme.example") is None
    )
    assert await repo.list_team_invitations(ENT_B, "t1") == []
    assert await repo.get_invitation(ENT_B, "inv-scope") is None
    assert await repo.leave_team(ENT_B, "t1", "user-1", "admin") is LeaveOutcome.ABSENT

    # Controls, on the same rows.
    assert (
        await repo.find_pending_invitation(ENT_A, "t1", "scoped@acme.example")
    ) is not None
    assert len(await repo.list_team_invitations(ENT_A, "t1")) == 1
    assert await repo.get_invitation(ENT_A, "inv-scope") is not None
    assert (
        await repo.leave_team(ENT_A, "t1", "user-1", "admin")
        is LeaveOutcome.LEFT_AND_RETIRED
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_f3_the_invitation_writes_are_scoped_by_enterprise(repo):
    """D8: the writes carry the predicate too, and it is applied.

    A write addressed by bare id does not merely observe another tenant's row,
    it changes it — so these matter more than the reads above, not less.
    """
    await repo.create_team(make_team("t1", enterprise_id=ENT_A))
    await repo.add_member(ENT_A, "t1", "user-1", "admin")
    now = datetime.now(timezone.utc)
    await repo.create_invitation(
        TeamInvitation(
            invitation_id="inv-write",
            enterprise_id=ENT_A,
            team_id="t1",
            email="writes@acme.example",
            status=TeamInvitationStatus.PENDING,
            created_at=now,
            expires_at=now + timedelta(days=14),
        )
    )

    assert (
        await repo.mark_invitation_revoked(ENT_B, "inv-write", "user-1", now) is False
    )
    assert await repo.expire_invitations(ENT_B, ["inv-write"]) == []
    outcome, _ = await repo.accept_invitation(
        ENT_B, "inv-write", "user-1", "member", now
    )
    assert outcome is AcceptOutcome.ABSENT
    assert await repo.add_member(ENT_B, "t1", "user-b") is False

    stored = await repo.get_invitation(ENT_A, "inv-write")
    assert (
        stored.status == TeamInvitationStatus.PENDING
    ), "a write from another enterprise changed the row"
    assert [m.user_id for m in await repo.list_team_members(ENT_A, "t1")] == ["user-1"]
