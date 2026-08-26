"""Last-admin constraint trigger — the concurrency guard, exercised concurrently.

Covers migration 044 (``c7d8e9f0a1b2``), which installs the deferred constraint
trigger ``organization_members_last_admin`` on ``organization_members`` so that
"an organization never loses its last admin" is a property of the table rather
than a check each writer has to remember (fm#1161).

The defect these tests exist for is a TOCTOU, so a test that demotes one admin
and looks at the error proves almost nothing: the application check already did
that, and it is exactly what a concurrent writer defeats. The bar here is
:func:`test_concurrent_last_admin_writes_leave_one_admin`, which drives **two
real transactions on two real connections** through the whole check-then-write
shape — each reads the roster and sees two admins, each writes, and neither
commits until both have written — and then asserts that exactly one commit
survives and the organization still has an admin.

PostgreSQL only. SQLite (Standalone) is single-tenant and has no organizations
to orphan, which is the line migration 018 already draws for RLS. These tests
run in CI's ``Test PostgreSQL Integration`` job, which fails if any test in it
skips.

Run locally::

    docker run -d -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:16
    export DATABASE_URL=postgresql+asyncpg://postgres:pw@localhost:5432/postgres
    alembic upgrade head
    pytest tests/integration/test_last_admin_constraint_trigger.py -v

Test ↔ guard mapping
--------------------
Every test below is anchored to a specific clause of the trigger function in
``alembic/versions/20260826_1200_c7d8e9f0a1b2_044_last_admin_constraint_trigger.py``.
Mutate the clause and the named test must fail:

===============================================  ==========================================
Guard clause (migration 044)                     Test that must fail without it
===============================================  ==========================================
the guard function's body (the whole guard)      ``test_concurrent_last_admin_writes_leave_one_admin``
``UPDATE organizations`` serialisation point     ``..._leave_one_admin_at_repeatable_read``
``IF v_admin_count = 0 THEN RAISE``              ``test_demoting_the_sole_admin_is_refused``
``DEFERRABLE INITIALLY DEFERRED``                ``test_swapping_the_admin_in_one_transaction_is_allowed``
``IF v_org_rows = 0`` (organization cascade)     ``test_deleting_the_organization_is_not_blocked``
``IF NOT EXISTS ... users`` (account cascade)    ``test_deleting_the_user_account_is_not_blocked``
``OLD.role_id IS DISTINCT FROM`` early return    ``test_removing_a_non_admin_is_unaffected``
===============================================  ==========================================

All three mutations were run against the committed tree:

* replacing the guard function's body with ``BEGIN RETURN NULL; END`` — the
  pre-044 world, with the trigger still installed so nothing short-circuits —
  leaves the concurrent tests with two successful commits and **zero** admins,
  and every single-writer refusal passing its write through unopposed. Six of
  the eleven tests here fail; the cascade, swap and non-admin tests correctly
  still pass, because the guard's absence is not what they are about.
* removing only the ``UPDATE organizations`` serialisation point, keeping the
  count, fails the REPEATABLE READ race **every** time. It does *not* reliably
  fail the READ COMMITTED one — measured at 2 breaches in 20 runs — which is
  the whole reason the REPEATABLE READ test exists rather than a second READ
  COMMITTED case: a guard that holds nine times in ten is not a guard, and a
  test of it would be flaky in whichever direction is worse that day.
* recreating the trigger ``NOT DEFERRABLE`` fails
  ``test_swapping_the_admin_in_one_transaction_is_allowed`` and nothing else —
  the demotion is rejected mid-transaction, before the promotion that makes it
  legitimate has been written. It also hangs the concurrent tests outright,
  which is why the deferral is anchored to the swap: an immediate trigger takes
  its lock while the member rows are still held, so the two writers deadlock
  against the barrier. The deferred trigger takes nothing until commit, which
  is what makes the barrier safe in the first place.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from faultmaven.infrastructure.persistence.organization_repository import (
    LAST_ADMIN_CONSTRAINT,
    is_last_admin_violation,
)
from faultmaven.models.rbac import Role
from faultmaven.models.rbac_seed import SYSTEM_ROLE_IDS

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

ADMIN_ROLE_ID = SYSTEM_ROLE_IDS[Role.ADMIN]
VIEWER_ROLE_ID = SYSTEM_ROLE_IDS[Role.VIEWER]

# The recogniser under test is the shipped one, imported above rather than
# reimplemented here: the Cloud service uses it to turn the trigger's refusal
# back into its own friendly error, and a private copy would let the two drift
# while both looked verified.
TRIGGER_NAME = LAST_ADMIN_CONSTRAINT
ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"

_COUNT_ADMINS = text(
    "SELECT count(*) FROM organization_members "
    "WHERE organization_id = :org AND role_id = :role"
)
_DEMOTE = text(
    "UPDATE organization_members SET role_id = :role "
    "WHERE organization_id = :org AND user_id = :user"
)
_REMOVE = text(
    "DELETE FROM organization_members WHERE organization_id = :org AND user_id = :user"
)


def _is_serialization_failure(exc: BaseException) -> bool:
    """SQLSTATE 40001 — PostgreSQL refusing the write on the guard's behalf.

    Reachable only at REPEATABLE READ or above, where the loser's write to the
    organization row the guard just versioned cannot be serialised against the
    winner's commit.
    """
    if not isinstance(exc, DBAPIError):
        return False
    return getattr(getattr(exc.orig, "__cause__", None), "sqlstate", None) == "40001"


@pytest.fixture
async def engine():
    """Engine as the migration role (owns the tables, so RLS does not apply)."""
    eng = create_async_engine(os.environ["DATABASE_URL"], future=True)
    assert eng.dialect.name == "postgresql"
    yield eng
    await eng.dispose()


@pytest.fixture
async def trigger_installed(engine):
    """Refuse to run if migration 044 is not actually applied to this database.

    Without this the whole module would pass vacuously against a database
    migrated to 043 — every "the write is refused" assertion would fail loudly,
    but the cascade and swap tests would go green having proven nothing, and a
    reader would have to notice which half was which.
    """
    async with engine.connect() as conn:
        found = await conn.scalar(
            text(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgname = :name AND NOT tgisinternal"
            ),
            {"name": TRIGGER_NAME},
        )
    assert found == 1, (
        f"constraint trigger {TRIGGER_NAME} is not installed — this database is "
        "not migrated to c7d8e9f0a1b2, so these tests would prove nothing."
    )


@pytest.fixture
async def org(engine, trigger_installed):
    """An organization with two admins and one viewer.

    Two admins is the roster the race needs: one each for the concurrent
    writers, with the second-to-last removal being the one that must lose.
    """
    org_id = f"org_{uuid4().hex[:12]}"
    admin_a = f"usr_{uuid4().hex[:12]}"
    admin_b = f"usr_{uuid4().hex[:12]}"
    viewer = f"usr_{uuid4().hex[:12]}"

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO enterprises (enterprise_id, name, slug) "
                "VALUES (:id, 'Test Enterprise', 'test-enterprise-044') "
                "ON CONFLICT (enterprise_id) DO NOTHING"
            ),
            {"id": ENTERPRISE_ID},
        )
        # Every bind is named once and used once: asyncpg deduces a type per
        # parameter and raises AmbiguousParameterError when one placeholder is
        # reused across columns of different widths.
        await conn.execute(
            text(
                "INSERT INTO organizations (organization_id, enterprise_id, name, slug) "
                "VALUES (:id, :ent, :name, :slug)"
            ),
            {"id": org_id, "ent": ENTERPRISE_ID, "name": org_id, "slug": org_id},
        )
        for user_id, role_id in (
            (admin_a, ADMIN_ROLE_ID),
            (admin_b, ADMIN_ROLE_ID),
            (viewer, VIEWER_ROLE_ID),
        ):
            await conn.execute(
                text(
                    "INSERT INTO users "
                    "(user_id, enterprise_id, username, email, display_name) "
                    "VALUES (:id, :ent, :username, :email, :display_name)"
                ),
                {
                    "id": user_id,
                    "ent": ENTERPRISE_ID,
                    "username": user_id,
                    "email": f"{user_id}@example.test",
                    "display_name": user_id,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO organization_members "
                    "(user_id, organization_id, role_id) VALUES (:u, :o, :r)"
                ),
                {"u": user_id, "o": org_id, "r": role_id},
            )

    yield org_id, admin_a, admin_b, viewer

    async with engine.begin() as conn:
        # Organizations cascade to their memberships, and the guard exempts that
        # cascade — so teardown works even for a fixture left with one admin.
        await conn.execute(
            text("DELETE FROM organizations WHERE organization_id = :o"), {"o": org_id}
        )
        await conn.execute(
            text("DELETE FROM users WHERE user_id IN (:a, :b, :c)"),
            {"a": admin_a, "b": admin_b, "c": viewer},
        )


async def _count_admins(engine, org_id: str) -> int:
    async with engine.connect() as conn:
        return await conn.scalar(_COUNT_ADMINS, {"org": org_id, "role": ADMIN_ROLE_ID})


async def _demote_to_sole_admin(engine, org_id: str, user_id: str) -> None:
    """Leave ``org_id`` with exactly one admin, without tripping the guard."""
    async with engine.begin() as conn:
        await conn.execute(
            _DEMOTE, {"role": VIEWER_ROLE_ID, "org": org_id, "user": user_id}
        )


# ---------------------------------------------------------------------------
# The bar: two genuinely concurrent writers
# ---------------------------------------------------------------------------


async def _race_two_writers(engine, org_id, user_a, user_b, op_a, op_b, isolation=None):
    """Drive two transactions through the whole check-then-write shape at once.

    Each writer reads the roster and sees two admins, passes the check the
    application would have passed, and writes. A barrier holds both open until
    each has written, so neither can observe the other's committed effect at
    check time — the interleaving the service's guard cannot see and cannot
    survive. Only then do both commit, concurrently.

    Returns the two outcomes: ``None`` for a writer that committed, the
    exception for one that did not.
    """
    barrier = asyncio.Barrier(2)

    async def writer(user_id: str, op: str) -> None:
        async with engine.connect() as conn:
            if isolation is not None:
                conn = await conn.execution_options(isolation_level=isolation)
            trans = await conn.begin()
            # The read half of the TOCTOU: the roster the service would check.
            observed = await conn.scalar(
                _COUNT_ADMINS, {"org": org_id, "role": ADMIN_ROLE_ID}
            )
            assert observed == 2, (
                "the race was not set up: this writer saw "
                f"{observed} admins, not the two it must be fooled by"
            )
            if op == "demote":
                await conn.execute(
                    _DEMOTE, {"role": VIEWER_ROLE_ID, "org": org_id, "user": user_id}
                )
            else:
                await conn.execute(_REMOVE, {"org": org_id, "user": user_id})
            # Neither writer commits until both have written, so both passed
            # their check against a roster that still held two admins.
            await barrier.wait()
            await trans.commit()

    return await asyncio.gather(
        writer(user_a, op_a), writer(user_b, op_b), return_exceptions=True
    )


@pytest.mark.parametrize(
    "op_a, op_b",
    [
        ("demote", "demote"),
        ("remove", "remove"),
        ("demote", "remove"),
    ],
)
async def test_concurrent_last_admin_writes_leave_one_admin(engine, org, op_a, op_b):
    """Two admins, two concurrent writers, one admin left standing.

    This is the shape of fm#1161, in all three combinations the issue names:
    two demotions, two removals, one of each.

    Exactly one commit must land. Which one is not asserted: either order is a
    correct outcome, and pinning it would be pinning a scheduling accident.
    """
    org_id, admin_a, admin_b, _viewer = org

    results = await _race_two_writers(engine, org_id, admin_a, admin_b, op_a, op_b)

    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(failures) == 1, (
        "both concurrent writers were allowed to commit — the organization was "
        f"stripped of its admins. Results: {results!r}"
    )
    assert is_last_admin_violation(
        failures[0]
    ), f"the losing writer failed for the wrong reason: {failures[0]!r}"
    assert (
        await _count_admins(engine, org_id) == 1
    ), "the organization does not have exactly one admin after the race"


async def test_concurrent_last_admin_writes_leave_one_admin_at_repeatable_read(
    engine, org
):
    """The same race with both transactions at REPEATABLE READ.

    This is the test that holds the serialisation point — the guard's no-op
    self-update of the organization row — in place, and it is here because the
    READ COMMITTED test above cannot. Strip the self-update and leave the count,
    and under READ COMMITTED the guard still usually wins: the loser's count
    takes a fresh snapshot at commit and normally sees the winner's committed
    row. Measured against this schema it breached roughly one run in ten, which
    makes it a coin toss rather than a control, and makes any test of it flaky
    in whichever direction is worse that day.

    At REPEATABLE READ the same omission fails *every* time and this test is
    deterministic. A counting-only guard takes its count from a snapshot
    predating the other writer's commit, sees the admin it is about to demote
    plus one, and waves both transactions through. The self-update is a real row
    version rather than a lock, so the loser's write to it raises
    ``could not serialize access due to concurrent update`` instead.

    That is also why the losing writer is allowed to fail with either SQLSTATE
    here: ``40001`` is the guard working through PostgreSQL's own conflict
    detection, ``23514`` is the guard refusing in its own words, and which one
    arrives depends on how far the loser got before the winner committed.
    """
    org_id, admin_a, admin_b, _viewer = org

    results = await _race_two_writers(
        engine,
        org_id,
        admin_a,
        admin_b,
        "demote",
        "demote",
        isolation="REPEATABLE READ",
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(failures) == 1, (
        "both concurrent writers were allowed to commit — the organization was "
        f"stripped of its admins. Results: {results!r}"
    )
    assert is_last_admin_violation(
        failures[0]
    ) or _is_serialization_failure(  # noqa: W503
        failures[0]
    ), f"the losing writer failed for the wrong reason: {failures[0]!r}"
    assert (
        await _count_admins(engine, org_id) == 1
    ), "the organization does not have exactly one admin after the race"


# ---------------------------------------------------------------------------
# The single-writer cases the trigger also has to get right
# ---------------------------------------------------------------------------


async def test_demoting_the_sole_admin_is_refused(engine, org):
    """The plain case: with one admin left, demoting them is rejected."""
    org_id, admin_a, admin_b, _viewer = org
    await _demote_to_sole_admin(engine, org_id, admin_b)

    with pytest.raises(DBAPIError) as caught:
        async with engine.begin() as conn:
            await conn.execute(
                _DEMOTE, {"role": VIEWER_ROLE_ID, "org": org_id, "user": admin_a}
            )

    assert is_last_admin_violation(caught.value)
    assert await _count_admins(engine, org_id) == 1


async def test_removing_the_sole_admin_is_refused(engine, org):
    """The same for a DELETE — the membership row, not just the role."""
    org_id, admin_a, admin_b, _viewer = org
    await _demote_to_sole_admin(engine, org_id, admin_b)

    with pytest.raises(DBAPIError) as caught:
        async with engine.begin() as conn:
            await conn.execute(_REMOVE, {"org": org_id, "user": admin_a})

    assert is_last_admin_violation(caught.value)
    assert await _count_admins(engine, org_id) == 1


async def test_removing_a_non_admin_is_unaffected(engine, org):
    """A viewer can still be removed from an org that has a sole admin.

    The guard returns before its serialisation point when the row it is looking
    at was never an admin. A guard that counted on every membership write would
    still pass this, but it anchors the early return: removing the viewer must
    not become contended or refused.
    """
    org_id, _admin_a, admin_b, viewer = org
    await _demote_to_sole_admin(engine, org_id, admin_b)

    async with engine.begin() as conn:
        await conn.execute(_REMOVE, {"org": org_id, "user": viewer})

    async with engine.connect() as conn:
        remaining = await conn.scalar(
            text(
                "SELECT count(*) FROM organization_members "
                "WHERE organization_id = :o AND user_id = :u"
            ),
            {"o": org_id, "u": viewer},
        )
    assert remaining == 0
    assert await _count_admins(engine, org_id) == 1


async def test_swapping_the_admin_in_one_transaction_is_allowed(engine, org):
    """Demote the sole admin and promote someone else, in one transaction.

    This is why the trigger is ``DEFERRABLE INITIALLY DEFERRED``. A per-statement
    trigger would reject whichever row it saw first and make handing over an
    organization impossible; deferring to commit asks the only question that
    matters — does the organization have an admin when the transaction is done.
    """
    org_id, admin_a, admin_b, viewer = org
    await _demote_to_sole_admin(engine, org_id, admin_b)

    async with engine.begin() as conn:
        await conn.execute(
            _DEMOTE, {"role": VIEWER_ROLE_ID, "org": org_id, "user": admin_a}
        )
        await conn.execute(
            _DEMOTE, {"role": ADMIN_ROLE_ID, "org": org_id, "user": viewer}
        )

    assert await _count_admins(engine, org_id) == 1
    async with engine.connect() as conn:
        role = await conn.scalar(
            text(
                "SELECT role_id FROM organization_members "
                "WHERE organization_id = :o AND user_id = :u"
            ),
            {"o": org_id, "u": viewer},
        )
    assert role == ADMIN_ROLE_ID


# ---------------------------------------------------------------------------
# The two cascades the guard deliberately steps out of the way for
# ---------------------------------------------------------------------------


async def test_deleting_the_organization_is_not_blocked(engine, org):
    """Deleting an organization cascades its roster away, guard and all.

    Nothing is left to keep manageable, so refusing here would only make an
    organization impossible to delete once it had an admin.
    """
    org_id, _admin_a, admin_b, _viewer = org
    await _demote_to_sole_admin(engine, org_id, admin_b)

    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM organizations WHERE organization_id = :o"), {"o": org_id}
        )

    async with engine.connect() as conn:
        survivors = await conn.scalar(
            text(
                "SELECT count(*) FROM organization_members WHERE organization_id = :o"
            ),
            {"o": org_id},
        )
    assert survivors == 0


async def test_deleting_the_user_account_is_not_blocked(engine, org):
    """Deleting the sole admin's *account* is not turned into a failure.

    ``organization_members`` cascades from ``users``. Refusing the cascade would
    make account deletion fail with no path through it — a far more destructive
    act than a demotion, and not the concurrency hazard this guard exists for.
    The organization is left admin-less, which is the documented cost.
    """
    org_id, admin_a, admin_b, _viewer = org
    await _demote_to_sole_admin(engine, org_id, admin_b)

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM users WHERE user_id = :u"), {"u": admin_a})

    assert await _count_admins(engine, org_id) == 0


# ---------------------------------------------------------------------------
# The role the application actually connects as
# ---------------------------------------------------------------------------


async def test_guard_holds_for_the_limited_application_role(engine, org):
    """The guard is not something the application's own role can write past.

    Production connects as a non-superuser, non-owner role with RLS applied
    (migration 018), not as the migration role every other test here uses. A
    guard verified only as the owner would be a guard verified in a shape the
    deployment never runs.
    """
    org_id, admin_a, admin_b, _viewer = org
    await _demote_to_sole_admin(engine, org_id, admin_b)

    role_name = f"fm_last_admin_test_{uuid4().hex[:8]}"
    password = "fm_last_admin_test_pw"
    drop_role = (
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role_name}') "
        f"THEN DROP OWNED BY {role_name}; DROP ROLE {role_name}; END IF; END $$;"
    )

    async with engine.begin() as conn:
        dbname = (await conn.exec_driver_sql("SELECT current_database()")).scalar()
        await conn.exec_driver_sql(drop_role)
        await conn.exec_driver_sql(
            f"CREATE ROLE {role_name} LOGIN PASSWORD '{password}' NOSUPERUSER"
        )
        await conn.exec_driver_sql(
            f'GRANT CONNECT ON DATABASE "{dbname}" TO {role_name}'
        )
        await conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role_name}")
        await conn.exec_driver_sql(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
            f"TO {role_name}"
        )

    limited_url = make_url(os.environ["DATABASE_URL"]).set(
        username=role_name, password=password
    )
    limited = create_async_engine(limited_url, future=True)
    try:
        with pytest.raises(DBAPIError) as caught:
            async with limited.begin() as conn:
                # RLS scopes this role's writes; the chokepoint binds the GUC
                # per transaction, so the test binds it the same way.
                await conn.execute(
                    text("SELECT set_config('app.current_org_id', :o, true)"),
                    {"o": org_id},
                )
                await conn.execute(
                    _DEMOTE, {"role": VIEWER_ROLE_ID, "org": org_id, "user": admin_a}
                )
        assert is_last_admin_violation(caught.value)
    finally:
        await limited.dispose()
        async with engine.begin() as conn:
            await conn.exec_driver_sql(drop_role)

    assert await _count_admins(engine, org_id) == 1
