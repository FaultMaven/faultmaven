"""PostgreSQL Row-Level Security tenant-isolation tests (migration 018, ADR-010 P2).

Proves the RLS policies actually block cross-organization reads.

CRITICAL: a PostgreSQL **superuser** and a **table owner** BYPASS RLS. The CI
``fmtest`` role is the superuser that ran the migration (and owns the tables), so a
test connecting as it would read everything and prove nothing. These tests therefore
create their **own non-superuser, non-owner role** and query as it — that is the only
way to exercise RLS truthfully.

PostgreSQL only; skipped on SQLite (Standalone is single-tenant, no RLS).

Run locally:

    docker run -d -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:16
    export DATABASE_URL=postgresql+asyncpg://postgres:pw@localhost:5432/postgres
    .venv/bin/alembic upgrade head
    .venv/bin/pytest tests/integration/test_rls_tenant_isolation.py -v
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.utils import seed_organizations

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

# Unique per worker process so parallel runs (pytest-xdist) don't collide on the
# shared role name (CREATE ROLE is not idempotent).
_LIMITED_ROLE = f"fm_rls_test_{uuid4().hex[:8]}"
_LIMITED_PW = "fm_rls_test_pw"
# A role that holds GRANTed privileges cannot be DROPped directly; DROP OWNED BY
# revokes them first. Guarded by existence so it is safe as both pre-clean + teardown.
_DROP_ROLE_SQL = f"""
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_LIMITED_ROLE}') THEN
    DROP OWNED BY {_LIMITED_ROLE};
    DROP ROLE {_LIMITED_ROLE};
  END IF;
END $$;
"""
_ORGS_QUERY = (
    "SELECT organization_id FROM organizations WHERE organization_id IN (:a, :b)"
)


@pytest.fixture
async def superuser_engine():
    """Engine as the migration/superuser role (owns the tables — bypasses RLS)."""
    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    assert engine.dialect.name == "postgresql"
    yield engine
    await engine.dispose()


@pytest.fixture
async def two_orgs(superuser_engine):
    """Seed two organizations (as superuser, which bypasses RLS for the insert)."""
    org_a = f"org_a_{uuid4().hex[:8]}"
    org_b = f"org_b_{uuid4().hex[:8]}"
    maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with maker() as session:
        await seed_organizations(session, [org_a, org_b])
    yield org_a, org_b
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM organizations WHERE organization_id IN (:a, :b)"),
            {"a": org_a, "b": org_b},
        )


@pytest.fixture
async def limited_engine(superuser_engine):
    """A non-superuser, non-owner role + an engine connecting as it (RLS applies)."""
    async with superuser_engine.begin() as conn:
        dbname = (await conn.exec_driver_sql("SELECT current_database()")).scalar()
        await conn.exec_driver_sql(_DROP_ROLE_SQL)
        await conn.exec_driver_sql(
            f"CREATE ROLE {_LIMITED_ROLE} LOGIN PASSWORD '{_LIMITED_PW}' NOSUPERUSER"
        )
        await conn.exec_driver_sql(
            f'GRANT CONNECT ON DATABASE "{dbname}" TO {_LIMITED_ROLE}'
        )
        await conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {_LIMITED_ROLE}")
        await conn.exec_driver_sql(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
            f"TO {_LIMITED_ROLE}"
        )

    limited_url = make_url(os.environ["DATABASE_URL"]).set(
        username=_LIMITED_ROLE, password=_LIMITED_PW
    )
    engine = create_async_engine(limited_url, future=True)
    yield engine
    await engine.dispose()
    async with superuser_engine.begin() as conn:
        await conn.exec_driver_sql(_DROP_ROLE_SQL)


@pytest.mark.asyncio
async def test_rls_scopes_reads_to_current_org(limited_engine, two_orgs):
    """With ``app.current_org_id`` set, the limited role sees only that org's row."""
    org_a, org_b = two_orgs
    maker = async_sessionmaker(limited_engine, expire_on_commit=False)

    async with maker() as session:
        await session.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": org_a}
        )
        rows = (
            (await session.execute(text(_ORGS_QUERY), {"a": org_a, "b": org_b}))
            .scalars()
            .all()
        )
        assert set(rows) == {org_a}, "RLS leaked another org's row"

    async with maker() as session:
        await session.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": org_b}
        )
        rows = (
            (await session.execute(text(_ORGS_QUERY), {"a": org_a, "b": org_b}))
            .scalars()
            .all()
        )
        assert set(rows) == {org_b}, "RLS leaked another org's row"


@pytest.mark.asyncio
@pytest.mark.security
async def test_rls_blocks_reads_without_context(limited_engine, two_orgs):
    """Fail-closed: no ``app.current_org_id`` set → zero rows for the limited role."""
    org_a, org_b = two_orgs
    maker = async_sessionmaker(limited_engine, expire_on_commit=False)
    async with maker() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM organizations "
                    "WHERE organization_id IN (:a, :b)"
                ),
                {"a": org_a, "b": org_b},
            )
        ).scalar()
        assert count == 0, "RLS leak: rows visible with no tenant context set"


@pytest.mark.asyncio
@pytest.mark.security
async def test_login_by_username_resolves_under_limited_role_without_org_context(
    limited_engine, superuser_engine
):
    """Pre-auth login reads ``users`` before any org context is established.

    ``users`` is deliberately NOT in the RLS-tenanted set (migration 018 — it
    is keyed by ``enterprise_id``, not ``organization_id``), so the login
    lookup must resolve as the limited, non-owner ``faultmaven_app``-style role
    even with NO / a mismatched ``app.current_org_id``. This pins the invariant
    that the #703 per-operation RLS scoping (a fresh ``get_db_session()`` per
    op re-applies ``app.current_org_id``) does not gate the pre-auth login path.

    Runs the REAL login code path: ``PostgreSQLUserRepository.get_by_username``.
    """
    from datetime import datetime, timezone

    from faultmaven.infrastructure.persistence.user_repository import (
        PostgreSQLUserRepository,
        User,
    )
    from tests.utils import seed_default_enterprise

    uid = f"login_user_{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    # Seed as superuser (bypasses RLS). Use a real email TLD — the domain User
    # model's EmailStr rejects reserved TLDs like `.local`, and get_by_username
    # hydrates the row back into User on read.
    su_maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with su_maker() as session:
        await seed_default_enterprise(session)
        await PostgreSQLUserRepository(session).save(
            User(
                user_id=uid,
                username=uid,
                email=f"{uid}@example.com",
                display_name=uid,
                created_at=now,
                updated_at=now,
                roles=["user"],
            )
        )

    try:
        maker = async_sessionmaker(limited_engine, expire_on_commit=False)

        # (a) No org context set at all — the true pre-auth state. A tenanted
        #     table would be fail-closed here; users must still resolve.
        async with maker() as session:
            user = await PostgreSQLUserRepository(session).get_by_username(uid)
            assert user is not None and user.username == uid, (
                "login-by-username must resolve under the limited role with no "
                "org context (users is not tenant-scoped)"
            )

        # (b) A mismatched org context set — the login read must still resolve,
        #     proving users is not filtered by app.current_org_id.
        async with maker() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :o, true)"),
                {"o": f"org_absent_{uuid4().hex[:8]}"},
            )
            user = await PostgreSQLUserRepository(session).get_by_username(uid)
            assert (
                user is not None
            ), "users read must not be tenant-filtered under a mismatched org scope"
    finally:
        async with su_maker() as session:
            await session.execute(
                text("DELETE FROM users WHERE user_id = :u"), {"u": uid}
            )
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.security
async def test_rls_scopes_team_members_to_current_org(
    limited_engine, superuser_engine, two_orgs
):
    """team_members has no organization_id; its policy scopes via teams (030).

    A membership row is visible only when its team belongs to the connection's
    current organization; cross-org membership rows are invisible, and with no
    context set the table is fail-closed.
    """
    from tests.utils import seed_users

    org_a, org_b = two_orgs
    team_a, team_b = f"team_a_{uuid4().hex[:8]}", f"team_b_{uuid4().hex[:8]}"
    user_a, user_b = f"tm_user_a_{uuid4().hex[:8]}", f"tm_user_b_{uuid4().hex[:8]}"

    su_maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with su_maker() as session:
        await seed_users(session, [user_a, user_b])
        for tid, oid in ((team_a, org_a), (team_b, org_b)):
            await session.execute(
                text(
                    "INSERT INTO teams (team_id, organization_id, name) "
                    "VALUES (:t, :o, :n)"
                ),
                {"t": tid, "o": oid, "n": tid},
            )
        for uid, tid in ((user_a, team_a), (user_b, team_b)):
            await session.execute(
                text("INSERT INTO team_members (user_id, team_id) VALUES (:u, :t)"),
                {"u": uid, "t": tid},
            )
        await session.commit()

    tm_query = "SELECT team_id FROM team_members WHERE team_id IN (:a, :b)"
    try:
        maker = async_sessionmaker(limited_engine, expire_on_commit=False)
        # Scoped to org_a -> only org_a's team's membership row is visible.
        async with maker() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :o, true)"), {"o": org_a}
            )
            rows = (
                (await session.execute(text(tm_query), {"a": team_a, "b": team_b}))
                .scalars()
                .all()
            )
            assert set(rows) == {team_a}, "RLS leaked a cross-org membership row"

        # Fail-closed: no org context -> no membership rows.
        async with maker() as session:
            count = (
                await session.execute(
                    text("SELECT count(*) FROM team_members WHERE team_id IN (:a, :b)"),
                    {"a": team_a, "b": team_b},
                )
            ).scalar()
            assert count == 0, "RLS leak: team_members visible with no tenant context"
    finally:
        async with su_maker() as session:
            await session.execute(
                text("DELETE FROM team_members WHERE team_id IN (:a, :b)"),
                {"a": team_a, "b": team_b},
            )
            await session.execute(
                text("DELETE FROM teams WHERE team_id IN (:a, :b)"),
                {"a": team_a, "b": team_b},
            )
            await session.execute(
                text("DELETE FROM users WHERE user_id IN (:a, :b)"),
                {"a": user_a, "b": user_b},
            )
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.security
async def test_rls_role_guard_rejects_superuser_and_passes_limited(
    limited_engine, superuser_engine
):
    """The startup guard fails closed for an RLS-exempt role, passes a limited one."""
    from faultmaven.config.deployment_coherence import DeploymentCoherenceError
    from faultmaven.infrastructure.persistence.rls_role_guard import (
        assert_app_db_role_enforces_rls,
    )

    # Superuser / table-owner role bypasses RLS -> refuse to boot.
    with pytest.raises(DeploymentCoherenceError):
        await assert_app_db_role_enforces_rls(
            is_multi_tenant=True, engine=superuser_engine
        )

    # Non-superuser, non-owner role enforces RLS -> passes.
    await assert_app_db_role_enforces_rls(is_multi_tenant=True, engine=limited_engine)

    # Single-tenant is a no-op even on the RLS-exempt engine.
    await assert_app_db_role_enforces_rls(
        is_multi_tenant=False, engine=superuser_engine
    )


@pytest.mark.asyncio
@pytest.mark.security
async def test_rls_role_guard_rejects_bypassrls_role(superuser_engine):
    """A non-superuser, non-owner role with BYPASSRLS is still RLS-exempt.

    BYPASSRLS is PostgreSQL's third RLS-exemption mechanism (besides SUPERUSER
    and table ownership); the guard must reject it too or it gives false
    assurance while cross-tenant reads leak.
    """
    from faultmaven.config.deployment_coherence import DeploymentCoherenceError
    from faultmaven.infrastructure.persistence.rls_role_guard import (
        assert_app_db_role_enforces_rls,
    )

    role = f"fm_rls_bypass_{uuid4().hex[:8]}"
    drop_sql = (
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
        f"THEN DROP OWNED BY {role}; DROP ROLE {role}; END IF; END $$;"
    )
    async with superuser_engine.begin() as conn:
        dbname = (await conn.exec_driver_sql("SELECT current_database()")).scalar()
        await conn.exec_driver_sql(drop_sql)
        await conn.exec_driver_sql(
            f"CREATE ROLE {role} LOGIN PASSWORD '{_LIMITED_PW}' "
            "NOSUPERUSER BYPASSRLS"
        )
        await conn.exec_driver_sql(f'GRANT CONNECT ON DATABASE "{dbname}" TO {role}')
        await conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")

    url = make_url(os.environ["DATABASE_URL"]).set(username=role, password=_LIMITED_PW)
    engine = create_async_engine(url, future=True)
    try:
        with pytest.raises(DeploymentCoherenceError, match="bypassrls=True"):
            await assert_app_db_role_enforces_rls(is_multi_tenant=True, engine=engine)
    finally:
        await engine.dispose()
        async with superuser_engine.begin() as conn:
            await conn.exec_driver_sql(drop_sql)


@pytest.mark.asyncio
async def test_rls_enabled_and_policy_present(superuser_engine):
    """Structural: RLS is enabled and the tenant-isolation policy exists on a
    representative sample of tenanted tables."""
    maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with maker() as session:
        for table in (
            "cases",
            "organizations",
            "evidence",
            "knowledge_items",
            # causal-graph tables enrolled by migration 023 (added after 018)
            "causal_nodes",
            "causal_edges",
            "causal_node_evidence",
            # polymorphic share table enrolled by migration 028 (ADR-013 §D4)
            "resource_shares",
            # team_members enrolled by migration 030 (subquery policy via teams)
            "team_members",
        ):
            enabled = (
                await session.execute(
                    text("SELECT relrowsecurity FROM pg_class WHERE relname = :t"),
                    {"t": table},
                )
            ).scalar()
            assert enabled is True, f"RLS not enabled on {table}"

            polname = (
                await session.execute(
                    text(
                        "SELECT p.polname FROM pg_policy p "
                        "JOIN pg_class c ON p.polrelid = c.oid WHERE c.relname = :t"
                    ),
                    {"t": table},
                )
            ).scalar()
            assert (
                polname == f"{table}_tenant_isolation"
            ), f"missing tenant-isolation policy on {table}"
