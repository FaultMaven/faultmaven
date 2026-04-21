"""Integration tests: PostgreSQL Row-Level Security tenant isolation.

Storage redesign 2026-04 — Phase 8.

These tests prove that RLS policies created in the Phase 8 migration
enforce tenant isolation at the database level — even a session that
forgets to set ``app.current_org_id`` cannot read tenanted rows. This is
the failure mode RLS guards against (e.g., a code path that drops the
``WHERE organization_id = ?`` clause by mistake).

PostgreSQL-only. SQLite is skipped because RLS is a PG-only feature; the
Phase 8 migration is a documented no-op on SQLite.

Wire-in status: the production middleware (``set_tenant_context`` in
``faultmaven/api/middleware/tenant_isolation.py``) is implemented but
not yet wired into the request lifecycle. These tests exercise the SQL
layer directly to validate the policy semantics independent of the
wire-in.

Currently skipped pending CI PostgreSQL infrastructure. To run locally
against a PG instance::

    DATABASE_URL=postgresql+asyncpg://user:pw@localhost:5432/test \\
        pytest tests/integration/test_rls_tenant_isolation.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="RLS is PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]


@pytest.fixture
async def pg_session():
    """Provide a raw async session against PostgreSQL for RLS tests.

    The session has RLS enforcement active (no BYPASSRLS role here). RLS
    visibility is controlled per-test by ``SET LOCAL app.current_org_id``.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    db_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(db_url, future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        yield session

    await engine.dispose()


@pytest.fixture
async def seeded_cases(pg_session):
    """Insert two cases under two different organizations.

    Uses an elevated path: temporarily disables RLS at the table level
    via the migration role assumption — actually, since this fixture
    runs inside the same RLS-enforcing role we use for the test, the
    INSERT must include an ``organization_id`` and we must SET LOCAL
    the matching org for the row to be inserted (RLS enforces both
    SELECT and INSERT/UPDATE/DELETE depending on policy form).

    Our policy is SELECT-only (USING clause without WITH CHECK), so
    INSERTs succeed regardless of the GUC. We rely on this to seed
    rows for both orgs from a single test session.
    """
    org_a = f"org_a_{uuid.uuid4().hex[:8]}"
    org_b = f"org_b_{uuid.uuid4().hex[:8]}"
    case_a = f"case_a_{uuid.uuid4().hex[:8]}"
    case_b = f"case_b_{uuid.uuid4().hex[:8]}"

    # Insert one case per org. Minimal columns — model has many NOT NULL
    # but we use only what the migration baseline requires for cases.
    # If the schema has additional NOT NULL columns without defaults,
    # this fixture must be expanded.
    for case_id, org in [(case_a, org_a), (case_b, org_b)]:
        await pg_session.execute(
            text(
                """
                INSERT INTO cases (case_id, organization_id, status, title, created_at, updated_at)
                VALUES (:case_id, :org_id, 'inquiry', 'rls test case', NOW(), NOW())
                """
            ),
            {"case_id": case_id, "org_id": org},
        )
    await pg_session.commit()

    yield {"org_a": org_a, "org_b": org_b, "case_a": case_a, "case_b": case_b}

    # Teardown — delete rows we created. Need RLS visibility for
    # DELETE; do it under each org context.
    for org, case_id in [(org_a, case_a), (org_b, case_b)]:
        await pg_session.execute(
            text("SET LOCAL app.current_org_id = :org"), {"org": org}
        )
        await pg_session.execute(
            text("DELETE FROM cases WHERE case_id = :cid"), {"cid": case_id}
        )
        await pg_session.commit()


async def test_rls_blocks_unset_context(pg_session, seeded_cases):
    """RLS test: a session without ``app.current_org_id`` returns zero rows.

    This is the central security guarantee. If an application code path
    forgets to set the GUC (e.g., a forgotten middleware call), the
    database itself returns no rows from any tenanted table — preventing
    accidental cross-tenant disclosure.
    """
    # Explicitly do NOT set app.current_org_id. current_setting(.., true)
    # returns NULL when unset; RLS USING clause evaluates `org_id = NULL`
    # which is always NULL → no rows visible.
    result = await pg_session.execute(
        text("SELECT COUNT(*) FROM cases WHERE case_id IN (:a, :b)"),
        {"a": seeded_cases["case_a"], "b": seeded_cases["case_b"]},
    )
    count = result.scalar()
    assert (
        count == 0
    ), f"RLS leak: query returned {count} rows without app.current_org_id set"


async def test_rls_allows_correct_org(pg_session, seeded_cases):
    """RLS test: setting ``app.current_org_id`` reveals only that org's rows.

    With ``app.current_org_id = org_a`` set, a query across both seeded
    cases must see ``case_a`` and not ``case_b``.
    """
    await pg_session.execute(
        text("SET LOCAL app.current_org_id = :org"),
        {"org": seeded_cases["org_a"]},
    )

    result = await pg_session.execute(
        text("SELECT case_id FROM cases WHERE case_id IN (:a, :b)"),
        {"a": seeded_cases["case_a"], "b": seeded_cases["case_b"]},
    )
    visible = {row[0] for row in result.fetchall()}

    assert seeded_cases["case_a"] in visible, "RLS over-blocked: own org's case hidden"
    assert (
        seeded_cases["case_b"] not in visible
    ), "RLS leak: other org's case visible to org_a"


async def test_rls_blocks_wrong_org(pg_session, seeded_cases):
    """RLS test: setting an unrelated org reveals zero rows.

    Defense check that the policy is keyed on equality, not on
    set-membership or any wildcard."""
    bogus_org = f"bogus_{uuid.uuid4().hex[:8]}"
    await pg_session.execute(
        text("SET LOCAL app.current_org_id = :org"), {"org": bogus_org}
    )

    result = await pg_session.execute(
        text("SELECT COUNT(*) FROM cases WHERE case_id IN (:a, :b)"),
        {"a": seeded_cases["case_a"], "b": seeded_cases["case_b"]},
    )
    count = result.scalar()
    assert count == 0, f"RLS leak: bogus org saw {count} rows"


async def test_rls_enabled_on_all_tenanted_tables(pg_session):
    """Sanity check: RLS is ENABLED on every tenanted table from Phase 8.

    Reads ``pg_class.relrowsecurity`` to confirm ``ALTER TABLE ...
    ENABLE ROW LEVEL SECURITY`` succeeded for every tenanted table.
    """
    expected_tables = {
        "cases",
        "case_messages",
        "case_actions",
        "case_tags",
        "case_checkpoints",
        "evidence",
        "hypotheses",
        "solutions",
        "uploaded_files",
        "investigation_sessions",
        "agent_executions",
        "agent_tool_calls",
        "knowledge_items",
        "knowledge_suggestions",
        "conversion_jobs",
        "user_audit_log",
    }

    result = await pg_session.execute(
        text(
            """
            SELECT relname
            FROM pg_class
            WHERE relname = ANY(:tables) AND relrowsecurity = true
            """
        ),
        {"tables": list(expected_tables)},
    )
    rls_enabled = {row[0] for row in result.fetchall()}
    missing = expected_tables - rls_enabled
    assert not missing, f"RLS not enabled on these tenanted tables: {missing}"


async def test_tenant_isolation_policies_present(pg_session):
    """Sanity check: every tenanted table has its ``<table>_tenant_isolation`` policy.

    Reads ``pg_policies`` to confirm the policy name + table mapping.
    """
    expected_policies = {
        "cases_tenant_isolation": "cases",
        "case_messages_tenant_isolation": "case_messages",
        "case_actions_tenant_isolation": "case_actions",
        "case_tags_tenant_isolation": "case_tags",
        "case_checkpoints_tenant_isolation": "case_checkpoints",
        "evidence_tenant_isolation": "evidence",
        "hypotheses_tenant_isolation": "hypotheses",
        "solutions_tenant_isolation": "solutions",
        "uploaded_files_tenant_isolation": "uploaded_files",
        "investigation_sessions_tenant_isolation": "investigation_sessions",
        "agent_executions_tenant_isolation": "agent_executions",
        "agent_tool_calls_tenant_isolation": "agent_tool_calls",
        "knowledge_items_tenant_isolation": "knowledge_items",
        "knowledge_suggestions_tenant_isolation": "knowledge_suggestions",
        "conversion_jobs_tenant_isolation": "conversion_jobs",
        "user_audit_log_tenant_isolation": "user_audit_log",
    }

    result = await pg_session.execute(
        text(
            "SELECT policyname, tablename FROM pg_policies WHERE policyname = ANY(:names)"
        ),
        {"names": list(expected_policies.keys())},
    )
    found = {row[0]: row[1] for row in result.fetchall()}
    missing = set(expected_policies) - set(found)
    assert not missing, f"Missing RLS policies: {missing}"
    for policy_name, table in expected_policies.items():
        assert (
            found[policy_name] == table
        ), f"Policy {policy_name} attached to wrong table: {found[policy_name]} (expected {table})"
