"""Benchmark test fixtures.

Provides database fixtures optimized for performance benchmarking with
minimal overhead from logging and other instrumentation.
"""

import asyncio
from typing import AsyncGenerator

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
                        organization_id="warmup-org",
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
