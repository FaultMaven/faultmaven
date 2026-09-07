"""``team_invitations`` ships before its endpoints, so its columns are pinned here.

The table is in the baseline and in the two-enterprise probe's contract — it is
tenant data (who was invited into which team), so the isolation rule is stated
once, now, rather than added later beside the endpoints. Those endpoints (create,
invite, accept, leave) are a separate PR (ADR-017 D4), which leaves the table
with a schema and no production writer.

That is exactly the shape a column set drifts in: nothing constructs a row, so
nothing notices a column that is the wrong type, the wrong nullability, or gone.
This module writes one row and reads it back, through the ORM, against the real
schema — so what the model believes and what the database holds have to agree
before anything is built on top of them.

What it pins, and why each is load-bearing for D4:

* ``email`` NOT NULL and ``invited_user_id`` nullable — a team admin may invite
  an address that does not yet have an account, and the invitation resolves when
  that address signs up **and lands in the same enterprise**;
* ``status`` constrained to the four values the design names, so an invitation
  cannot be parked in a fifth state no code branches on. A pending invitation
  grants nothing, which is the consent rule D4 is built on;
* ``enterprise_id`` NOT NULL and denormalised — the policy reaches the tenant
  without a hop through ``teams``, like every other scoped table.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import TeamInvitationModel
from tests.integration.security.conftest import DEFAULT_ENTERPRISE_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]


@pytest.fixture
async def session_factory():
    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def team(session_factory):
    """A team of the default test enterprise, and its cleanup."""
    team_id = f"team_inv_{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO teams (team_id, enterprise_id, name) VALUES (:t, :e, :n)"
            ),
            {"t": team_id, "e": DEFAULT_ENTERPRISE_ID, "n": f"invite probe {team_id}"},
        )
        await session.commit()
    yield team_id
    async with session_factory() as session:
        await session.execute(
            text("DELETE FROM team_invitations WHERE team_id = :t"), {"t": team_id}
        )
        await session.execute(
            text("DELETE FROM teams WHERE team_id = :t"), {"t": team_id}
        )
        await session.commit()


async def test_an_invitation_to_an_address_with_no_account_round_trips(
    session_factory, team
):
    """The D4 case the nullability exists for: invite first, account later."""
    invitation_id = str(uuid.uuid4())
    expires = datetime.now(UTC) + timedelta(days=7)

    async with session_factory() as session:
        session.add(
            TeamInvitationModel(
                invitation_id=invitation_id,
                enterprise_id=DEFAULT_ENTERPRISE_ID,
                team_id=team,
                email="newcomer@acme.example",
                invited_user_id=None,
                invited_by=None,
                status="pending",
                expires_at=expires,
            )
        )
        await session.commit()

    async with session_factory() as session:
        stored = await session.get(TeamInvitationModel, invitation_id)
        assert stored is not None
        assert stored.enterprise_id == DEFAULT_ENTERPRISE_ID
        assert stored.team_id == team
        assert stored.email == "newcomer@acme.example"
        assert stored.invited_user_id is None, (
            "the address need not have an account yet — that is what makes "
            "'invite an address, it resolves at sign-up' possible (D4)"
        )
        assert stored.status == "pending"
        assert stored.accepted_at is None
        assert stored.created_at is not None
        assert stored.expires_at is not None


async def test_the_status_vocabulary_is_the_four_the_design_names(
    session_factory, team
):
    """A fifth status is a state no code branches on, so the column refuses it."""
    async with session_factory() as session:
        session.add(
            TeamInvitationModel(
                invitation_id=str(uuid.uuid4()),
                enterprise_id=DEFAULT_ENTERPRISE_ID,
                team_id=team,
                email="someone@acme.example",
                status="maybe",
            )
        )
        with pytest.raises((IntegrityError, DBAPIError), match="status_check"):
            await session.commit()

    # And all four the design DOES name are accepted, so a constraint that
    # refused everything would not pass for correctness.
    for status in ("pending", "accepted", "revoked", "expired"):
        async with session_factory() as session:
            session.add(
                TeamInvitationModel(
                    invitation_id=str(uuid.uuid4()),
                    enterprise_id=DEFAULT_ENTERPRISE_ID,
                    team_id=team,
                    email=f"{status}@acme.example",
                    status=status,
                )
            )
            await session.commit()


async def test_an_invitation_names_a_tenant_and_an_address(session_factory, team):
    """Both NOT NULL, and for different reasons.

    Without the enterprise the row is one no policy can place. Without the email
    there is nobody the invitation is for — ``invited_user_id`` cannot stand in,
    because the whole point is that it may be absent.
    """
    async with session_factory() as session:
        session.add(
            TeamInvitationModel(
                invitation_id=str(uuid.uuid4()),
                enterprise_id=DEFAULT_ENTERPRISE_ID,
                team_id=team,
                email="   ",
                status="pending",
            )
        )
        with pytest.raises((IntegrityError, DBAPIError), match="email_not_empty"):
            await session.commit()

    async with session_factory() as session:
        session.add(
            TeamInvitationModel(
                invitation_id=str(uuid.uuid4()),
                enterprise_id=None,
                team_id=team,
                email="someone@acme.example",
                status="pending",
            )
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.commit()
