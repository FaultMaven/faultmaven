"""Regression test (#703): the user read path leaves no lingering transaction.

The cluster incident was a leaked ``idle in transaction`` connection: a
singleton ``DatabaseUserStore`` held a single process-lifetime
``AsyncSession``, and ``PostgreSQLUserRepository``'s read methods
(``get``/``get_by_username``/…) issue ``execute()`` — which autobegins a
transaction on PostgreSQL — but never commit/close. With the session never
closed, the backing connection sat ``idle in transaction`` for ~1.7 days,
holding ``ACCESS SHARE`` on ``users`` and blocking migration 025's DDL.

The fix replaced the owned singleton session with ``SessionlessUserRepository``,
which opens a fresh session per operation via ``get_db_session()`` — the
context manager commits/rolls-back/closes every time. These tests pin that
invariant:

- each operation uses its OWN session (never one shared across calls), and
- the session is closed / not ``in_transaction()`` once the operation returns.

The old ``aclose()``-on-owned-session hygiene test that lived here is gone:
the owned-session pattern it regressed no longer exists.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.user_repository import (
    PostgreSQLUserRepository,
    SessionlessUserRepository,
    User,
)


def _make_user(user_id: str, username: str) -> User:
    now = datetime.now(timezone.utc)
    return User(
        user_id=user_id,
        username=username,
        email=f"{username}@example.com",
        display_name=username.title(),
        created_at=now,
        updated_at=now,
        roles=["user"],
    )


@pytest.fixture()
async def sessionless_repo(monkeypatch):
    """A ``SessionlessUserRepository`` wired to a seeded in-memory SQLite DB.

    Uses ``StaticPool`` so the schema + seeded row persist across the many
    short-lived sessions the sessionless repo opens. ``get_db_session`` is
    patched with a context manager that mirrors production semantics
    (commit on success, rollback on error, always close) AND records every
    session it hands out, so the tests can assert per-operation isolation
    and transaction hygiene.

    Yields ``(repo, captured_sessions)``.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Seed one user directly (not through the sessionless path).
    async with factory() as seed_session:
        await PostgreSQLUserRepository(seed_session).save(_make_user("u-seed", "seed"))

    captured: list[AsyncSession] = []

    @asynccontextmanager
    async def fake_get_db_session(database_url=None):
        session = factory()
        captured.append(session)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.get_db_session",
        fake_get_db_session,
    )

    try:
        yield SessionlessUserRepository(), captured
    finally:
        await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
class TestSessionlessUserRepositoryHygiene:
    async def test_read_leaves_no_open_transaction(self, sessionless_repo):
        """A read returns data AND leaves its session closed / not in a
        transaction — the exact property the leaked singleton violated."""
        repo, captured = sessionless_repo

        user = await repo.get("u-seed")

        assert user is not None and user.username == "seed"
        assert len(captured) == 1
        # After the op, the per-op session must not be sitting in a
        # transaction (a closed session reports False).
        assert captured[0].in_transaction() is False

    async def test_each_operation_uses_a_fresh_session(self, sessionless_repo):
        """Two operations must not share a session (the singleton bug shared
        one across every request — both an idle-in-transaction leak and a
        concurrent-use hazard)."""
        repo, captured = sessionless_repo

        await repo.get("u-seed")
        await repo.get_by_username("seed")

        assert len(captured) == 2
        assert captured[0] is not captured[1]
        assert all(s.in_transaction() is False for s in captured)

    async def test_write_then_read_are_independently_scoped(self, sessionless_repo):
        """A write commits and closes on its own session; a following read
        opens another. Neither leaves a transaction open."""
        repo, captured = sessionless_repo

        await repo.save(_make_user("u-2", "second"))
        fetched = await repo.get("u-2")

        assert fetched is not None and fetched.username == "second"
        assert len(captured) == 2
        assert captured[0] is not captured[1]
        assert all(s.in_transaction() is False for s in captured)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_idle_in_transaction_after_user_lookup_postgres():
    """PG-only: after a user lookup through the sessionless repo, the app's
    own connections show zero ``idle in transaction`` in pg_stat_activity.

    Skipped unless a real PostgreSQL URL is provided via
    ``FAULTMAVEN_TEST_POSTGRES_URL`` (or a ``postgresql`` ``DATABASE_URL``),
    since CI defaults to SQLite where ``idle in transaction`` does not apply.
    """
    pg_url = os.environ.get("FAULTMAVEN_TEST_POSTGRES_URL") or (
        os.environ.get("DATABASE_URL", "")
        if os.environ.get("DATABASE_URL", "").startswith("postgresql")
        else ""
    )
    if not pg_url:
        pytest.skip("No PostgreSQL URL configured for idle-in-transaction check")

    engine = create_async_engine(pg_url, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _cm(database_url=None):
        session = factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    import faultmaven.infrastructure.persistence.database as db_module

    original = db_module.get_db_session
    db_module.get_db_session = _cm
    try:
        async with factory() as seed_session:
            await PostgreSQLUserRepository(seed_session).save(
                _make_user("pg-seed", "pgseed")
            )

        repo = SessionlessUserRepository()
        assert (await repo.get("pg-seed")) is not None

        # Inspect the backend for lingering idle-in-transaction connections.
        async with factory() as check:
            result = await check.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE state = 'idle in transaction' "
                    "AND datname = current_database()"
                )
            )
            idle_in_txn = result.scalar()
        assert idle_in_txn == 0, f"{idle_in_txn} idle-in-transaction connection(s)"
    finally:
        db_module.get_db_session = original
        await engine.dispose()
