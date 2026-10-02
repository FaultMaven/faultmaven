"""Revision 006 under row-level security, on a real PostgreSQL (#836).

``006_kb_conversion_source_storage_ref_null`` clears the filesystem paths two
KB writers stored in ``uploaded_files.storage_ref``. The table is tenant-scoped,
so what the UPDATE reaches depends on who runs it, and only a real database can
show that:

* **The owner reaches every enterprise.** The role that runs the migrations
  owns the table, and the baseline ``ENABLE``s its policy without ``FORCE``-ing
  it, so the owner is exempt. The probe migrates as a NON-superuser owner: a
  superuser bypasses row-level security whatever the policy says, so a probe
  run as one would pass even if the owner were not exempt.
* **A role the policy filters raises.** With no enterprise bound it sees no
  rows, so a plain UPDATE would clear nothing and report success. Under the
  revision's ``row_security = off`` the same UPDATE raises instead. A positive
  control first shows the plain UPDATE really does clear nothing for that role:
  without it, a role the policy never filtered would pass for the wrong reason.

The SQLite half, which pins exactly which rows are cleared, is
``TestConversionSourceStorageRefRevision`` in
``tests/integration/test_alembic_migrations.py``.

Each test migrates a database of its own, because each steps the chain to the
parent revision first.
"""

from __future__ import annotations

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

#: ``006_kb_conversion_source_storage_ref_null``.
_REVISION = "f37066de2792"
#: ``005_definer_search_path_without_public``, the revision 006 parents onto.
_PARENT_REVISION = "1c5a2ad13a65"
_PW = "fm_storage_ref_probe_pw"

#: The statement 006 runs, verbatim, for the positive control.
_UPDATE = (
    "UPDATE uploaded_files SET storage_ref = NULL "
    "WHERE upload_source = 'conversion_source' "
    "AND case_id IS NULL "
    "AND storage_ref IS NOT NULL"
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


async def _seed(url: str) -> dict:
    """Two enterprises, each with a conversion-source row holding a path, and
    one ``file_upload`` row holding a backend key. Returns what was seeded."""
    tag = uuid.uuid4().hex[:8]
    enterprises = (f"ent_a_{tag}", f"ent_b_{tag}")
    rows = {
        f"file_kb_a_{tag}": (
            enterprises[0],
            "conversion_source",
            "data/knowledge/a.md",
        ),
        f"file_kb_b_{tag}": (
            enterprises[1],
            "conversion_source",
            "data/knowledge/b.md",
        ),
        f"file_ev_{tag}": (enterprises[0], "file_upload", f"evidence/{tag}/obj"),
    }
    statements = [
        f"INSERT INTO enterprises (enterprise_id, name, slug) "
        f"VALUES ('{enterprise}', 'Enterprise {enterprise}', '{enterprise}')"
        for enterprise in enterprises
    ]
    statements += [
        "INSERT INTO uploaded_files (file_id, enterprise_id, filename, size_bytes, "
        f"storage_ref, upload_source) VALUES ('{file_id}', '{enterprise}', 'f.md', "
        f"1, '{ref}', '{source}')"
        for file_id, (enterprise, source, ref) in rows.items()
    ]
    await _execute(url, *statements)
    return {file_id: ref for file_id, (_, _, ref) in rows.items()}


async def _refs(url: str) -> dict:
    return dict(await _execute(url, "SELECT file_id, storage_ref FROM uploaded_files"))


async def _revision(url: str) -> str:
    ((version,),) = await _execute(url, "SELECT version_num FROM alembic_version")
    return version


async def test_the_owner_clears_the_paths_in_every_enterprise():
    """Migrated by a role that owns the tables and is not a superuser — Cloud's
    shape. Both enterprises' conversion-source rows are cleared, the backend key
    is not, and the count logged is both."""
    superuser_url = os.environ["DATABASE_URL"]
    role = f"fm_006_owner_{uuid.uuid4().hex[:8]}"
    await _execute(superuser_url, drop_role_sql(role))
    await _execute(
        superuser_url,
        f"CREATE ROLE {role} LOGIN PASSWORD '{_PW}' NOSUPERUSER NOBYPASSRLS",
    )
    try:
        async with database_of_its_own(
            superuser_url, "fm_006_owner", owner=role
        ) as su_url:
            owner_url = limited_url(su_url, role, _PW)
            result = alembic_on(owner_url, f"upgrade {_PARENT_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            seeded = await _seed(owner_url)

            result = alembic_on(owner_url, f"upgrade {_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            assert "cleared storage_ref on 2 KB conversion-source row(s)" in (
                result.stderr
            ), result.stderr[-2000:]
            assert await _refs(su_url) == {
                file_id: (None if file_id.startswith("file_kb_") else ref)
                for file_id, ref in seeded.items()
            }
            assert await _revision(su_url) == _REVISION
    finally:
        await _execute(superuser_url, drop_role_sql(role))


async def test_a_role_the_policy_filters_raises_instead_of_clearing_nothing():
    """A role with the runtime grants and no ownership — the policy applies to
    it. The upgrade fails naming row-level security, and every row and the
    revision stay where they were."""
    superuser_url = os.environ["DATABASE_URL"]
    role = f"fm_006_limited_{uuid.uuid4().hex[:8]}"
    async with database_of_its_own(superuser_url, "fm_006_limited") as su_url:
        result = alembic_on(su_url, f"upgrade {_PARENT_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        seeded = await _seed(su_url)
        await create_limited_role(su_url, role, _PW)
        try:
            role_url = limited_url(su_url, role, _PW)

            # Positive control: with row security on, this role's UPDATE runs,
            # clears nothing, and raises nothing — the silent partial update the
            # revision's setting exists to turn into an error.
            engine = create_async_engine(role_url)
            try:
                async with engine.connect() as conn:
                    transaction = await conn.begin()
                    cleared = (await conn.execute(text(_UPDATE))).rowcount
                    await transaction.rollback()
            finally:
                await engine.dispose()
            assert cleared == 0, "the policy does not filter this role"

            result = alembic_on(role_url, f"upgrade {_REVISION}")
            assert result.returncode != 0, result.stdout[-2000:]
            assert "row-level security" in result.stderr.lower(), result.stderr[-2000:]
            assert await _refs(su_url) == seeded
            assert await _revision(su_url) == _PARENT_REVISION
        finally:
            await _execute(su_url, drop_role_sql(role))
