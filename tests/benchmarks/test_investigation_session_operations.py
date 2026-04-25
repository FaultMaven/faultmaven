"""Benchmark investigation session management operations.

Establishes performance baselines for investigation session CRUD operations.

Performance Targets (p95 latency):
- Session creation: < 200ms
- Session retrieval: < 100ms
- Session update: < 150ms
- List sessions by case (100 sessions): < 200ms
- Get active session: < 100ms
- CASCADE delete (session → executions): < 500ms

Run with:
    pytest tests/benchmarks/test_investigation_session_operations.py -m benchmark -v
"""

import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)
from faultmaven.infrastructure.persistence.investigation_session_repository import (
    DatabaseInvestigationSessionRepository,
)
from faultmaven.models.investigation_session import InvestigationSession, SessionStatus
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    InvestigationStrategy,
)

from .conftest import generate_case_id


def generate_session_id() -> str:
    """Generate a valid session ID."""
    return f"sess_{uuid4().hex[:12]}"


def create_sample_session(
    case_id: str,
    user_id: str = "benchmark-user-001",
    organization_id: str = "benchmark-org-001",
    status: SessionStatus = SessionStatus.ACTIVE,
    session_goal: str = None,
    token_budget_limit: int = None,
    started_at: datetime = None,
) -> InvestigationSession:
    """Create a sample investigation session for benchmarking."""
    return InvestigationSession(
        session_id=generate_session_id(),
        case_id=case_id,
        user_id=user_id,
        organization_id=organization_id,
        status=status,
        session_goal=session_goal or "Benchmark investigation session",
        token_budget_limit=token_budget_limit,
        started_at=started_at or datetime.now(timezone.utc),
    )


@pytest.fixture
async def session_repository(
    benchmark_session,
) -> DatabaseInvestigationSessionRepository:
    """Create investigation session repository for benchmarks."""
    return DatabaseInvestigationSessionRepository(benchmark_session)


@pytest.fixture
async def benchmark_case(case_repository: SQLiteCaseRepository) -> Case:
    """Create a case for benchmark session operations."""
    case = Case(
        case_id=generate_case_id(),
        user_id="benchmark-user-001",
        organization_id="benchmark-org-001",
        title="Benchmark Session Case",
        description="Case for benchmarking investigation session operations",
        status=CaseStatus.INQUIRY,
        investigation_strategy=InvestigationStrategy.POST_MORTEM,
    )
    return await case_repository.save(case)


@pytest.mark.benchmark
class TestSessionCreationPerformance:
    """Benchmark session creation operations."""

    @pytest.mark.asyncio
    async def test_single_session_creation_latency(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of creating a single investigation session.

        Target: p95 < 200ms
        """
        session = create_sample_session(benchmark_case.case_id)

        start = time.perf_counter()
        result = await session_repository.create(session)
        latency = time.perf_counter() - start

        assert result is not None
        assert (
            latency < 0.200
        ), f"Session creation latency {latency*1000:.1f}ms exceeds 200ms target"
        print(f"\n  Session creation latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_session_creation_with_metadata_latency(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of creating session with rich metadata.

        Target: p95 < 200ms
        """
        session = create_sample_session(benchmark_case.case_id)
        session.metadata = {
            "environment": "production",
            "priority": "high",
            "tags": ["timeout", "database", "connection_pool"],
            "nested": {"key": "value", "list": [1, 2, 3]},
        }

        start = time.perf_counter()
        result = await session_repository.create(session)
        latency = time.perf_counter() - start

        assert result is not None
        assert result.metadata is not None
        assert (
            latency < 0.200
        ), f"Session with metadata creation latency {latency*1000:.1f}ms exceeds 200ms target"
        print(f"\n  Session with metadata creation latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_batch_session_creation_throughput(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        case_repository: SQLiteCaseRepository,
        benchmark_session,
    ):
        """Measure throughput of creating multiple sessions.

        Target: > 20 sessions/second
        """
        # Create multiple cases for sessions
        cases = []
        for i in range(30):
            case = Case(
                case_id=generate_case_id(),
                user_id="benchmark-user-001",
                organization_id="benchmark-org-001",
                title=f"Batch Benchmark Case {i}",
                description="For batch session creation benchmark",
                status=CaseStatus.INQUIRY,
            )
            saved = await case_repository.save(case)
            cases.append(saved)

        sessions = [create_sample_session(case.case_id) for case in cases]

        start = time.perf_counter()
        for session in sessions:
            await session_repository.create(session)
        duration = time.perf_counter() - start

        throughput = len(sessions) / duration
        assert (
            throughput > 20
        ), f"Session creation throughput {throughput:.1f}/sec below 20/sec target"
        print(
            f"\n  Batch creation throughput: {throughput:.1f} sessions/sec "
            f"({len(sessions)} items in {duration:.2f}s)"
        )


@pytest.mark.benchmark
class TestSessionRetrievalPerformance:
    """Benchmark session retrieval operations."""

    @pytest.mark.asyncio
    async def test_single_session_retrieval_latency(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of retrieving a single session.

        Target: p95 < 100ms
        """
        # Setup - Create test session
        session = create_sample_session(benchmark_case.case_id)
        await session_repository.create(session)

        # Benchmark retrieval
        start = time.perf_counter()
        result = await session_repository.get_by_id(session.session_id)
        latency = time.perf_counter() - start

        assert result is not None
        assert (
            latency < 0.100
        ), f"Session retrieval latency {latency*1000:.1f}ms exceeds 100ms target"
        print(f"\n  Session retrieval latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_get_active_session_latency(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of getting active session for a case.

        Target: p95 < 100ms
        """
        # Setup - Create active session
        session = create_sample_session(benchmark_case.case_id)
        await session_repository.create(session)

        # Benchmark get active
        start = time.perf_counter()
        result = await session_repository.get_active_session(benchmark_case.case_id)
        latency = time.perf_counter() - start

        assert result is not None
        assert result.status == SessionStatus.ACTIVE
        assert (
            latency < 0.100
        ), f"Get active session latency {latency*1000:.1f}ms exceeds 100ms target"
        print(f"\n  Get active session latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_list_sessions_by_case_latency(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of listing sessions for a case.

        Target: < 200ms for 100 sessions
        """
        # Setup - Create 100 sessions with different statuses
        for i in range(100):
            status = SessionStatus.COMPLETED if i % 2 == 0 else SessionStatus.ABANDONED
            session = create_sample_session(benchmark_case.case_id, status=status)
            await session_repository.create(session)

        # Benchmark list operation
        start = time.perf_counter()
        result = await session_repository.list_by_case_id(benchmark_case.case_id)
        latency = time.perf_counter() - start

        assert len(result) == 100
        assert (
            latency < 0.200
        ), f"List sessions latency {latency*1000:.1f}ms exceeds 200ms target"
        print(f"\n  List sessions latency: {latency*1000:.1f}ms ({len(result)} items)")

    @pytest.mark.asyncio
    async def test_list_sessions_with_status_filter_latency(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of listing sessions with status filter.

        Target: < 150ms for filtered query
        """
        # Setup - Create mixed status sessions
        for i in range(50):
            session = create_sample_session(
                benchmark_case.case_id,
                status=SessionStatus.COMPLETED,
            )
            await session_repository.create(session)

        for i in range(50):
            session = create_sample_session(
                benchmark_case.case_id,
                status=SessionStatus.ABANDONED,
            )
            await session_repository.create(session)

        # Benchmark filtered list
        start = time.perf_counter()
        result = await session_repository.list_by_case_id(
            benchmark_case.case_id,
            status=SessionStatus.COMPLETED,
        )
        latency = time.perf_counter() - start

        assert len(result) == 50
        assert (
            latency < 0.150
        ), f"Filtered list latency {latency*1000:.1f}ms exceeds 150ms target"
        print(f"\n  Filtered list latency: {latency*1000:.1f}ms ({len(result)} items)")

    @pytest.mark.asyncio
    async def test_list_sessions_by_user_latency(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        case_repository: SQLiteCaseRepository,
        benchmark_session,
    ):
        """Measure latency of listing sessions by user.

        Target: < 200ms for paginated query
        """
        user_id = "benchmark-list-user"

        # Create cases and sessions for user
        for i in range(50):
            case = Case(
                case_id=generate_case_id(),
                user_id=user_id,
                organization_id="benchmark-org-001",
                title=f"User Benchmark Case {i}",
                description="For user list benchmark",
                status=CaseStatus.INQUIRY,
            )
            saved = await case_repository.save(case)

            session = create_sample_session(saved.case_id, user_id=user_id)
            await session_repository.create(session)

        # Benchmark list by user
        start = time.perf_counter()
        result = await session_repository.list_by_user_id(user_id, limit=50)
        latency = time.perf_counter() - start

        assert len(result) == 50
        assert (
            latency < 0.200
        ), f"List by user latency {latency*1000:.1f}ms exceeds 200ms target"
        print(f"\n  List by user latency: {latency*1000:.1f}ms ({len(result)} items)")


@pytest.mark.benchmark
class TestSessionUpdatePerformance:
    """Benchmark session update operations."""

    @pytest.mark.asyncio
    async def test_session_status_update_latency(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of updating session status.

        Target: < 150ms
        """
        # Setup - Create test session
        session = create_sample_session(benchmark_case.case_id)
        await session_repository.create(session)

        # Update status
        session.pause()

        # Benchmark update
        start = time.perf_counter()
        result = await session_repository.update(session)
        latency = time.perf_counter() - start

        assert result is not None
        assert result.status == SessionStatus.PAUSED
        assert (
            latency < 0.150
        ), f"Status update latency {latency*1000:.1f}ms exceeds 150ms target"
        print(f"\n  Session status update latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_session_completion_update_latency(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of completing a session.

        Target: < 150ms
        """
        # Setup - Create and work with session
        session = create_sample_session(benchmark_case.case_id)
        session.add_agent_execution(token_usage=500)
        await session_repository.create(session)

        # Complete session
        session.complete(
            "Root cause identified: connection pool exhaustion in database layer"
        )

        # Benchmark update
        start = time.perf_counter()
        result = await session_repository.update(session)
        latency = time.perf_counter() - start

        assert result is not None
        assert result.status == SessionStatus.COMPLETED
        assert (
            latency < 0.150
        ), f"Completion update latency {latency*1000:.1f}ms exceeds 150ms target"
        print(f"\n  Session completion update latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_session_token_usage_update_latency(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of updating session with token usage.

        Target: < 150ms
        """
        # Setup - Create session
        session = create_sample_session(
            benchmark_case.case_id,
            token_budget_limit=10000,
        )
        await session_repository.create(session)

        # Add multiple executions
        session.add_agent_execution(token_usage=500)
        session.add_agent_execution(token_usage=750)
        session.add_agent_execution(token_usage=1000)

        # Benchmark update
        start = time.perf_counter()
        result = await session_repository.update(session)
        latency = time.perf_counter() - start

        assert result is not None
        assert result.total_token_usage == 2250
        assert result.total_agent_executions == 3
        assert (
            latency < 0.150
        ), f"Token usage update latency {latency*1000:.1f}ms exceeds 150ms target"
        print(f"\n  Token usage update latency: {latency*1000:.1f}ms")


@pytest.mark.benchmark
class TestSessionDeletePerformance:
    """Benchmark session deletion operations."""

    @pytest.mark.asyncio
    async def test_session_delete_latency(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of deleting a session.

        Target: < 150ms
        """
        # Setup - Create test session
        session = create_sample_session(benchmark_case.case_id)
        await session_repository.create(session)

        # Benchmark delete
        start = time.perf_counter()
        result = await session_repository.delete(session.session_id)
        latency = time.perf_counter() - start

        assert result is True
        assert (
            latency < 0.150
        ), f"Session delete latency {latency*1000:.1f}ms exceeds 150ms target"
        print(f"\n  Session delete latency: {latency*1000:.1f}ms")


@pytest.mark.benchmark
class TestSessionCountPerformance:
    """Benchmark session count operations."""

    @pytest.mark.asyncio
    async def test_count_sessions_latency(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of counting sessions for a case.

        Target: < 50ms
        """
        # Setup - Create 50 sessions
        for i in range(50):
            session = create_sample_session(benchmark_case.case_id)
            await session_repository.create(session)

        # Benchmark count
        start = time.perf_counter()
        count = await session_repository.count_by_case_id(benchmark_case.case_id)
        latency = time.perf_counter() - start

        assert count == 50
        assert (
            latency < 0.050
        ), f"Count latency {latency*1000:.1f}ms exceeds 50ms target"
        print(f"\n  Count sessions latency: {latency*1000:.1f}ms ({count} items)")


@pytest.mark.benchmark
class TestSessionMixedWorkloadPerformance:
    """Benchmark mixed session workload patterns."""

    @pytest.mark.asyncio
    async def test_session_lifecycle_workload(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure performance of typical session lifecycle workload.

        Simulates: create → update (add execution) → update (add execution) → complete
        Target: < 600ms total
        """
        # Create session
        session = create_sample_session(benchmark_case.case_id)

        start = time.perf_counter()

        # Create
        await session_repository.create(session)

        # First execution update
        session.add_agent_execution(token_usage=500)
        await session_repository.update(session)

        # Second execution update
        session.add_agent_execution(token_usage=750)
        await session_repository.update(session)

        # Complete
        session.complete("Investigation complete")
        await session_repository.update(session)

        total_latency = time.perf_counter() - start

        assert session.status == SessionStatus.COMPLETED
        assert (
            total_latency < 0.600
        ), f"Lifecycle workload latency {total_latency*1000:.1f}ms exceeds 600ms target"
        print(f"\n  Session lifecycle workload latency: {total_latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_session_pause_resume_workload(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure performance of pause/resume workflow.

        Simulates: create → pause → resume → pause → resume → complete
        Target: < 900ms total
        """
        session = create_sample_session(benchmark_case.case_id)

        start = time.perf_counter()

        # Create
        await session_repository.create(session)

        # Pause
        session.pause()
        await session_repository.update(session)

        # Resume
        session.resume()
        await session_repository.update(session)

        # Add execution
        session.add_agent_execution(token_usage=500)
        await session_repository.update(session)

        # Pause again
        session.pause()
        await session_repository.update(session)

        # Resume and complete
        session.resume()
        session.complete("Done after pause/resume cycles")
        await session_repository.update(session)

        total_latency = time.perf_counter() - start

        assert session.status == SessionStatus.COMPLETED
        assert (
            total_latency < 0.900
        ), f"Pause/resume workload latency {total_latency*1000:.1f}ms exceeds 900ms target"
        print(f"\n  Pause/resume workload latency: {total_latency*1000:.1f}ms")
