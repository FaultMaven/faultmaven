"""PostgreSQL Row-Level Security tenant-isolation tests (ADR-017).

Proves the RLS policies actually block cross-**enterprise** reads and writes.
The enterprise is the isolation boundary: every scoped table carries
``enterprise_id``, every policy keys on ``app.current_enterprise_id``, and the
organization beside it is billing attribution the policies never read.

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

from tests.utils import seed_enterprises

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
_ENTERPRISES_QUERY = (
    "SELECT enterprise_id FROM enterprises WHERE enterprise_id IN (:a, :b)"
)
#: ``enterprises`` itself is NOT tenant-scoped — it is the parent tier, and the
#: rows a session may see of it are decided by nothing. The scoped table this
#: module reads through is ``teams``, which every arm below seeds one of per
#: enterprise. Using a real scoped table rather than the tier table is what makes
#: the assertions about the policies rather than about a table with none.
_TEAMS_QUERY = "SELECT enterprise_id FROM teams WHERE team_id IN (:a, :b)"


@pytest.fixture
async def superuser_engine():
    """Engine as the migration/superuser role (owns the tables — bypasses RLS)."""
    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    assert engine.dialect.name == "postgresql"
    yield engine
    await engine.dispose()


@pytest.fixture
async def two_enterprises(superuser_engine):
    """Seed two enterprises + a team in each (as superuser, which bypasses RLS).

    The team is what carries the tenant key on a scoped table; the enterprise row
    is only its parent. Yields ``(enterprise_a, enterprise_b, team_a, team_b)``.
    """
    ent_a = f"ent_a_{uuid4().hex[:8]}"
    ent_b = f"ent_b_{uuid4().hex[:8]}"
    team_a = f"team_a_{uuid4().hex[:8]}"
    team_b = f"team_b_{uuid4().hex[:8]}"
    maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with maker() as session:
        await seed_enterprises(session, [ent_a, ent_b])
        for team_id, enterprise_id in ((team_a, ent_a), (team_b, ent_b)):
            await session.execute(
                text(
                    "INSERT INTO teams (team_id, enterprise_id, name) "
                    "VALUES (:t, :e, :n)"
                ),
                {"t": team_id, "e": enterprise_id, "n": team_id},
            )
        await session.commit()
    yield ent_a, ent_b, team_a, team_b
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM teams WHERE team_id IN (:a, :b)"),
            {"a": team_a, "b": team_b},
        )
        await conn.execute(
            text("DELETE FROM enterprises WHERE enterprise_id IN (:a, :b)"),
            {"a": ent_a, "b": ent_b},
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


async def _bind(session, enterprise_id) -> None:
    """Bind the session to an enterprise, exactly as the request front door will."""
    await session.execute(
        text("SELECT set_config('app.current_enterprise_id', :e, true)"),
        {"e": enterprise_id},
    )


@pytest.mark.asyncio
async def test_rls_scopes_reads_to_current_enterprise(limited_engine, two_enterprises):
    """With ``app.current_enterprise_id`` set, the limited role sees only its own."""
    ent_a, ent_b, team_a, team_b = two_enterprises
    maker = async_sessionmaker(limited_engine, expire_on_commit=False)

    for bound, expected in ((ent_a, ent_a), (ent_b, ent_b)):
        async with maker() as session:
            await _bind(session, bound)
            rows = (
                (await session.execute(text(_TEAMS_QUERY), {"a": team_a, "b": team_b}))
                .scalars()
                .all()
            )
            assert set(rows) == {expected}, "RLS leaked another enterprise's row"


@pytest.mark.asyncio
@pytest.mark.security
async def test_rls_blocks_reads_without_context(limited_engine, two_enterprises):
    """Fail-closed: no ``app.current_enterprise_id`` → zero rows.

    ``current_setting(..., true)`` is NULL when nothing is bound, every
    comparison against it is NULL, and no row matches. This is the property the
    binder's fail-closed refusal rests on.
    """
    _ent_a, _ent_b, team_a, team_b = two_enterprises
    maker = async_sessionmaker(limited_engine, expire_on_commit=False)
    async with maker() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM teams WHERE team_id IN (:a, :b)"),
                {"a": team_a, "b": team_b},
            )
        ).scalar()
        assert count == 0, "RLS leak: rows visible with no tenant context set"


@pytest.mark.asyncio
@pytest.mark.security
async def test_rls_rejects_a_write_stamped_with_another_enterprise(
    limited_engine, two_enterprises
):
    """The WITH CHECK half: a foreign-stamped INSERT is refused, not hidden.

    The policies carry no ``FOR`` clause, so PostgreSQL applies the USING
    expression as the WITH CHECK on writes too. Without that, a session bound to
    A could plant a row stamped B — invisible to A afterwards, and perfectly
    visible to B. The positive control in the same test is the identically
    shaped INSERT stamped with the session's own enterprise.
    """
    from sqlalchemy.exc import DBAPIError

    ent_a, ent_b, _team_a, _team_b = two_enterprises
    insert = text(
        "INSERT INTO teams (team_id, enterprise_id, name) VALUES (:t, :e, :n)"
    )
    maker = async_sessionmaker(limited_engine, expire_on_commit=False)

    async with maker() as session:
        await _bind(session, ent_a)
        with pytest.raises(DBAPIError, match="row-level security"):
            await session.execute(
                insert,
                {"t": f"planted_{uuid4().hex[:8]}", "e": ent_b, "n": "planted"},
            )
            await session.commit()

    own = f"own_{uuid4().hex[:8]}"
    async with maker() as session:
        await _bind(session, ent_a)
        await session.execute(insert, {"t": own, "e": ent_a, "n": own})
        await session.commit()

    async with maker() as session:
        await _bind(session, ent_a)
        assert (
            await session.execute(
                text("SELECT count(*) FROM teams WHERE team_id = :t"), {"t": own}
            )
        ).scalar() == 1, (
            "the positive control failed: the refusal above proves nothing if an "
            "own-enterprise INSERT is refused too"
        )
        await session.execute(text("DELETE FROM teams WHERE team_id = :t"), {"t": own})
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.security
async def test_login_by_username_resolves_under_limited_role_without_context(
    limited_engine, superuser_engine
):
    """Pre-auth login reads ``users`` before any enterprise is bound.

    ``users`` is deliberately NOT in the RLS-enrolled set — it is the account
    table the login resolves *before* it knows which enterprise to bind — so the
    lookup must resolve as the limited, non-owner ``faultmaven_app``-style role
    even with NO / a mismatched ``app.current_enterprise_id``.

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

        # (a) No enterprise context set at all — the true pre-auth state. A
        #     scoped table would be fail-closed here; users must still resolve.
        async with maker() as session:
            user = await PostgreSQLUserRepository(session).get_by_username(uid)
            assert user is not None and user.username == uid, (
                "login-by-username must resolve under the limited role with no "
                "enterprise context (users is not tenant-scoped)"
            )

        # (b) A mismatched enterprise context set — the login read must still
        #     resolve, proving users is not filtered by the session GUC.
        async with maker() as session:
            await _bind(session, f"ent_absent_{uuid4().hex[:8]}")
            user = await PostgreSQLUserRepository(session).get_by_username(uid)
            assert (
                user is not None
            ), "users read must not be tenant-filtered under a mismatched scope"
    finally:
        async with su_maker() as session:
            await session.execute(
                text("DELETE FROM users WHERE user_id = :u"), {"u": uid}
            )
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.security
async def test_rls_scopes_team_members_by_the_hop_through_teams(
    limited_engine, superuser_engine, two_enterprises
):
    """``team_members`` has no ``enterprise_id``; its policy hops via ``teams``.

    A membership row is visible only when its team belongs to the connection's
    current enterprise; cross-enterprise membership rows are invisible, and with
    no context set the table is fail-closed. The hop's target is
    ``teams.enterprise_id``, and the ``teams`` subquery is itself scoped by the
    same predicate — so the two policies agree by construction.
    """
    from tests.utils import seed_users

    ent_a, _ent_b, team_a, team_b = two_enterprises
    user_a, user_b = f"tm_user_a_{uuid4().hex[:8]}", f"tm_user_b_{uuid4().hex[:8]}"

    su_maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with su_maker() as session:
        await seed_users(session, [user_a, user_b])
        for uid, tid in ((user_a, team_a), (user_b, team_b)):
            await session.execute(
                text("INSERT INTO team_members (user_id, team_id) VALUES (:u, :t)"),
                {"u": uid, "t": tid},
            )
        await session.commit()

    tm_query = "SELECT team_id FROM team_members WHERE team_id IN (:a, :b)"
    try:
        maker = async_sessionmaker(limited_engine, expire_on_commit=False)
        # Scoped to enterprise A -> only A's team's membership row is visible.
        async with maker() as session:
            await _bind(session, ent_a)
            rows = (
                (await session.execute(text(tm_query), {"a": team_a, "b": team_b}))
                .scalars()
                .all()
            )
            assert set(rows) == {
                team_a
            }, "RLS leaked a membership row from another enterprise"

        # Fail-closed: no enterprise context -> no membership rows.
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
    """Structural: RLS is enabled and the enterprise-keyed policy exists.

    ``knowledge_items`` is the exception: four per-command policies, so the
    global-tier read exemption cannot double as a tenant write/delete license.
    """
    _KNOWLEDGE_ITEMS_POLICIES = {
        "knowledge_items_tenant_read",
        "knowledge_items_tenant_insert",
        "knowledge_items_tenant_update",
        "knowledge_items_tenant_delete",
    }
    maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with maker() as session:
        for table in (
            "cases",
            "organizations",
            "evidence",
            "knowledge_items",
            "causal_nodes",
            "causal_edges",
            "causal_node_evidence",
            "resource_shares",
            "teams",
            # the consent record a team forms by (ADR-017 D4)
            "team_invitations",
            # the turn ledger, re-keyed on a billing subject (ADR-017 D5)
            "turn_usage",
            # no enterprise_id of its own: one hop through teams
            "team_members",
        ):
            enabled = (
                await session.execute(
                    text("SELECT relrowsecurity FROM pg_class WHERE relname = :t"),
                    {"t": table},
                )
            ).scalar()
            assert enabled is True, f"RLS not enabled on {table}"

            polnames = set(
                (
                    await session.execute(
                        text(
                            "SELECT p.polname FROM pg_policy p "
                            "JOIN pg_class c ON p.polrelid = c.oid WHERE c.relname = :t"
                        ),
                        {"t": table},
                    )
                )
                .scalars()
                .all()
            )
            if table == "knowledge_items":
                assert polnames == _KNOWLEDGE_ITEMS_POLICIES, (
                    "knowledge_items must carry exactly the four per-command "
                    f"platform-tier policies, got {polnames}"
                )
            else:
                assert polnames == {
                    f"{table}_tenant_isolation"
                }, f"missing tenant-isolation policy on {table}, got {polnames}"


@pytest.mark.asyncio
async def test_no_policy_reads_the_retired_organization_binding(superuser_engine):
    """Not one policy still keys on the organization.

    A single surviving ``app.current_org_id`` reference would be a table nobody
    can read once the binder stops setting that GUC — fail-closed, but silently
    and only for that table. Asserted over the whole catalog rather than the
    sample above, because the failure this catches is one table nobody listed.
    """
    maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with maker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT tablename, policyname, "
                    "coalesce(qual, '') || ' ' || coalesce(with_check, '') "
                    "FROM pg_policies WHERE schemaname = 'public'"
                )
            )
        ).all()
    assert rows, "no policies at all — the migration did not run"
    org_keyed = [f"{t}.{p}" for t, p, body in rows if "app.current_org_id" in body]
    assert org_keyed == [], f"policies still keyed on the organization: {org_keyed}"
    unkeyed = [
        f"{t}.{p}" for t, p, body in rows if "app.current_enterprise_id" not in body
    ]
    assert unkeyed == [], f"policies not keyed on the enterprise: {unkeyed}"


# =============================================================================
# Global-tier KB platform corpus (#770, re-keyed by ADR-017)
# =============================================================================

_KI_INSERT = text(
    "INSERT INTO knowledge_items "
    "(item_id, enterprise_id, organization_id, scope, title, content, item_type) "
    "VALUES (:i, :e, :org, :s, :t, :c, 'runbook')"
)
_KI_DELETE = text("DELETE FROM knowledge_items WHERE item_id = :i")


@pytest.fixture
async def kb_rows(superuser_engine, two_enterprises):
    """One platform-tier global row + one enterprise-A personal row."""
    from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID

    ent_a, ent_b, _team_a, _team_b = two_enterprises
    global_id = f"kb_{uuid4().hex[:12]}"
    personal_id = f"ki_{uuid4().hex[:8]}"
    su_maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with su_maker() as session:
        await session.execute(
            _KI_INSERT,
            {
                "i": global_id,
                "e": STANDALONE_ENTERPRISE_ID,
                "org": None,
                "s": "global",
                "t": "G",
                "c": "body",
            },
        )
        await session.execute(
            _KI_INSERT,
            {
                "i": personal_id,
                "e": ent_a,
                "org": None,
                "s": "personal",
                "t": "P",
                "c": "body",
            },
        )
        await session.commit()
    yield ent_a, ent_b, global_id, personal_id
    async with su_maker() as session:
        await session.execute(_KI_DELETE, {"i": global_id})
        await session.execute(_KI_DELETE, {"i": personal_id})
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.security
async def test_global_kb_readable_by_every_enterprise(limited_engine, kb_rows):
    """The platform tier is readable from ANY enterprise context — the #770 read
    exemption — while enterprise-owned rows stay isolated."""
    ent_a, ent_b, global_id, personal_id = kb_rows
    query = text("SELECT item_id FROM knowledge_items WHERE item_id IN (:g, :p)")
    maker = async_sessionmaker(limited_engine, expire_on_commit=False)

    async with maker() as session:
        await _bind(session, ent_a)
        rows = (
            (await session.execute(query, {"g": global_id, "p": personal_id}))
            .scalars()
            .all()
        )
        assert set(rows) == {global_id, personal_id}

    async with maker() as session:
        await _bind(session, ent_b)
        rows = (
            (await session.execute(query, {"g": global_id, "p": personal_id}))
            .scalars()
            .all()
        )
        assert set(rows) == {
            global_id
        }, "enterprise B must see the platform tier but never A's personal row"


@pytest.mark.asyncio
@pytest.mark.security
async def test_tenant_session_cannot_write_platform_tier(limited_engine, kb_rows):
    """A tenant-bound session can neither INSERT, UPDATE, nor DELETE global
    rows — the read exemption must not double as a write license (#770 I2)."""
    from sqlalchemy.exc import DBAPIError

    from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID

    ent_a, _ent_b, global_id, _personal_id = kb_rows
    maker = async_sessionmaker(limited_engine, expire_on_commit=False)

    # INSERT a platform-tier row from a tenant context → policy violation.
    async with maker() as session:
        await _bind(session, ent_a)
        with pytest.raises(DBAPIError, match="row-level security"):
            await session.execute(
                _KI_INSERT,
                {
                    "i": f"kb_{uuid4().hex[:12]}",
                    "e": STANDALONE_ENTERPRISE_ID,
                    "org": None,
                    "s": "global",
                    "t": "X",
                    "c": "x",
                },
            )
            await session.commit()

    # UPDATE of an existing global row silently matches zero rows.
    async with maker() as session:
        await _bind(session, ent_a)
        result = await session.execute(
            text("UPDATE knowledge_items SET title = 'own3d' WHERE item_id = :i"),
            {"i": global_id},
        )
        await session.commit()
        assert result.rowcount == 0, "tenant session updated a platform-tier row"

    # DELETE of an existing global row silently matches zero rows.
    async with maker() as session:
        await _bind(session, ent_a)
        result = await session.execute(_KI_DELETE, {"i": global_id})
        await session.commit()
        assert result.rowcount == 0, "tenant session deleted a platform-tier row"


@pytest.mark.asyncio
@pytest.mark.security
async def test_tenant_cannot_publish_a_global_row_billed_to_an_organization(
    limited_engine, kb_rows, superuser_engine
):
    """``knowledge_items_global_org_check`` is what closes the last hole.

    A tenant stamping ``scope='global'`` with its OWN enterprise passes the
    policy's own-enterprise arm — the platform tier is readable by everyone, so a
    row a tenant could plant there would be readable by everyone too. The CHECK
    refuses the one shape that would make it a *billed* global row.
    """
    from sqlalchemy.exc import DBAPIError, IntegrityError

    from tests.utils import seed_organizations

    ent_a, _ent_b, _global_id, _personal_id = kb_rows
    org_id = f"org_{uuid4().hex[:8]}"
    su_maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with su_maker() as session:
        await seed_organizations(session, [org_id], enterprise_id=ent_a)

    try:
        maker = async_sessionmaker(limited_engine, expire_on_commit=False)
        async with maker() as session:
            await _bind(session, ent_a)
            with pytest.raises((IntegrityError, DBAPIError), match="global_org_check"):
                await session.execute(
                    _KI_INSERT,
                    {
                        "i": f"kb_{uuid4().hex[:12]}",
                        "e": ent_a,
                        "org": org_id,
                        "s": "global",
                        "t": "X",
                        "c": "x",
                    },
                )
                await session.commit()
    finally:
        async with su_maker() as session:
            await session.execute(
                text("DELETE FROM organizations WHERE organization_id = :o"),
                {"o": org_id},
            )
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.security
async def test_standalone_enterprise_session_can_maintain_platform_tier(
    limited_engine,
):
    """A session bound to the STANDALONE enterprise (standalone and cloud+single
    — today's production shape) CAN insert/update/delete global rows through the
    limited app role: this is the KB pack bootstrap path. Under multi no tenant
    session ever binds that sentinel (fail-closed request binder), so this arm is
    unreachable for tenants."""
    from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID

    item_id = f"kb_{uuid4().hex[:12]}"
    maker = async_sessionmaker(limited_engine, expire_on_commit=False)
    async with maker() as session:
        await _bind(session, STANDALONE_ENTERPRISE_ID)
        await session.execute(
            _KI_INSERT,
            {
                "i": item_id,
                "e": STANDALONE_ENTERPRISE_ID,
                "org": None,
                "s": "global",
                "t": "G",
                "c": "body",
            },
        )
        await session.commit()

    async with maker() as session:
        await _bind(session, STANDALONE_ENTERPRISE_ID)
        result = await session.execute(
            text("UPDATE knowledge_items SET title = 'G2' WHERE item_id = :i"),
            {"i": item_id},
        )
        assert result.rowcount == 1
        result = await session.execute(_KI_DELETE, {"i": item_id})
        assert result.rowcount == 1
        await session.commit()
