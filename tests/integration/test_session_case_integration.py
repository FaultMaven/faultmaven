"""Integration tests for Session-Case lifecycle.

Tests the complete session-case integration including:
- Full lifecycle: create session → create case → retrieve → cleanup
- Session cleanup preserves cases (orphaning)
- Multiple cases per session
- Repository factory session functionality

Run with:
    pytest tests/integration/test_session_case_integration.py -v

Requirements:
    - SQLite for local testing (automatic)
    - PostgreSQL for production testing (optional, requires DATABASE_URL)
"""

import os
import pytest
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator
from uuid import uuid4

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.database import reset_engine
from faultmaven.infrastructure.persistence.database_case_repository import (
    DatabaseCaseRepository,
)
from faultmaven.modules.auth.infrastructure.repositories.session_repository import (
    DatabaseSessionRepository,
    InMemorySessionRepository,
)
from faultmaven.infrastructure.persistence.repository_factory import (
    get_session_repository,
    get_session_repository_async,
    reset_inmemory_session_repository,
    STORAGE_TYPE_INMEMORY,
    STORAGE_TYPE_DATABASE,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    InvestigationStrategy,
)
from faultmaven.modules.auth.domain.models.session import Session


# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture(scope="function")
async def test_engine():
    """Create test engine with in-memory SQLite with foreign key constraints enabled."""
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    reset_engine()

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )

    # Enable foreign key constraints for SQLite
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()
    reset_engine()


@pytest.fixture(scope="function")
async def test_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create test session."""
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest.fixture(scope="function")
async def case_repository(test_session) -> DatabaseCaseRepository:
    """Create DatabaseCaseRepository with test session."""
    return DatabaseCaseRepository(test_session)


@pytest.fixture(scope="function")
async def session_repository(test_session) -> DatabaseSessionRepository:
    """Create DatabaseSessionRepository with test session."""
    return DatabaseSessionRepository(test_session)


@pytest.fixture(scope="function")
def inmemory_session_repository() -> InMemorySessionRepository:
    """Create fresh InMemorySessionRepository."""
    reset_inmemory_session_repository()
    return InMemorySessionRepository()


# ============================================================
# Full Session-Case Lifecycle Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_session_case_lifecycle(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository
):
    """Test full lifecycle: create session → create case → retrieve → cleanup."""
    # Step 1: Create session
    session = Session(
        session_id=str(uuid4()),
        user_id="lifecycle-test-user",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        metadata={"source": "integration_test"},
    )
    created_session = await session_repository.create_session(session)
    assert created_session.session_id == session.session_id

    # Step 2: Create case linked to session
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="lifecycle-test-user",
        organization_id="test-org",
        title="Lifecycle Test Case",
        description="Testing session-case lifecycle",
        status=CaseStatus.CONSULTING,
    )
    await case_repository.save_with_session(case, session_id=session.session_id)

    # Step 3: Retrieve case through session
    cases = await case_repository.get_cases_by_session(session.session_id)
    assert len(cases) == 1
    assert cases[0].case_id == case.case_id
    assert cases[0].title == "Lifecycle Test Case"

    # Step 4: Update session last_accessed
    updated = await session_repository.update_last_accessed(session.session_id)
    assert updated is True

    # Step 5: Verify session still active
    retrieved_session = await session_repository.get_session(session.session_id)
    assert retrieved_session is not None
    assert retrieved_session.is_active() is True

    # Step 6: Cleanup - delete session
    # First unlink the case (simulating ON DELETE SET NULL)
    await case_repository.link_case_to_session(case.case_id, None)
    deleted = await session_repository.delete_session(session.session_id)
    assert deleted is True

    # Verify session is gone
    deleted_session = await session_repository.get_session(session.session_id)
    assert deleted_session is None

    # Case should still exist but be orphaned
    orphaned_case = await case_repository.get(case.case_id)
    assert orphaned_case is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_session_cleanup_preserves_cases(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository
):
    """Test deleting session doesn't delete cases."""
    # Arrange - Create session with multiple cases
    session = Session(
        session_id=str(uuid4()),
        user_id="cleanup-test-user",
    )
    await session_repository.create_session(session)

    case_ids = []
    for i in range(3):
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id="cleanup-test-user",
            organization_id="test-org",
            title=f"Cleanup Test Case {i}",
        )
        await case_repository.save_with_session(case, session_id=session.session_id)
        case_ids.append(case.case_id)

    # Verify cases are linked
    linked_cases = await case_repository.get_cases_by_session(session.session_id)
    assert len(linked_cases) == 3

    # Act - Unlink cases and delete session
    for case_id in case_ids:
        await case_repository.link_case_to_session(case_id, None)
    await session_repository.delete_session(session.session_id)

    # Assert - All cases still exist
    for case_id in case_ids:
        case = await case_repository.get(case_id)
        assert case is not None, f"Case {case_id} should still exist"

    # Cases should now be orphaned
    orphans, count = await case_repository.get_orphaned_cases(user_id="cleanup-test-user")
    assert count == 3


@pytest.mark.asyncio
@pytest.mark.integration
async def test_multiple_cases_per_session(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository
):
    """Test multiple cases linked to same session."""
    # Arrange
    session = Session(
        session_id=str(uuid4()),
        user_id="multi-case-user",
    )
    await session_repository.create_session(session)

    # Create cases with different statuses
    statuses = [
        CaseStatus.CONSULTING,
        CaseStatus.INVESTIGATING,
        CaseStatus.CONSULTING,
    ]

    for i, status in enumerate(statuses):
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id="multi-case-user",
            organization_id="test-org",
            title=f"Multi Case {i}",
            status=status,
        )
        await case_repository.save_with_session(case, session_id=session.session_id)

    # Act
    cases = await case_repository.get_cases_by_session(session.session_id)

    # Assert
    assert len(cases) == 3

    # Verify different statuses
    status_values = [c.status for c in cases]
    assert CaseStatus.CONSULTING in status_values
    assert CaseStatus.INVESTIGATING in status_values


@pytest.mark.asyncio
@pytest.mark.integration
async def test_session_expiry_workflow(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository
):
    """Test expired session cleanup with active cases."""
    # Arrange - Create expired session with case
    expired_session = Session(
        session_id=str(uuid4()),
        user_id="expiry-test-user",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    await session_repository.create_session(expired_session)

    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="expiry-test-user",
        organization_id="test-org",
        title="Expiry Test Case",
    )
    await case_repository.save_with_session(case, session_id=expired_session.session_id)

    # Create active session
    active_session = Session(
        session_id=str(uuid4()),
        user_id="expiry-test-user",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    await session_repository.create_session(active_session)

    # Verify session is expired
    retrieved = await session_repository.get_session(expired_session.session_id)
    assert retrieved.is_expired() is True

    # Unlink case before cleanup
    await case_repository.link_case_to_session(case.case_id, None)

    # Act - Cleanup expired sessions
    deleted_count = await session_repository.cleanup_expired_sessions()

    # Assert
    assert deleted_count == 1

    # Expired session should be gone
    expired = await session_repository.get_session(expired_session.session_id)
    assert expired is None

    # Active session should remain
    active = await session_repository.get_session(active_session.session_id)
    assert active is not None

    # Case should be orphaned but still exist
    orphaned_case = await case_repository.get(case.case_id)
    assert orphaned_case is not None


# ============================================================
# Repository Factory Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repository_factory_session_inmemory():
    """Test session repository factory returns InMemorySessionRepository."""
    # Set storage type
    os.environ["SESSION_STORAGE_TYPE"] = STORAGE_TYPE_INMEMORY
    reset_inmemory_session_repository()

    # Get repository
    async with get_session_repository_async() as repo:
        assert isinstance(repo, InMemorySessionRepository)

        # Test basic operations
        session = Session(
            session_id=str(uuid4()),
            user_id="factory-test-user",
        )
        await repo.create_session(session)
        retrieved = await repo.get_session(session.session_id)
        assert retrieved is not None
        assert retrieved.user_id == "factory-test-user"

    # Cleanup
    reset_inmemory_session_repository()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repository_factory_session_database(test_session: AsyncSession):
    """Test session repository factory with explicit session returns DatabaseSessionRepository."""
    os.environ["SESSION_STORAGE_TYPE"] = STORAGE_TYPE_DATABASE

    # Get repository with session
    repo = get_session_repository(session=test_session)
    assert isinstance(repo, DatabaseSessionRepository)

    # Test basic operations
    session = Session(
        session_id=str(uuid4()),
        user_id="factory-db-test-user",
    )
    await repo.create_session(session)
    retrieved = await repo.get_session(session.session_id)
    assert retrieved is not None
    assert retrieved.user_id == "factory-db-test-user"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repository_factory_session_invalid_type():
    """Test session repository factory with invalid storage type."""
    os.environ["SESSION_STORAGE_TYPE"] = "invalid_type"

    with pytest.raises(ValueError) as exc_info:
        async with get_session_repository_async() as repo:
            pass

    assert "Unknown storage type" in str(exc_info.value)

    # Reset
    os.environ["SESSION_STORAGE_TYPE"] = STORAGE_TYPE_INMEMORY


# ============================================================
# Cross-Repository Integration Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_session_user_cases_query(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository
):
    """Test querying cases by user across sessions."""
    user_id = "cross-query-user"

    # Create multiple sessions for the user
    session1 = Session(session_id=str(uuid4()), user_id=user_id)
    session2 = Session(session_id=str(uuid4()), user_id=user_id)
    await session_repository.create_session(session1)
    await session_repository.create_session(session2)

    # Create cases in each session
    for session in [session1, session2]:
        for i in range(2):
            case = Case(
                case_id=f"case_{uuid4().hex[:12]}",
                user_id=user_id,
                organization_id="test-org",
                title=f"Case for session {session.session_id[:8]} - {i}",
            )
            await case_repository.save_with_session(case, session_id=session.session_id)

    # Query all cases for user
    all_cases, total = await case_repository.list(user_id=user_id)

    # Assert
    assert total == 4

    # Query by specific session
    session1_cases = await case_repository.get_cases_by_session(session1.session_id)
    session2_cases = await case_repository.get_cases_by_session(session2.session_id)

    assert len(session1_cases) == 2
    assert len(session2_cases) == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_case_status_updates_with_session(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository
):
    """Test case status updates maintain session linkage."""
    # Arrange
    session = Session(session_id=str(uuid4()), user_id="status-update-user")
    await session_repository.create_session(session)

    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="status-update-user",
        organization_id="test-org",
        title="Status Update Test",
        status=CaseStatus.CONSULTING,
    )
    await case_repository.save_with_session(case, session_id=session.session_id)

    # Act - Update case status through normal save
    case.status = CaseStatus.INVESTIGATING
    await case_repository.save(case)

    # Assert - Case still linked to session
    cases = await case_repository.get_cases_by_session(session.session_id)
    assert len(cases) == 1

    # Need to re-save with session to maintain linkage
    case.status = CaseStatus.RESOLVED
    await case_repository.save_with_session(case, session_id=session.session_id)

    # Verify still linked
    cases = await case_repository.get_cases_by_session(session.session_id)
    assert len(cases) == 1
    assert cases[0].status == CaseStatus.RESOLVED


@pytest.mark.asyncio
@pytest.mark.integration
async def test_concurrent_session_operations(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository
):
    """Test concurrent session and case operations."""
    import asyncio

    # Create session
    session = Session(session_id=str(uuid4()), user_id="concurrent-user")
    await session_repository.create_session(session)

    # Create cases concurrently
    async def create_case(index: int):
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id="concurrent-user",
            organization_id="test-org",
            title=f"Concurrent Case {index}",
        )
        await case_repository.save_with_session(case, session_id=session.session_id)
        return case.case_id

    case_ids = await asyncio.gather(*[create_case(i) for i in range(5)])

    # Verify all cases created and linked
    cases = await case_repository.get_cases_by_session(session.session_id)
    assert len(cases) == 5

    retrieved_ids = [c.case_id for c in cases]
    for case_id in case_ids:
        assert case_id in retrieved_ids
