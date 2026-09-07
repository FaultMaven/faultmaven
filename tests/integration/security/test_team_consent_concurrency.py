"""What the consent surface does under real constraints and real concurrency.

Three of the fm#1365 review's correctness items are only observable against
PostgreSQL, because what they are about IS PostgreSQL: a unique index that a
bare INSERT turns into a 500, and two read-then-write rules that a single-
threaded test can never lose.

* **A2** — a duplicate team name, and a retired team squatting on one. The
  uniqueness rule is a PARTIAL index (``WHERE deleted_at IS NULL``): retiring a
  team must free its name, or the ordinary sole-member-leaves path makes a name
  permanently unusable.
* **A3** — two admins inviting one address at the same instant. The loser of the
  race returns the winner's offer, which is what a repeat invite is documented
  to do; raising would answer 500 to two people doing the same reasonable thing.
* **A4** — two admins leaving at the same instant. The last-admin rule is a read
  of the roster followed by a write to it, so without a lock both pass and the
  team is left with members and no admin — unadministrable for ever, because
  there is no promote endpoint.

The concurrency tests drive **two separate sessions on two connections**, which
is the only arrangement in which the second caller can observe the first's lock.
A single session would serialise them by construction and the tests would pass
against code with no lock at all.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.team_repository import (
    PostgreSQLTeamRepository,
)
from faultmaven.models.interfaces_user import (
    LeaveOutcome,
    Team,
    TeamInvitation,
    TeamInvitationStatus,
    TeamNameTakenError,
)
from tests.integration.security.conftest import DEFAULT_ENTERPRISE_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

ADMIN = "admin"


@pytest.fixture
async def engine():
    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def accounts(session_factory):
    """Three accounts anchored to the default enterprise, and their cleanup.

    Real ``users`` rows, because ``create_team_with_admin`` and ``leave_team``
    both read ``users.enterprise_id`` — the anchor comparison is the rule, and a
    test that skipped it would exercise a path the deployment never takes.
    """
    ids = [f"user_cc_{uuid.uuid4().hex[:8]}" for _ in range(3)]
    async with session_factory() as session:
        for user_id in ids:
            await session.execute(
                text(
                    "INSERT INTO users "
                    "(user_id, username, email, display_name, enterprise_id, "
                    " is_active, created_at, updated_at) "
                    "VALUES (:u, :u, :e, :u, :ent, true, NOW(), NOW())"
                ),
                {
                    "u": user_id,
                    "e": f"{user_id}@acme.example",
                    "ent": DEFAULT_ENTERPRISE_ID,
                },
            )
        await session.commit()
    yield ids
    async with session_factory() as session:
        await session.execute(
            text("DELETE FROM users WHERE user_id = ANY(:ids)"), {"ids": ids}
        )
        await session.commit()


@pytest.fixture
async def cleanup_teams(session_factory):
    """Delete every team this module made, whatever the test did to it."""
    made: list[str] = []
    yield made
    async with session_factory() as session:
        await session.execute(
            text("DELETE FROM team_invitations WHERE team_id = ANY(:ids)"),
            {"ids": made},
        )
        await session.execute(
            text("DELETE FROM team_members WHERE team_id = ANY(:ids)"), {"ids": made}
        )
        await session.execute(
            text("DELETE FROM teams WHERE team_id = ANY(:ids)"), {"ids": made}
        )
        await session.commit()


def _team(name: str) -> Team:
    now = datetime.now(UTC)
    return Team(
        team_id=str(uuid.uuid4()),
        enterprise_id=DEFAULT_ENTERPRISE_ID,
        name=name,
        created_at=now,
        updated_at=now,
    )


async def _create(session_factory, cleanup_teams, name: str, admin: str):
    team = _team(name)
    cleanup_teams.append(team.team_id)
    async with session_factory() as session:
        return await PostgreSQLTeamRepository(session).create_team_with_admin(
            team, admin, ADMIN
        )


# =============================================================================
# A2 — a duplicate name is a refusal, and a retired team frees its name
# =============================================================================


async def test_a2_a_duplicate_live_team_name_is_a_typed_refusal_not_a_500(
    session_factory, accounts, cleanup_teams
):
    """The bare INSERT used to let the unique violation escape as a 500.

    A name collision is the most ordinary thing a user can do on this endpoint,
    and it has an obvious right answer. What it must not be is an unhandled
    ``IntegrityError``, which is what a repository with no violation handling
    produces.
    """
    name = f"consent-dup-{uuid.uuid4().hex[:8]}"
    assert await _create(session_factory, cleanup_teams, name, accounts[0])

    with pytest.raises(TeamNameTakenError):
        await _create(session_factory, cleanup_teams, name, accounts[0])


async def test_a2_a_retired_team_does_not_hold_its_name_hostage(
    session_factory, accounts, cleanup_teams
):
    """The reason the index is PARTIAL, reached by the ordinary path.

    The sole member of a team leaving retires it — that is the designed way a
    team ends (ADR-017 D4). Under a full constraint the name it was using could
    never be used again, by anyone, for ever, and the second attempt answered a
    500 for a team the enterprise can no longer see.
    """
    name = f"consent-reuse-{uuid.uuid4().hex[:8]}"
    first = await _create(session_factory, cleanup_teams, name, accounts[0])
    assert first is not None

    async with session_factory() as session:
        outcome = await PostgreSQLTeamRepository(session).leave_team(
            DEFAULT_ENTERPRISE_ID, first.team_id, accounts[0], ADMIN
        )
    assert outcome is LeaveOutcome.LEFT_AND_RETIRED

    second = await _create(session_factory, cleanup_teams, name, accounts[0])
    assert second is not None and second.team_id != first.team_id


async def test_a2_a_refused_creation_writes_neither_row(
    session_factory, accounts, cleanup_teams
):
    """One transaction, so a creator who cannot join leaves nothing behind.

    Two writes and a compensating delete left a soft-deleted squatter whenever
    the compensation ran — which, before the index became partial, is what made
    the name unusable afterwards.
    """
    team = _team(f"consent-atomic-{uuid.uuid4().hex[:8]}")
    cleanup_teams.append(team.team_id)
    async with session_factory() as session:
        created = await PostgreSQLTeamRepository(session).create_team_with_admin(
            team, "user_that_does_not_exist", ADMIN
        )
    assert created is None

    async with session_factory() as session:
        rows = (
            await session.execute(
                text("SELECT count(*) FROM teams WHERE team_id = :t"),
                {"t": team.team_id},
            )
        ).scalar()
    assert rows == 0, "a refused creation left a team row behind"


# =============================================================================
# A3 — two invites of one address, at once
# =============================================================================


async def test_a3_a_concurrent_duplicate_invite_returns_the_live_offer(
    session_factory, accounts, cleanup_teams
):
    """The loser of the race gets the winner's row, not a 500.

    Written as a deliberate double-insert of the same address, because that is
    exactly the state the race leaves the second writer in: it read no pending
    offer, and by the time it writes there is one.
    """
    team = await _create(
        session_factory,
        cleanup_teams,
        f"consent-race-{uuid.uuid4().hex[:8]}",
        accounts[0],
    )
    assert team is not None
    address = f"newcomer-{uuid.uuid4().hex[:8]}@acme.example"

    def offer() -> TeamInvitation:
        now = datetime.now(UTC)
        return TeamInvitation(
            invitation_id=str(uuid.uuid4()),
            enterprise_id=DEFAULT_ENTERPRISE_ID,
            team_id=team.team_id,
            email=address,
            invited_by=accounts[0],
            status=TeamInvitationStatus.PENDING,
            created_at=now,
            expires_at=now + timedelta(days=14),
        )

    async with session_factory() as session:
        winner = await PostgreSQLTeamRepository(session).create_invitation(offer())
    async with session_factory() as session:
        loser = await PostgreSQLTeamRepository(session).create_invitation(offer())

    assert loser.invitation_id == winner.invitation_id, (
        "the second invite minted a second live offer, or raised — it must "
        "return the offer that already exists, which is what a repeat invite "
        "is documented to do"
    )

    async with session_factory() as session:
        live = (
            await session.execute(
                text(
                    "SELECT count(*) FROM team_invitations "
                    "WHERE team_id = :t AND status = 'pending'"
                ),
                {"t": team.team_id},
            )
        ).scalar()
    assert live == 1


# =============================================================================
# A4 — two admins leaving at the same instant
# =============================================================================


async def test_a4_leaving_takes_a_row_lock_on_the_team(
    session_factory, accounts, cleanup_teams
):
    """The lock itself, proved by holding it against ``leave_team``.

    The obvious version of this test — two leaves under ``asyncio.gather`` —
    **passes with the lock removed**, which was checked rather than assumed:
    the two coroutines happen to serialise on connection acquisition, so the
    window is never actually opened and the test asserts nothing about locking.
    A gate that cannot fail is worse than no gate.

    This version cannot be satisfied without the lock. One session holds
    ``SELECT … FOR UPDATE`` on the team row and does not commit; the other runs
    ``leave_team`` under a 250 ms ``lock_timeout``. If ``leave_team`` takes the
    lock it blocks and the timeout fires; if it does not, it reads straight
    past the held row and completes, and this test goes red.

    What the lock is *for* is the rule below: the last-admin check is a read of
    the roster followed by a write to it, and without serialisation two admins
    each read a roster containing the other and both leave.
    """
    team = await _create(
        session_factory,
        cleanup_teams,
        f"consent-lock-{uuid.uuid4().hex[:8]}",
        accounts[0],
    )
    assert team is not None
    async with session_factory() as session:
        repository = PostgreSQLTeamRepository(session)
        assert await repository.add_member(team.team_id, accounts[1], ADMIN)
        assert await repository.add_member(team.team_id, accounts[2], "member")

    async with session_factory() as blocker:
        await blocker.execute(
            text("SELECT 1 FROM teams WHERE team_id = :t FOR UPDATE"),
            {"t": team.team_id},
        )
        # Deliberately NOT committed: the lock is held for the block below.
        async with session_factory() as contender:
            await contender.execute(text("SET LOCAL lock_timeout = '250ms'"))
            with pytest.raises(DBAPIError) as blocked:
                await PostgreSQLTeamRepository(contender).leave_team(
                    DEFAULT_ENTERPRISE_ID, team.team_id, accounts[1], ADMIN
                )
            assert "lock" in str(blocked.value).lower(), (
                "leave_team failed for some reason other than waiting on the "
                f"team row: {blocked.value}"
            )
        await blocker.rollback()

    async with session_factory() as session:
        still_there = (
            await session.execute(
                text("SELECT count(*) FROM team_members WHERE team_id = :t"),
                {"t": team.team_id},
            )
        ).scalar()
    assert still_there == 3, "the blocked leave removed a membership anyway"


async def test_a4_the_last_admin_of_a_team_with_members_is_refused(
    session_factory, accounts, cleanup_teams
):
    """The rule the lock protects: no member is left in an unadministrable team.

    Two admins and one plain member. The first admin may go — the second still
    administers the team. The second may not, because a member would remain and
    no route can promote them.
    """
    team = await _create(
        session_factory,
        cleanup_teams,
        f"consent-lastadmin-{uuid.uuid4().hex[:8]}",
        accounts[0],
    )
    assert team is not None
    async with session_factory() as session:
        repository = PostgreSQLTeamRepository(session)
        assert await repository.add_member(team.team_id, accounts[1], ADMIN)
        assert await repository.add_member(team.team_id, accounts[2], "member")

    async def leave(user_id: str) -> LeaveOutcome:
        async with session_factory() as session:
            return await PostgreSQLTeamRepository(session).leave_team(
                DEFAULT_ENTERPRISE_ID, team.team_id, user_id, ADMIN
            )

    assert await leave(accounts[0]) is LeaveOutcome.LEFT
    assert await leave(accounts[1]) is LeaveOutcome.LAST_ADMIN

    async with session_factory() as session:
        admins = (
            await session.execute(
                text(
                    "SELECT count(*) FROM team_members "
                    "WHERE team_id = :t AND team_role = 'admin'"
                ),
                {"t": team.team_id},
            )
        ).scalar()
    assert admins == 1, "the team is left unadministrable"


async def test_a4_two_admins_and_nobody_else_may_both_leave(
    session_factory, accounts, cleanup_teams
):
    """The control for the rule above: it protects MEMBERS, not the team.

    Without this, the concurrency test could be satisfied by a rule that simply
    refuses the second admin always — which would make a two-person team
    impossible to wind up, and would be a different bug wearing the same green.
    """
    team = await _create(
        session_factory,
        cleanup_teams,
        f"consent-winddown-{uuid.uuid4().hex[:8]}",
        accounts[0],
    )
    assert team is not None
    async with session_factory() as session:
        assert await PostgreSQLTeamRepository(session).add_member(
            team.team_id, accounts[1], ADMIN
        )

    async def leave(user_id: str) -> LeaveOutcome:
        async with session_factory() as session:
            return await PostgreSQLTeamRepository(session).leave_team(
                DEFAULT_ENTERPRISE_ID, team.team_id, user_id, ADMIN
            )

    assert await leave(accounts[0]) is LeaveOutcome.LEFT
    assert await leave(accounts[1]) is LeaveOutcome.LEFT_AND_RETIRED


async def test_a4_the_sole_member_leaving_revokes_the_teams_pending_offers(
    session_factory, accounts, cleanup_teams
):
    """A8, in the same transaction that retires the team.

    An offer to a team nobody can see can be neither accepted (the team is
    gone) nor declined (declining writes a withdrawal against it), so it is
    ended where the team is.
    """
    team = await _create(
        session_factory,
        cleanup_teams,
        f"consent-orphan-{uuid.uuid4().hex[:8]}",
        accounts[0],
    )
    assert team is not None
    now = datetime.now(UTC)
    async with session_factory() as session:
        await PostgreSQLTeamRepository(session).create_invitation(
            TeamInvitation(
                invitation_id=str(uuid.uuid4()),
                enterprise_id=DEFAULT_ENTERPRISE_ID,
                team_id=team.team_id,
                email=f"orphan-{uuid.uuid4().hex[:8]}@acme.example",
                invited_by=accounts[0],
                status=TeamInvitationStatus.PENDING,
                created_at=now,
                expires_at=now + timedelta(days=14),
            )
        )

    async with session_factory() as session:
        outcome = await PostgreSQLTeamRepository(session).leave_team(
            DEFAULT_ENTERPRISE_ID, team.team_id, accounts[0], ADMIN
        )
    assert outcome is LeaveOutcome.LEFT_AND_RETIRED

    async with session_factory() as session:
        still_pending = (
            await session.execute(
                text(
                    "SELECT count(*) FROM team_invitations "
                    "WHERE team_id = :t AND status = 'pending'"
                ),
                {"t": team.team_id},
            )
        ).scalar()
    assert still_pending == 0


# =============================================================================
# A5 — the request model must not be wider than the column
# =============================================================================


async def test_a5_a_name_at_the_request_limit_fits_the_column(
    session_factory, accounts, cleanup_teams
):
    """``TeamCreateRequest.name`` caps at exactly ``teams.name``'s width.

    A wider request field accepts nothing more — it defers the refusal to
    PostgreSQL, which answers ``StringDataRightTruncation`` and a 500 where a
    422 naming the field belongs. SQLite hides this entirely, which is why the
    check lives here.
    """
    from faultmaven.infrastructure.persistence.models import TeamModel
    from faultmaven.modules.auth.api.teams import TeamCreateRequest

    limit = next(
        constraint.max_length
        for constraint in TeamCreateRequest.model_fields["name"].metadata
        if getattr(constraint, "max_length", None) is not None
    )
    assert limit == TeamModel.__table__.c.name.type.length, (
        "the request field and the column have drifted apart again: a wider "
        "request accepts nothing more, it only moves the refusal from a 422 "
        "naming the field to a 500 from PostgreSQL"
    )
    name = ("n" * (limit - 8)) + uuid.uuid4().hex[:8]
    assert len(name) == limit

    created = await _create(session_factory, cleanup_teams, name, accounts[0])
    assert created is not None and created.name == name
