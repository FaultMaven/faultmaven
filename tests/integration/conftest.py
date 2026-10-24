"""
Integration test configuration and fixtures.

The all-skipped guard, the per-test shutdown of a served app's protection
middleware, and the shared service fixtures for tests under tests/integration/.
"""

import asyncio
import os
import sys
from collections import Counter
from uuid import uuid4

import pytest
import pytest_asyncio

# Conditional Redis import - only available in enterprise edition
try:
    import redis.asyncio as redis

    REDIS_AVAILABLE = True
except ImportError:
    redis = None
    REDIS_AVAILABLE = False

from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService as SessionService,
)

# Conditional RedisSessionStore import - only available in enterprise edition
if REDIS_AVAILABLE:
    from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
        RedisSessionStore,
    )
else:
    RedisSessionStore = None

# Legacy services/domain/* was removed; use extracted module path.
# This import triggers: CaseService -> persistence -> chromadb -> protobuf -> ctypes
# But _ctypes mock is already loaded from root conftest above
from faultmaven.modules.case.domain.services.case_service import CaseService

# Configure pytest-asyncio to fix deprecation warnings
pytest_asyncio.asyncio_default_fixture_loop_scope = "function"
pytest_asyncio.asyncio_default_test_loop_scope = "function"

# Test configuration
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
REDIS_URL = (
    f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}"
    if REDIS_PASSWORD
    else f"redis://{REDIS_HOST}:{REDIS_PORT}"
)


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


@pytest.fixture
def mock_case_service():
    """Create a mock case service for tests."""
    from unittest.mock import AsyncMock

    from faultmaven.modules.case.contracts import (
        Case,
        CaseState,
        InquiryData,
    )

    service = AsyncMock()

    # Team-share enrichment on the case-detail read path (ADR-013 §D4). Default
    # to no team shares so CaseDetail.shared_team_ids serializes as [] rather than
    # an un-awaited MagicMock.
    service.get_case_team_ids = AsyncMock(return_value=[])

    # Store created cases to maintain state across create_case and get_case calls
    created_cases = {}

    # Mock create_case to return a simple case
    async def create_case_mock(title=None, description=None, owner_id=None, **kwargs):
        case_id = f"case_{uuid4().hex[:12]}"
        case = Case(
            case_id=case_id,
            title=title or "Test Case",
            description=description or "Test Description",
            user_id=owner_id or "test_user_123",
            enterprise_id="test_org_123",
            state=CaseState.INQUIRY,
            current_turn=0,
            message_count=0,
        )
        # Store case for later retrieval
        created_cases[case_id] = case
        return case

    service.create_case = create_case_mock

    # Mock get_case to return the case (with evidence if it exists)
    async def get_case_mock(case_id, user_id):
        # Return the stored case if it exists, otherwise create a new one.
        # Note: INVESTIGATING cases require Gate 1 (the problem statement
        # confirmed) to construct. The unified opportunistic flow no longer
        # has a path-selection gate.
        if case_id in created_cases:
            return created_cases[case_id]
        else:
            case = Case(
                case_id=case_id,
                title="Test Case",
                description="Test Description",
                user_id=user_id,
                enterprise_id="test_org_123",
                state=CaseState.INVESTIGATING,
                current_turn=1,
                message_count=1,
                inquiry=InquiryData(
                    problem_statement_confirmed=True,
                    proposed_problem_statement="Test Description",
                ),
            )
            created_cases[case_id] = case
            return case

    service.get_case = get_case_mock

    return service


@pytest.fixture
def mock_session_service():
    """Create a mock session service for tests."""
    from unittest.mock import AsyncMock

    service = AsyncMock()
    return service


# Architectural Compliance Test Fixtures


@pytest_asyncio.fixture
async def session_service() -> SessionService:
    """Create SessionService with real Redis for integration testing

    Note: Uses RedisSessionStore which creates its own Redis client from .env.
    Does NOT clean Redis - tests work with existing data to simulate
    production environment where FLUSHDB may be disabled.

    Requires enterprise edition with redis installed.
    """
    if not REDIS_AVAILABLE or RedisSessionStore is None:
        pytest.skip("RedisSessionStore not available - requires enterprise edition")

    # Create RedisSessionStore - it will use create_redis_client() from .env
    try:
        session_store = RedisSessionStore()
    except Exception as e:
        pytest.skip(f"Redis not reachable - skipping: {e}")

    # Verify Redis connectivity before proceeding
    try:
        client = redis.from_url(REDIS_URL)
        await client.ping()
        await client.aclose()
    except Exception as e:
        pytest.skip(f"Redis not reachable at {REDIS_URL} - skipping: {e}")

    # Create SessionService with session store
    service = SessionService(
        session_store=session_store,
        max_sessions_per_user=10,
        inactive_threshold_hours=24,
    )

    return service


@pytest_asyncio.fixture
async def case_service() -> CaseService:
    """Create CaseService with real dependencies for integration testing

    Note: Uses services from dependency injection container.
    Does NOT clean Redis - tests work with existing data.
    """
    from faultmaven.container import container

    # Ensure container is initialized (idempotent - safe to call multiple times)
    await container.initialize()

    # Return the case service from the container
    service = container.get_case_service()
    if service is None:
        raise RuntimeError(
            "case_service is None - container initialization may have failed"
        )
    return service


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
