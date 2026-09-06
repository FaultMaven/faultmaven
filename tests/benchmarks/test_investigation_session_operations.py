"""Benchmark investigation session management operations.

Establishes performance baselines for investigation session CRUD operations.

Performance Targets:
- Session creation: < 200ms
- Session retrieval: < 100ms
- Session update: < 150ms
- List sessions by case (100 sessions): < 200ms
- Get active session: < 100ms
- CASCADE delete (session → executions): < 500ms

Every wall-clock assertion here goes through ``measure_min_latency`` (warm-up
call, then N samples, compare the MINIMUM). See that helper's docstring for
why the minimum rather than a single sample or a small-n p95.

Run with:
    pytest tests/benchmarks/test_investigation_session_operations.py -m benchmark -v
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from faultmaven.infrastructure.persistence.investigation_session_repository import (
    DatabaseInvestigationSessionRepository,
)
from faultmaven.models.investigation_session import InvestigationSession, SessionState
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    InvestigationStrategy,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)

from .conftest import generate_case_id, measure_min_latency


def generate_session_id() -> str:
    """Generate a valid session ID."""
    return f"sess_{uuid4().hex[:12]}"


def create_sample_session(
    case_id: str,
    user_id: str = "benchmark-user-001",
    organization_id: str = "benchmark-org-001",
    state: SessionState = SessionState.ACTIVE,
    session_goal: str = None,
    token_budget_limit: int = None,
    started_at: datetime = None,
) -> InvestigationSession:
    """Create a sample investigation session for benchmarking."""
    return InvestigationSession(
        session_id=generate_session_id(),
        case_id=case_id,
        user_id=user_id,
        enterprise_id=organization_id,
        state=state,
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
        enterprise_id="benchmark-org-001",
        title="Benchmark Session Case",
        description="Case for benchmarking investigation session operations",
        state=CaseState.INQUIRY,
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

        Target: < 200ms

        A fresh session per sample: ``create`` inserts by primary key and
        raises on a duplicate ``session_id``.
        """

        async def _fresh_session():
            return create_sample_session(benchmark_case.case_id)

        measured = await measure_min_latency(
            session_repository.create, setup=_fresh_session
        )

        assert measured.result is not None
        assert (
            measured.best < 0.200
        ), f"Session creation latency {measured.report()} exceeds 200ms target"
        print(f"\n  Session creation latency: {measured.report()}")

    @pytest.mark.asyncio
    async def test_session_creation_with_metadata_latency(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of creating session with rich metadata.

        Target: < 200ms
        """

        async def _fresh_session():
            session = create_sample_session(benchmark_case.case_id)
            session.metadata = {
                "environment": "production",
                "priority": "high",
                "tags": ["timeout", "database", "connection_pool"],
                "nested": {"key": "value", "list": [1, 2, 3]},
            }
            return session

        measured = await measure_min_latency(
            session_repository.create, setup=_fresh_session
        )

        assert measured.result is not None
        assert measured.result.metadata is not None
        assert (
            measured.best < 0.200
        ), f"Session with metadata creation latency {measured.report()} exceeds 200ms target"
        print(f"\n  Session with metadata creation latency: {measured.report()}")

    @pytest.mark.asyncio
    async def test_batch_session_creation_throughput(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        case_repository: SQLiteCaseRepository,
        benchmark_session,
    ):
        """Measure throughput of creating multiple sessions.

        Target: > 20 sessions/second

        ``samples`` is cut to 3: the measured unit is a 30-write batch, which
        already averages per-write noise, and each sample's setup has to write
        30 cases first. The minimum batch duration gives the MAXIMUM
        throughput — the same one-sided-error argument the latency sites make.
        """

        batch_size = 30

        async def _fresh_batch() -> list:
            # Untimed: the sessions being created need parent cases, and a
            # session_id may not repeat, so both are rebuilt per sample.
            sessions = []
            for i in range(batch_size):
                case = Case(
                    case_id=generate_case_id(),
                    user_id="benchmark-user-001",
                    enterprise_id="benchmark-org-001",
                    title=f"Batch Benchmark Case {i}",
                    description="For batch session creation benchmark",
                    state=CaseState.INQUIRY,
                )
                saved = await case_repository.save(case)
                sessions.append(create_sample_session(saved.case_id))
            return sessions

        async def _create_batch(sessions: list) -> int:
            for session in sessions:
                await session_repository.create(session)
            return len(sessions)

        measured = await measure_min_latency(
            _create_batch, samples=3, setup=_fresh_batch
        )

        throughput = batch_size / measured.best
        assert (
            throughput > 20
        ), f"Session creation throughput {throughput:.1f}/sec below 20/sec target"
        print(
            f"\n  Batch creation throughput: {throughput:.1f} sessions/sec "
            f"({batch_size} items, batch {measured.report()})"
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

        # Benchmark retrieval — read-only, so every sample is the same work.
        measured = await measure_min_latency(
            lambda: session_repository.get_by_id(session.session_id)
        )

        assert measured.result is not None
        assert (
            measured.best < 0.100
        ), f"Session retrieval latency {measured.report()} exceeds 100ms target"
        print(f"\n  Session retrieval latency: {measured.report()}")

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

        # Benchmark get active — read-only.
        measured = await measure_min_latency(
            lambda: session_repository.get_active_session(benchmark_case.case_id)
        )

        assert measured.result is not None
        assert measured.result.state == SessionState.ACTIVE
        assert (
            measured.best < 0.100
        ), f"Get active session latency {measured.report()} exceeds 100ms target"
        print(f"\n  Get active session latency: {measured.report()}")

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
            state = SessionState.COMPLETED if i % 2 == 0 else SessionState.ABANDONED
            session = create_sample_session(benchmark_case.case_id, state=state)
            await session_repository.create(session)

        # Benchmark list operation — read-only.
        measured = await measure_min_latency(
            lambda: session_repository.list_by_case_id(benchmark_case.case_id)
        )

        assert len(measured.result) == 100
        assert (
            measured.best < 0.200
        ), f"List sessions latency {measured.report()} exceeds 200ms target"
        print(
            f"\n  List sessions latency: {measured.report()} "
            f"({len(measured.result)} items)"
        )

    @pytest.mark.asyncio
    async def test_list_sessions_with_status_filter_latency(
        self,
        session_repository: DatabaseInvestigationSessionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of listing sessions with state filter.

        Target: < 150ms for filtered query
        """
        # Setup - Create mixed state sessions
        for i in range(50):
            session = create_sample_session(
                benchmark_case.case_id,
                state=SessionState.COMPLETED,
            )
            await session_repository.create(session)

        for i in range(50):
            session = create_sample_session(
                benchmark_case.case_id,
                state=SessionState.ABANDONED,
            )
            await session_repository.create(session)

        # Benchmark filtered list — read-only.
        measured = await measure_min_latency(
            lambda: session_repository.list_by_case_id(
                benchmark_case.case_id,
                state=SessionState.COMPLETED,
            )
        )

        assert len(measured.result) == 50
        assert (
            measured.best < 0.150
        ), f"Filtered list latency {measured.report()} exceeds 150ms target"
        print(
            f"\n  Filtered list latency: {measured.report()} "
            f"({len(measured.result)} items)"
        )

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
                enterprise_id="benchmark-org-001",
                title=f"User Benchmark Case {i}",
                description="For user list benchmark",
                state=CaseState.INQUIRY,
            )
            saved = await case_repository.save(case)

            session = create_sample_session(saved.case_id, user_id=user_id)
            await session_repository.create(session)

        # Benchmark list by user — read-only.
        measured = await measure_min_latency(
            lambda: session_repository.list_by_user_id(user_id, limit=50)
        )

        assert len(measured.result) == 50
        assert (
            measured.best < 0.200
        ), f"List by user latency {measured.report()} exceeds 200ms target"
        print(
            f"\n  List by user latency: {measured.report()} "
            f"({len(measured.result)} items)"
        )


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
        """Measure latency of updating session state.

        Target: < 150ms
        """
        # Setup - Create test session
        session = create_sample_session(benchmark_case.case_id)
        await session_repository.create(session)

        # Update state
        session.pause()

        # Benchmark update. Repeating writes the SAME already-paused session
        # to the SAME row: `update` is a plain field-by-field write with no
        # version check, so every sample issues the same statement and no row
        # is added (the repository stamps its own `updated_at`, so that one
        # column differs). The state transition itself happens once, above.
        measured = await measure_min_latency(lambda: session_repository.update(session))

        assert measured.result is not None
        assert measured.result.state == SessionState.PAUSED
        assert (
            measured.best < 0.150
        ), f"Status update latency {measured.report()} exceeds 150ms target"
        print(f"\n  Session state update latency: {measured.report()}")

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

        # Benchmark update — same already-completed session each sample.
        measured = await measure_min_latency(lambda: session_repository.update(session))

        assert measured.result is not None
        assert measured.result.state == SessionState.COMPLETED
        assert (
            measured.best < 0.150
        ), f"Completion update latency {measured.report()} exceeds 150ms target"
        print(f"\n  Session completion update latency: {measured.report()}")

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

        # Benchmark update. The three executions are accumulated ONCE, above:
        # `add_agent_execution` mutates in-memory counters, so calling it
        # inside the loop would write a different (growing) row each sample.
        measured = await measure_min_latency(lambda: session_repository.update(session))

        assert measured.result is not None
        assert measured.result.total_token_usage == 2250
        assert measured.result.total_agent_executions == 3
        assert (
            measured.best < 0.150
        ), f"Token usage update latency {measured.report()} exceeds 150ms target"
        print(f"\n  Token usage update latency: {measured.report()}")


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

        Each sample deletes a session created for it, untimed. Repeating the
        delete of one id would measure a MISS from the second sample onwards
        (``delete`` returns False for an absent row) — a cheaper operation
        than the one this test names, so the assertion would weaken silently.
        """

        async def _a_session_to_delete() -> str:
            session = create_sample_session(benchmark_case.case_id)
            await session_repository.create(session)
            return session.session_id

        measured = await measure_min_latency(
            session_repository.delete, setup=_a_session_to_delete
        )

        assert measured.result is True
        assert (
            measured.best < 0.150
        ), f"Session delete latency {measured.report()} exceeds 150ms target"
        print(f"\n  Session delete latency: {measured.report()}")


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

        # Benchmark count — read-only.
        measured = await measure_min_latency(
            lambda: session_repository.count_by_case_id(benchmark_case.case_id)
        )

        assert measured.result == 50
        assert (
            measured.best < 0.050
        ), f"Count latency {measured.report()} exceeds 50ms target"
        print(
            f"\n  Count sessions latency: {measured.report()} "
            f"({measured.result} items)"
        )


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

        A fresh session per sample: the workload creates and then transitions
        one session through its whole life, none of which can be replayed on
        the same object.
        """

        async def _fresh_session():
            return create_sample_session(benchmark_case.case_id)

        async def _lifecycle(session):
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
            return session

        measured = await measure_min_latency(_lifecycle, setup=_fresh_session)

        assert measured.result.state == SessionState.COMPLETED
        assert (
            measured.best < 0.600
        ), f"Lifecycle workload latency {measured.report()} exceeds 600ms target"
        print(f"\n  Session lifecycle workload latency: {measured.report()}")

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

        A fresh session per sample, for the same reason as the lifecycle
        workload: a COMPLETED session cannot be paused again.
        """

        async def _fresh_session():
            return create_sample_session(benchmark_case.case_id)

        async def _pause_resume(session):
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
            return session

        measured = await measure_min_latency(_pause_resume, setup=_fresh_session)

        assert measured.result.state == SessionState.COMPLETED
        assert (
            measured.best < 0.900
        ), f"Pause/resume workload latency {measured.report()} exceeds 900ms target"
        print(f"\n  Pause/resume workload latency: {measured.report()}")
