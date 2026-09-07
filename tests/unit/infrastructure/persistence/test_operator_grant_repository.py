"""Break-glass grant repository (``operator_access_grants``, ADR-012 D9 #815).

The properties that matter here are the ones the API gate delegates to the
database for:

- ``find_live_grant`` returns only grants that actually authorise a read, and
  its SQL predicate agrees with the domain's ``is_live``. A divergence would
  mean the gate consults one definition and the query another.
- Overlapping grants resolve to the one whose window still covers the access.
- Revocation is monotonic: it cannot move the record of when access ended.

Exercised over in-memory SQLite, the same engine the standalone deployment runs.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.operator_grant_repository import (
    OperatorGrantRepository,
)
from faultmaven.models.interfaces_operator_grant import (
    GrantApprovalState,
    OperatorAccessGrant,
)

OPERATOR = "op-1"
CASE_ID = "case_a1b2c3d4e5f6"
ORG = "org-tenant-1"


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _aged_out() -> dict:
    """A grant whose window opened and then closed.

    Not "expires before it was created" — the schema forbids that outright, and
    rightly so. A grant expires by the clock moving past it, so that is what the
    fixture models.
    """
    now = datetime.now(timezone.utc)
    return {
        "created_at": now - timedelta(hours=2),
        "expires_at": now - timedelta(hours=1),
    }


def _grant(grant_id: str, **overrides) -> OperatorAccessGrant:
    now = datetime.now(timezone.utc)
    defaults = dict(
        grant_id=grant_id,
        operator_user_id=OPERATOR,
        target_case_id=CASE_ID,
        target_enterprise_id=ORG,
        reason="customer reports the investigation is stuck; ticket SUP-4821",
        created_at=now,
        expires_at=now + timedelta(minutes=60),
        approval_state=GrantApprovalState.AUTO_APPROVED,
    )
    defaults.update(overrides)
    return OperatorAccessGrant(**defaults)


async def _store(factory, grant: OperatorAccessGrant) -> None:
    async with factory() as session:
        await OperatorGrantRepository(session).create_grant(grant)


async def _find_live(factory, case_id: str = CASE_ID, operator: str = OPERATOR):
    async with factory() as session:
        return await OperatorGrantRepository(session).find_live_grant(
            operator_user_id=operator, target_case_id=case_id
        )


@pytest.mark.unit
@pytest.mark.security
class TestFindLiveGrant:
    async def test_a_live_grant_is_found(self, session_factory):
        await _store(session_factory, _grant("g-live"))
        found = await _find_live(session_factory)
        assert found is not None
        assert found.grant_id == "g-live"
        assert found.is_live()

    @pytest.mark.parametrize(
        "overrides,label",
        [
            pytest.param(
                _aged_out(),
                "expired",
                id="expired",
            ),
            pytest.param(
                {"approval_state": GrantApprovalState.PENDING}, "pending", id="pending"
            ),
            pytest.param(
                {"approval_state": GrantApprovalState.DENIED}, "denied", id="denied"
            ),
        ],
    )
    async def test_a_grant_that_is_not_live_is_not_found(
        self, session_factory, overrides, label
    ):
        """The SQL predicate must agree with ``is_live`` on every axis.

        Revocation is covered separately because it is written by an UPDATE
        rather than at creation.
        """
        await _store(session_factory, _grant(f"g-{label}", **overrides))
        assert await _find_live(session_factory) is None

    async def test_a_revoked_grant_is_not_found(self, session_factory):
        await _store(session_factory, _grant("g-revoked"))
        async with session_factory() as session:
            await OperatorGrantRepository(session).revoke_grant("g-revoked", "op-2")

        assert await _find_live(session_factory) is None

    async def test_a_grant_over_another_case_does_not_match(self, session_factory):
        await _store(session_factory, _grant("g-other-case"))
        assert await _find_live(session_factory, case_id="case_zzzzzzzzzzzz") is None

    async def test_another_operators_grant_does_not_match(self, session_factory):
        """A grant licenses one operator, not the operator role."""
        await _store(session_factory, _grant("g-theirs", operator_user_id="op-other"))
        assert await _find_live(session_factory) is None

    async def test_overlapping_grants_resolve_to_the_later_expiry(
        self, session_factory
    ):
        """An operator who re-justifies mid-window holds two live grants.

        The access belongs to the window that still covers it, so the audit row
        names the grant that is actually keeping the door open.
        """
        now = datetime.now(timezone.utc)
        await _store(
            session_factory,
            _grant("g-short", expires_at=now + timedelta(minutes=5)),
        )
        await _store(
            session_factory,
            _grant("g-long", expires_at=now + timedelta(minutes=120)),
        )

        found = await _find_live(session_factory)
        assert found.grant_id == "g-long"


@pytest.mark.unit
@pytest.mark.security
class TestTimestampsComeBackAware:
    """SQLite has no timezone type, so every value it returns is naive.

    Left that way, ``expires_at`` reaches the API serialiser with no offset and a
    client parses a break-glass window as *local* time — hours wrong in either
    direction depending on where the operator sits, on the one field that says
    when their access ends. ``is_live`` compensates internally, which is exactly
    why the normalisation has to happen a layer down: the domain object is what
    the wire is built from.
    """

    @pytest.mark.parametrize(
        "field", ["created_at", "expires_at", "revoked_at", "approved_at"]
    )
    async def test_every_timestamp_is_timezone_aware(self, session_factory, field):
        now = datetime.now(timezone.utc)
        await _store(
            session_factory,
            _grant("g-1", approved_at=now, approval_state=GrantApprovalState.APPROVED),
        )
        async with session_factory() as session:
            await OperatorGrantRepository(session).revoke_grant("g-1", "op-2")
        async with session_factory() as session:
            grant = await OperatorGrantRepository(session).get_grant("g-1")

        value = getattr(grant, field)
        assert value is not None, f"{field} should be set by this fixture"
        assert value.tzinfo is not None, f"{field} came back naive"

    async def test_the_aware_value_still_means_the_same_instant(self, session_factory):
        """Stamping UTC must not shift the timestamp, only label it."""
        expires = datetime.now(timezone.utc) + timedelta(minutes=30)
        await _store(session_factory, _grant("g-1", expires_at=expires))

        found = await _find_live(session_factory)

        assert abs((found.expires_at - expires).total_seconds()) < 1


@pytest.mark.unit
@pytest.mark.security
class TestRevocationIsMonotonic:
    async def test_revoking_twice_keeps_the_first_timestamp(self, session_factory):
        """``revoked_at`` is the record of when access actually ended.

        Letting a second revoke move it forward would let an operator restate
        that their access ran longer than it did.
        """
        await _store(session_factory, _grant("g-1"))

        async with session_factory() as session:
            first = await OperatorGrantRepository(session).revoke_grant("g-1", "op-2")
        async with session_factory() as session:
            second = await OperatorGrantRepository(session).revoke_grant("g-1", "op-3")

        assert first.revoked_at == second.revoked_at
        assert second.revoked_by == "op-2"

    async def test_revoking_an_unknown_grant_returns_none(self, session_factory):
        async with session_factory() as session:
            assert (
                await OperatorGrantRepository(session).revoke_grant("nope", "op-2")
                is None
            )


@pytest.mark.unit
class TestListing:
    async def test_live_only_filters_the_dead_ones(self, session_factory):

        await _store(session_factory, _grant("g-live"))
        await _store(session_factory, _grant("g-expired", **_aged_out()))

        async with session_factory() as session:
            repo = OperatorGrantRepository(session)
            live, live_total = await repo.list_grants(live_only=True)
            everything, all_total = await repo.list_grants()

        assert [g.grant_id for g in live] == ["g-live"]
        assert live_total == 1
        assert all_total == 2
        assert {g.grant_id for g in everything} == {"g-live", "g-expired"}

    async def test_filters_by_target_organization(self, session_factory):
        await _store(session_factory, _grant("g-here"))
        await _store(
            session_factory,
            _grant("g-elsewhere", target_enterprise_id="org-other"),
        )

        async with session_factory() as session:
            found, total = await OperatorGrantRepository(session).list_grants(
                target_enterprise_id=ORG
            )

        assert total == 1
        assert found[0].grant_id == "g-here"
