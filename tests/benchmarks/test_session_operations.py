"""Benchmark session management operations.

Establishes performance baselines for session CRUD operations.

Performance Targets (p95 latency):
- Session creation: < 50ms
- Session retrieval: < 30ms
- Session cleanup: > 1000 sessions/second

Run with:
    pytest tests/benchmarks/test_session_operations.py -m benchmark -v
"""

import pytest
import time
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from faultmaven.modules.auth.domain.models.session import Session
from faultmaven.modules.auth.infrastructure.repositories.session_repository import (
    DatabaseSessionRepository,
)


@pytest.mark.benchmark
class TestSessionOperationPerformance:
    """Benchmark session management operations."""

    @pytest.mark.asyncio
    async def test_session_creation_latency(
        self,
        session_repository: DatabaseSessionRepository,
        benchmark_session,
    ):
        """Measure latency of creating a session.

        Target: p95 < 50ms
        """
        session = Session(
            session_id=str(uuid4()),
            user_id="benchmark-user-001",
            created_at=datetime.now(timezone.utc),
            last_accessed=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            metadata={"source": "benchmark", "device": "test"},
        )

        start = time.perf_counter()
        result = await session_repository.create_session(session)
        latency = time.perf_counter() - start

        assert result is not None
        assert (
            latency < 0.050
        ), f"Session creation latency {latency*1000:.1f}ms exceeds 50ms target"
        print(f"\n  Session creation latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_session_retrieval_latency(
        self,
        session_repository: DatabaseSessionRepository,
        benchmark_session,
    ):
        """Measure latency of retrieving a session.

        Target: p95 < 30ms
        """
        # Setup - Create test session
        session_id = str(uuid4())
        session = Session(
            session_id=session_id,
            user_id="benchmark-user-001",
            created_at=datetime.now(timezone.utc),
            last_accessed=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        await session_repository.create_session(session)

        # Benchmark retrieval
        start = time.perf_counter()
        result = await session_repository.get_session(session_id)
        latency = time.perf_counter() - start

        assert result is not None
        assert (
            latency < 0.030
        ), f"Session retrieval latency {latency*1000:.1f}ms exceeds 30ms target"
        print(f"\n  Session retrieval latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_batch_session_creation_throughput(
        self,
        session_repository: DatabaseSessionRepository,
        benchmark_session,
    ):
        """Measure throughput of creating multiple sessions.

        Target: > 100 sessions/second
        """
        num_sessions = 100

        sessions = [
            Session(
                session_id=str(uuid4()),
                user_id=f"benchmark-user-{i:04d}",
                created_at=datetime.now(timezone.utc),
                last_accessed=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            for i in range(num_sessions)
        ]

        start = time.perf_counter()
        for session in sessions:
            await session_repository.create_session(session)
        duration = time.perf_counter() - start

        throughput = num_sessions / duration
        assert (
            throughput > 100
        ), f"Session creation throughput {throughput:.1f} sessions/sec below 100/sec target"
        print(
            f"\n  Batch session creation: {throughput:.1f} sessions/sec ({num_sessions} in {duration:.2f}s)"
        )

    @pytest.mark.asyncio
    async def test_get_sessions_by_user_latency(
        self,
        session_repository: DatabaseSessionRepository,
        benchmark_session,
    ):
        """Measure latency of retrieving all sessions for a user.

        Target: < 50ms for 10 sessions
        """
        user_id = "benchmark-user-multi-session"

        # Setup - Create multiple sessions for user
        for i in range(10):
            session = Session(
                session_id=str(uuid4()),
                user_id=user_id,
                created_at=datetime.now(timezone.utc),
                last_accessed=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            await session_repository.create_session(session)

        # Benchmark retrieval
        start = time.perf_counter()
        result = await session_repository.get_sessions_by_user(user_id)
        latency = time.perf_counter() - start

        assert len(result) == 10
        assert (
            latency < 0.050
        ), f"Get sessions by user latency {latency*1000:.1f}ms exceeds 50ms target"
        print(
            f"\n  Get sessions by user latency: {latency*1000:.1f}ms ({len(result)} sessions)"
        )

    @pytest.mark.asyncio
    async def test_session_update_last_accessed_latency(
        self,
        session_repository: DatabaseSessionRepository,
        benchmark_session,
    ):
        """Measure latency of updating session last_accessed.

        Target: < 30ms
        """
        # Setup
        session_id = str(uuid4())
        session = Session(
            session_id=session_id,
            user_id="benchmark-user-001",
            created_at=datetime.now(timezone.utc),
            last_accessed=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        await session_repository.create_session(session)

        # Benchmark update
        start = time.perf_counter()
        result = await session_repository.update_last_accessed(session_id)
        latency = time.perf_counter() - start

        assert result is True
        assert (
            latency < 0.030
        ), f"Update last_accessed latency {latency*1000:.1f}ms exceeds 30ms target"
        print(f"\n  Update last_accessed latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_session_cleanup_throughput(
        self,
        session_repository: DatabaseSessionRepository,
        benchmark_session,
    ):
        """Measure throughput of cleaning up expired sessions.

        Target: > 500 sessions/second (batch delete)
        """
        # Setup - Create expired sessions
        num_sessions = 500
        expired_time = datetime.now(timezone.utc) - timedelta(hours=2)

        for i in range(num_sessions):
            session = Session(
                session_id=str(uuid4()),
                user_id=f"cleanup-user-{i}",
                created_at=expired_time,
                last_accessed=expired_time,
                expires_at=expired_time + timedelta(hours=1),  # Expired 1 hour ago
            )
            await session_repository.create_session(session)

        # Benchmark cleanup
        start = time.perf_counter()
        deleted_count = await session_repository.cleanup_expired_sessions()
        duration = time.perf_counter() - start

        if duration > 0:
            throughput = deleted_count / duration
        else:
            throughput = float("inf")

        assert deleted_count == num_sessions
        assert (
            throughput > 500
        ), f"Session cleanup throughput {throughput:.1f} sessions/sec below 500/sec target"
        print(
            f"\n  Session cleanup throughput: {throughput:.1f} sessions/sec ({deleted_count} deleted in {duration:.3f}s)"
        )

    @pytest.mark.asyncio
    async def test_session_delete_latency(
        self,
        session_repository: DatabaseSessionRepository,
        benchmark_session,
    ):
        """Measure latency of deleting a session.

        Target: < 30ms
        """
        # Setup
        session_id = str(uuid4())
        session = Session(
            session_id=session_id,
            user_id="benchmark-user-001",
            created_at=datetime.now(timezone.utc),
            last_accessed=datetime.now(timezone.utc),
        )
        await session_repository.create_session(session)

        # Benchmark delete
        start = time.perf_counter()
        result = await session_repository.delete_session(session_id)
        latency = time.perf_counter() - start

        assert result is True
        assert (
            latency < 0.030
        ), f"Session delete latency {latency*1000:.1f}ms exceeds 30ms target"
        print(f"\n  Session delete latency: {latency*1000:.1f}ms")
