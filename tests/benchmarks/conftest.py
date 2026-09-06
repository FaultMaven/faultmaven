"""Benchmark test fixtures.

Provides database fixtures optimized for performance benchmarking with
minimal overhead from logging and other instrumentation.

Also provides ``measure_min_latency`` — the sampling helper every wall-clock
assertion in this suite goes through. See its docstring for why the statistic
is the minimum.
"""

import asyncio
import statistics
import time
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    NamedTuple,
    Optional,
    Tuple,
)

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# DatabaseEvidenceArtifactRepository removed in storage redesign 2026-04
# phase 2 (standalone evidence path deletion).
from faultmaven.infrastructure.persistence.investigation_session_repository import (
    DatabaseInvestigationSessionRepository,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    InvestigationStrategy,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)

# DatabaseSessionRepository (SQL auth session repo) removed in storage
# redesign 2026-04 phase 3 — auth sessions are Redis-only.
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    DatabaseKnowledgeItemRepository,
)

# generate_case_id is used by the warm-up fixture below; generate_org_id is
# re-exported for test_knowledge_item_operations.py. generate_item_id was
# dropped when that module stopped importing it — ruff cannot flag it here
# (conftest.py has F401 in per-file-ignores, and CI's rule selection excludes
# F401 anyway).
from tests.utils import generate_case_id, generate_org_id

#: Timed samples taken per measured operation, after one untimed warm-up call.
#:
#: Five is a compromise, not a magic number: enough that a single contended
#: sample cannot be the only one, few enough that the cost stays small. It is
#: affordable because the expensive part of these benchmarks is the fixture
#: setup — hundreds or thousands of rows — which is paid ONCE, outside the
#: loop. Measured over the whole suite locally, going from one sample to
#: warm-up + 5 moved the runtime from ~160s to ~170s. Sites whose measured
#: operation is itself a batch of dozens of writes pass a smaller ``samples``
#: explicitly.
DEFAULT_SAMPLES = 5


class Measurement(NamedTuple):
    """Timings from ``measure_min_latency``, plus the last operation result.

    ``samples`` are in seconds, warm-up excluded, in the order taken.
    """

    samples: Tuple[float, ...]
    result: Any

    @property
    def best(self) -> float:
        """The minimum sample — the statistic assertions compare against."""
        return min(self.samples)

    @property
    def median(self) -> float:
        return statistics.median(self.samples)

    @property
    def worst(self) -> float:
        return max(self.samples)

    def report(self) -> str:
        """The distribution, recorded so a threshold can be re-anchored later.

        Printed by every converted benchmark, so re-anchoring works from the
        RUNNER's own numbers rather than someone's laptop's — the two differ
        by several fold in both directions. A rising median against a flat
        minimum is contention; both rising together is the operation genuinely
        getting slower.

        Where it lands: pytest captures stdout on PASSING tests, so this does
        not appear in the job log. It is in the ``benchmark_results.json`` the
        benchmarks workflow uploads (``.call.stdout`` per test, 90-day
        retention)::

            gh run download <run-id> -R FaultMaven/faultmaven \\
                -n benchmark-results
            jq -r '.tests[].call.stdout' benchmark_results.json
        """
        return (
            f"min {self.best * 1000:.1f}ms "
            f"(median {self.median * 1000:.1f}ms, "
            f"max {self.worst * 1000:.1f}ms, n={len(self.samples)})"
        )


async def measure_min_latency(
    operation: Callable[..., Awaitable[Any]],
    *,
    samples: int = DEFAULT_SAMPLES,
    setup: Optional[Callable[[], Awaitable[Any]]] = None,
) -> Measurement:
    """Warm up once, take ``samples`` timings, and report the MINIMUM.

    Why the minimum. These benchmarks run on a shared GitHub-hosted runner
    whose CPU is contended by other tenants. Every source of error there is
    one-sided: scheduling delay, page-cache misses, GC pauses and co-tenant
    bursts can only ADD time to an operation, never subtract it. So the fastest
    observed run is the closest estimate available of what the operation itself
    costs, and the spread above it measures the runner, not the code.

    That keeps the gate's teeth. The regressions this suite exists to catch —
    a dropped index, an O(n) query turning O(n**2), an accidental round-trip
    per row — make the operation slower *every* time, so they raise the
    minimum as much as they raise the mean. What no longer fails the build is
    one hiccup during one sample, which is all a single-sample assertion could
    ever have been measuring on a machine like this.

    A p95 over a handful of samples would be the opposite choice: with n < 20
    it is effectively the maximum, i.e. the noisiest value in the set.

    The warm-up call is untimed and its result discarded. It absorbs the
    one-time costs that belong to nobody's latency budget: lazily imported
    modules, SQLAlchemy statement compilation and caching, aiosqlite's first
    round-trip on a connection, and cold pages of whatever rows the fixture
    just wrote.

    ‼ ``operation`` is run ``samples + 1`` times, so it must measure the SAME
    work each time. Read-only operations satisfy that for free. Anything that
    mutates state needs ``setup`` to hand it fresh material per call (a new
    entity to insert, a fresh row to delete); pass one, or leave the site
    alone.

    What ``setup`` is for is keeping every sample the SAME OPERATION. The
    failures it prevents are a sample that silently becomes a different call
    from the first — an UPDATE where sample 1 was an INSERT, a delete that
    misses because the row is already gone, a second insert on a primary key
    that just raises.

    Exact invariance is not achievable for an insert, and this is stated
    rather than hidden: fresh material per sample necessarily leaves the table
    one row (or one batch) larger, so the insert sites here grow their table
    by up to a few hundred rows over a run. That is accepted. Each benchmark
    gets its own fresh in-memory SQLite (``benchmark_engine`` is
    function-scoped), insert cost is flat in table size at that scale, and
    taking the MINIMUM biases towards the earliest and smallest sample anyway.
    Idempotent-write sites are likewise near- rather than exactly invariant:
    the repositories stamp their own ``updated_at`` on each write, so one
    column differs per sample while the statement and the row do not.

    Args:
        operation: The measured coroutine function. Called with no arguments,
            or with ``setup``'s return value as its single argument when
            ``setup`` is given.
        samples: Number of timed calls. Must be >= 1.
        setup: Optional coroutine function run before each call, INCLUDING the
            warm-up, and outside the timed window. Its return value is passed
            to ``operation``.

    Returns:
        A ``Measurement`` carrying every timing and the last result.
    """
    if samples < 1:
        raise ValueError(f"samples must be >= 1, got {samples}")

    timings = []
    result: Any = None
    # Iteration 0 is the warm-up: run identically, timed identically, and then
    # dropped. Running it through the same path is deliberate — a warm-up that
    # took a different code path would warm the wrong thing.
    for iteration in range(samples + 1):
        args = () if setup is None else (await setup(),)
        start = time.perf_counter()
        result = await operation(*args)
        elapsed = time.perf_counter() - start
        if iteration:
            timings.append(elapsed)

    return Measurement(tuple(timings), result)


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests.

    Scope is session to allow reuse across all benchmark tests.
    """
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="function")
async def benchmark_engine():
    """Create database engine for benchmarks (SQLite in-memory).

    Uses SQLite in-memory for fast, isolated benchmarks.
    Echo is disabled for clean benchmark results.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,  # Disable SQL logging for clean benchmarks
    )

    # Create schema
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest.fixture
async def benchmark_session(
    benchmark_engine,
) -> AsyncGenerator[AsyncSession, None]:
    """Create database session for benchmarks.

    Each test gets a fresh session that is rolled back after the test.
    """
    SessionLocal = async_sessionmaker(
        benchmark_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with SessionLocal() as session:
        yield session


@pytest.fixture(scope="session")
def warm_repository_paths():
    """Pay one-time initialisation cost OUTSIDE every timed window.

    `SQLiteCaseRepository.save()` lazily imports
    `faultmaven.core.investigation.terminal_transitions`, which transitively
    executes `core.investigation.__init__` -> `milestone_engine` ->
    `prompts.context_builder` -> `core.preprocessing.vector_storage` ->
    `infrastructure.model_cache` -> `sentence_transformers`.

    Measured on the first save in a process, under this suite (so with
    `tests/conftest.py`'s torch mocks in force — the figures are much larger
    against real torch, but that is not the environment benchmarks run in):

        cold first save   632.6 ms
        warm saves        3.24 ms (median)
        ratio             195x

    (That specific chain is severed by #849/#852, which makes the import cheap
    at the source. This fixture stays useful regardless: it also absorbs
    SQLAlchemy/aiosqlite first-statement setup, and it keeps any future
    one-time cost out of the timed windows rather than relying on the import
    graph staying light.)

    Whichever benchmark happened to run first therefore timed an import chain
    rather than the operation it names, and asserted a latency threshold
    against it. That is how
    `test_case_operations.py::test_single_case_creation_latency` failed at
    1165.9ms against its 1000ms target: the threshold sits *below* the import
    cost, so it could only ever flap, never measure.

    Requested by the repository fixtures rather than being autouse so that it
    does not run for `test_memory_usage.py::test_memory_usage_baseline`, which
    takes no fixtures precisely so it can sample a clean process RSS. That is a
    question of not changing what a test measures, not of magnitude — the chain
    adds ~50MB here, comfortably inside that test's 1500MB assertion (~432MB is
    already resident from `tests/conftest.py`'s own imports).

    Coverage caveat: `test_case_service_operations.py` and
    `test_investigation_session_service_operations.py` define their own
    repository fixtures that do NOT request this one. Under the full-suite
    invocation CI uses, alphabetical collection runs the warmed
    `test_case_operations.py` first, so they are warm by the time they execute.
    Run standalone they are not, and the cold import lands in the first
    iteration of their timed loops — absorbed by their p95 assertions rather
    than failing. Point those fixtures here too if that ever stops holding.

    This warms the import chain and SQLAlchemy/aiosqlite statement setup on a
    throwaway engine, so it perturbs no benchmark's own state. Deliberately a
    *synchronous* fixture driving its own loop via `asyncio.run`: a
    session-scoped async fixture would need a session-scoped pytest-asyncio
    runner and raises ScopeMismatch against the function-scoped default.
    """

    async def _warm() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            SessionLocal = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            async with SessionLocal() as session:
                await SQLiteCaseRepository(session).save(
                    Case(
                        case_id=generate_case_id(),
                        user_id="warmup-user",
                        enterprise_id="warmup-org",
                        title="Warm-up",
                        description="Discarded write that absorbs one-time init.",
                        state=CaseState.INQUIRY,
                        investigation_strategy=InvestigationStrategy.POST_MORTEM,
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_warm())


@pytest.fixture
async def case_repository(
    benchmark_session, warm_repository_paths
) -> SQLiteCaseRepository:
    """Create case repository for benchmarks."""
    return SQLiteCaseRepository(benchmark_session)


# session_repository (SQL auth session repo) fixture removed in storage
# redesign 2026-04 phase 3 — auth sessions are Redis-only via
# RedisSessionStore.

# evidence_artifact_repository fixture removed in storage redesign 2026-04
# phase 2 (standalone evidence path deletion). Evidence is case-tied only.


@pytest.fixture
async def investigation_session_repository(
    benchmark_session, warm_repository_paths
) -> DatabaseInvestigationSessionRepository:
    """Create investigation session repository for benchmarks."""
    return DatabaseInvestigationSessionRepository(benchmark_session)


@pytest.fixture
async def knowledge_item_repository(
    benchmark_session, warm_repository_paths
) -> DatabaseKnowledgeItemRepository:
    """Create knowledge item repository for benchmarks."""
    return DatabaseKnowledgeItemRepository(benchmark_session)
