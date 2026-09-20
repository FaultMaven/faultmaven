"""Performance benchmarks for APICaseService operations (TASK-011).

Benchmarks for case service operations with performance targets:
- Create case: target <200ms p95
- Get case: target <100ms p95
- Update case: target <150ms p95
- List cases (100 cases): target <300ms p95
- Get case with details: target <250ms p95
- Get statistics (100 cases): target <1000ms p95

Note: Thresholds are set for CI environments where performance varies.
"""

import asyncio
import statistics
import time
from typing import AsyncGenerator, List
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# InMemoryEvidenceArtifactRepository import removed in storage redesign
# 2026-04 phase 2 (standalone evidence path deletion).
from faultmaven.infrastructure.persistence.investigation_session_repository import (
    InMemoryInvestigationSessionRepository,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.domain.models import CaseSeverity
from faultmaven.modules.case.domain.services.api_case_service import APICaseService
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)

from .conftest import assert_latency_within

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture(scope="function")
async def async_engine():
    """Create in-memory SQLite engine for benchmarks."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest.fixture(scope="function")
async def async_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create async session for benchmarks."""
    session_factory = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_factory() as session:
        yield session


@pytest.fixture
async def case_repo(async_session) -> SQLiteCaseRepository:
    """Create database case repository."""
    return SQLiteCaseRepository(async_session)


@pytest.fixture
def case_service(case_repo) -> APICaseService:
    """Create APICaseService with repositories."""
    return APICaseService(
        case_repo=case_repo,
        session_repo=InMemoryInvestigationSessionRepository(),
    )


# ============================================================
# Benchmark Helpers
# ============================================================


def create_test_enterprise_id() -> str:
    """Generate a unique test ENTERPRISE id.

    The isolation key, which is what ``APICaseService`` takes (ADR-017 D1). It
    used to be an organization id under an ``enterprise_id`` parameter — the
    same value, one tier down, and every call here broke when the parameter
    became the enterprise it always meant.
    """
    return f"ent_{uuid4().hex[:8]}"


def create_test_user_id() -> str:
    """Generate unique test user ID."""
    return f"user_{uuid4().hex[:8]}"


async def measure_operation(operation, iterations: int = 100) -> dict:
    """Measure operation timing statistics.

    Args:
        operation: Async callable to measure
        iterations: Number of iterations to run

    Returns:
        Dictionary with timing statistics
    """
    times: List[float] = []

    for _ in range(iterations):
        start = time.perf_counter()
        await operation()
        elapsed = (time.perf_counter() - start) * 1000  # Convert to ms
        times.append(elapsed)

    times.sort()
    return {
        "min_ms": min(times),
        "max_ms": max(times),
        "mean_ms": statistics.mean(times),
        "median_ms": statistics.median(times),
        "p95_ms": times[int(len(times) * 0.95)],
        "p99_ms": times[int(len(times) * 0.99)],
        "iterations": iterations,
    }


def report_p95(name: str, stats: dict, target_ms: float) -> None:
    """Print the distribution, then assert the p95 against its target.

    The one comparison site in this module. It converts to seconds and hands
    off to ``assert_latency_within``, so the machine-throughput calibration
    (#908) applies here exactly as it does to the ``measure_min_latency``
    modules — this file used to spell the same rule out seven times as
    ``assert stats["p95_ms"] < N``, which is how it got missed.
    """
    print(f"\n{name}:")
    print(f"  Mean: {stats['mean_ms']:.2f}ms")
    print(f"  P95: {stats['p95_ms']:.2f}ms")
    print(f"  Target: <{target_ms:.0f}ms p95")
    assert_latency_within(
        stats["p95_ms"] / 1000.0,
        target_ms / 1000.0,
        f"{name} p95",
    )


# ============================================================
# Create Case Benchmark
# ============================================================


@pytest.mark.benchmark
class TestCreateCaseBenchmark:
    """Benchmark create_case operation."""

    @pytest.mark.asyncio
    async def test_create_case_performance(self, case_service):
        """Benchmark create_case performance. Target: <200ms p95."""
        enterprise_id = create_test_enterprise_id()
        user_id = create_test_user_id()
        counter = 0

        async def create_case():
            nonlocal counter
            counter += 1
            await case_service.create_case(
                user_id=user_id,
                enterprise_id=enterprise_id,
                title=f"Benchmark Case {counter}",
                description="Benchmark case description",
                severity=CaseSeverity.MEDIUM,
            )

        stats = await measure_operation(create_case, iterations=50)

        report_p95("Create Case Benchmark", stats, 200)


# ============================================================
# Get Case Benchmark
# ============================================================


@pytest.mark.benchmark
class TestGetCaseBenchmark:
    """Benchmark get_case operation."""

    @pytest.mark.asyncio
    async def test_get_case_performance(self, case_service):
        """Benchmark get_case performance. Target: <100ms p95."""
        enterprise_id = create_test_enterprise_id()
        user_id = create_test_user_id()

        # Create a case to retrieve
        case = await case_service.create_case(
            user_id=user_id,
            enterprise_id=enterprise_id,
            title="Benchmark Case",
            description="Case for get benchmarking",
            severity=CaseSeverity.LOW,
        )

        async def get_case():
            await case_service.get_case(case.case_id, enterprise_id)

        stats = await measure_operation(get_case, iterations=100)

        report_p95("Get Case Benchmark", stats, 100)


# ============================================================
# Update Case Benchmark
# ============================================================


@pytest.mark.benchmark
class TestUpdateCaseBenchmark:
    """Benchmark update_case operation."""

    @pytest.mark.asyncio
    async def test_update_case_performance(self, case_service):
        """Benchmark update_case performance. Target: <150ms p95."""
        enterprise_id = create_test_enterprise_id()
        user_id = create_test_user_id()

        # Create a case to update
        case = await case_service.create_case(
            user_id=user_id,
            enterprise_id=enterprise_id,
            title="Benchmark Case",
            description="Case for update benchmarking",
            severity=CaseSeverity.LOW,
        )

        counter = 0

        async def update_case():
            nonlocal counter
            counter += 1
            await case_service.update_case(
                case.case_id,
                enterprise_id,
                {"title": f"Updated Title {counter}"},
            )

        stats = await measure_operation(update_case, iterations=50)

        report_p95("Update Case Benchmark", stats, 150)


# ============================================================
# List Cases Benchmark
# ============================================================


@pytest.mark.benchmark
class TestListCasesBenchmark:
    """Benchmark list_cases operation."""

    @pytest.mark.asyncio
    async def test_list_cases_performance(self, case_service):
        """Benchmark list_cases with 100 cases. Target: <300ms p95."""
        enterprise_id = create_test_enterprise_id()
        user_id = create_test_user_id()

        # Create 100 cases
        for i in range(100):
            await case_service.create_case(
                user_id=user_id,
                enterprise_id=enterprise_id,
                title=f"Benchmark Case {i}",
                description=f"Case {i} for list benchmarking",
                severity=CaseSeverity.LOW,
            )

        async def list_cases():
            await case_service.list_cases(enterprise_id, limit=100)

        stats = await measure_operation(list_cases, iterations=30)

        report_p95("List Cases (100 cases) Benchmark", stats, 300)


# ============================================================
# Get Case With Details Benchmark
# ============================================================


@pytest.mark.benchmark
class TestGetCaseWithDetailsBenchmark:
    """Benchmark get_case_with_details operation."""

    @pytest.mark.asyncio
    async def test_get_case_with_details_performance(self, case_service):
        """Benchmark get_case_with_details. Target: <250ms p95."""
        enterprise_id = create_test_enterprise_id()
        user_id = create_test_user_id()

        # Create a case
        case = await case_service.create_case(
            user_id=user_id,
            enterprise_id=enterprise_id,
            title="Benchmark Case",
            description="Case for details benchmarking",
            severity=CaseSeverity.LOW,
        )

        async def get_details():
            await case_service.get_case_with_details(
                case.case_id,
                enterprise_id,
                include_sessions=True,
                include_evidence=True,
            )

        stats = await measure_operation(get_details, iterations=50)

        report_p95("Get Case With Details Benchmark", stats, 250)


# ============================================================
# Get Statistics Benchmark
# ============================================================


@pytest.mark.benchmark
class TestGetStatisticsBenchmark:
    """Benchmark get_case_statistics operation."""

    @pytest.mark.asyncio
    async def test_get_statistics_performance(self, case_service):
        """Benchmark get_case_statistics with ~100 cases. Target: <1000ms p95.

        Note: Threshold increased from 500ms to 1000ms to account for
        slower CI environments where resource availability varies.
        """
        enterprise_id = create_test_enterprise_id()
        user_id = create_test_user_id()

        # Create 100 cases (reduced from 1000 for faster test execution)
        severities = [
            CaseSeverity.LOW,
            CaseSeverity.MEDIUM,
            CaseSeverity.HIGH,
            CaseSeverity.CRITICAL,
        ]
        for i in range(100):
            await case_service.create_case(
                user_id=user_id,
                enterprise_id=enterprise_id,
                title=f"Benchmark Case {i}",
                description=f"Case {i} for statistics benchmarking",
                severity=severities[i % 4],
            )

        async def get_stats():
            await case_service.get_case_statistics(enterprise_id)

        stats = await measure_operation(get_stats, iterations=20)

        report_p95("Get Statistics (100 cases) Benchmark", stats, 1000)


# ============================================================
# Close Case Benchmark
# ============================================================


@pytest.mark.benchmark
class TestCloseBenchmark:
    """Benchmark close_case operation."""

    @pytest.mark.asyncio
    async def test_close_case_performance(self, case_service):
        """Benchmark close_case performance. Target: <200ms p95."""
        enterprise_id = create_test_enterprise_id()
        user_id = create_test_user_id()

        # Create cases to close
        cases = []
        for i in range(30):
            case = await case_service.create_case(
                user_id=user_id,
                enterprise_id=enterprise_id,
                title=f"Benchmark Case {i}",
                description="Case for close benchmarking",
                severity=CaseSeverity.LOW,
            )
            cases.append(case)

        times = []
        for case in cases:
            start = time.perf_counter()
            await case_service.close_case(case.case_id, enterprise_id)
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)

        times.sort()
        stats = {
            "mean_ms": statistics.mean(times),
            "p95_ms": times[int(len(times) * 0.95)],
        }

        report_p95("Close Case Benchmark", stats, 200)
