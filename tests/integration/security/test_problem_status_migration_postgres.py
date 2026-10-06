"""Revision 007 under row-level security, on a real PostgreSQL.

``007_problem_status_single_source`` rewrites ``cases.progress`` (JSONB:
``symptom_verified`` becomes ``problem_status``), retires ``captured``
hypotheses, and drops ``captured`` from the ``hypotheses.state`` CHECK and
default. Both tables are tenant-scoped, so what the UPDATEs reach depends on who
runs them, and only a real database can show that:

* **The owner reaches every enterprise.** The probe migrates as a NON-superuser
  owner — a superuser bypasses row-level security whatever the policy says, so a
  probe run as one would pass even if the owner were not exempt.
* **A role the policy filters raises** under the revision's
  ``row_security = off`` instead of rewriting no rows and reporting success.

The SQLite half — the table rebuild and what it keeps — is
``TestProblemStatusRevision`` in ``tests/integration/test_alembic_migrations.py``.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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

#: ``007_problem_status_single_source``.
_REVISION = "497ae8900ae2"
#: ``006_kb_conversion_source_storage_ref_null``, the revision 007 parents onto.
_PARENT_REVISION = "f37066de2792"  # pragma: allowlist secret
_PW = "fm_problem_status_probe_pw"


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


async def _seed(url: str) -> dict:
    """Two enterprises, each with a verified case carrying a queued hypothesis,
    and one unverified case. Returns ``{tag: ...}`` names for the asserts."""
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
            f"INSERT INTO cases (case_id, enterprise_id, user_id, title, progress) "
            f"VALUES ('case_{enterprise}', '{enterprise}', 'u_{enterprise}', 't', "
            f"""'{{"symptom_verified": true, "cause_state": "unknown"}}')""",
            f"INSERT INTO hypotheses (hypothesis_id, enterprise_id, case_id, "
            f"statement, category, state) VALUES ('hyp_{enterprise}', "
            f"'{enterprise}', 'case_{enterprise}', 's', 'code', 'captured')",
        ]
    statements.append(
        f"INSERT INTO cases (case_id, enterprise_id, user_id, title, progress) "
        f"VALUES ('case_unverified_{tag}', '{enterprises[0]}', "
        f"""'u_{enterprises[0]}', 't', '{{"symptom_verified": false}}')"""
    )
    await _execute(url, *statements)
    return {"enterprises": enterprises, "tag": tag}


async def _progress(url: str) -> dict:
    rows = await _execute(url, "SELECT case_id, progress::text FROM cases")
    return {case_id: json.loads(progress) for case_id, progress in rows}


async def _states(url: str) -> dict:
    return dict(await _execute(url, "SELECT hypothesis_id, state FROM hypotheses"))


async def _revision(url: str) -> str:
    ((version,),) = await _execute(url, "SELECT version_num FROM alembic_version")
    return version


async def test_the_owner_rewrites_every_enterprise_and_round_trips():
    """Migrated by a role that owns the tables and is not a superuser — Cloud's
    shape. Both enterprises' rows move; the CHECK refuses ``captured`` and the
    default is ``active``; the downgrade restores exactly what moved."""
    superuser_url = os.environ["DATABASE_URL"]
    role = f"fm_007_owner_{uuid.uuid4().hex[:8]}"
    await _execute(superuser_url, drop_role_sql(role))
    await _execute(
        superuser_url,
        f"CREATE ROLE {role} LOGIN PASSWORD '{_PW}' NOSUPERUSER NOBYPASSRLS",
    )
    try:
        async with database_of_its_own(
            superuser_url, "fm_007_owner", owner=role
        ) as su_url:
            owner_url = limited_url(su_url, role, _PW)
            result = alembic_on(owner_url, f"upgrade {_PARENT_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            seeded = await _seed(owner_url)
            enterprises, tag = seeded["enterprises"], seeded["tag"]
            before = await _progress(su_url)

            result = alembic_on(owner_url, f"upgrade {_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            assert "moved symptom_verified on 3 row(s)" in result.stderr
            assert "retired queued hypotheses on 2 row(s)" in result.stderr

            progress = await _progress(su_url)
            for enterprise in enterprises:
                assert progress[f"case_{enterprise}"] == {
                    "problem_status": "verified",
                    "cause_state": "unknown",
                }
            assert progress[f"case_unverified_{tag}"] == {
                "problem_status": "unverified"
            }
            assert set((await _states(su_url)).values()) == {"retired"}

            with pytest.raises(IntegrityError, match="hypotheses_state_check"):
                await _execute(
                    su_url,
                    "INSERT INTO hypotheses (hypothesis_id, enterprise_id, case_id, "
                    f"statement, category, state) VALUES ('hyp_x_{tag}', "
                    f"'{enterprises[0]}', 'case_{enterprises[0]}', 's', 'code', "
                    "'captured')",
                )
            ((default,),) = await _execute(
                su_url,
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'hypotheses' AND column_name = 'state'",
            )
            assert default.startswith("'active'"), default

            result = alembic_on(owner_url, f"downgrade {_PARENT_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            assert await _revision(su_url) == _PARENT_REVISION
            assert await _progress(su_url) == before
            assert set((await _states(su_url)).values()) == {"captured"}
    finally:
        await _execute(superuser_url, drop_role_sql(role))


async def test_a_role_the_policy_filters_raises_instead_of_rewriting_nothing():
    """A role with the runtime grants and no ownership — the policy applies to
    it. The upgrade fails naming row-level security, and every row and the
    revision stay where they were."""
    superuser_url = os.environ["DATABASE_URL"]
    role = f"fm_007_limited_{uuid.uuid4().hex[:8]}"
    async with database_of_its_own(superuser_url, "fm_007_limited") as su_url:
        result = alembic_on(su_url, f"upgrade {_PARENT_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        await _seed(su_url)
        before = await _progress(su_url)
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
            assert await _progress(su_url) == before
            assert await _revision(su_url) == _PARENT_REVISION
        finally:
            await _execute(su_url, drop_role_sql(role))
