"""The LLM usage ledger against a real PostgreSQL under RLS (#640).

The SQLite coverage module pins what gets written. This one pins what only a
real multi-tenant database can answer:

* the writes are **atomic increments** — concurrent flushes of one key, from
  sessions that could be two replicas, lose no update. A ``SET col =
  excluded.col`` (replace instead of add) would keep one writer's figures;
* one enterprise's rows are **invisible** to another's session, in both
  tables, and a row stamped for another enterprise is **refused**, not hidden;
* revision 002 steps **down and back up** on PostgreSQL, policies included —
  in a database of its own, so the rest of the lane never sees a downgraded
  schema.

It drives the shipped ``SqlUsageLedger`` as a limited role, because PostgreSQL
exempts superusers and table owners from RLS; every residue check reads back
as the owner.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.infrastructure.llm.usage_ledger import (
    CallBucket,
    SqlUsageLedger,
    TurnSpend,
    UsageAttribution,
)
from tests.integration.security.conftest import (
    create_limited_role,
    drop_limited_role,
    limited_url,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

_ROLE = f"fm_usage_probe_{uuid.uuid4().hex[:8]}"
_PW = "fm_usage_probe_pw"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
#: The ledger's parent revision (001_enterprise_baseline).
_BASELINE_REVISION = "a1e0c17bd001"
TODAY = date(2026, 9, 29)


@pytest.fixture(scope="module")
def limited_role_env():
    """Point the persistence layer at the limited role, multi-tenant, for
    this module only — restored wholesale in teardown (the ``-m postgres`` lane
    runs sibling modules that expect the superuser url)."""
    superuser_url = os.environ["DATABASE_URL"]
    saved = {
        key: os.environ.get(key)
        for key in ("DATABASE_URL", "DEPLOYMENT_MODE", "TENANT_PROVIDER")
    }
    asyncio.run(create_limited_role(superuser_url, _ROLE, _PW))
    os.environ["DATABASE_URL"] = limited_url(superuser_url, _ROLE, _PW)
    os.environ["DEPLOYMENT_MODE"] = "cloud"
    os.environ["TENANT_PROVIDER"] = "multi"

    from faultmaven.infrastructure.persistence.database import reset_engine
    from tests.utils import reset_settings_singleton

    reset_settings_singleton()
    reset_engine()
    yield superuser_url
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_settings_singleton()
    reset_engine()
    asyncio.run(drop_limited_role(superuser_url, _ROLE))


@pytest.fixture(autouse=True)
async def fresh_engine_per_loop(limited_role_env):
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        reset_engine,
    )

    reset_engine()
    yield
    await close_database()
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


async def _as_owner(superuser_url: str, sql: str, **params):
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return result.fetchall() if result.returns_rows else []
    finally:
        await engine.dispose()


@pytest.fixture
async def two_enterprises(limited_role_env):
    """Two enterprises, each with an account and a case (the turn row's FK).
    Seeded and removed as the owner; the cascade takes the ledger rows."""
    made = []
    for label in ("a", "b"):
        suffix = uuid.uuid4().hex[:8]
        enterprise = f"ent_usage_{label}_{suffix}"
        user = f"user_usage_{label}_{suffix}"
        case_id = f"case_{uuid.uuid4().hex[:12]}"
        await _as_owner(
            limited_role_env,
            "INSERT INTO enterprises (enterprise_id, name, slug) VALUES (:e, :e, :e)",
            e=enterprise,
        )
        await _as_owner(
            limited_role_env,
            "INSERT INTO users (user_id, enterprise_id, username, email, "
            "display_name) VALUES (:u, :e, :u, :m, :u)",
            u=user,
            e=enterprise,
            m=f"{user}@example.com",
        )
        await _as_owner(
            limited_role_env,
            "INSERT INTO cases (case_id, enterprise_id, user_id, title) "
            "VALUES (:c, :e, :u, 't')",
            c=case_id,
            e=enterprise,
            u=user,
        )
        made.append((enterprise, user, case_id))
    yield made
    for enterprise, _user, _case in made:
        await _as_owner(
            limited_role_env,
            "DELETE FROM enterprises WHERE enterprise_id = :e",
            e=enterprise,
        )


def _attribution(enterprise: str, user: str) -> UsageAttribution:
    return UsageAttribution(enterprise, "account", user, user)


def _bucket(calls: int = 1) -> CallBucket:
    return CallBucket(
        "anthropic",
        "claude-sonnet-4-6",
        "kept",
        input_tokens=100 * calls,
        output_tokens=10 * calls,
        estimated_cost_usd=0.001 * calls,
        calls=calls,
    )


def _turn(case_id: str) -> TurnSpend:
    return TurnSpend(
        case_id=case_id,
        turn_number=3,
        investigation_turn=3,
        input_tokens=100,
        output_tokens=10,
        cache_read_tokens=0,
        cache_write_tokens=0,
        spend_weighted_tokens=110,
        calls=1,
        low_confidence_calls=0,
        unpriced_calls=0,
        estimated_cost_usd=0.001,
        occurred_at=datetime.now(timezone.utc),
    )


async def test_the_role_under_test_is_subject_to_rls(limited_role_env):
    """If RLS were bypassed, every assertion below would be vacuous."""
    engine = create_async_engine(limited_url(limited_role_env, _ROLE, _PW), future=True)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).one()
            assert (row.rolsuper, row.rolbypassrls) == (False, False)
            for table in ("llm_usage_daily", "llm_turn_spend"):
                enabled = (
                    await conn.execute(
                        text("SELECT relrowsecurity FROM pg_class WHERE relname = :t"),
                        {"t": table},
                    )
                ).scalar()
                assert enabled is True, f"{table} is not enrolled in RLS"
    finally:
        await engine.dispose()


async def test_concurrent_flushes_of_one_key_lose_no_update(
    limited_role_env, two_enterprises
):
    """Twenty writers on one daily key and one turn key, as two replicas would
    be. The sums must be exactly twenty; a replace would leave one writer's."""
    enterprise, user, case_id = two_enterprises[0]
    attribution = _attribution(enterprise, user)
    ledger = SqlUsageLedger()
    set_current_enterprise_id(enterprise)

    await asyncio.gather(
        *(ledger.record_call(attribution, _bucket(), TODAY) for _ in range(10)),
        *(
            ledger.record_turn(attribution, _turn(case_id), [_bucket()], TODAY)
            for _ in range(10)
        ),
    )

    daily = await _as_owner(
        limited_role_env,
        "SELECT calls, input_tokens, estimated_cost_usd FROM llm_usage_daily "
        "WHERE enterprise_id = :e",
        e=enterprise,
    )
    assert len(daily) == 1, "one key, one row"
    assert daily[0][0] == 20 and daily[0][1] == 2000
    assert daily[0][2] == pytest.approx(0.02)
    turn = await _as_owner(
        limited_role_env,
        "SELECT calls, spend_weighted_tokens FROM llm_turn_spend "
        "WHERE enterprise_id = :e",
        e=enterprise,
    )
    assert turn == [(10, 1100)]


async def test_one_enterprise_cannot_read_anothers_rows(
    limited_role_env, two_enterprises
):
    ledger = SqlUsageLedger()
    for enterprise, user, case_id in two_enterprises:
        set_current_enterprise_id(enterprise)
        await ledger.record_turn(
            _attribution(enterprise, user), _turn(case_id), [_bucket()], TODAY
        )

    (a, _user_a, _case_a), (b, _user_b, _case_b) = two_enterprises
    from faultmaven.infrastructure.persistence.database import get_db_session

    set_current_enterprise_id(a)
    async with get_db_session() as session:
        for table in ("llm_usage_daily", "llm_turn_spend"):
            seen = (
                (await session.execute(text(f"SELECT enterprise_id FROM {table}")))
                .scalars()
                .all()
            )
            assert set(seen) == {a}, f"{table}: {seen}"

    both = await _as_owner(
        limited_role_env,
        "SELECT count(*) FROM llm_turn_spend WHERE enterprise_id IN (:a, :b)",
        a=a,
        b=b,
    )
    assert both == [(2,)], "the owner sees both, so the policy is what hid B"


async def test_a_row_stamped_for_another_enterprise_is_refused(
    limited_role_env, two_enterprises
):
    (a, _user_a, _case_a), (b, user_b, _case_b) = two_enterprises
    set_current_enterprise_id(a)
    with pytest.raises(Exception, match="row-level security"):
        await SqlUsageLedger().record_call(_attribution(b, user_b), _bucket(), TODAY)
    residue = await _as_owner(
        limited_role_env,
        "SELECT count(*) FROM llm_usage_daily WHERE enterprise_id = :b",
        b=b,
    )
    assert residue == [(0,)]


def _alembic(url: str, command: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, DATABASE_URL=url)
    env["PYTHONPATH"] = f"{_PROJECT_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *command.split()],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


async def test_revision_002_steps_down_and_up(limited_role_env):
    """In a database of its own: downgrading the shared one would pull the
    tables out from under every other test in the lane."""
    name = f"fm_usage_updown_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(
        limited_role_env, future=True, isolation_level="AUTOCOMMIT"
    )
    url = (
        make_url(limited_role_env)
        .set(database=name)
        .render_as_string(hide_password=False)
    )
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:

        async def tables_and_policies():
            engine = create_async_engine(url, future=True)
            try:
                async with engine.connect() as conn:
                    tables = set(
                        (
                            await conn.execute(
                                text(
                                    "SELECT tablename FROM pg_tables WHERE "
                                    "tablename LIKE 'llm_%'"
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    policies = set(
                        (
                            await conn.execute(
                                text(
                                    "SELECT polname FROM pg_policy WHERE "
                                    "polname LIKE 'llm_%'"
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    return tables, policies
            finally:
                await engine.dispose()

        up = {"llm_usage_daily", "llm_turn_spend"}
        policies = {f"{t}_tenant_isolation" for t in up}

        result = _alembic(url, "upgrade head")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await tables_and_policies() == (up, policies)

        # To the ledger's parent, stepping over whatever was added after it.
        result = _alembic(url, f"downgrade {_BASELINE_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await tables_and_policies() == (set(), set())

        result = _alembic(url, "upgrade head")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await tables_and_policies() == (up, policies)
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
