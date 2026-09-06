"""Integration tests for Investigation Session Repository.

Tests the full case-session-execution-tool_call relationship with real database operations.

Run with:
    pytest tests/integration/test_investigation_session_integration.py -v

Requirements:
    - SQLite for local testing (automatic)
    - PostgreSQL for production testing (optional, requires DATABASE_URL)
"""

import time
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.database import reset_engine
from faultmaven.infrastructure.persistence.investigation_session_repository import (
    DatabaseInvestigationSessionRepository,
    InMemoryInvestigationSessionRepository,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.repository_factory import (
    reset_inmemory_investigation_session_repository,
)
from faultmaven.models.investigation_session import InvestigationSession, SessionState
from faultmaven.modules.case.domain.models import Case, CaseState, InquiryData
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)
from tests.utils import (
    generate_case_id,
    generate_session_id,
    install_org_autoseed,
    seed_organizations,
    seed_users,
)

# ============================================================
# Test Fixtures
# ============================================================


def generate_execution_id() -> str:
    """Generate a valid execution ID."""
    return f"exec_{uuid4().hex[:12]}"


def generate_tool_call_id() -> str:
    """Generate a valid tool call ID."""
    return f"tc_{uuid4().hex[:12]}"


@pytest.fixture(scope="function")
async def test_engine(in_memory_database_url):
    """Create test engine with in-memory SQLite with foreign key constraints enabled.

    ``in_memory_database_url`` sets ``DATABASE_URL`` for the duration of one
    test and puts the process back afterwards. This fixture used to assign
    ``os.environ`` itself and never restore it, which repointed every later
    test in the process at an empty database (fm#1325) — see that fixture in
    ``conftest.py``. The engine below is built from the URL it hands back, so
    the application and this engine cannot name two different databases.
    """
    reset_engine()

    engine = create_async_engine(
        in_memory_database_url,
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

    # Pre-seed every hardcoded org_id used across the file so the case
    # repository's raw-SQL writes find their FK target. The ORM autoseed
    # hook only fires for ORM flushes; raw INSERTs need orgs in place
    # before the cursor executes.
    seed_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with seed_factory() as seed_session:
        await seed_organizations(
            seed_session,
            [
                "integration-test-org",
                "cascade-test-org",
                "test-org",
                "different-org",
                "order-test-org",
            ],
        )
        await seed_users(
            seed_session,
            [
                "integration-test-user",
                "cascade-test-user",
                "different-user",
                "order-test-user",
                "pagination-test-user",
                "user_1",
                "user_2",
                "user_3",
            ],
        )

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
        install_org_autoseed(session.sync_session)
        yield session


@pytest.fixture(scope="function")
async def case_repository(test_session) -> SQLiteCaseRepository:
    """Create SQLiteCaseRepository with test session."""
    return SQLiteCaseRepository(test_session)


@pytest.fixture(scope="function")
async def session_repository(test_session) -> DatabaseInvestigationSessionRepository:
    """Create DatabaseInvestigationSessionRepository with test session."""
    return DatabaseInvestigationSessionRepository(test_session)


# execution_repository fixture removed - APIInvestigationSessionService uses case_repo


@pytest.fixture
async def sample_case(case_repository: SQLiteCaseRepository) -> Case:
    """Create a sample case for testing sessions.

    The parent ``integration-test-org`` is pre-seeded by the
    ``test_engine`` fixture above, so this raw-SQL case write satisfies
    the FK without explicit per-test seeding.
    """
    case = Case(
        case_id=generate_case_id(),
        user_id="integration-test-user",
        enterprise_id="integration-test-org",
        title="Investigation Session Integration Test Case",
        description="Testing investigation session management",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="Test problem statement",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )
    return await case_repository.save(case)


def create_sample_session(
    case_id: str,
    user_id: str = "integration-test-user",
    organization_id: str = "integration-test-org",
    state: SessionState = SessionState.ACTIVE,
    session_goal: str = None,
    token_budget_limit: int = None,
) -> InvestigationSession:
    """Create a sample investigation session for testing."""
    return InvestigationSession(
        session_id=generate_session_id(),
        case_id=case_id,
        user_id=user_id,
        organization_id=organization_id,
        state=state,
        session_goal=session_goal,
        token_budget_limit=token_budget_limit,
    )


# ============================================================
# Full Session Lifecycle Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_full_session_lifecycle(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test create -> update -> retrieve -> delete flow."""
    # Step 1: Create session
    session = create_sample_session(
        sample_case.case_id,
        session_goal="Find root cause of timeout issue",
        token_budget_limit=10000,
    )
    created = await session_repository.create(session)

    assert created.session_id == session.session_id
    assert created.case_id == sample_case.case_id
    assert created.state == SessionState.ACTIVE
    assert created.session_goal == "Find root cause of timeout issue"
    assert created.token_budget_limit == 10000

    # Step 2: Retrieve session
    retrieved = await session_repository.get_by_id(session.session_id)
    assert retrieved is not None
    assert retrieved.session_id == session.session_id

    # Step 3: Update session (add execution)
    session.add_agent_execution(token_usage=500)
    updated = await session_repository.update(session)
    assert updated.total_token_usage == 500
    assert updated.total_agent_executions == 1

    # Step 4: Update session (pause)
    session.pause()
    updated = await session_repository.update(session)
    assert updated.state == SessionState.PAUSED

    # Step 5: Update session (resume)
    session.resume()
    updated = await session_repository.update(session)
    assert updated.state == SessionState.ACTIVE

    # Step 6: Update session (complete)
    session.complete("Root cause identified: connection pool exhaustion")
    updated = await session_repository.update(session)
    assert updated.state == SessionState.COMPLETED
    assert (
        updated.findings_summary == "Root cause identified: connection pool exhaustion"
    )
    assert updated.ended_at is not None

    # Step 7: Delete session
    deleted = await session_repository.delete(session.session_id)
    assert deleted is True

    # Step 8: Verify deleted
    retrieved = await session_repository.get_by_id(session.session_id)
    assert retrieved is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_session_lifecycle_active_to_abandoned(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test session lifecycle from active to abandoned."""
    session = create_sample_session(sample_case.case_id)
    await session_repository.create(session)

    # Add some executions
    session.add_agent_execution(token_usage=100)
    session.add_agent_execution(token_usage=200)
    await session_repository.update(session)

    # Abandon session
    session.abandon()
    updated = await session_repository.update(session)

    assert updated.state == SessionState.ABANDONED
    assert updated.ended_at is not None
    assert updated.total_token_usage == 300
    assert updated.total_agent_executions == 2


# ============================================================
# CASCADE Delete Tests (Four-Level Chain)
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cascade_delete_case_to_sessions(
    case_repository: SQLiteCaseRepository,
    session_repository: DatabaseInvestigationSessionRepository,
    test_session: AsyncSession,
):
    """Verify Case → Session CASCADE delete."""
    # Create case
    case = Case(
        case_id=generate_case_id(),
        user_id="cascade-test-user",
        enterprise_id="cascade-test-org",
        title="CASCADE Delete Test Case",
        description="Testing CASCADE delete",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="Test problem statement",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )
    await case_repository.save(case)

    # Create multiple sessions
    session1 = create_sample_session(case.case_id)
    session2 = create_sample_session(case.case_id)
    session3 = create_sample_session(case.case_id)
    await session_repository.create(session1)
    await session_repository.create(session2)
    await session_repository.create(session3)

    # Verify sessions exist
    count = await session_repository.count_by_case_id(case.case_id)
    assert count == 3

    # Delete case
    await case_repository.delete(case.case_id)

    # Verify all sessions are deleted (CASCADE)
    remaining = await session_repository.list_by_case_id(case.case_id)
    assert len(remaining) == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_sessions_by_case_with_status_filter(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test filtering sessions by status."""
    # Create sessions with different statuses
    active_session = create_sample_session(
        sample_case.case_id, state=SessionState.ACTIVE
    )
    paused_session = create_sample_session(
        sample_case.case_id, state=SessionState.PAUSED
    )
    completed_session = create_sample_session(
        sample_case.case_id, state=SessionState.COMPLETED
    )

    await session_repository.create(active_session)
    await session_repository.create(paused_session)
    await session_repository.create(completed_session)

    # Test filter by ACTIVE
    active_list = await session_repository.list_by_case_id(
        sample_case.case_id,
        state=SessionState.ACTIVE,
    )
    assert len(active_list) == 1
    assert active_list[0].state == SessionState.ACTIVE

    # Test filter by PAUSED
    paused_list = await session_repository.list_by_case_id(
        sample_case.case_id,
        state=SessionState.PAUSED,
    )
    assert len(paused_list) == 1
    assert paused_list[0].state == SessionState.PAUSED

    # Test no filter (all)
    all_list = await session_repository.list_by_case_id(sample_case.case_id)
    assert len(all_list) == 3


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_active_session_single(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test getting single active session."""
    active_session = create_sample_session(sample_case.case_id)
    await session_repository.create(active_session)

    result = await session_repository.get_active_session(sample_case.case_id)

    assert result is not None
    assert result.session_id == active_session.session_id
    assert result.state == SessionState.ACTIVE


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_active_session_returns_none_when_no_active(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test get_active_session returns None when no active session."""
    # Create only completed session
    completed_session = create_sample_session(
        sample_case.case_id,
        state=SessionState.COMPLETED,
    )
    await session_repository.create(completed_session)

    result = await session_repository.get_active_session(sample_case.case_id)

    assert result is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_sessions_by_user_pagination(
    session_repository: DatabaseInvestigationSessionRepository,
    case_repository: SQLiteCaseRepository,
):
    """Test pagination for list_by_user_id."""
    user_id = "pagination-test-user"

    # Create multiple cases and sessions
    for i in range(10):
        case = Case(
            case_id=generate_case_id(),
            user_id=user_id,
            enterprise_id="test-org",
            title=f"Pagination Test Case {i}",
            description="Testing pagination",
            state=CaseState.INVESTIGATING,
            inquiry=InquiryData(
                proposed_problem_statement="Test problem statement",
                problem_statement_confirmed=True,
                decided_to_investigate=True,
            ),
        )
        await case_repository.save(case)

        session = create_sample_session(case.case_id, user_id=user_id)
        await session_repository.create(session)

    # Test pagination
    page1 = await session_repository.list_by_user_id(user_id, limit=3, offset=0)
    page2 = await session_repository.list_by_user_id(user_id, limit=3, offset=3)
    page3 = await session_repository.list_by_user_id(user_id, limit=5, offset=6)

    assert len(page1) == 3
    assert len(page2) == 3
    assert len(page3) == 4  # Only 4 remaining


@pytest.mark.asyncio
@pytest.mark.integration
async def test_count_sessions_by_case(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test counting sessions for a case."""
    # Create 5 sessions
    for _ in range(5):
        session = create_sample_session(sample_case.case_id)
        await session_repository.create(session)

    count = await session_repository.count_by_case_id(sample_case.case_id)
    assert count == 5


# ============================================================
# Active Session Enforcement Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_single_active_session_pattern(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Verify pattern: complete previous session before starting new one."""
    # Start first session
    session1 = create_sample_session(sample_case.case_id)
    await session_repository.create(session1)

    # Get active session
    active = await session_repository.get_active_session(sample_case.case_id)
    assert active.session_id == session1.session_id

    # Complete first session before starting second
    session1.complete("First investigation complete")
    await session_repository.update(session1)

    # Now start second session
    session2 = create_sample_session(sample_case.case_id)
    await session_repository.create(session2)

    # New active session should be session2
    active = await session_repository.get_active_session(sample_case.case_id)
    assert active.session_id == session2.session_id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pause_resume_active_session(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test pause and resume of active session."""
    session = create_sample_session(sample_case.case_id)
    await session_repository.create(session)

    # Pause session
    session.pause()
    await session_repository.update(session)

    # No active session after pause
    active = await session_repository.get_active_session(sample_case.case_id)
    assert active is None

    # Resume session
    session.resume()
    await session_repository.update(session)

    # Active session restored
    active = await session_repository.get_active_session(sample_case.case_id)
    assert active.session_id == session.session_id


# ============================================================
# Token Budget Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_session_token_budget_tracking(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test token budget tracking across updates."""
    session = create_sample_session(
        sample_case.case_id,
        token_budget_limit=5000,
    )
    await session_repository.create(session)

    # Add executions
    session.add_agent_execution(token_usage=1000)
    await session_repository.update(session)

    retrieved = await session_repository.get_by_id(session.session_id)
    assert retrieved.total_token_usage == 1000
    assert retrieved.is_over_budget() is False

    # Add more executions
    session.add_agent_execution(token_usage=2000)
    session.add_agent_execution(token_usage=2500)  # Now over budget
    await session_repository.update(session)

    retrieved = await session_repository.get_by_id(session.session_id)
    assert retrieved.total_token_usage == 5500
    assert retrieved.is_over_budget() is True


# ============================================================
# Metadata Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_session_metadata_persistence(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test that session metadata is persisted correctly."""
    session = create_sample_session(sample_case.case_id)
    session.metadata = {
        "environment": "production",
        "priority": "high",
        "tags": ["timeout", "database"],
        "nested": {"key": "value"},
    }
    await session_repository.create(session)

    retrieved = await session_repository.get_by_id(session.session_id)
    assert retrieved.metadata == {
        "environment": "production",
        "priority": "high",
        "tags": ["timeout", "database"],
        "nested": {"key": "value"},
    }


@pytest.mark.asyncio
@pytest.mark.integration
async def test_session_metadata_update(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test updating session metadata."""
    session = create_sample_session(sample_case.case_id)
    session.metadata = {"initial": "value"}
    await session_repository.create(session)

    session.metadata = {"updated": "data", "new_key": 123}
    await session_repository.update(session)

    retrieved = await session_repository.get_by_id(session.session_id)
    assert retrieved.metadata == {"updated": "data", "new_key": 123}


# ============================================================
# Error Handling Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_session_invalid_case_fails(
    session_repository: DatabaseInvestigationSessionRepository,
):
    """Test creating session with non-existent case fails."""
    session = create_sample_session("case_nonexistent")

    with pytest.raises(ValueError, match="not found"):
        await session_repository.create(session)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_duplicate_session_fails(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test creating session with duplicate ID fails."""
    session = create_sample_session(sample_case.case_id)
    await session_repository.create(session)

    duplicate = InvestigationSession(
        session_id=session.session_id,  # Same ID
        case_id=sample_case.case_id,
        user_id="different-user",
        organization_id="different-org",
    )

    with pytest.raises(ValueError, match="already exists"):
        await session_repository.create(duplicate)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_update_non_existent_session_fails(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test updating non-existent session fails."""
    session = create_sample_session(sample_case.case_id)
    # Don't create it

    with pytest.raises(ValueError, match="not found"):
        await session_repository.update(session)


# ============================================================
# InMemory Repository Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_inmemory_full_lifecycle(inmemory_session_repository):
    """Test in-memory repository full lifecycle."""
    case_id = generate_case_id()
    session = create_sample_session(case_id)

    # Create
    created = await inmemory_session_repository.create(session)
    assert created.session_id == session.session_id

    # Read
    retrieved = await inmemory_session_repository.get_by_id(session.session_id)
    assert retrieved is not None

    # Update
    session.session_goal = "Updated goal"
    updated = await inmemory_session_repository.update(session)
    assert updated.session_goal == "Updated goal"

    # Delete
    deleted = await inmemory_session_repository.delete(session.session_id)
    assert deleted is True

    # Verify deleted
    assert await inmemory_session_repository.get_by_id(session.session_id) is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_inmemory_cascade_simulation(inmemory_session_repository):
    """Test in-memory cascade delete simulation."""
    case_id = generate_case_id()

    # Create multiple sessions
    for _ in range(5):
        session = create_sample_session(case_id)
        await inmemory_session_repository.create(session)

    assert await inmemory_session_repository.count_by_case_id(case_id) == 5

    # Simulate CASCADE delete
    count = inmemory_session_repository.delete_sessions_for_case(case_id)
    assert count == 5

    # Verify all deleted
    assert await inmemory_session_repository.count_by_case_id(case_id) == 0


# ============================================================
# Additional Query and Edge Case Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_session_ordering_by_started_at(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test sessions are returned ordered by started_at descending."""
    base_time = datetime.now(timezone.utc)

    # Create sessions with different started_at times
    for i in range(5):
        started_at = base_time - timedelta(hours=i)
        session = InvestigationSession(
            session_id=generate_session_id(),
            case_id=sample_case.case_id,
            user_id="order-test-user",
            organization_id="order-test-org",
            started_at=started_at,
        )
        await session_repository.create(session)

    sessions = await session_repository.list_by_case_id(sample_case.case_id)

    # Verify descending order
    for i in range(len(sessions) - 1):
        assert sessions[i].started_at >= sessions[i + 1].started_at


@pytest.mark.asyncio
@pytest.mark.integration
async def test_session_with_null_metadata(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test session creation and retrieval with null metadata."""
    session = create_sample_session(sample_case.case_id)
    session.metadata = None
    await session_repository.create(session)

    retrieved = await session_repository.get_by_id(session.session_id)
    # Metadata should be None or empty dict
    assert retrieved.metadata is None or retrieved.metadata == {}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_session_with_large_findings_summary(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test session with large findings summary text."""
    session = create_sample_session(sample_case.case_id)
    await session_repository.create(session)

    # Complete with large summary
    large_summary = "Investigation findings:\n" + ("- Finding detail line\n" * 1000)
    session.complete(large_summary)
    await session_repository.update(session)

    retrieved = await session_repository.get_by_id(session.session_id)
    assert retrieved.findings_summary == large_summary
    assert len(retrieved.findings_summary) > 10000


@pytest.mark.asyncio
@pytest.mark.integration
async def test_delete_returns_false_for_nonexistent(
    session_repository: DatabaseInvestigationSessionRepository,
):
    """Test delete returns False for non-existent session."""
    result = await session_repository.delete("sess_nonexistent_123")
    assert result is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_multiple_users_same_case(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test multiple users can have sessions for same case."""
    users = ["user_1", "user_2", "user_3"]

    for user_id in users:
        session = create_sample_session(sample_case.case_id, user_id=user_id)
        await session_repository.create(session)

    all_sessions = await session_repository.list_by_case_id(sample_case.case_id)
    assert len(all_sessions) == 3

    # Each user should have their session
    for user_id in users:
        user_sessions = await session_repository.list_by_user_id(user_id)
        assert len(user_sessions) == 1
        assert user_sessions[0].user_id == user_id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_session_goal_persistence(
    session_repository: DatabaseInvestigationSessionRepository,
    sample_case: Case,
):
    """Test session goal is correctly persisted and retrieved."""
    session_goal = (
        "Identify the root cause of the production timeout affecting user checkout flow"
    )
    session = create_sample_session(sample_case.case_id, session_goal=session_goal)
    await session_repository.create(session)

    retrieved = await session_repository.get_by_id(session.session_id)
    assert retrieved.session_goal == session_goal
