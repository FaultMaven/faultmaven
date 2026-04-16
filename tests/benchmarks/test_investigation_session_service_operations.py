"""Performance benchmarks for APIInvestigationSessionService operations (TASK-012).

Benchmarks for investigation session service operations with performance targets:
- Create session: target <200ms p95
- Get session: target <100ms p95
- Update session: target <150ms p95
- Pause/resume session: target <150ms p95
- Complete session: target <150ms p95
- List sessions (50 sessions): target <300ms p95
- Get session with executions (10 executions): target <250ms p95
- Add execution to session: target <150ms p95
- Check budget exceeded: target <100ms p95
- Get statistics (100 sessions): target <500ms p95
"""

import asyncio
import statistics
import time
from typing import AsyncGenerator, List
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.database_case_repository import (
    DatabaseCaseRepository,
)
from faultmaven.infrastructure.persistence.investigation_session_repository import (
    InMemoryInvestigationSessionRepository,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.models.investigation_session import InvestigationSession, SessionStatus
from faultmaven.modules.agent.domain.models.agent_execution import (
    AgentExecution,
    AgentType,
    ExecutionStatus,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    InvestigationStrategy,
)

# AgentExecutionRepository removed - APIInvestigationSessionService now uses case_repo
from faultmaven.modules.case.domain.services.investigation_session_service import (
    APIInvestigationSessionService,
)

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
async def case_repo(async_session) -> DatabaseCaseRepository:
    """Create database case repository."""
    return DatabaseCaseRepository(async_session)


@pytest.fixture
def session_repo() -> InMemoryInvestigationSessionRepository:
    """Create in-memory session repository."""
    return InMemoryInvestigationSessionRepository()


# execution_repo fixture removed - case_repo now handles agent executions


@pytest.fixture
def session_service(session_repo, case_repo) -> APIInvestigationSessionService:
    """Create APIInvestigationSessionService with repositories."""
    return APIInvestigationSessionService(
        session_repo=session_repo,
        case_repo=case_repo,
    )


@pytest.fixture
async def sample_case(case_repo) -> Case:
    """Create sample case for benchmarks."""
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id=create_test_user_id(),
        organization_id=create_test_org_id(),
        title="Benchmark Test Case",
        description="Case for performance benchmarks",
        status=CaseStatus.INQUIRY,
        investigation_strategy=InvestigationStrategy.POST_MORTEM,
    )
    return await case_repo.save(case)


# ============================================================
# Benchmark Helpers
# ============================================================


def create_test_org_id() -> str:
    """Generate unique test organization ID."""
    return f"org_{uuid4().hex[:8]}"


def create_test_user_id() -> str:
    """Generate unique test user ID."""
    return f"user_{uuid4().hex[:8]}"


def create_test_session_id() -> str:
    """Generate unique test session ID."""
    return f"session_{uuid4().hex[:12]}"


def create_test_execution_id() -> str:
    """Generate unique test execution ID."""
    return f"exec_{uuid4().hex[:12]}"


async def time_async_operation(coro) -> float:
    """Time an async operation and return duration in milliseconds."""
    start = time.perf_counter()
    await coro
    end = time.perf_counter()
    return (end - start) * 1000


def calculate_p95(timings: List[float]) -> float:
    """Calculate 95th percentile of timings."""
    sorted_timings = sorted(timings)
    index = int(len(sorted_timings) * 0.95)
    return sorted_timings[min(index, len(sorted_timings) - 1)]


def report_benchmark(name: str, timings: List[float], target_p95_ms: float):
    """Report benchmark results."""
    p50 = statistics.median(timings)
    p95 = calculate_p95(timings)
    mean = statistics.mean(timings)

    status = "PASS" if p95 <= target_p95_ms else "FAIL"
    print(f"\n{name}:")
    print(f"  Mean: {mean:.2f}ms")
    print(f"  P50: {p50:.2f}ms")
    print(f"  P95: {p95:.2f}ms (target: {target_p95_ms}ms) [{status}]")

    return p95 <= target_p95_ms


# ============================================================
# Create Session Benchmarks
# ============================================================


class TestCreateSessionBenchmarks:
    """Benchmark session creation operations."""

    @pytest.mark.asyncio
    async def test_benchmark_create_session(
        self, session_service, sample_case, case_repo
    ):
        """Benchmark create_session operation - target <200ms p95."""
        iterations = 50
        timings = []

        for i in range(iterations):
            # Create new case for each iteration to avoid conflict
            case = Case(
                case_id=f"case_{uuid4().hex[:12]}",
                user_id=sample_case.user_id,
                organization_id=sample_case.organization_id,
                title=f"Benchmark Case {i}",
                description="",
                status=CaseStatus.INQUIRY,
            )
            await case_repo.save(case)

            duration = await time_async_operation(
                session_service.create_session(
                    case_id=case.case_id,
                    organization_id=case.organization_id,
                    user_id=case.user_id,
                    session_goal=f"Benchmark session {i}",
                )
            )
            timings.append(duration)

        passed = report_benchmark("Create Session", timings, 200)
        assert passed, f"P95 exceeded 200ms target"


# ============================================================
# Get Session Benchmarks
# ============================================================


class TestGetSessionBenchmarks:
    """Benchmark session retrieval operations."""

    @pytest.mark.asyncio
    async def test_benchmark_get_session(self, session_service, sample_case):
        """Benchmark get_session operation - target <100ms p95."""
        # Create a session first
        session = await session_service.create_session(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id=sample_case.user_id,
        )

        iterations = 100
        timings = []

        for _ in range(iterations):
            duration = await time_async_operation(
                session_service.get_session(
                    session.session_id,
                    sample_case.organization_id,
                )
            )
            timings.append(duration)

        passed = report_benchmark("Get Session", timings, 100)
        assert passed, f"P95 exceeded 100ms target"


# ============================================================
# Update Session Benchmarks
# ============================================================


class TestUpdateSessionBenchmarks:
    """Benchmark session update operations."""

    @pytest.mark.asyncio
    async def test_benchmark_update_session(self, session_service, sample_case):
        """Benchmark update_session operation - target <150ms p95."""
        session = await session_service.create_session(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id=sample_case.user_id,
        )

        iterations = 50
        timings = []

        for i in range(iterations):
            duration = await time_async_operation(
                session_service.update_session(
                    session.session_id,
                    sample_case.organization_id,
                    {"session_goal": f"Updated goal {i}"},
                )
            )
            timings.append(duration)

        passed = report_benchmark("Update Session", timings, 150)
        assert passed, f"P95 exceeded 150ms target"


# ============================================================
# Pause/Resume Session Benchmarks
# ============================================================


class TestPauseResumeSessionBenchmarks:
    """Benchmark session pause/resume operations."""

    @pytest.mark.asyncio
    async def test_benchmark_pause_session(
        self, session_service, sample_case, session_repo, case_repo
    ):
        """Benchmark pause_session operation - target <150ms p95."""
        iterations = 50
        timings = []

        for i in range(iterations):
            # Create new case and session for each iteration
            case = Case(
                case_id=f"case_{uuid4().hex[:12]}",
                user_id=sample_case.user_id,
                organization_id=sample_case.organization_id,
                title=f"Pause Test Case {i}",
                description="",
                status=CaseStatus.INQUIRY,
            )
            await case_repo.save(case)

            session = await session_service.create_session(
                case_id=case.case_id,
                organization_id=case.organization_id,
                user_id=case.user_id,
            )

            duration = await time_async_operation(
                session_service.pause_session(
                    session.session_id,
                    case.organization_id,
                )
            )
            timings.append(duration)

        passed = report_benchmark("Pause Session", timings, 150)
        assert passed, f"P95 exceeded 150ms target"

    @pytest.mark.asyncio
    async def test_benchmark_resume_session(
        self, session_service, sample_case, session_repo, case_repo
    ):
        """Benchmark resume_session operation - target <150ms p95."""
        iterations = 50
        timings = []

        for i in range(iterations):
            # Create new case and session for each iteration
            case = Case(
                case_id=f"case_{uuid4().hex[:12]}",
                user_id=sample_case.user_id,
                organization_id=sample_case.organization_id,
                title=f"Resume Test Case {i}",
                description="",
                status=CaseStatus.INQUIRY,
            )
            await case_repo.save(case)

            session = await session_service.create_session(
                case_id=case.case_id,
                organization_id=case.organization_id,
                user_id=case.user_id,
            )
            await session_service.pause_session(
                session.session_id, case.organization_id
            )

            duration = await time_async_operation(
                session_service.resume_session(
                    session.session_id,
                    case.organization_id,
                )
            )
            timings.append(duration)

        passed = report_benchmark("Resume Session", timings, 150)
        assert passed, f"P95 exceeded 150ms target"


# ============================================================
# Complete Session Benchmarks
# ============================================================


class TestCompleteSessionBenchmarks:
    """Benchmark session completion operations."""

    @pytest.mark.asyncio
    async def test_benchmark_complete_session(
        self, session_service, sample_case, case_repo
    ):
        """Benchmark complete_session operation - target <150ms p95."""
        iterations = 50
        timings = []

        for i in range(iterations):
            # Create new case and session for each iteration
            case = Case(
                case_id=f"case_{uuid4().hex[:12]}",
                user_id=sample_case.user_id,
                organization_id=sample_case.organization_id,
                title=f"Complete Test Case {i}",
                description="",
                status=CaseStatus.INQUIRY,
            )
            await case_repo.save(case)

            session = await session_service.create_session(
                case_id=case.case_id,
                organization_id=case.organization_id,
                user_id=case.user_id,
            )

            duration = await time_async_operation(
                session_service.complete_session(
                    session.session_id,
                    case.organization_id,
                    f"Findings summary {i}",
                )
            )
            timings.append(duration)

        passed = report_benchmark("Complete Session", timings, 150)
        assert passed, f"P95 exceeded 150ms target"


# ============================================================
# List Sessions Benchmarks
# ============================================================


class TestListSessionsBenchmarks:
    """Benchmark session listing operations."""

    @pytest.mark.asyncio
    async def test_benchmark_list_sessions_50(self, session_service, sample_case):
        """Benchmark list_sessions with 50 sessions - target <300ms p95."""
        # Create 50 sessions
        for i in range(50):
            session = await session_service.create_session(
                case_id=sample_case.case_id,
                organization_id=sample_case.organization_id,
                user_id=sample_case.user_id,
            )
            await session_service.complete_session(
                session.session_id,
                sample_case.organization_id,
                f"Finding {i}",
            )

        iterations = 50
        timings = []

        for _ in range(iterations):
            duration = await time_async_operation(
                session_service.list_sessions(
                    sample_case.case_id,
                    sample_case.organization_id,
                    limit=50,
                )
            )
            timings.append(duration)

        passed = report_benchmark("List Sessions (50)", timings, 300)
        assert passed, f"P95 exceeded 300ms target"


# ============================================================
# Get Session with Executions Benchmarks
# ============================================================


class TestGetSessionWithExecutionsBenchmarks:
    """Benchmark get session with executions operations."""

    @pytest.mark.skip(
        reason="Agent execution methods not yet implemented in DatabaseCaseRepository after migration"
    )
    @pytest.mark.asyncio
    async def test_benchmark_get_session_with_executions(
        self, session_service, sample_case, case_repo
    ):
        """Benchmark get_session_with_executions - target <250ms p95."""
        session = await session_service.create_session(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id=sample_case.user_id,
        )

        # Create 10 executions using case_repo (migrated from execution_repo)
        for i in range(10):
            execution = AgentExecution(
                execution_id=create_test_execution_id(),
                case_id=sample_case.case_id,
                agent_type=AgentType.INVESTIGATOR,
                agent_model="gpt-4",
                status=ExecutionStatus.COMPLETED,
            )
            await case_repo.create_agent_execution(execution)

        iterations = 50
        timings = []

        for _ in range(iterations):
            duration = await time_async_operation(
                session_service.get_session_with_executions(
                    session.session_id,
                    sample_case.organization_id,
                    include_tool_calls=False,
                )
            )
            timings.append(duration)

        passed = report_benchmark("Get Session with Executions (10)", timings, 250)
        assert passed, f"P95 exceeded 250ms target"


# ============================================================
# Add Execution to Session Benchmarks
# ============================================================


class TestAddExecutionBenchmarks:
    """Benchmark add execution to session operations."""

    @pytest.mark.skip(
        reason="Agent execution methods not yet implemented in DatabaseCaseRepository after migration"
    )
    @pytest.mark.asyncio
    async def test_benchmark_add_execution_to_session(
        self, session_service, sample_case, case_repo
    ):
        """Benchmark add_execution_to_session operation - target <150ms p95."""
        session = await session_service.create_session(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id=sample_case.user_id,
        )

        iterations = 50
        timings = []

        for i in range(iterations):
            execution = AgentExecution(
                execution_id=create_test_execution_id(),
                case_id=sample_case.case_id,
                agent_type=AgentType.INVESTIGATOR,
                agent_model="gpt-4",
                status=ExecutionStatus.COMPLETED,
            )
            await case_repo.create_agent_execution(execution)

            duration = await time_async_operation(
                session_service.add_execution_to_session(
                    session.session_id,
                    sample_case.organization_id,
                    execution.execution_id,
                    token_usage=100,
                )
            )
            timings.append(duration)

        passed = report_benchmark("Add Execution to Session", timings, 150)
        assert passed, f"P95 exceeded 150ms target"


# ============================================================
# Check Budget Exceeded Benchmarks
# ============================================================


class TestCheckBudgetBenchmarks:
    """Benchmark budget check operations."""

    @pytest.mark.asyncio
    async def test_benchmark_check_budget_exceeded(self, session_service, sample_case):
        """Benchmark check_budget_exceeded operation - target <100ms p95."""
        session = await session_service.create_session(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id=sample_case.user_id,
            token_budget_limit=10000,
        )

        iterations = 100
        timings = []

        for _ in range(iterations):
            duration = await time_async_operation(
                session_service.check_budget_exceeded(
                    session.session_id,
                    sample_case.organization_id,
                )
            )
            timings.append(duration)

        passed = report_benchmark("Check Budget Exceeded", timings, 100)
        assert passed, f"P95 exceeded 100ms target"


# ============================================================
# Get Statistics Benchmarks
# ============================================================


class TestGetStatisticsBenchmarks:
    """Benchmark statistics operations."""

    @pytest.mark.asyncio
    async def test_benchmark_get_statistics_100_sessions(
        self, session_service, sample_case
    ):
        """Benchmark get_session_statistics with 100 sessions - target <500ms p95."""
        # Create 100 sessions
        for i in range(100):
            session = await session_service.create_session(
                case_id=sample_case.case_id,
                organization_id=sample_case.organization_id,
                user_id=sample_case.user_id,
            )
            await session_service.complete_session(
                session.session_id,
                sample_case.organization_id,
                f"Finding {i}",
            )

        iterations = 30
        timings = []

        for _ in range(iterations):
            duration = await time_async_operation(
                session_service.get_session_statistics(
                    sample_case.case_id,
                    sample_case.organization_id,
                )
            )
            timings.append(duration)

        passed = report_benchmark("Get Statistics (100 sessions)", timings, 500)
        assert passed, f"P95 exceeded 500ms target"
