"""Revision 023 under row-level security, on a real PostgreSQL.

``023_drop_in_flight_revisions`` drops every revised problem statement in
flight at deploy (#2379, ruling 5): ``cases.progress.problem_status`` returns to
the revision's ``prior_status`` and ``problem_verification.pending_revision`` is
removed. ``cases`` is tenant-scoped, so what the UPDATE reaches depends on who
runs it, and only a real database can show that:

* **The owner reaches every enterprise.** The probe migrates as a NON-superuser
  owner — a superuser bypasses row-level security whatever the policy says.
* **A role the policy filters raises** under the revision's
  ``row_security = off`` instead of rewriting no rows and reporting success.

The SQLite half is ``TestDropInFlightRevisionsRevision`` in
``tests/integration/test_alembic_migrations.py``.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.security.conftest import (
    alembic_on,
    create_limited_role,
    database_of_its_own,
    drop_role_sql,
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

#: ``023_drop_in_flight_revisions``.
_REVISION = "cf17b34fc8c4"  # pragma: allowlist secret
#: ``022_knowledge_item_vector_generation``, the revision 023 parents onto.
_PARENT_REVISION = "03f93180f630"  # pragma: allowlist secret
_PW = "fm_drop_revisions_probe_pw"

_HELD = '{"problem_status": "revision_pending", "cause_state": "unknown"}'
_REVISION_PV = (
    '{"symptom_statement": "s", "pending_revision": '
    '{"text": "r", "prior_status": "verified"}}'
)


async def _execute(url: str, *statements: str) -> list:
    """Run ``statements`` in one transaction; return the last one's rows."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            rows: list = []
            for statement in statements:
                result = await conn.execute(text(statement))
                rows = list(result.fetchall()) if result.returns_rows else []
            return rows
    finally:
        await engine.dispose()


async def _seed(url: str) -> tuple:
    """Two enterprises, each with a case holding a revision, and one case with
    none. Returns the enterprises and the tag."""
    tag = uuid.uuid4().hex[:8]
    enterprises = (f"ent_a_{tag}", f"ent_b_{tag}")
    statements = []
    for enterprise in enterprises:
        statements += [
            f"INSERT INTO enterprises (enterprise_id, name, slug) "
            f"VALUES ('{enterprise}', 'Enterprise {enterprise}', '{enterprise}')",
            f"INSERT INTO users (user_id, enterprise_id, username, email, "
            f"display_name) VALUES ('u_{enterprise}', '{enterprise}', "
            f"'u_{enterprise}', 'u_{enterprise}@example.com', 'U')",
            f"INSERT INTO cases (case_id, enterprise_id, user_id, title, progress, "
            f"problem_verification) VALUES ('case_{enterprise}', '{enterprise}', "
            f"'u_{enterprise}', 't', '{_HELD}', '{_REVISION_PV}')",
        ]
    statements.append(
        f"INSERT INTO cases (case_id, enterprise_id, user_id, title, progress, "
        f"problem_verification) VALUES ('case_none_{tag}', '{enterprises[0]}', "
        f"""'u_{enterprises[0]}', 't', '{{"problem_status": "verified"}}', """
        f"""'{{"symptom_statement": "s"}}')"""
    )
    await _execute(url, *statements)
    return enterprises, tag


async def _rows(url: str) -> dict:
    rows = await _execute(
        url, "SELECT case_id, progress::text, problem_verification::text FROM cases"
    )
    return {
        case_id: (json.loads(progress), json.loads(verification))
        for case_id, progress, verification in rows
    }


async def _revision(url: str) -> str:
    ((version,),) = await _execute(url, "SELECT version_num FROM alembic_version")
    return version


async def test_the_owner_drops_every_enterprises_revisions():
    """Migrated by a role that owns the tables and is not a superuser. Both
    enterprises' held cases return to their prior status with no revision;
    the case with none is untouched; the downgrade restores nothing."""
    superuser_url = os.environ["DATABASE_URL"]
    role = f"fm_023_owner_{uuid.uuid4().hex[:8]}"
    await _execute(superuser_url, drop_role_sql(role))
    await _execute(
        superuser_url,
        f"CREATE ROLE {role} LOGIN PASSWORD '{_PW}' NOSUPERUSER NOBYPASSRLS",
    )
    try:
        async with database_of_its_own(
            superuser_url, "fm_023_owner", owner=role
        ) as su_url:
            owner_url = limited_url(su_url, role, _PW)
            result = alembic_on(owner_url, f"upgrade {_PARENT_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            enterprises, tag = await _seed(owner_url)
            before = await _rows(su_url)

            result = alembic_on(owner_url, f"upgrade {_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            assert "dropped in-flight revisions on 2 row(s)" in result.stderr

            rows = await _rows(su_url)
            for enterprise in enterprises:
                assert rows[f"case_{enterprise}"] == (
                    {"problem_status": "verified", "cause_state": "unknown"},
                    {"symptom_statement": "s"},
                )
            assert rows[f"case_none_{tag}"] == before[f"case_none_{tag}"]

            result = alembic_on(owner_url, f"downgrade {_PARENT_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            assert await _revision(su_url) == _PARENT_REVISION
            assert await _rows(su_url) == rows
    finally:
        await _execute(superuser_url, drop_role_sql(role))


async def test_a_role_the_policy_filters_raises_instead_of_rewriting_nothing():
    """A role with the runtime grants and no ownership: the upgrade fails
    naming row-level security, and every row and the revision stay put."""
    superuser_url = os.environ["DATABASE_URL"]
    role = f"fm_023_limited_{uuid.uuid4().hex[:8]}"
    async with database_of_its_own(superuser_url, "fm_023_limited") as su_url:
        result = alembic_on(su_url, f"upgrade {_PARENT_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        await _seed(su_url)
        before = await _rows(su_url)
        await create_limited_role(su_url, role, _PW)
        try:
            role_url = limited_url(su_url, role, _PW)

            # Positive control: with row security on, this role sees no case,
            # so a plain UPDATE would rewrite nothing and raise nothing.
            visible = await _execute(role_url, "SELECT count(*) FROM cases")
            assert visible == [(0,)], "the policy does not filter this role"

            result = alembic_on(role_url, f"upgrade {_REVISION}")
            assert result.returncode != 0, result.stdout[-2000:]
            assert "row-level security" in result.stderr.lower(), result.stderr[-2000:]
            assert await _rows(su_url) == before
            assert await _revision(su_url) == _PARENT_REVISION
        finally:
            await _execute(su_url, drop_role_sql(role))
