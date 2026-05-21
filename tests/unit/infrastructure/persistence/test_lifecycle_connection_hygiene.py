"""Regression test: DatabaseUserStore.aclose() releases its connection
cleanly so no non-checked-in SAWarnings fire at GC time.

Item 11 in the 2026-05-20 investigation-pipeline-followups handoff.

The full unit-test sweep previously emitted SAWarnings of the form:
  "The garbage collector is trying to clean up non-checked-in
   connection <AdaptedConnection <aiosqlite.core.Connection ...>>"

Sourced from ``container.providers.infrastructure._create_user_store``,
which created a parallel AsyncEngine + a long-lived AsyncSession handed
to ``PostgreSQLUserRepository``. The session was never closed at app
shutdown, and the repository held a reference to it (``self.db``)
keeping the AdaptedConnection wrapper alive until process-exit GC —
which then fired the "non-checked-in" warning.

This test pins the no-leak invariant directly on the
``DatabaseUserStore.aclose()`` code path the PR introduced, without
running FastAPI lifespan startup. An earlier version of this test
exercised the full app lifecycle via ``TestClient(app)`` — that
turned out to depend on an implicit cross-lifecycle assumption (the
pre-PR shutdown did NOT dispose the engine, so the in-memory SQLite
schema seeded by the first lifecycle's Alembic run persisted into
subsequent lifecycles in the same process). Once the PR correctly
disposes the engine at shutdown, the second lifecycle starts fresh
and Alembic migrations don't re-run, breaking bootstrap. Testing
``aclose()`` in isolation pins the actual fix's invariant and
sidesteps the migration / bootstrap question entirely.
"""

from __future__ import annotations

import gc
import warnings

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
from faultmaven.infrastructure.persistence.user_repository import (
    PostgreSQLUserRepository,
)


@pytest.mark.unit
@pytest.mark.asyncio
class TestDatabaseUserStoreAcloseHygiene:
    """Pin the no-leak invariant on ``aclose()`` directly. The bug
    this regresses was in the long-lived-session pattern that
    ``_create_user_store`` introduced; ``aclose()`` is the fix.
    Testing the fix's actual code path is more direct than driving
    the full FastAPI lifespan.
    """

    @staticmethod
    def _make_store_with_owned_session() -> (
        tuple[DatabaseUserStore, AsyncSession, object]
    ):
        """Build the exact shape ``_create_user_store`` produces:
        an engine + session + repository + DatabaseUserStore that
        owns the session via ``db_session=``. Returns the store, the
        session (for identity checks), and the engine (caller
        disposes after exercising aclose)."""
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=NullPool,
        )
        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        session = session_factory()
        repo = PostgreSQLUserRepository(session)
        store = DatabaseUserStore(repo, db_session=session)
        return store, session, engine

    async def test_aclose_emits_no_non_checked_in_warning(self):
        """The headline invariant: building + aclose'ing the store
        leaves no AdaptedConnection lingering for GC to complain
        about. Forces GC before inspecting captured warnings so the
        finalizer has a chance to fire."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")

            store, session, engine = self._make_store_with_owned_session()
            await store.aclose()
            await engine.dispose()
            # Drop local refs so the connection wrapper can be reclaimed
            # by GC before we inspect warnings.
            del store, session, engine
            gc.collect()

            leaks = [m for m in w if "non-checked-in connection" in str(m.message)]

        assert not leaks, (
            f"aclose() leaked {len(leaks)} AdaptedConnection(s): "
            f"{[str(m.message) for m in leaks]}"
        )

    async def test_aclose_clears_session_and_repo_db_references(self):
        """The two reference-clear properties aclose() must guarantee:

        - ``store._db_session`` is set to None (so subsequent aclose
          calls are no-ops).
        - ``user_repository.db`` is set to None (so the repo doesn't
          keep the AdaptedConnection wrapper alive past close — the
          load-bearing second step of the fix; without it the
          warning still fired despite proper session.close()).
        """
        store, session, engine = self._make_store_with_owned_session()
        try:
            # Sanity: before aclose, both references point at the session.
            assert store._db_session is session
            assert store.user_repository.db is session

            await store.aclose()

            assert store._db_session is None
            assert store.user_repository.db is None
        finally:
            await engine.dispose()

    async def test_aclose_is_idempotent(self):
        """Calling aclose() twice must not error. Lifespan shutdown
        is sometimes invoked more than once (e.g., nested TestClient
        contexts, double-shutdown signals), and aclose() must
        tolerate that."""
        store, _session, engine = self._make_store_with_owned_session()
        try:
            await store.aclose()
            await store.aclose()  # second call must be a no-op
        finally:
            await engine.dispose()

    async def test_aclose_no_op_when_no_session_passed(self):
        """When DatabaseUserStore is constructed WITHOUT db_session=
        (the caller is managing session lifecycle themselves), aclose
        must be a clean no-op — not error, not touch the repository's
        .db attribute (since the caller still owns that reference)."""
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=NullPool,
        )
        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        session = session_factory()
        try:
            repo = PostgreSQLUserRepository(session)
            # NOTE: no db_session= passed — caller-managed mode.
            store = DatabaseUserStore(repo)
            assert store._db_session is None

            await store.aclose()

            # Caller-managed session must NOT have been cleared.
            assert repo.db is session
        finally:
            await session.close()
            await engine.dispose()
