"""Regression test: app lifecycle must not leak AsyncSession connections.

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

Fixes landed in this PR:
  - ``_create_user_store`` reuses the canonical engine from
    ``database.py:get_session_factory`` instead of creating a parallel one.
  - ``DatabaseUserStore.aclose()`` closes the owned session AND clears
    ``user_repository.db`` so the connection wrapper can be GC'd promptly.
  - ``main.py:lifespan`` shutdown calls ``user_store.aclose()`` and then
    ``close_database()`` to dispose the engine.

This test pins the no-leak invariant: two sequential app lifecycles
(the pattern that surfaced the warnings originally — composition-root
tests creating multiple ``TestClient(app)`` contexts in one process)
must produce ZERO ``non-checked-in connection`` warnings.
"""

from __future__ import annotations

import warnings

import pytest
from fastapi.testclient import TestClient


@pytest.mark.unit
class TestAppLifecycleConnectionHygiene:
    """The architectural invariant: app startup + shutdown leaks no
    AsyncSession connections. Tests run with warnings escalated to
    errors specifically for the SAWarning pattern that surfaced the
    original Item 11 issue, so any regression fails loudly here
    rather than as silent noise in unrelated test outputs.
    """

    @staticmethod
    def _run_lifecycle() -> list[warnings.WarningMessage]:
        """Run one TestClient lifecycle and return any non-checked-in
        warnings captured during it.

        Note: SQLAlchemy emits the warning during GC, so the warning
        may fire AFTER the TestClient context exits. We capture in a
        broader scope and trigger a GC pass before returning.
        """
        import gc

        from faultmaven.main import app

        captured: list[warnings.WarningMessage] = []

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            with TestClient(app) as client:
                # Trivial request to exercise the request pipeline.
                client.get("/health")
            # Force GC so any lingering AsyncSession finalizers run
            # before we inspect the captured warnings.
            gc.collect()
            captured = [
                msg for msg in w if "non-checked-in connection" in str(msg.message)
            ]
        return captured

    def test_single_lifecycle_no_leak(self):
        """One full app startup + shutdown emits no non-checked-in
        connection warnings."""
        leaks = self._run_lifecycle()
        assert not leaks, (
            f"Single TestClient lifecycle leaked {len(leaks)} connection(s): "
            f"{[str(m.message) for m in leaks]}"
        )

    def test_sequential_lifecycles_no_leak(self):
        """The original failure shape: two sequential TestClient
        contexts in one process. The first lifecycle disposed its
        engine; the second lifecycle reused the (now-stale) container
        and triggered the warning if the user_store still referenced
        a closed session. Must produce zero warnings."""
        leaks_1 = self._run_lifecycle()
        leaks_2 = self._run_lifecycle()
        total = leaks_1 + leaks_2
        assert not total, (
            f"Sequential TestClient lifecycles leaked {len(total)} "
            f"connection(s): {[str(m.message) for m in total]}"
        )
