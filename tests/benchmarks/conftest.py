"""Benchmark test fixtures.

Provides database fixtures optimized for performance benchmarking with
minimal overhead from logging and other instrumentation.
"""

import asyncio
from typing import AsyncGenerator
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.database_case_repository import DatabaseCaseRepository
from faultmaven.infrastructure.persistence.session_repository import DatabaseSessionRepository
from faultmaven.infrastructure.persistence.evidence_artifact_repository import (
    DatabaseEvidenceArtifactRepository,
)
from faultmaven.infrastructure.persistence.agent_execution_repository import (
    DatabaseAgentExecutionRepository,
)


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


@pytest.fixture
async def case_repository(benchmark_session) -> DatabaseCaseRepository:
    """Create case repository for benchmarks."""
    return DatabaseCaseRepository(benchmark_session)


@pytest.fixture
async def session_repository(benchmark_session) -> DatabaseSessionRepository:
    """Create session repository for benchmarks."""
    return DatabaseSessionRepository(benchmark_session)


@pytest.fixture
async def evidence_artifact_repository(benchmark_session) -> DatabaseEvidenceArtifactRepository:
    """Create evidence artifact repository for benchmarks."""
    return DatabaseEvidenceArtifactRepository(benchmark_session)


@pytest.fixture
async def agent_execution_repository(benchmark_session) -> DatabaseAgentExecutionRepository:
    """Create agent execution repository for benchmarks."""
    return DatabaseAgentExecutionRepository(benchmark_session)


def generate_case_id() -> str:
    """Generate a valid case ID matching the pattern ^case_[a-f0-9]{12}$."""
    return f"case_{uuid4().hex[:12]}"
