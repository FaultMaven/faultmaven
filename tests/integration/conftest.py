"""
Integration test configuration and fixtures.

The all-skipped guard, the per-test shutdown of a served app's protection
middleware, the real-Redis client, and the shared service fixtures for tests
under tests/integration/.
"""

import asyncio
import os
import sys
from collections import Counter

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.redis_client import (
    get_async_redis_client,
    get_fakeredis_client,
    is_fakeredis,
)
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService as SessionService,
)
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)

# Legacy services/domain/* was removed; use extracted module path.
# This import triggers: CaseService -> persistence -> chromadb -> protobuf -> ctypes
# But _ctypes mock is already loaded from root conftest above
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
    SQLiteCaseRepository,
)
from tests.utils import seed_enterprises, seed_users

# Configure pytest-asyncio to fix deprecation warnings
pytest_asyncio.asyncio_default_fixture_loop_scope = "function"
pytest_asyncio.asyncio_default_test_loop_scope = "function"


# ---------------------------------------------------------------------------
# All-skipped guard
#
# A session that selects tests and then skips every one of them exits 0: green,
# having verified nothing. That is the failure mode this suite was in — a
# missing service silently turned the whole tree into no-ops. Treat it as a
# failure instead: tally outcomes as they are reported and force a non-zero
# exit when nothing actually ran.
#
# Individual fixtures may still skip legitimately (Redis, PostgreSQL). The
# guard only fires when *every* selected test skipped, so a mixed run — some
# passes, some environment-dependent skips — stays green.
#
# Scope is session-level by design: in a mixed whole-tree run, unit passes
# suppress it, and that is intended — the CI shape that matters here
# (`pytest tests/ -m postgres`) selects only integration tests, so it fires.
# ---------------------------------------------------------------------------


class _SessionOutcomes:
    """Tally of per-test outcomes, used by the all-skipped guard."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.errored = 0
        self.skipped = 0
        self.skip_reasons: Counter = Counter()

    @property
    def ran_something(self) -> bool:
        """True if at least one test body produced a real verdict."""
        return bool(self.passed or self.failed or self.errored)

    @property
    def everything_skipped(self) -> bool:
        """True if tests were selected and every one of them skipped."""
        return self.skipped > 0 and not self.ran_something


_session_outcomes = _SessionOutcomes()


def _skip_reason(report) -> str:
    """Extract a readable skip reason from a test report."""
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        reason = str(longrepr[2])
    elif longrepr:
        reason = str(longrepr)
    else:
        reason = "unknown"
    return reason.replace("Skipped: ", "", 1).strip() or "unknown"


def _reset_outcomes() -> None:
    """Start a fresh tally.

    The tally is module state, and this conftest stays in ``sys.modules``
    across repeated in-process ``pytest.main()`` calls, so it must be cleared
    per session or a later run inherits an earlier run's verdicts.
    """
    global _session_outcomes
    _session_outcomes = _SessionOutcomes()


def pytest_sessionstart(session):
    """Reset the tally when this conftest is the initial one."""
    _reset_outcomes()


def pytest_collection_modifyitems(session, config, items):
    """Reset the tally again, for the runs ``pytest_sessionstart`` cannot see.

    ``pytest_sessionstart`` fires before collection, so a non-initial conftest
    (``pytest tests/``, where this file is loaded while collecting) is not yet
    registered and never receives it. This hook runs once collection is done,
    by which point the conftest is always registered — so it is the reset that
    actually covers every invocation shape.
    """
    _reset_outcomes()


def pytest_runtest_logreport(report):
    """Count per-test outcomes for the all-skipped guard."""
    if hasattr(report, "wasxfail"):
        # xfail/xpass means the test body ran; it is not "nothing ran".
        if report.when == "call":
            _session_outcomes.passed += 1
        return

    if report.skipped:
        _session_outcomes.skipped += 1
        _session_outcomes.skip_reasons[_skip_reason(report)] += 1
    elif report.failed:
        if report.when == "call":
            _session_outcomes.failed += 1
        else:
            _session_outcomes.errored += 1
    elif report.passed and report.when == "call":
        _session_outcomes.passed += 1


def pytest_sessionfinish(session, exitstatus):
    """Fail a session that selected tests but skipped every single one."""
    if not _session_outcomes.everything_skipped:
        return

    lines = [
        "Every selected test was skipped, so this run verified nothing.",
        "An all-skipped integration run is treated as a failure: green must not",
        "mean that no test executed.",
        "",
        f"tests selected: {_session_outcomes.skipped} (all skipped)",
        "skip reasons:",
    ]
    lines.extend(
        f"  [{count}] {reason}"
        for reason, count in _session_outcomes.skip_reasons.most_common()
    )
    lines.extend(
        [
            "",
            "Give the skipped tests what they ask for — e.g. point DATABASE_URL at a",
            "scratch PostgreSQL for the postgres-marked suites, or start a reachable",
            "Redis — or narrow the selection so the run stops claiming to cover them.",
        ]
    )

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "ALL TESTS SKIPPED", red=True, bold=True)
        for line in lines:
            reporter.write_line(line)
    else:  # pragma: no cover - terminal reporter is disabled only with -p no:terminal
        print("\n".join(lines), file=sys.stderr)

    # Only escalate a would-be-green run. A session that already failed for
    # another reason — a collection error (exit 2), a user interrupt — keeps its
    # more severe status; the banner is printed either way.
    if session.exitstatus == 0:
        session.exitstatus = 1


@pytest.fixture(autouse=True)
async def _served_apps_end_their_service():
    """End the protection middleware's service after every test, deterministically.

    Modules here serve ``faultmaven.main.app`` or a ``rebuild_app()`` app without
    its lifespan, so the app is never composed and the protection middleware
    builds a Redis client of its own (on a real Redis; standalone gets the
    FakeRedis stand-in, which it never owns). Each is shut down the way the
    lifespan's ``shutdown_protection`` does it, per test.

    A redis-py asyncio pool can only be closed on the event loop it was opened
    on. Under the function-scoped loop this teardown runs on the test's own, so
    an app served from it (``AsyncClient(ASGITransport(app))`` in an async test)
    is closed here. A client opened on a loop that is still running belongs to a
    live lifespan, the module's shared boot, whose own shutdown closes it, so it
    is left to that. An app served from a loop that has ended, a bare
    ``TestClient``'s per-request portal above all, leaves a client nothing can
    close: its sockets surface later as ``unclosed transport`` in whichever test
    is running at garbage collection. So that fails here, in the test that
    served it, naming the app, instead of depending on when the collector runs.
    """
    yield
    from tests.integration._app_rebuild import rebuilt_apps

    apps = [(f"rebuild_app() app #{n}", app) for n, app in enumerate(rebuilt_apps())]
    published = sys.modules.get("faultmaven.main")
    if published is not None:
        apps.append(("faultmaven.main.app", published.app))
    await _end_the_service_of(apps)


async def _end_the_service_of(apps) -> None:
    """Shut down each named app's protection middleware; fail on a stranded client.

    ``apps`` is ``(name, app)`` pairs; the name is what the failure reports.
    """
    from faultmaven.api.protection import live_protection_middleware

    running = asyncio.get_running_loop()
    stranded = []
    failures = []
    for name, app in apps:
        for middleware in live_protection_middleware(app):
            opened_on = middleware.owned_client_loop
            if opened_on is not None and opened_on is not running:
                if opened_on.is_running():
                    continue
                stranded.append(f"{name}: {type(middleware).__name__}")
            try:
                await middleware.shutdown()
            except Exception as e:
                failures.append(e)
    if failures:
        raise ExceptionGroup("protection middleware shutdown failed", failures)
    if stranded:
        pytest.fail(
            "an app was served from an event loop other than this test's, and "
            "its protection middleware built a Redis client there that nothing "
            "can close: " + "; ".join(stranded) + ". Serve it from the test's "
            "own loop (an async test with AsyncClient(transport=ASGITransport("
            "app=app))), or borrow the composed app (booted_app_client).",
            pytrace=False,
        )


# ---------------------------------------------------------------------------
# The Redis a test runs on
#
# The suite steps of Test Standalone and Test Cloud name no Redis, so every
# Redis consumer in them resolves the in-process FakeRedis; Test Cloud's
# real-Redis step names its server in REDIS_HOST. Two fixtures read that:
#
# - ``real_redis``, for a test that exists to drive a real server: it skips
#   where none is named.
# - ``session_redis``, for a test of behaviour both stacks share: the named
#   server, or the standalone stack's stand-in where none is named.
#
# Where a server is named and does not answer, both FAIL:
# ``get_async_redis_client()`` falls back to FakeRedis silently, and a broken
# service container must not turn a test green on the stand-in (#2243).
#
# Where none is named, a run that must reach a server says so with
# ``FM_TEST_REQUIRE_REDIS`` (below), and both fail instead.
# ---------------------------------------------------------------------------

#: Test-only. Set to any non-empty value, it says this run must reach a named
#: Redis: where neither ``REDIS_URL`` nor ``REDIS_HOST`` names one, every test
#: that takes ``real_redis`` or ``session_redis`` FAILS, instead of skipping or
#: running on the stand-in. Test Cloud's real-Redis step sets it, so a step
#: that lost its ``REDIS_*`` env cannot pass on FakeRedis.
REQUIRE_REDIS_ENV = "FM_TEST_REQUIRE_REDIS"


def _named_redis() -> str | None:
    """The variable naming this run's Redis, or None when nothing names one.

    ``REDIS_URL`` wins, as in the client factory. An empty ``REDIS_HOST``, what
    ``tests/conftest.py`` sets when the caller set none, names nothing.
    """
    if os.environ.get("REDIS_URL"):
        return "REDIS_URL"  # its value can carry a password, so it is not shown
    if os.environ.get("REDIS_HOST"):
        port = os.environ.get("REDIS_PORT", "6379")
        return f"REDIS_HOST={os.environ['REDIS_HOST']!r} REDIS_PORT={port!r}"
    return None


def _fail_if_a_redis_is_required() -> None:
    """Called where nothing names a Redis: fail if this run requires one."""
    if os.environ.get(REQUIRE_REDIS_ENV):
        pytest.fail(
            f"{REQUIRE_REDIS_ENV} is set, so this run must reach a named Redis, "
            "but neither REDIS_URL nor REDIS_HOST names one. Set REDIS_URL, or "
            "REDIS_HOST (with REDIS_PORT), to the server this run drives.",
            pytrace=False,
        )


async def _answering_client(named: str):
    """A client of this test's own on the named server, or a failure.

    Built by ``get_async_redis_client()``, the application's own resolution
    and ping, on this test's event loop. The caller closes it.
    """
    client = await get_async_redis_client()
    if is_fakeredis(client):
        # Not closed: it is the loop's shared stand-in, not this test's own.
        pytest.fail(
            f"{named} is set, but get_async_redis_client() resolved the "
            "in-process FakeRedis: the server did not answer (the captured log "
            "names the reason). A test given the named Redis exists to run on "
            "that server, so it fails rather than skips; check that the Redis "
            "service is up.",
            pytrace=False,
        )
    return client


@pytest.fixture
async def real_redis():
    """A client of this test's own on the named Redis, or a skip, or a failure."""
    named = _named_redis()
    if named is None:
        _fail_if_a_redis_is_required()
        pytest.skip(
            "neither REDIS_HOST nor REDIS_URL names a Redis to drive "
            "(Test Cloud's real-Redis step sets REDIS_HOST)"
        )
    client = await _answering_client(named)
    yield client
    await client.aclose()


@pytest.fixture
async def session_redis():
    """The named Redis, else the standalone stack's in-process stand-in.

    Never a skip. Where nothing names a server (and ``FM_TEST_REQUIRE_REDIS``
    is unset), the client is the one the standalone composition builds its
    stores on, ``get_fakeredis_client()``: this loop's shared client, so it is
    not closed here.
    """
    named = _named_redis()
    if named is None:
        _fail_if_a_redis_is_required()
        yield get_fakeredis_client()
        return
    client = await _answering_client(named)
    yield client
    await client.aclose()


# Architectural Compliance Test Fixtures


@pytest.fixture
def session_service(session_redis) -> SessionService:
    """An ``AuthSessionService`` over a ``RedisSessionStore`` on ``session_redis``.

    Nothing is cleaned: each test's ids are unique, and every key the store
    writes carries a TTL.
    """
    return SessionService(
        session_store=RedisSessionStore(session_redis, session_ttl_seconds=1800),
        max_sessions_per_user=10,
    )


@pytest.fixture
async def case_db_session(tmp_path):
    """One session on a SQLite database of this test's own.

    The schema is built from the ORM metadata, and foreign keys are enforced
    as the application's engine enforces them. The Standalone enterprise, the
    one a case created outside a request is stamped with, is seeded; a case's
    owner has to be seeded too (``case_owners``), because ``cases.user_id`` is
    a foreign key to ``users``.
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'cases.db'}", poolclass=NullPool
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enforce_foreign_keys(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await seed_enterprises(session, [STANDALONE_ENTERPRISE_ID])
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
def case_owners(case_db_session):
    """Seed accounts a case may name as its owner: ``await case_owners(id, ...)``."""

    async def seed(*user_ids: str) -> None:
        await seed_users(
            case_db_session, user_ids, enterprise_id=STANDALONE_ENTERPRISE_ID
        )

    return seed


@pytest.fixture
def case_service(case_db_session) -> CaseService:
    """The real ``CaseService`` over the SQLite repository on ``case_db_session``."""
    return CaseService(case_repository=SQLiteCaseRepository(case_db_session))


#: The URL the engine fixtures below point the process at. One spelling, in one
#: place, so the isolating fixture and the engines it covers cannot drift into
#: naming two different databases.
IN_MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
def in_memory_database_url(monkeypatch):
    """Point ``DATABASE_URL`` at an in-memory SQLite for ONE test, and put the
    process back afterwards.

    Three modules here build their own engine and need the application to agree
    with it, so they set ``DATABASE_URL``. They used to do it by assigning
    ``os.environ`` outright, from *function*-scoped fixtures that never restored
    it — so the first such test permanently repointed the whole process at an
    in-memory database, and ``tests/integration`` runs before ``tests/unit`` in
    a whole-suite run.

    That was invisible for as long as nothing rebuilt the settings afterwards:
    ``get_settings()`` caches a process-wide singleton, and the one built before
    these tests ran kept the real URL. The moment anything downstream reset that
    singleton, the rebuild picked this value up and the application booted
    against an empty database — ``no such table: enterprises`` out of
    ``bootstrap_application``, in tests that have nothing to do with any of this
    (fm#1325). A fixture whose blast radius is the rest of the process is a trap
    with a delay on it, and the delay is why it survived.

    ``monkeypatch`` is the whole fix: it restores the variable after each test,
    so a later rebuild resolves what it would have resolved before. The settings
    singleton is deliberately NOT snapshotted here as well. It would only matter
    if something built the singleton *inside* this window, and measured against
    these three modules nothing does — the guard in
    ``test_database_url_fixture_isolation.py`` asserts the resolved URL after the
    session rather than trusting that, so if that ever changes it goes red and
    the machinery can be added with evidence instead of on speculation.
    """
    monkeypatch.setenv("DATABASE_URL", IN_MEMORY_DATABASE_URL)
    return IN_MEMORY_DATABASE_URL
