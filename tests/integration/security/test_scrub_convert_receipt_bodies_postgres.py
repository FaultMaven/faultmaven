"""Revision 026 under row-level security, on a real PostgreSQL.

``026_scrub_convert_receipt_bodies`` replaces the response of every convert
receipt with its job id only, dropping the drafts' text a receipt stored before
contract 15.3.0 (#2452, ruling 2026-11-17); upload receipts are not touched.
``kb_request_receipts`` is tenant-scoped, so what the UPDATE reaches depends on
who runs it, and only a real database can show that:

* **The owner reaches every enterprise.** The probe migrates as a NON-superuser
  owner — a superuser bypasses row-level security whatever the policy says.
* **A role the policy filters raises** under the revision's
  ``row_security = off`` instead of scrubbing no rows and reporting success.

The SQLite half is ``TestScrubConvertReceiptBodiesRevision`` in
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

#: ``026_scrub_convert_receipt_bodies``.
_REVISION = "4492ab088c62"  # pragma: allowlist secret
#: ``025_admin_case_metadata_mitigation_disposed``, the revision 026 parents onto.
_PARENT_REVISION = "45970adab01d"  # pragma: allowlist secret
_PW = "fm_scrub_receipts_probe_pw"

_MARKER = "draft-secret-2452"
_UPLOAD_BODY = '{"document_id": "kb_x", "title": "t", "status": "published"}'


#: Each enterprise's convert receipts: ``(key, job prefix, body)``. The old
#: body names another job, so only the column can supply the new body's id;
#: ``[1, 2]`` is not an object, so the old body is never parsed.
_CONVERTS = (
    ("key-old", "job_old", None),
    ("key-new", "job_new", "in-shape"),
    ("key-list", "job_list", "[1, 2]"),
)
#: Every column but ``response``, which must not move.
_COLUMNS = (
    "enterprise_id, idempotency_key, author_id, route, request_fingerprint, "
    "status_code, knowledge_item_id, conversion_id, created_at"
)


def _old_body() -> str:
    """A pre-15.3.0 convert answer: every draft's text and the analysis."""
    return json.dumps(
        {
            "conversion_id": "job_elsewhere",
            "drafts": [{"draft_id": "d1", "content": f"step {_MARKER} one"}],
            "analysis": {"summary": "s", "failure_modes": ["a"]},
        }
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
    """Two enterprises, each with three conversion jobs and their convert
    receipts (``_CONVERTS``), and a knowledge item with its upload receipt.
    Returns the enterprises."""
    tag = uuid.uuid4().hex[:8]
    enterprises = (f"ent_a_{tag}", f"ent_b_{tag}")
    insert = (
        "INSERT INTO kb_request_receipts (enterprise_id, author_id, route, "
        "idempotency_key, request_fingerprint, status_code, response, "
        "knowledge_item_id, conversion_id, created_at) VALUES "
    )
    statements = []
    for enterprise in enterprises:
        statements += [
            f"INSERT INTO enterprises (enterprise_id, name, slug) "
            f"VALUES ('{enterprise}', 'Enterprise {enterprise}', '{enterprise}')",
            f"INSERT INTO knowledge_items (item_id, enterprise_id, title, content, "
            f"item_type, vector_generation) VALUES ('kb_{enterprise}', "
            f"'{enterprise}', 't', 'c', 'runbook', 'g1')",
            f"INSERT INTO uploaded_files (file_id, enterprise_id, filename, "
            f"size_bytes) VALUES ('file_{enterprise}', '{enterprise}', 'src', 1)",
            f"{insert}('{enterprise}', 'u1', 'upload', 'key-upload', 'f', 201, "
            f"'{_UPLOAD_BODY}', 'kb_{enterprise}', NULL, now())",
        ]
        for key, prefix, body in _CONVERTS:
            job = f"{prefix}_{enterprise}"
            if body is None:
                body = _old_body()
            elif body == "in-shape":
                body = json.dumps({"conversion_id": job})
            statements += [
                f"INSERT INTO conversion_jobs (id, enterprise_id, source_file_id, "
                f"scope, source_type) VALUES ('{job}', '{enterprise}', "
                f"'file_{enterprise}', 'personal', 'document')",
                f"{insert}('{enterprise}', 'u1', 'convert', '{key}', 'f', 201, "
                f"'{body}', NULL, '{job}', now())",
            ]
    await _execute(url, *statements)
    return enterprises


async def _rows(url: str) -> dict:
    """Each receipt's other columns and ``response`` as stored, by enterprise
    and key."""
    rows = await _execute(
        url, f"SELECT {_COLUMNS}, response::text FROM kb_request_receipts"
    )
    return {(row[0], row[1]): (tuple(row[:-1]), row[-1]) for row in rows}


async def _revision(url: str) -> str:
    ((version,),) = await _execute(url, "SELECT version_num FROM alembic_version")
    return version


async def test_the_owner_scrubs_every_enterprises_convert_receipts():
    """Migrated by a role that owns the tables and is not a superuser. Every
    convert receipt in both enterprises holds its own job id only, whatever it
    held; no other column and no upload receipt moves; the downgrade moves
    nothing."""
    superuser_url = os.environ["DATABASE_URL"]
    role = f"fm_026_owner_{uuid.uuid4().hex[:8]}"
    await _execute(superuser_url, drop_role_sql(role))
    await _execute(
        superuser_url,
        f"CREATE ROLE {role} LOGIN PASSWORD '{_PW}' NOSUPERUSER NOBYPASSRLS",
    )
    try:
        async with database_of_its_own(
            superuser_url, "fm_026_owner", owner=role
        ) as su_url:
            owner_url = limited_url(su_url, role, _PW)
            result = alembic_on(owner_url, f"upgrade {_PARENT_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            enterprises = await _seed(owner_url)
            before = await _rows(su_url)
            assert all(
                _MARKER in before[(enterprise, "key-old")][1]
                for enterprise in enterprises
            ), "positive control"

            result = alembic_on(owner_url, f"upgrade {_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            assert "026: rewrote 6 convert receipt(s)" in result.stderr

            rows = await _rows(su_url)
            assert {key: rest for key, (rest, _) in rows.items()} == {
                key: rest for key, (rest, _) in before.items()
            }
            for enterprise in enterprises:
                for key, prefix, _ in _CONVERTS:
                    _, response = rows[(enterprise, key)]
                    job = f"{prefix}_{enterprise}"
                    assert json.loads(response) == {"conversion_id": job}
                upload = (enterprise, "key-upload")
                assert rows[upload] == before[upload]
            assert all(_MARKER not in response for _, response in rows.values())

            result = alembic_on(owner_url, f"downgrade {_PARENT_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            assert await _revision(su_url) == _PARENT_REVISION
            assert await _rows(su_url) == rows
    finally:
        await _execute(superuser_url, drop_role_sql(role))


async def test_a_role_the_policy_filters_raises_instead_of_scrubbing_nothing():
    """A role with the runtime grants and no ownership: the upgrade fails
    naming row-level security, and every row and the revision stay put."""
    superuser_url = os.environ["DATABASE_URL"]
    role = f"fm_026_limited_{uuid.uuid4().hex[:8]}"
    async with database_of_its_own(superuser_url, "fm_026_limited") as su_url:
        result = alembic_on(su_url, f"upgrade {_PARENT_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        await _seed(su_url)
        before = await _rows(su_url)
        await create_limited_role(su_url, role, _PW)
        try:
            role_url = limited_url(su_url, role, _PW)

            # Positive controls: the receipts are there to scrub, and with row
            # security on this role sees none of them, so a plain UPDATE would
            # rewrite nothing and raise nothing.
            converts = await _execute(
                su_url,
                "SELECT count(*) FROM kb_request_receipts WHERE route = 'convert'",
            )
            assert converts == [(6,)]
            visible = await _execute(
                role_url, "SELECT count(*) FROM kb_request_receipts"
            )
            assert visible == [(0,)], "the policy does not filter this role"

            result = alembic_on(role_url, f"upgrade {_REVISION}")
            assert result.returncode != 0, result.stdout[-2000:]
            assert "row-level security" in result.stderr.lower(), result.stderr[-2000:]
            assert await _rows(su_url) == before
            assert await _revision(su_url) == _PARENT_REVISION
        finally:
            await _execute(su_url, drop_role_sql(role))
