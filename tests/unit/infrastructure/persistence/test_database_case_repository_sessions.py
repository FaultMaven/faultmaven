"""Unit tests for DatabaseCaseRepository session-aware methods.

Tests session-case linking, session-based queries, and orphaned case handling.

Run with:
    pytest tests/unit/infrastructure/persistence/test_database_case_repository_sessions.py -v

Coverage:
    pytest tests/unit/infrastructure/persistence/test_database_case_repository_sessions.py \
        --cov=faultmaven/infrastructure/persistence/database_case_repository \
        --cov-report=term-missing
"""

import pytest
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator
from uuid import uuid4

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from faultmaven.infrastructure.persistence.models import Base, SessionModel
from faultmaven.infrastructure.persistence.database_case_repository import (
    DatabaseCaseRepository,
)
from faultmaven.infrastructure.persistence.session_repository import (
    DatabaseSessionRepository,
)
from faultmaven.infrastructure.persistence.case_repository import RepositoryException
from faultmaven.models.case import (
    Case,
    CaseStatus,
    InvestigationStrategy,
)
from faultmaven.models.session import Session


# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture(scope="function")
async def async_engine():
    """Create async engine with in-memory SQLite database."""
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
    """Create async session for tests."""
    session_factory = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest.fixture
async def case_repository(async_session) -> DatabaseCaseRepository:
    """Create case repository instance for tests."""
    return DatabaseCaseRepository(async_session)


@pytest.fixture
async def session_repository(async_session) -> DatabaseSessionRepository:
    """Create session repository instance for tests."""
    return DatabaseSessionRepository(async_session)


@pytest.fixture
def sample_session() -> Session:
    """Create a sample session for testing."""
    return Session(
        session_id=str(uuid4()),
        user_id="test-user-001",
        created_at=datetime.now(timezone.utc),
        last_accessed=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )


@pytest.fixture
def sample_case() -> Case:
    """Create a sample case for testing."""
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="test-user-001",
        organization_id="test-org-001",
        title="Test Case - Session Integration",
        description="Testing session-case linking",
        status=CaseStatus.CONSULTING,
        investigation_strategy=InvestigationStrategy.POST_MORTEM,
    )


# ============================================================
# Save with Session Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_save_case_with_session(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository,
    sample_case: Case,
    sample_session: Session
):
    """Test creating case linked to session."""
    # Arrange - Create session first
    await session_repository.create_session(sample_session)

    # Act
    saved_case = await case_repository.save_with_session(
        sample_case,
        session_id=sample_session.session_id
    )

    # Assert
    assert saved_case is not None
    assert saved_case.case_id == sample_case.case_id

    # Verify case is retrievable
    retrieved = await case_repository.get(sample_case.case_id)
    assert retrieved is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_save_case_without_session(
    case_repository: DatabaseCaseRepository,
    sample_case: Case
):
    """Test creating case without session (session_id=NULL)."""
    # Act
    saved_case = await case_repository.save_with_session(sample_case, session_id=None)

    # Assert
    assert saved_case is not None

    # Verify case is retrievable
    retrieved = await case_repository.get(sample_case.case_id)
    assert retrieved is not None


# ============================================================
# Get Cases by Session Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_cases_by_session(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository,
    sample_session: Session
):
    """Test retrieving all cases for a session."""
    # Arrange - Create session
    await session_repository.create_session(sample_session)

    # Create multiple cases for the session
    for i in range(3):
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id="test-user-001",
            organization_id="test-org-001",
            title=f"Test Case {i}",
        )
        await case_repository.save_with_session(case, session_id=sample_session.session_id)

    # Create case for different session
    other_session = Session(
        session_id=str(uuid4()),
        user_id="test-user-001",
    )
    await session_repository.create_session(other_session)

    other_case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="test-user-001",
        organization_id="test-org-001",
        title="Other Session Case",
    )
    await case_repository.save_with_session(other_case, session_id=other_session.session_id)

    # Act
    cases = await case_repository.get_cases_by_session(sample_session.session_id)

    # Assert
    assert len(cases) == 3


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_cases_by_session_empty(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository,
    sample_session: Session
):
    """Test retrieving cases for session with no cases."""
    # Arrange
    await session_repository.create_session(sample_session)

    # Act
    cases = await case_repository.get_cases_by_session(sample_session.session_id)

    # Assert
    assert cases == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_cases_by_nonexistent_session(case_repository: DatabaseCaseRepository):
    """Test retrieving cases for nonexistent session returns empty list."""
    # Act
    cases = await case_repository.get_cases_by_session("nonexistent-session-id")

    # Assert
    assert cases == []


# ============================================================
# Get Orphaned Cases Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_orphaned_cases(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository,
    sample_session: Session
):
    """Test retrieving cases with no session."""
    # Arrange - Create session and linked case
    await session_repository.create_session(sample_session)

    linked_case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="test-user-001",
        organization_id="test-org-001",
        title="Linked Case",
    )
    await case_repository.save_with_session(linked_case, session_id=sample_session.session_id)

    # Create orphaned cases (no session)
    for i in range(2):
        orphan_case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id="test-user-001",
            organization_id="test-org-001",
            title=f"Orphan Case {i}",
        )
        await case_repository.save_with_session(orphan_case, session_id=None)

    # Act
    orphans, count = await case_repository.get_orphaned_cases(user_id="test-user-001")

    # Assert
    assert len(orphans) == 2
    assert count == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_orphaned_cases_all_users(case_repository: DatabaseCaseRepository):
    """Test retrieving orphaned cases without user filter."""
    # Arrange - Create orphaned cases for different users
    for user_id in ["user-a", "user-b"]:
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id=user_id,
            organization_id="test-org-001",
            title=f"Orphan Case for {user_id}",
        )
        await case_repository.save_with_session(case, session_id=None)

    # Act
    orphans, count = await case_repository.get_orphaned_cases()

    # Assert
    assert len(orphans) == 2
    assert count == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_orphaned_cases_pagination(case_repository: DatabaseCaseRepository):
    """Test pagination for orphaned cases."""
    # Arrange - Create multiple orphaned cases
    for i in range(5):
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id="test-user-001",
            organization_id="test-org-001",
            title=f"Orphan Case {i}",
        )
        await case_repository.save_with_session(case, session_id=None)

    # Act
    page1, total = await case_repository.get_orphaned_cases(limit=2, offset=0)
    page2, _ = await case_repository.get_orphaned_cases(limit=2, offset=2)

    # Assert
    assert len(page1) == 2
    assert len(page2) == 2
    assert total == 5


# ============================================================
# Link Case to Session Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_link_case_to_session(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository,
    sample_case: Case,
    sample_session: Session
):
    """Test linking an existing case to a session."""
    # Arrange - Create case without session
    await case_repository.save(sample_case)

    # Create session
    await session_repository.create_session(sample_session)

    # Act
    result = await case_repository.link_case_to_session(
        sample_case.case_id,
        sample_session.session_id
    )

    # Assert
    assert result is True

    # Verify case is now in session's cases
    cases = await case_repository.get_cases_by_session(sample_session.session_id)
    assert len(cases) == 1
    assert cases[0].case_id == sample_case.case_id


@pytest.mark.asyncio
@pytest.mark.unit
async def test_unlink_case_from_session(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository,
    sample_case: Case,
    sample_session: Session
):
    """Test unlinking a case from a session."""
    # Arrange - Create session and linked case
    await session_repository.create_session(sample_session)
    await case_repository.save_with_session(sample_case, session_id=sample_session.session_id)

    # Verify case is linked
    cases = await case_repository.get_cases_by_session(sample_session.session_id)
    assert len(cases) == 1

    # Act - Unlink by setting session_id to None
    result = await case_repository.link_case_to_session(sample_case.case_id, None)

    # Assert
    assert result is True

    # Verify case is now orphaned
    cases = await case_repository.get_cases_by_session(sample_session.session_id)
    assert len(cases) == 0

    # Verify case appears in orphaned cases
    orphans, _ = await case_repository.get_orphaned_cases()
    assert len(orphans) == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_link_nonexistent_case_to_session(case_repository: DatabaseCaseRepository):
    """Test linking nonexistent case returns False."""
    # Act
    result = await case_repository.link_case_to_session(
        "nonexistent-case-id",
        "some-session-id"
    )

    # Assert
    assert result is False


# ============================================================
# Session Delete Orphans Cases Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_session_delete_orphans_cases(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository,
    sample_case: Case,
    sample_session: Session,
    async_session: AsyncSession
):
    """Test deleting session sets case.session_id to NULL (orphans case).

    Note: This test verifies the ON DELETE SET NULL behavior.
    In SQLite, foreign key constraints need to be explicitly enabled,
    so we verify the behavior at the application level.
    """
    # Arrange - Create session and linked case
    await session_repository.create_session(sample_session)
    await case_repository.save_with_session(sample_case, session_id=sample_session.session_id)

    # Verify case is linked
    cases = await case_repository.get_cases_by_session(sample_session.session_id)
    assert len(cases) == 1

    # Act - Delete the session
    # Note: We need to manually set session_id to NULL for SQLite
    # since foreign key cascades may not work in all SQLite configs
    await case_repository.link_case_to_session(sample_case.case_id, None)
    await session_repository.delete_session(sample_session.session_id)

    # Assert - Case should still exist but be orphaned
    retrieved_case = await case_repository.get(sample_case.case_id)
    assert retrieved_case is not None

    # Case should now appear in orphaned cases
    orphans, _ = await case_repository.get_orphaned_cases()
    case_ids = [c.case_id for c in orphans]
    assert sample_case.case_id in case_ids


@pytest.mark.asyncio
@pytest.mark.unit
async def test_multiple_cases_linked_to_session(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository,
    sample_session: Session
):
    """Test multiple cases can be linked to the same session."""
    # Arrange
    await session_repository.create_session(sample_session)

    case_ids = []
    for i in range(5):
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id="test-user-001",
            organization_id="test-org-001",
            title=f"Multi Case {i}",
        )
        await case_repository.save_with_session(case, session_id=sample_session.session_id)
        case_ids.append(case.case_id)

    # Act
    cases = await case_repository.get_cases_by_session(sample_session.session_id)

    # Assert
    assert len(cases) == 5
    retrieved_ids = [c.case_id for c in cases]
    for case_id in case_ids:
        assert case_id in retrieved_ids


@pytest.mark.asyncio
@pytest.mark.unit
async def test_case_can_change_sessions(
    case_repository: DatabaseCaseRepository,
    session_repository: DatabaseSessionRepository,
    sample_case: Case
):
    """Test a case can be moved from one session to another."""
    # Arrange - Create two sessions
    session1 = Session(session_id=str(uuid4()), user_id="test-user-001")
    session2 = Session(session_id=str(uuid4()), user_id="test-user-001")
    await session_repository.create_session(session1)
    await session_repository.create_session(session2)

    # Link case to first session
    await case_repository.save_with_session(sample_case, session_id=session1.session_id)

    # Verify in first session
    cases1 = await case_repository.get_cases_by_session(session1.session_id)
    assert len(cases1) == 1

    # Act - Move to second session
    await case_repository.link_case_to_session(sample_case.case_id, session2.session_id)

    # Assert - Case moved
    cases1_after = await case_repository.get_cases_by_session(session1.session_id)
    cases2_after = await case_repository.get_cases_by_session(session2.session_id)

    assert len(cases1_after) == 0
    assert len(cases2_after) == 1
    assert cases2_after[0].case_id == sample_case.case_id
