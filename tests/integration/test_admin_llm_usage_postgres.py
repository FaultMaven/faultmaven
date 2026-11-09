"""``GET /admin/llm/usage`` on PostgreSQL under RLS (#1764).

The SQLite half, through the mounted route:
``tests/integration/api/test_admin_llm_usage_api.py``. This one adds what SQLite
cannot show: the reader runs as a non-superuser, non-owner role, tenant-bound
per transaction, so RLS backs the explicit ``enterprise_id`` predicate. Rows are
written through the shipped ``SqlUsageLedger`` as the same role.

Run locally:

    docker run -d -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:16
    export DATABASE_URL=postgresql+asyncpg://postgres:pw@localhost:5432/postgres
    .venv-cloud/bin/alembic upgrade head
    .venv-cloud/bin/pytest tests/integration/test_admin_llm_usage_postgres.py -v
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from faultmaven.infrastructure.llm.usage_ledger import (
    CallBucket,
    SqlUsageLedger,
    TurnSpend,
    UsageAttribution,
)
from faultmaven.infrastructure.llm.usage_report import read_usage_report
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser

# The #1882 fixtures: the superuser engine, two seeded enterprises, sessions as
# a limited role tenant-bound per transaction.
from tests.integration.test_turn_rows_commit_with_case_postgres_1882 import (  # noqa: F401
    enterprises,
    superuser_engine,
    tenant,
    tenant_sessions,
)
from tests.utils import reset_settings_singleton

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]


@pytest.fixture
def limited_sessions(tenant_sessions, monkeypatch):
    """``get_db_session`` over the limited role, shaped like production's."""
    from faultmaven.infrastructure.persistence import database

    @asynccontextmanager
    async def _get_db_session():
        session = tenant_sessions()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    monkeypatch.setattr(database, "get_db_session", _get_db_session)


async def _seed(
    superuser_engine,
    enterprise_id: str,
    calls: int,
    spend: int,
    account: str = "",
    kind: str = "account",
):
    """One call and one turn for ``enterprise_id``, written as the limited role."""
    case_id = f"case_{uuid4().hex[:12]}"
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO cases (case_id, enterprise_id, title) "
                "VALUES (:c, :e, 't')"
            ),
            {"c": case_id, "e": enterprise_id},
        )
    who = (
        UsageAttribution(enterprise_id, kind, account, account)
        if account
        else UsageAttribution(enterprise_id, "none", "", "")
    )
    now = datetime.now(timezone.utc)
    bucket = CallBucket("anthropic", "claude-sonnet-4-6", "kept", calls=calls)
    turn = TurnSpend(
        case_id=case_id,
        turn_number=1,
        investigation_turn=1,
        input_tokens=spend,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        spend_weighted_tokens=spend,
        calls=calls,
        low_confidence_calls=0,
        unpriced_calls=0,
        estimated_cost_usd=0.01,
        occurred_at=now,
    )
    with tenant(enterprise_id):
        await SqlUsageLedger().record_turn(who, turn, [bucket], now.date())
    return case_id


async def _report(enterprise_id: str):
    return await read_usage_report(
        enterprise_id=enterprise_id,
        window="7d",
        group_by="provider_model",
        soft_budget=100_000,
        ceiling=150_000,
        turn_retention_days=90,
        hide_account_ids=True,
    )


async def test_an_enterprise_reads_its_own_usage_only(
    superuser_engine, enterprises, limited_sessions, tenant_sessions
):
    ent_a, ent_b = enterprises
    case_a = await _seed(superuser_engine, ent_a, calls=2, spend=20_000)
    await _seed(superuser_engine, ent_b, calls=7, spend=90_000)

    with tenant(ent_a):
        own = await _report(ent_a)
        # The predicate names B; RLS binds A. RLS wins: nothing of B's is read.
        across = await _report(ent_b)
        # And the positive control: an unpredicated read sees A's row alone.
        async with tenant_sessions() as session:
            visible = (
                (
                    await session.execute(
                        text("SELECT DISTINCT enterprise_id FROM llm_usage_daily")
                    )
                )
                .scalars()
                .all()
            )

    assert own["totals"]["calls"]["total"] == 2
    assert [h["case_id"] for h in own["turns"]["heaviest"]] == [case_a]
    assert across["totals"]["calls"]["total"] == 0
    assert across["turns"]["count"] == 0
    assert visible == [ent_a]


async def test_the_route_scopes_to_the_bound_enterprise_under_multi(
    superuser_engine, enterprises, limited_sessions, monkeypatch
):
    from faultmaven.api.routes.admin_usage import get_llm_usage

    ent_a, ent_b = enterprises
    await _seed(superuser_engine, ent_a, calls=3, spend=10_000, account="u_1764_a")
    await _seed(superuser_engine, ent_a, calls=4, spend=10_000, account="u_1764_b")
    # Every seed records no tokens, so the two payer groups tie on the sort.
    await _seed(
        superuser_engine,
        ent_a,
        calls=1,
        spend=1,
        account="org_1764",
        kind="organization",
    )
    await _seed(superuser_engine, ent_b, calls=5, spend=10_000)
    monkeypatch.setenv("TENANT_PROVIDER", "multi")
    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    reset_settings_singleton()
    operator = AuthenticatedUser(
        user_id="op_1764",
        enterprise_id=ent_a,
        email="op@example.com",
        roles=["platform_admin"],
        permissions=[],
    )
    try:
        with tenant(ent_a):
            report = await get_llm_usage(
                window="7d", group_by="provider_model", current_user=operator
            )
            payers = await get_llm_usage(
                window="7d", group_by="billing_subject", current_user=operator
            )
    finally:
        monkeypatch.undo()
        reset_settings_singleton()

    assert report.scope.enterprise_id == ent_a
    assert report.scope.tenancy == "multi"
    assert report.totals.calls.total == 8
    # #640 Q3: under multi, the account subjects (user ids) are one group, and
    # its null id sorts after an organization's id on a tie.
    assert [(g.key, g.calls.total) for g in payers.groups] == [
        ({"kind": "organization", "id": "org_1764"}, 1),
        ({"kind": "account", "id": None}, 7),
    ]


async def test_a_refused_checkout_is_503_not_500(superuser_engine, monkeypatch):
    """A REAL checkout failure: a role PostgreSQL refuses at connect
    (``CONNECTION LIMIT 0``), which asyncpg raises unwrapped by SQLAlchemy."""
    import asyncpg
    from fastapi import HTTPException
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import SQLAlchemyError

    from faultmaven.api.routes.admin_usage import get_llm_usage
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        reset_engine,
    )

    role = f"fm_usage_refused_{uuid4().hex[:8]}"
    async with superuser_engine.begin() as conn:
        await conn.exec_driver_sql(
            f"CREATE ROLE {role} LOGIN PASSWORD 'pw' NOSUPERUSER CONNECTION LIMIT 0"
        )
    refused_url = make_url(os.environ["DATABASE_URL"]).set(username=role, password="pw")
    monkeypatch.setenv(
        "DATABASE_URL", refused_url.render_as_string(hide_password=False)
    )
    reset_settings_singleton()
    reset_engine()
    operator = AuthenticatedUser(
        user_id="op_1764",
        enterprise_id="ent_1764",
        email="op@example.com",
        roles=["platform_admin"],
        permissions=[],
    )
    try:
        with pytest.raises(HTTPException) as refused:
            await get_llm_usage(
                window="7d", group_by="provider_model", current_user=operator
            )
    finally:
        await close_database()
        monkeypatch.undo()
        reset_settings_singleton()
        reset_engine()
        async with superuser_engine.begin() as conn:
            await conn.exec_driver_sql(f"DROP ROLE IF EXISTS {role}")

    assert refused.value.status_code == 503
    assert refused.value.headers == {
        "x-error-code": "DATABASE_UNAVAILABLE",
        "Retry-After": "5",
    }
    assert role not in str(refused.value.detail)
    # The asyncpg path, not the SQLAlchemy one: the store error's cause is
    # asyncpg's own, unwrapped.
    cause = refused.value.__context__.__cause__
    assert isinstance(cause, asyncpg.PostgresError), type(cause)
    assert not isinstance(cause, SQLAlchemyError)
