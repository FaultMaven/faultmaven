"""Revision 018 on a real PostgreSQL (#1950).

``018_runbook_drafts_in_the_database`` gives every runbook draft its text in
``conversion_drafts.content``. On Cloud the migration Job sees no pod's disk, so
every live draft that is not published loses its text and is discarded, and a
case job whose last live draft goes releases its ``live_case_id``. Both tables
are enrolled in row-level security, so the data steps must reach EVERY
enterprise's rows: the probe migrates as a NON-superuser owner, Cloud's shape,
over two enterprises.

The SQLite half, which pins the file backfill and the standalone guard, is
``TestRunbookDraftsInTheDatabaseRevision`` in
``tests/integration/test_alembic_migrations.py``.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.security.conftest import (
    alembic_on,
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

#: ``018_runbook_drafts_in_the_database``.
_REVISION = "c4441b83ba24"  # pragma: allowlist secret
#: ``017_drop_investigation_sessions``, the revision 018 parents onto.
_PARENT_REVISION = "f95a5b3a58eb"  # pragma: allowlist secret
_PW = "fm_drafts_probe_pw"  # pragma: allowlist secret


async def _execute(url: str, *statements: str, params: dict | None = None) -> list:
    """Run ``statements`` in one transaction; return the last one's rows."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            rows: list = []
            for statement in statements:
                result = await conn.execute(text(statement), params or {})
                rows = list(result.fetchall()) if result.returns_rows else []
            return rows
    finally:
        await engine.dispose()


async def _seed(url: str) -> list[str]:
    """Two enterprises, each with a verified draft (published text), a live
    case draft whose file no migration Job can see, and a discarded draft.
    Returns the enterprise ids."""
    tag = uuid.uuid4().hex[:8]
    enterprises = [f"ent_a_{tag}", f"ent_b_{tag}"]
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            for e in enterprises:

                async def run(sql: str, **params):
                    await conn.execute(text(sql), {"e": e, **params})

                await run(
                    "INSERT INTO enterprises (enterprise_id, name, slug) "
                    "VALUES (:e, :e, :e)"
                )
                await run(
                    "INSERT INTO knowledge_items (item_id, enterprise_id, title, "
                    "content, item_type) VALUES (:k, :e, 'Published', :c, 'runbook')",
                    k=f"ki_{e}",
                    c=f"PUBLISHED {e}",
                )
                for job, case in ((f"job_doc_{e}", None), (f"job_case_{e}", f"c_{e}")):
                    await run(
                        "INSERT INTO uploaded_files (file_id, enterprise_id, "
                        "filename, size_bytes) VALUES (:f, :e, 'src', 1)",
                        f=f"file_{job}",
                    )
                    await run(
                        "INSERT INTO conversion_jobs (id, enterprise_id, "
                        "source_file_id, scope, source_type, case_id, live_case_id) "
                        "VALUES (:j, :e, :f, 'personal', :t, NULL, :c)",
                        j=job,
                        f=f"file_{job}",
                        t="case" if case else "document",
                        c=case,
                    )
                for draft, job, status, item in (
                    (f"d_ver_{e}", f"job_doc_{e}", "verified", f"ki_{e}"),
                    (f"d_live_{e}", f"job_case_{e}", "draft", None),
                    (f"d_old_{e}", f"job_doc_{e}", "discarded", None),
                ):
                    await run(
                        "INSERT INTO conversion_drafts (id, enterprise_id, "
                        "conversion_id, runbook_id, title, file_path, status, "
                        "knowledge_item_id) VALUES (:d, :e, :j, :d, 't', :p, :s, :k)",
                        d=draft,
                        j=job,
                        p=f"data/knowledge/user_{tag}/{draft}.md",
                        s=status,
                        k=item,
                    )
    finally:
        await engine.dispose()
    return enterprises


async def test_the_owner_settles_every_enterprises_drafts():
    superuser_url = os.environ["DATABASE_URL"]
    role = f"fm_017_owner_{uuid.uuid4().hex[:8]}"
    await _execute(superuser_url, drop_role_sql(role))
    await _execute(
        superuser_url,
        f"CREATE ROLE {role} LOGIN PASSWORD '{_PW}' NOSUPERUSER NOBYPASSRLS",
    )
    try:
        async with database_of_its_own(
            superuser_url, "fm_017_owner", owner=role
        ) as su_url:
            owner_url = limited_url(su_url, role, _PW)
            result = alembic_on(owner_url, f"upgrade {_PARENT_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            enterprises = await _seed(owner_url)

            result = alembic_on(owner_url, f"upgrade {_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            assert "018: discarding 2 live draft(s) whose text is gone" in (
                result.stderr
            ), result.stderr[-2000:]

            drafts = {
                row[0]: (row[1], row[2])
                for row in await _execute(
                    su_url, "SELECT id, status, content FROM conversion_drafts"
                )
            }
            keys = dict(
                await _execute(su_url, "SELECT id, live_case_id FROM conversion_jobs")
            )
            for e in enterprises:
                assert drafts[f"d_ver_{e}"] == ("verified", f"PUBLISHED {e}")
                assert drafts[f"d_live_{e}"] == ("discarded", "")
                assert drafts[f"d_old_{e}"] == ("discarded", "")
                assert keys[f"job_case_{e}"] is None
            nullability = await _execute(
                su_url,
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'conversion_drafts' "
                "AND column_name IN ('content', 'file_path')",
            )
            assert nullability == [("content", "NO")]

            result = alembic_on(owner_url, f"downgrade {_PARENT_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            nullability = await _execute(
                su_url,
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'conversion_drafts' "
                "AND column_name IN ('content', 'file_path')",
            )
            assert nullability == [("file_path", "YES")]

            result = alembic_on(owner_url, f"upgrade {_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            published = await _execute(
                su_url,
                "SELECT content FROM conversion_drafts WHERE status = 'verified' "
                "ORDER BY id",
            )
            assert published == [(f"PUBLISHED {e}",) for e in sorted(enterprises)]
    finally:
        await _execute(superuser_url, drop_role_sql(role))
