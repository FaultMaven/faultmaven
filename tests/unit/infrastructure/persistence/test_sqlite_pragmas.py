"""SQLite connection PRAGMA regression tests.

The async engine setup in ``database.py`` registers a ``connect`` event
listener that sets three production PRAGMAs on every new SQLite
connection:

- ``journal_mode=WAL`` — readers don't block writers (essential for
  async workloads where multiple coroutines query/commit concurrently).
- ``busy_timeout=5000`` — wait 5s on contention before failing with
  "database is locked" (default is 0ms — instant failure).
- ``foreign_keys=ON`` — make ``ON DELETE CASCADE`` actually cascade.

Without these, eval turn 2 on case_78c6ad39e2d4 crashed with
``sqlite3.OperationalError: database is locked`` on the first
commit-during-contention. These tests pin the configuration so a
future "engine cleanup" PR can't silently regress it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_sqlite_pragmas_set_on_new_connection():
    """Open a fresh engine against a temp SQLite file, run a query so
    the connect listener fires, and assert each PRAGMA returns its
    expected value."""
    # Import inside the test so we control the engine lifecycle (the
    # module caches a singleton in ``_engine``).
    from faultmaven.infrastructure.persistence import database as db_module

    saved_engine = db_module._engine
    saved_factory = db_module._session_factory
    db_module._engine = None
    db_module._session_factory = None

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "pragma_test.db"
        url = f"sqlite+aiosqlite:///{db_path}"
        try:
            engine = db_module.get_engine(database_url=url)
            async with engine.connect() as conn:
                journal_mode = (
                    await conn.execute(text("PRAGMA journal_mode"))
                ).scalar()
                busy_timeout = (
                    await conn.execute(text("PRAGMA busy_timeout"))
                ).scalar()
                foreign_keys = (
                    await conn.execute(text("PRAGMA foreign_keys"))
                ).scalar()

            # WAL mode is the load-bearing one. SQLite returns the mode
            # name lowercase; "wal" is what we want.
            assert str(journal_mode).lower() == "wal", (
                f"Expected journal_mode=WAL, got {journal_mode!r}. "
                f"Without WAL, concurrent read/write transactions in the "
                f"async event loop collide and surface as 'database is "
                f"locked' on commit."
            )
            assert busy_timeout == 5000, (
                f"Expected busy_timeout=5000ms, got {busy_timeout!r}. "
                f"With busy_timeout=0 (default), any contention fails "
                f"instantly instead of waiting."
            )
            assert foreign_keys == 1, (
                f"Expected foreign_keys=ON (1), got {foreign_keys!r}. "
                f"Without this, ON DELETE CASCADE silently no-ops and "
                f"call sites need explicit multi-phase deletes."
            )

            await engine.dispose()
        finally:
            db_module._engine = saved_engine
            db_module._session_factory = saved_factory


@pytest.mark.asyncio
async def test_sqlite_pragmas_apply_per_connection_with_nullpool():
    """SQLite uses NullPool — each session opens a fresh connection.
    The listener must fire on *every* connect, not just the first.
    Verify by opening two independent connections and checking both."""
    from faultmaven.infrastructure.persistence import database as db_module

    saved_engine = db_module._engine
    saved_factory = db_module._session_factory
    db_module._engine = None
    db_module._session_factory = None

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "pragma_per_conn.db"
        url = f"sqlite+aiosqlite:///{db_path}"
        try:
            engine = db_module.get_engine(database_url=url)
            for _ in range(2):
                async with engine.connect() as conn:
                    journal_mode = (
                        await conn.execute(text("PRAGMA journal_mode"))
                    ).scalar()
                    foreign_keys = (
                        await conn.execute(text("PRAGMA foreign_keys"))
                    ).scalar()
                    assert str(journal_mode).lower() == "wal"
                    assert foreign_keys == 1
            await engine.dispose()
        finally:
            db_module._engine = saved_engine
            db_module._session_factory = saved_factory
