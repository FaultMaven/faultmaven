"""Revision 019 on a real PostgreSQL (#1948).

``019_verified_by_is_the_display_name`` rewrites a verified runbook's frontmatter
``verified_by`` from the username verify wrote before #1942 to the verifier's
current display name, in ``knowledge_items.content`` and the verified draft's
``conversion_drafts.content`` alike. Both tables are enrolled in row-level
security, so the rewrite must reach EVERY enterprise's rows:

* **The owner rewrites every enterprise.** The probe migrates as a
  NON-superuser owner, Cloud's shape, over two enterprises: a superuser bypasses
  row-level security whatever the policy says.
* **A role the policy filters raises** under the revision's
  ``row_security = off`` instead of rewriting no rows and reporting success.

The SQLite half, which pins each row rule, is
``TestVerifiedByIsTheDisplayNameRevision`` in
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

#: ``019_verified_by_is_the_display_name``.
_REVISION = "82bc95a1c02f"  # pragma: allowlist secret
#: ``018_runbook_drafts_in_the_database``, the revision 019 parents onto.
_PARENT_REVISION = "c4441b83ba24"  # pragma: allowlist secret
_PW = "fm_verified_by_probe_pw"  # pragma: allowlist secret


def _runbook(verified_by: str) -> str:
    """A runbook as verify's serialiser writes it."""
    return (
        f"---\nid: rb\nstatus: verified\nverified_by: {verified_by}\n"
        "---\n\n# Disk full"
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


async def _seed(url: str) -> list[str]:
    """Two enterprises, each with a verifier, a runbook they verified (and its
    verified draft) holding their username, and a runbook whose verifier is
    gone holding an address. Returns the enterprise ids."""
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
                    "INSERT INTO users (user_id, enterprise_id, username, email, "
                    "display_name, hashed_password) VALUES "
                    "(:u, :e, :name, :email, :display, 'x')",
                    u=f"u_{e}",
                    name=f"user.{e}",
                    email=f"user.{e}@example.com",
                    display=f"Name {e}",
                )
                await run(
                    "INSERT INTO uploaded_files (file_id, enterprise_id, filename, "
                    "size_bytes) VALUES (:f, :e, 'src', 1)",
                    f=f"file_{e}",
                )
                await run(
                    "INSERT INTO conversion_jobs (id, enterprise_id, source_file_id, "
                    "scope, source_type) VALUES (:j, :e, :f, 'personal', 'document')",
                    j=f"job_{e}",
                    f=f"file_{e}",
                )
                for item, verifier, content in (
                    (f"ki_{e}", f"u_{e}", _runbook(f"user.{e}")),
                    (f"ki_gone_{e}", None, _runbook(f"gone.{e}@example.com")),
                ):
                    await run(
                        "INSERT INTO knowledge_items (item_id, enterprise_id, title, "
                        "content, item_type, scope, verified_by, "
                        "verification_level, source_url) VALUES "
                        "(:k, :e, 't', :c, 'runbook', 'personal', :v, 1, :s)",
                        k=item,
                        c=content,
                        v=verifier,
                        s=f"conversion:job_{e}",
                    )
                    await run(
                        "INSERT INTO conversion_drafts (id, enterprise_id, "
                        "conversion_id, runbook_id, title, content, status, "
                        "knowledge_item_id, verified_by) VALUES (:d, :e, :j, :d, "
                        "'t', :c, 'verified', :k, :v)",
                        d=f"d_{item}",
                        j=f"job_{e}",
                        c=content,
                        k=item,
                        v=verifier,
                    )
    finally:
        await engine.dispose()
    return enterprises


async def test_the_owner_rewrites_every_enterprises_runbooks():
    superuser_url = os.environ["DATABASE_URL"]
    role = f"fm_019_owner_{uuid.uuid4().hex[:8]}"
    await _execute(superuser_url, drop_role_sql(role))
    await _execute(
        superuser_url,
        f"CREATE ROLE {role} LOGIN PASSWORD '{_PW}' NOSUPERUSER NOBYPASSRLS",
    )
    try:
        async with database_of_its_own(
            superuser_url, "fm_019_owner", owner=role
        ) as su_url:
            owner_url = limited_url(su_url, role, _PW)
            result = alembic_on(owner_url, f"upgrade {_PARENT_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            enterprises = await _seed(owner_url)

            result = alembic_on(owner_url, f"upgrade {_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            assert (
                "019: rewrote verified_by in 4 published runbook(s) and 4 verified "
                "draft(s); 2 of those runbooks have no verifier account"
            ) in result.stderr, result.stderr[-2000:]

            items = dict(
                await _execute(su_url, "SELECT item_id, content FROM knowledge_items")
            )
            drafts = dict(
                await _execute(su_url, "SELECT id, content FROM conversion_drafts")
            )
            for e in enterprises:
                assert items[f"ki_{e}"] == _runbook(f'"Name {e}"')
                assert items[f"ki_gone_{e}"] == _runbook('""')
                for item in (f"ki_{e}", f"ki_gone_{e}"):
                    assert drafts[f"d_{item}"] == items[item], item

            # The downgrade restores nothing; the upgrade again changes nothing.
            result = alembic_on(owner_url, f"downgrade {_PARENT_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            result = alembic_on(owner_url, f"upgrade {_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            assert (
                "019: rewrote verified_by in 0 published runbook(s) and 0 verified "
                "draft(s)"
            ) in result.stderr, result.stderr[-2000:]
            assert (
                dict(
                    await _execute(
                        su_url, "SELECT item_id, content FROM knowledge_items"
                    )
                )
                == items
            )
    finally:
        await _execute(superuser_url, drop_role_sql(role))


async def test_a_role_the_policy_filters_raises_instead_of_rewriting_nothing():
    """A role with the runtime grants and no ownership: the policy applies to
    it. The upgrade fails naming row-level security, and every row and the
    revision stay where they were."""
    superuser_url = os.environ["DATABASE_URL"]
    role = f"fm_019_limited_{uuid.uuid4().hex[:8]}"
    async with database_of_its_own(superuser_url, "fm_019_limited") as su_url:
        result = alembic_on(su_url, f"upgrade {_PARENT_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        await _seed(su_url)
        before = await _execute(
            su_url, "SELECT item_id, content FROM knowledge_items ORDER BY item_id"
        )
        await create_limited_role(su_url, role, _PW)
        try:
            role_url = limited_url(su_url, role, _PW)

            # Positive control: with row security on, this role sees no runbook,
            # so a plain rewrite would change nothing and raise nothing.
            visible = await _execute(role_url, "SELECT count(*) FROM knowledge_items")
            assert visible == [(0,)], "the policy does not filter this role"

            result = alembic_on(role_url, f"upgrade {_REVISION}")
            assert result.returncode != 0, result.stdout[-2000:]
            assert "row-level security" in result.stderr.lower(), result.stderr[-2000:]
            assert (
                await _execute(
                    su_url,
                    "SELECT item_id, content FROM knowledge_items ORDER BY item_id",
                )
                == before
            )
            assert await _execute(
                su_url, "SELECT version_num FROM alembic_version"
            ) == [(_PARENT_REVISION,)]
        finally:
            await _execute(su_url, drop_role_sql(role))
