"""Benchmark test fixtures.

Provides database fixtures optimized for performance benchmarking with
minimal overhead from logging and other instrumentation.
"""

import asyncio
from typing import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.database_case_repository import DatabaseCaseRepository
from faultmaven.modules.auth.infrastructure.repositories.session_repository import DatabaseSessionRepository
from faultmaven.infrastructure.persistence.evidence_artifact_repository import (
    DatabaseEvidenceArtifactRepository,
)
# AgentExecutionRepository removed - agent executions now handled by ICaseRepository
from faultmaven.infrastructure.persistence.investigation_session_repository import (
    DatabaseInvestigationSessionRepository,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    DatabaseKnowledgeItemRepository,
)
from tests.utils import generate_case_id, generate_item_id, generate_org_id


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


# agent_execution_repository fixture removed - no longer needed


@pytest.fixture
async def investigation_session_repository(benchmark_session) -> DatabaseInvestigationSessionRepository:
    """Create investigation session repository for benchmarks."""
    return DatabaseInvestigationSessionRepository(benchmark_session)


@pytest.fixture
async def knowledge_item_repository(benchmark_session) -> DatabaseKnowledgeItemRepository:
    """Create knowledge item repository for benchmarks."""
    return DatabaseKnowledgeItemRepository(benchmark_session)
