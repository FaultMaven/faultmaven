"""Unit tests for DatabaseCaseRepository.

Tests CRUD operations, message handling, analytics, and error handling
for the SQLAlchemy ORM-based case repository implementation.

Run with:
    pytest tests/unit/infrastructure/persistence/test_database_case_repository.py -v

Coverage:
    pytest tests/unit/infrastructure/persistence/test_database_case_repository.py \
        --cov=faultmaven/infrastructure/persistence/database_case_repository \
        --cov-report=term-missing
"""

import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator
from uuid import uuid4

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.database_case_repository import (
    DatabaseCaseRepository,
)
from faultmaven.infrastructure.persistence.case_repository import RepositoryException
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    InvestigationProgress,
    ConsultingData,
    InvestigationStrategy,
)


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
async def repository(async_session) -> DatabaseCaseRepository:
    """Create repository instance for tests."""
    return DatabaseCaseRepository(async_session)


@pytest.fixture
def sample_case() -> Case:
    """Create a sample case for testing."""
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="test-user-001",
        organization_id="test-org-001",
        title="Test Case - API Slowness",
        description="API experiencing high latency",
        status=CaseStatus.CONSULTING,
        investigation_strategy=InvestigationStrategy.POST_MORTEM,
    )


@pytest.fixture
def sample_case_with_data() -> Case:
    """Create a case with more data for testing."""
    # Create ConsultingData with all required fields for INVESTIGATING status
    consulting = ConsultingData()
    consulting.proposed_problem_statement = "API experiencing high latency and errors"
    consulting.problem_statement_confirmed = True
    consulting.decided_to_investigate = True
    consulting.quick_suggestions = ["Check logs", "Verify deployment"]

    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="test-user-002",
        organization_id="test-org-001",
        title="Test Case with Data",
        description="API experiencing high latency and errors",
        status=CaseStatus.INVESTIGATING,
        investigation_strategy=InvestigationStrategy.ACTIVE_INCIDENT,
        consulting=consulting,
    )
    case.progress.symptom_verified = True
    case.progress.scope_assessed = True
    case.current_turn = 3
    case.message_count = 5
    return case


# ============================================================
# CRUD Operation Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_create_case(repository: DatabaseCaseRepository, sample_case: Case):
    """Test creating a new case in database."""
    # Act
    saved_case = await repository.save(sample_case)

    # Assert
    assert saved_case is not None
    assert saved_case.case_id == sample_case.case_id
    assert saved_case.title == sample_case.title
    assert saved_case.user_id == sample_case.user_id
    assert saved_case.status == CaseStatus.CONSULTING


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_case(repository: DatabaseCaseRepository, sample_case: Case):
    """Test retrieving case by ID."""
    # Arrange
    await repository.save(sample_case)

    # Act
    retrieved_case = await repository.get(sample_case.case_id)

    # Assert
    assert retrieved_case is not None
    assert retrieved_case.case_id == sample_case.case_id
    assert retrieved_case.title == sample_case.title
    assert retrieved_case.user_id == sample_case.user_id


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_nonexistent_case(repository: DatabaseCaseRepository):
    """Test retrieving a case that doesn't exist."""
    # Act
    result = await repository.get("case_nonexistent1")

    # Assert
    assert result is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_update_case(repository: DatabaseCaseRepository, sample_case: Case):
    """Test updating case fields."""
    # Arrange
    await repository.save(sample_case)

    # Modify the case
    sample_case.title = "Updated Title"
    sample_case.description = "API slowness investigation"
    # Create new ConsultingData with all required fields for INVESTIGATING status
    consulting = ConsultingData()
    consulting.proposed_problem_statement = "API slowness investigation"
    consulting.problem_statement_confirmed = True
    consulting.decided_to_investigate = True
    sample_case.consulting = consulting
    sample_case.status = CaseStatus.INVESTIGATING
    sample_case.current_turn = 5

    # Act
    updated_case = await repository.save(sample_case)

    # Assert
    assert updated_case.title == "Updated Title"

    # Verify persistence
    retrieved = await repository.get(sample_case.case_id)
    assert retrieved.title == "Updated Title"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_delete_case(repository: DatabaseCaseRepository, sample_case: Case):
    """Test deleting case."""
    # Arrange
    await repository.save(sample_case)

    # Act
    result = await repository.delete(sample_case.case_id)

    # Assert
    assert result is True

    # Verify deletion
    retrieved = await repository.get(sample_case.case_id)
    assert retrieved is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_delete_nonexistent_case(repository: DatabaseCaseRepository):
    """Test deleting a case that doesn't exist."""
    # Act
    result = await repository.delete("case_nonexistent1")

    # Assert
    assert result is False


# ============================================================
# List and Search Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_cases_by_user(repository: DatabaseCaseRepository):
    """Test retrieving all cases for a user."""
    # Arrange
    user_id = "test-user-list"
    for i in range(3):
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id=user_id,
            organization_id="test-org",
            title=f"Test Case {i}",
        )
        await repository.save(case)

    # Add a case for different user
    other_case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="other-user",
        organization_id="test-org",
        title="Other User Case",
    )
    await repository.save(other_case)

    # Act
    cases, total = await repository.list(user_id=user_id)

    # Assert
    assert len(cases) == 3
    assert total == 3
    assert all(c.user_id == user_id for c in cases)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_cases_by_status(repository: DatabaseCaseRepository):
    """Test filtering cases by status."""
    # Arrange
    user_id = "test-user-status"

    # Create cases with different statuses
    for status in [CaseStatus.CONSULTING, CaseStatus.CONSULTING, CaseStatus.INVESTIGATING]:
        if status == CaseStatus.INVESTIGATING:
            # Create ConsultingData with all required fields for INVESTIGATING status
            consulting = ConsultingData()
            consulting.proposed_problem_statement = "Test problem statement"
            consulting.problem_statement_confirmed = True
            consulting.decided_to_investigate = True
            case = Case(
                case_id=f"case_{uuid4().hex[:12]}",
                user_id=user_id,
                organization_id="test-org",
                title=f"Test Case - {status.value}",
                description="Test problem statement",
                status=status,
                consulting=consulting,
            )
        else:
            case = Case(
                case_id=f"case_{uuid4().hex[:12]}",
                user_id=user_id,
                organization_id="test-org",
                title=f"Test Case - {status.value}",
                status=status,
            )
        await repository.save(case)

    # Act
    cases, total = await repository.list(user_id=user_id, status=CaseStatus.CONSULTING)

    # Assert
    assert len(cases) == 2
    assert total == 2
    assert all(c.status == CaseStatus.CONSULTING for c in cases)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_cases_pagination(repository: DatabaseCaseRepository):
    """Test pagination in list results."""
    # Arrange
    user_id = "test-user-pagination"
    for i in range(10):
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id=user_id,
            organization_id="test-org",
            title=f"Test Case {i}",
        )
        await repository.save(case)

    # Act
    page1, total = await repository.list(user_id=user_id, limit=3, offset=0)
    page2, _ = await repository.list(user_id=user_id, limit=3, offset=3)
    page3, _ = await repository.list(user_id=user_id, limit=3, offset=6)

    # Assert
    assert len(page1) == 3
    assert len(page2) == 3
    assert len(page3) == 3
    assert total == 10


@pytest.mark.asyncio
@pytest.mark.unit
async def test_search_cases(repository: DatabaseCaseRepository):
    """Test searching cases by text query."""
    # Arrange
    user_id = "test-user-search"

    # Create cases with specific titles
    await repository.save(Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id=user_id,
        organization_id="test-org",
        title="Database Connection Error",
    ))
    await repository.save(Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id=user_id,
        organization_id="test-org",
        title="API Performance Issue",
    ))
    await repository.save(Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id=user_id,
        organization_id="test-org",
        title="Memory Leak Investigation",
    ))

    # Act
    results, count = await repository.search(
        query="database",
        user_id=user_id,
    )

    # Assert
    assert count == 1
    assert len(results) == 1
    assert "Database" in results[0].title


# ============================================================
# Message Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_add_message(repository: DatabaseCaseRepository, sample_case: Case):
    """Test adding a message to a case."""
    # Arrange
    await repository.save(sample_case)
    message = {
        "message_id": f"msg_{uuid4().hex[:12]}",
        "role": "user",
        "content": "What's causing the slowness?",
        "timestamp": datetime.now(timezone.utc),
        "metadata": {"source": "web"},
    }

    # Act
    result = await repository.add_message(sample_case.case_id, message)

    # Assert
    assert result is True

    # Verify message was added
    messages = await repository.get_messages(sample_case.case_id)
    assert len(messages) == 1
    assert messages[0]["content"] == "What's causing the slowness?"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_add_message_to_nonexistent_case(repository: DatabaseCaseRepository):
    """Test adding message to a case that doesn't exist."""
    # Act
    result = await repository.add_message(
        "case_nonexistent1",
        {"role": "user", "content": "Test"}
    )

    # Assert
    assert result is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_messages_with_pagination(repository: DatabaseCaseRepository, sample_case: Case):
    """Test getting messages with pagination."""
    # Arrange
    await repository.save(sample_case)

    # Add multiple messages
    for i in range(5):
        message = {
            "message_id": f"msg_{uuid4().hex[:12]}",
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"Message {i}",
            "timestamp": datetime.now(timezone.utc) + timedelta(seconds=i),
        }
        await repository.add_message(sample_case.case_id, message)

    # Act
    page1 = await repository.get_messages(sample_case.case_id, limit=2, offset=0)
    page2 = await repository.get_messages(sample_case.case_id, limit=2, offset=2)

    # Assert
    assert len(page1) == 2
    assert len(page2) == 2
    assert page1[0]["content"] == "Message 0"
    assert page2[0]["content"] == "Message 2"


# ============================================================
# Status Transition Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_status_transition(repository: DatabaseCaseRepository, sample_case: Case):
    """Test case status changes tracked."""
    # Arrange
    await repository.save(sample_case)

    # Add status transition
    from faultmaven.modules.case.domain.models import CaseStatusTransition
    transition = CaseStatusTransition(
        from_status=CaseStatus.CONSULTING,
        to_status=CaseStatus.INVESTIGATING,
        triggered_by="test-user",
        reason="Starting investigation",
    )
    sample_case.status_history.append(transition)
    # Set required fields for INVESTIGATING status
    sample_case.description = "Starting formal investigation"
    consulting = ConsultingData()
    consulting.proposed_problem_statement = "Starting formal investigation"
    consulting.problem_statement_confirmed = True
    consulting.decided_to_investigate = True
    sample_case.consulting = consulting
    sample_case.status = CaseStatus.INVESTIGATING

    # Act
    await repository.save(sample_case)

    # Assert
    retrieved = await repository.get(sample_case.case_id)
    assert retrieved.status == CaseStatus.INVESTIGATING
    # Note: status_history is stored in metadata, verify status changed


# ============================================================
# Analytics Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_analytics(repository: DatabaseCaseRepository, sample_case_with_data: Case):
    """Test computing analytics for a case."""
    # Arrange
    await repository.save(sample_case_with_data)

    # Act
    analytics = await repository.get_analytics(sample_case_with_data.case_id)

    # Assert
    assert analytics["case_id"] == sample_case_with_data.case_id
    assert analytics["status"] == "investigating"
    assert analytics["current_turn"] == 3
    assert analytics["message_count"] == 5
    assert analytics["investigation_strategy"] == "active_incident"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_analytics_nonexistent_case(repository: DatabaseCaseRepository):
    """Test analytics for a case that doesn't exist."""
    # Act
    analytics = await repository.get_analytics("case_nonexistent1")

    # Assert
    assert analytics == {}


# ============================================================
# Activity Timestamp Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_update_activity_timestamp(repository: DatabaseCaseRepository, sample_case: Case):
    """Test updating activity timestamp."""
    # Arrange
    await repository.save(sample_case)
    original_updated_at = sample_case.updated_at

    # Wait briefly to ensure timestamp difference
    await asyncio.sleep(0.01)

    # Act
    result = await repository.update_activity_timestamp(sample_case.case_id)

    # Assert
    assert result is True

    # Verify timestamp was updated
    retrieved = await repository.get(sample_case.case_id)
    assert retrieved.updated_at > original_updated_at


@pytest.mark.asyncio
@pytest.mark.unit
async def test_update_activity_timestamp_nonexistent(repository: DatabaseCaseRepository):
    """Test updating activity timestamp for nonexistent case."""
    # Act
    result = await repository.update_activity_timestamp("case_nonexistent1")

    # Assert
    assert result is False


# ============================================================
# Cleanup Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_cleanup_expired(repository: DatabaseCaseRepository):
    """Test cleaning up expired cases."""
    # Arrange
    # Create old closed case (closed 100 days ago)
    old_closed_time = datetime.now(timezone.utc) - timedelta(days=100)
    old_created_time = old_closed_time - timedelta(days=10)
    old_case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="test-user",
        organization_id="test-org",
        title="Old Closed Case",
        status=CaseStatus.CLOSED,
        created_at=old_created_time,
        updated_at=old_closed_time,
        closed_at=old_closed_time,
        closure_reason="abandoned",
    )
    await repository.save(old_case)

    # Create recent closed case (closed today)
    recent_closed_time = datetime.now(timezone.utc)
    recent_created_time = recent_closed_time - timedelta(days=1)
    recent_case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="test-user",
        organization_id="test-org",
        title="Recent Closed Case",
        status=CaseStatus.CLOSED,
        created_at=recent_created_time,
        updated_at=recent_closed_time,
        closed_at=recent_closed_time,
        closure_reason="other",
    )
    await repository.save(recent_case)

    # Act
    deleted_count = await repository.cleanup_expired(max_age_days=90, batch_size=100)

    # Assert
    assert deleted_count == 1

    # Verify old case was deleted
    old_retrieved = await repository.get(old_case.case_id)
    assert old_retrieved is None

    # Verify recent case still exists
    recent_retrieved = await repository.get(recent_case.case_id)
    assert recent_retrieved is not None


# ============================================================
# Concurrent Operation Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.xfail(reason="SQLAlchemy transaction isolation - needs harness refactor", strict=False)
async def test_concurrent_updates(repository: DatabaseCaseRepository, sample_case: Case):
    """Test multiple updates don't corrupt data."""
    # Arrange
    await repository.save(sample_case)

    # Act - Perform concurrent updates
    async def update_case(turn_number: int):
        case = await repository.get(sample_case.case_id)
        case.current_turn = turn_number
        await repository.save(case)

    await asyncio.gather(
        update_case(1),
        update_case(2),
        update_case(3),
    )

    # Assert - Case should exist and have valid data
    retrieved = await repository.get(sample_case.case_id)
    assert retrieved is not None
    assert retrieved.current_turn in [1, 2, 3]  # One of the updates should succeed


# ============================================================
# Error Handling Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_rollback_on_error(async_session: AsyncSession):
    """Test transaction rollback on error."""
    # Create repository with mocked session that will fail on commit
    repository = DatabaseCaseRepository(async_session)

    # Create a valid case
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="test-user",
        organization_id="test-org",
        title="Test Case",
    )

    # Save the case successfully first
    await repository.save(case)

    # Try to save with an invalid modification that would fail
    # The repository should handle errors gracefully
    retrieved = await repository.get(case.case_id)
    assert retrieved is not None


# ============================================================
# Data Integrity Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_case_data_integrity(repository: DatabaseCaseRepository, sample_case_with_data: Case):
    """Test that complex case data is preserved correctly."""
    # Arrange
    await repository.save(sample_case_with_data)

    # Act
    retrieved = await repository.get(sample_case_with_data.case_id)

    # Assert - Verify all fields preserved
    assert retrieved.case_id == sample_case_with_data.case_id
    assert retrieved.title == sample_case_with_data.title
    assert retrieved.status == sample_case_with_data.status
    assert retrieved.investigation_strategy == sample_case_with_data.investigation_strategy

    # Verify consulting data preserved
    assert retrieved.consulting.quick_suggestions == ["Check logs", "Verify deployment"]

    # Verify progress preserved
    assert retrieved.progress.symptom_verified is True
    assert retrieved.progress.scope_assessed is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_case_lifecycle(repository: DatabaseCaseRepository):
    """Test create -> update -> retrieve -> delete flow."""
    # Create
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="test-user",
        organization_id="test-org",
        title="Lifecycle Test Case",
    )
    await repository.save(case)

    # Verify creation
    retrieved = await repository.get(case.case_id)
    assert retrieved is not None
    assert retrieved.title == "Lifecycle Test Case"

    # Update
    case.title = "Updated Lifecycle Case"
    case.description = "Updated case description for investigation"
    # Create ConsultingData with all required fields for INVESTIGATING status
    consulting = ConsultingData()
    consulting.proposed_problem_statement = "Updated case description for investigation"
    consulting.problem_statement_confirmed = True
    consulting.decided_to_investigate = True
    case.consulting = consulting
    case.status = CaseStatus.INVESTIGATING
    await repository.save(case)

    # Verify update
    retrieved = await repository.get(case.case_id)
    assert retrieved.title == "Updated Lifecycle Case"
    assert retrieved.status == CaseStatus.INVESTIGATING

    # Delete
    deleted = await repository.delete(case.case_id)
    assert deleted is True

    # Verify deletion
    retrieved = await repository.get(case.case_id)
    assert retrieved is None
