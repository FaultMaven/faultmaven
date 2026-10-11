"""DB-level enforcement test for the ``evidence_source_invariant`` CHECK.

Revision 013 (#1930) declares the CHECK on the ``evidence`` table:

    source_file_id IS NOT NULL OR source_type = 'engine_inference'

Evidence is submitted data; only a fact the engine derived has no file.

This test exercises the constraint by attempting raw SQL INSERTs that
violate it and asserting the database rejects them. Catches drift if
the constraint is accidentally dropped or weakened.

The rows it builds carry ``enterprise_id`` because that is the NOT NULL
isolation key on every tenanted table (ADR-017 D1); the nullable
``organization_id`` beside it is billing attribution, and is stamped here only
so the INSERT exercises the same column set a real write does.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from faultmaven.config.settings import set_env_var

pytestmark = pytest.mark.integration


async def _setup_fresh_db(db_path: str) -> AsyncSession:
    """Run all migrations on a fresh SQLite file, return a session."""
    import subprocess
    import sys

    env = os.environ.copy()
    set_env_var(env, "DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    # Use the running interpreter rather than hardcoding ``.venv/bin/python``;
    # the local-dev venv path does not exist in the CI runner (Python lives
    # at /opt/hostedtoolcache/Python/.../python there).
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).parents[2],
        env=env,
        check=True,
        capture_output=True,
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    return AsyncSession(engine, expire_on_commit=False)


async def _seed_minimum(session: AsyncSession) -> tuple[str, str, str]:
    """Insert minimal enterprise/org/case rows so evidence FKs resolve."""
    from sqlalchemy import text

    eid, oid, cid = (
        f"ent_{uuid.uuid4().hex[:8]}",
        f"org_{uuid.uuid4().hex[:8]}",
        f"case_{uuid.uuid4().hex[:8]}",
    )
    await session.execute(
        text(
            "INSERT INTO enterprises (enterprise_id, name, slug) "
            "VALUES (:id, 'T', :slug)"
        ),
        {"id": eid, "slug": f"t-{eid[-6:]}"},
    )
    await session.execute(
        text(
            "INSERT INTO organizations (organization_id, enterprise_id, name, slug) "
            "VALUES (:oid, :eid, 'O', :slug)"
        ),
        {"oid": oid, "eid": eid, "slug": f"o-{oid[-6:]}"},
    )
    await session.execute(
        text(
            "INSERT INTO cases (case_id, enterprise_id, organization_id, title) "
            "VALUES (:cid, :eid, :oid, 'Test')"
        ),
        {"cid": cid, "eid": eid, "oid": oid},
    )
    await session.commit()
    return eid, oid, cid


@pytest.mark.asyncio
async def test_check_constraint_blocks_logs_without_source_file():
    """source_type=logs + source_file_id NULL must be rejected by the DB."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        session = await _setup_fresh_db(db_path)
        try:
            ent_id, oid, cid = await _seed_minimum(session)

            from sqlalchemy import text

            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO evidence "
                        "(evidence_id, enterprise_id, organization_id, case_id, "
                        " category, source_type, source_file_id, "
                        " summary, metadata) "
                        "VALUES (:evid, :ent, :oid, :cid, 'symptom_evidence', "
                        " 'logs', NULL, 'bad row', '{}')"
                    ),
                    {
                        "evid": f"ev_{uuid.uuid4().hex[:12]}",
                        "ent": ent_id,
                        "oid": oid,
                        "cid": cid,
                    },
                )
                await session.commit()
        finally:
            await session.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_type", "admitted"),
    [("engine_inference", True), ("user_description", False), ("text", False)],
)
async def test_only_an_engine_inference_may_have_no_source_file(source_type, admitted):
    """The one fileless row the DB admits is the engine's own inference; the
    retired chat-quote type is refused like every submitted-data type."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        session = await _setup_fresh_db(db_path)
        try:
            ent_id, oid, cid = await _seed_minimum(session)

            from sqlalchemy import text

            insert = text(
                "INSERT INTO evidence "
                "(evidence_id, enterprise_id, organization_id, case_id, "
                " category, source_type, source_file_id, "
                " summary, collected_by, metadata) "
                "VALUES (:evid, :ent, :oid, :cid, 'causal_absence_evidence', "
                " :stype, NULL, 'a fileless row', 'engine', '{}')"
            )
            params = {
                "evid": f"ev_{uuid.uuid4().hex[:12]}",
                "ent": ent_id,
                "oid": oid,
                "cid": cid,
                "stype": source_type,
            }
            if admitted:
                await session.execute(insert, params)
                await session.commit()
            else:
                with pytest.raises(IntegrityError):
                    await session.execute(insert, params)
                    await session.commit()
        finally:
            await session.close()
    finally:
        os.unlink(db_path)


#: The revision before 013, the one that changes the CHECK.
_BEFORE_013 = "6c1f0d2a9b47"  # pragma: allowlist secret


def _alembic(db_path: str, *args: str) -> None:
    import subprocess
    import sys

    env = os.environ.copy()
    set_env_var(env, "DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=Path(__file__).parents[2],
        env=env,
        check=True,
        capture_output=True,
    )


def test_013_settles_every_user_description_row_and_their_links():
    """013 on a database holding all three kinds of ``user_description`` row:
    the engine's row is re-typed, a file-backed row keeps its file as ``text``,
    a chat quote is deleted WITH its links (SQLite under this revision's
    connection runs no ON DELETE action, so the links must go explicitly), and
    nothing else is touched. Downgrade re-types the engine's row back."""
    import sqlite3

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        _alembic(db_path, "upgrade", _BEFORE_013)
        db = sqlite3.connect(db_path)
        db.execute(
            "INSERT INTO enterprises (enterprise_id, name, slug) VALUES ('e1','T','t')"
        )
        db.execute(
            "INSERT INTO cases (case_id, enterprise_id, title) VALUES ('c1','e1','T')"
        )
        db.execute(
            "INSERT INTO uploaded_files (file_id, enterprise_id, case_id, "
            "filename, size_bytes) VALUES ('f1','e1','c1','app.log',10)"
        )
        rows = [
            ("ev_engine", "user_description", None, "engine"),
            ("ev_filed", "user_description", "f1", "u1"),
            ("ev_quote", "user_description", None, "u1"),
            ("ev_logs", "logs", "f1", "u1"),
        ]
        for evid, stype, file_id, author in rows:
            db.execute(
                "INSERT INTO evidence (evidence_id, enterprise_id, case_id, "
                "category, source_type, source_file_id, summary, collected_by) "
                "VALUES (?, 'e1', 'c1', 'causal_absence_evidence', ?, ?, 's', ?)",
                (evid, stype, file_id, author),
            )
        for evid in ("ev_quote", "ev_logs"):
            db.execute(
                "INSERT INTO hypothesis_evidence (hypothesis_id, evidence_id, "
                "enterprise_id, relationship_type) VALUES ('h1', ?, 'e1', 'supports')",
                (evid,),
            )
            db.execute(
                "INSERT INTO evidence_need_fulfillment (need_id, evidence_id, "
                "enterprise_id, linked_at_turn) VALUES ('n1', ?, 'e1', 1)",
                (evid,),
            )
        db.commit()
        db.close()

        _alembic(db_path, "upgrade", "head")

        db = sqlite3.connect(db_path)
        assert dict(db.execute("SELECT evidence_id, source_type FROM evidence")) == {
            "ev_engine": "engine_inference",
            "ev_filed": "text",
            "ev_logs": "logs",
        }
        assert [
            r[0] for r in db.execute("SELECT evidence_id FROM hypothesis_evidence")
        ] == ["ev_logs"]
        assert [
            r[0]
            for r in db.execute("SELECT evidence_id FROM evidence_need_fulfillment")
        ] == ["ev_logs"]
        assert (
            db.execute(
                "SELECT COUNT(*) FROM evidence WHERE source_type = 'user_description'"
            ).fetchone()[0]
            == 0
        )
        db.close()

        _alembic(db_path, "downgrade", _BEFORE_013)

        db = sqlite3.connect(db_path)
        assert (
            db.execute(
                "SELECT source_type FROM evidence WHERE evidence_id = 'ev_engine'"
            ).fetchone()[0]
            == "user_description"
        )
        db.close()
    finally:
        os.unlink(db_path)
