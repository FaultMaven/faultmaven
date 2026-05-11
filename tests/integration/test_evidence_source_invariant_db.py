"""DB-level enforcement test for the ``evidence_source_invariant`` CHECK.

Migration 010 added a CHECK constraint on the ``evidence`` table:

    source_file_id IS NOT NULL OR source_type = 'user_description'

This test exercises the constraint by attempting raw SQL INSERTs that
violate it and asserting the database rejects them. Catches drift if
the constraint is accidentally dropped or weakened.
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

pytestmark = pytest.mark.integration


async def _setup_fresh_db(db_path: str) -> AsyncSession:
    """Run all migrations on a fresh SQLite file, return a session."""
    import subprocess

    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    subprocess.run(
        [".venv/bin/python", "-m", "alembic", "upgrade", "head"],
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
            "INSERT INTO cases (case_id, organization_id, title) "
            "VALUES (:cid, :oid, 'Test')"
        ),
        {"cid": cid, "oid": oid},
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
            _, oid, cid = await _seed_minimum(session)

            from sqlalchemy import text

            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO evidence "
                        "(evidence_id, organization_id, case_id, "
                        " category, source_type, source_file_id, "
                        " summary, metadata) "
                        "VALUES (:eid, :oid, :cid, 'symptom_evidence', "
                        " 'logs', NULL, 'bad row', '{}')"
                    ),
                    {
                        "eid": f"ev_{uuid.uuid4().hex[:12]}",
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
async def test_check_constraint_allows_user_description_without_source_file():
    """source_type=user_description + source_file_id NULL is the legal
    carve-out and must be accepted by the DB."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        session = await _setup_fresh_db(db_path)
        try:
            _, oid, cid = await _seed_minimum(session)

            from sqlalchemy import text

            eid = f"ev_{uuid.uuid4().hex[:12]}"
            await session.execute(
                text(
                    "INSERT INTO evidence "
                    "(evidence_id, organization_id, case_id, "
                    " category, source_type, source_file_id, "
                    " summary, metadata) "
                    "VALUES (:eid, :oid, :cid, 'symptom_evidence', "
                    " 'user_description', NULL, 'user pasted error', '{}')"
                ),
                {"eid": eid, "oid": oid, "cid": cid},
            )
            await session.commit()

            row = (
                await session.execute(
                    text("SELECT source_type FROM evidence WHERE evidence_id = :id"),
                    {"id": eid},
                )
            ).first()
            assert row is not None
            assert row[0] == "user_description"
        finally:
            await session.close()
    finally:
        os.unlink(db_path)
