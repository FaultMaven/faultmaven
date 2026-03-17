"""Unit tests for Investigation Session Repository.

Tests both InMemoryInvestigationSessionRepository and the repository interface.
"""

from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.infrastructure.persistence.investigation_session_repository import (
    InMemoryInvestigationSessionRepository,
)
from faultmaven.models.investigation_session import InvestigationSession, SessionStatus
from tests.utils import generate_case_id, generate_session_id


def create_sample_session(
    session_id: str = None,
    case_id: str = None,
    user_id: str = "user_test123",
    organization_id: str = "org_test456",
    status: SessionStatus = SessionStatus.ACTIVE,
    started_at: datetime = None,
    total_token_usage: int = 0,
    total_agent_executions: int = 0,
    session_goal: str = None,
    token_budget_limit: int = None,
    metadata: dict = None,
) -> InvestigationSession:
    """Create a sample investigation session for testing."""
    return InvestigationSession(
        session_id=session_id or generate_session_id(),
        case_id=case_id or generate_case_id(),
        user_id=user_id,
        organization_id=organization_id,
        status=status,
        started_at=started_at or datetime.now(timezone.utc),
        total_token_usage=total_token_usage,
        total_agent_executions=total_agent_executions,
        session_goal=session_goal,
        token_budget_limit=token_budget_limit,
        metadata=metadata,
    )


class TestInMemoryRepositoryCreate:
    """Tests for create operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryInvestigationSessionRepository()

    @pytest.mark.asyncio
    async def test_create_session_success(self, repository):
        """Test successful session creation."""
        session = create_sample_session()

        result = await repository.create(session)

        assert result.session_id == session.session_id
        assert result.case_id == session.case_id
        assert result.user_id == session.user_id
        assert result.organization_id == session.organization_id
        assert result.status == SessionStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_create_session_with_all_fields(self, repository):
        """Test creation with all fields populated."""
        session = create_sample_session(
            session_goal="Find the root cause",
            token_budget_limit=5000,
            total_token_usage=100,
            total_agent_executions=2,
            metadata={"environment": "production"},
        )

        result = await repository.create(session)

        assert result.session_goal == "Find the root cause"
        assert result.token_budget_limit == 5000
        assert result.total_token_usage == 100
        assert result.total_agent_executions == 2
        assert result.metadata == {"environment": "production"}

    @pytest.mark.asyncio
    async def test_create_duplicate_id_fails(self, repository):
        """Test creating session with duplicate ID fails."""
        session_id = generate_session_id()
        session1 = create_sample_session(session_id=session_id)
        session2 = create_sample_session(session_id=session_id)

        await repository.create(session1)

        with pytest.raises(ValueError, match="already exists"):
            await repository.create(session2)

    @pytest.mark.asyncio
    async def test_create_returns_copy(self, repository):
        """Test that create returns a copy, not the original."""
        session = create_sample_session()
        result = await repository.create(session)

        # Modify original
        session.session_goal = "Modified goal"

        # Result should not be affected
        retrieved = await repository.get_by_id(result.session_id)
        assert retrieved.session_goal != "Modified goal"


class TestInMemoryRepositoryGetById:
    """Tests for get_by_id operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryInvestigationSessionRepository()

    @pytest.mark.asyncio
    async def test_get_by_id_found(self, repository):
        """Test retrieving existing session."""
        session = create_sample_session()
        await repository.create(session)

        result = await repository.get_by_id(session.session_id)

        assert result is not None
        assert result.session_id == session.session_id
        assert result.case_id == session.case_id

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self, repository):
        """Test retrieving non-existent session returns None."""
        result = await repository.get_by_id("sess_nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_id_returns_copy(self, repository):
        """Test that get_by_id returns a copy."""
        session = create_sample_session()
        await repository.create(session)

        result1 = await repository.get_by_id(session.session_id)
        result1.session_goal = "Modified"

        result2 = await repository.get_by_id(session.session_id)
        assert result2.session_goal != "Modified"


class TestInMemoryRepositoryUpdate:
    """Tests for update operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryInvestigationSessionRepository()

    @pytest.mark.asyncio
    async def test_update_session_success(self, repository):
        """Test successful session update."""
        session = create_sample_session()
        await repository.create(session)

        session.status = SessionStatus.PAUSED
        session.session_goal = "Updated goal"

        result = await repository.update(session)

        assert result.status == SessionStatus.PAUSED
        assert result.session_goal == "Updated goal"

    @pytest.mark.asyncio
    async def test_update_updates_timestamp(self, repository):
        """Test that update modifies updated_at."""
        session = create_sample_session()
        await repository.create(session)

        original_updated = session.updated_at

        import time

        time.sleep(0.001)

        session.session_goal = "New goal"
        result = await repository.update(session)

        assert result.updated_at > original_updated

    @pytest.mark.asyncio
    async def test_update_not_found_fails(self, repository):
        """Test updating non-existent session fails."""
        session = create_sample_session()

        with pytest.raises(ValueError, match="not found"):
            await repository.update(session)

    @pytest.mark.asyncio
    async def test_update_all_fields(self, repository):
        """Test updating all mutable fields."""
        session = create_sample_session()
        await repository.create(session)

        # Update all fields
        session.status = SessionStatus.COMPLETED
        session.ended_at = datetime.now(timezone.utc)
        session.session_goal = "Final goal"
        session.findings_summary = "Found the issue"
        session.total_token_usage = 5000
        session.total_agent_executions = 10
        session.total_duration_ms = 60000

        result = await repository.update(session)

        assert result.status == SessionStatus.COMPLETED
        assert result.ended_at is not None
        assert result.session_goal == "Final goal"
        assert result.findings_summary == "Found the issue"
        assert result.total_token_usage == 5000
        assert result.total_agent_executions == 10
        assert result.total_duration_ms == 60000


class TestInMemoryRepositoryDelete:
    """Tests for delete operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryInvestigationSessionRepository()

    @pytest.mark.asyncio
    async def test_delete_session_success(self, repository):
        """Test successful session deletion."""
        session = create_sample_session()
        await repository.create(session)

        result = await repository.delete(session.session_id)

        assert result is True
        assert await repository.get_by_id(session.session_id) is None

    @pytest.mark.asyncio
    async def test_delete_not_found(self, repository):
        """Test deleting non-existent session returns False."""
        result = await repository.delete("sess_nonexistent")

        assert result is False

    @pytest.mark.asyncio
    async def test_delete_does_not_affect_others(self, repository):
        """Test that deleting one session doesn't affect others."""
        session1 = create_sample_session()
        session2 = create_sample_session()
        await repository.create(session1)
        await repository.create(session2)

        await repository.delete(session1.session_id)

        assert await repository.get_by_id(session2.session_id) is not None


class TestInMemoryRepositoryListByCaseId:
    """Tests for list_by_case_id operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryInvestigationSessionRepository()

    @pytest.mark.asyncio
    async def test_list_by_case_id_empty(self, repository):
        """Test listing sessions for case with no sessions."""
        result = await repository.list_by_case_id("case_empty")

        assert result == []

    @pytest.mark.asyncio
    async def test_list_by_case_id_multiple(self, repository):
        """Test listing multiple sessions for a case."""
        case_id = generate_case_id()

        sessions = [
            create_sample_session(case_id=case_id),
            create_sample_session(case_id=case_id),
            create_sample_session(case_id=case_id),
        ]
        for s in sessions:
            await repository.create(s)

        result = await repository.list_by_case_id(case_id)

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_list_by_case_id_only_matching(self, repository):
        """Test that only sessions for the specified case are returned."""
        case_id1 = generate_case_id()
        case_id2 = generate_case_id()

        await repository.create(create_sample_session(case_id=case_id1))
        await repository.create(create_sample_session(case_id=case_id1))
        await repository.create(create_sample_session(case_id=case_id2))

        result = await repository.list_by_case_id(case_id1)

        assert len(result) == 2
        assert all(s.case_id == case_id1 for s in result)

    @pytest.mark.asyncio
    async def test_list_by_case_id_ordered_by_started_at_desc(self, repository):
        """Test sessions are ordered by started_at descending."""
        case_id = generate_case_id()
        base_time = datetime.now(timezone.utc)

        s1 = create_sample_session(
            case_id=case_id, started_at=base_time - timedelta(hours=2)
        )
        s2 = create_sample_session(
            case_id=case_id, started_at=base_time - timedelta(hours=1)
        )
        s3 = create_sample_session(case_id=case_id, started_at=base_time)

        await repository.create(s1)
        await repository.create(s2)
        await repository.create(s3)

        result = await repository.list_by_case_id(case_id)

        assert result[0].session_id == s3.session_id
        assert result[1].session_id == s2.session_id
        assert result[2].session_id == s1.session_id

    @pytest.mark.asyncio
    async def test_list_by_case_id_with_status_filter(self, repository):
        """Test filtering by status."""
        case_id = generate_case_id()

        active_session = create_sample_session(
            case_id=case_id, status=SessionStatus.ACTIVE
        )
        paused_session = create_sample_session(
            case_id=case_id, status=SessionStatus.PAUSED
        )
        completed_session = create_sample_session(
            case_id=case_id, status=SessionStatus.COMPLETED
        )

        await repository.create(active_session)
        await repository.create(paused_session)
        await repository.create(completed_session)

        result = await repository.list_by_case_id(case_id, status=SessionStatus.ACTIVE)

        assert len(result) == 1
        assert result[0].status == SessionStatus.ACTIVE


class TestInMemoryRepositoryGetActiveSession:
    """Tests for get_active_session operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryInvestigationSessionRepository()

    @pytest.mark.asyncio
    async def test_get_active_session_found(self, repository):
        """Test getting active session when one exists."""
        case_id = generate_case_id()
        active_session = create_sample_session(
            case_id=case_id, status=SessionStatus.ACTIVE
        )
        await repository.create(active_session)

        result = await repository.get_active_session(case_id)

        assert result is not None
        assert result.status == SessionStatus.ACTIVE
        assert result.session_id == active_session.session_id

    @pytest.mark.asyncio
    async def test_get_active_session_none_when_no_active(self, repository):
        """Test getting active session when none is active."""
        case_id = generate_case_id()
        paused_session = create_sample_session(
            case_id=case_id, status=SessionStatus.PAUSED
        )
        await repository.create(paused_session)

        result = await repository.get_active_session(case_id)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_active_session_returns_most_recent(self, repository):
        """Test that most recent active session is returned when multiple exist."""
        case_id = generate_case_id()
        base_time = datetime.now(timezone.utc)

        older_active = create_sample_session(
            case_id=case_id,
            status=SessionStatus.ACTIVE,
            started_at=base_time - timedelta(hours=1),
        )
        newer_active = create_sample_session(
            case_id=case_id,
            status=SessionStatus.ACTIVE,
            started_at=base_time,
        )

        await repository.create(older_active)
        await repository.create(newer_active)

        result = await repository.get_active_session(case_id)

        assert result.session_id == newer_active.session_id

    @pytest.mark.asyncio
    async def test_get_active_session_ignores_other_cases(self, repository):
        """Test that active sessions from other cases are ignored."""
        case_id1 = generate_case_id()
        case_id2 = generate_case_id()

        # Active session for case 2
        await repository.create(
            create_sample_session(case_id=case_id2, status=SessionStatus.ACTIVE)
        )

        result = await repository.get_active_session(case_id1)

        assert result is None


class TestInMemoryRepositoryListByUserId:
    """Tests for list_by_user_id operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryInvestigationSessionRepository()

    @pytest.mark.asyncio
    async def test_list_by_user_id_empty(self, repository):
        """Test listing sessions for user with no sessions."""
        result = await repository.list_by_user_id("user_empty")

        assert result == []

    @pytest.mark.asyncio
    async def test_list_by_user_id_multiple(self, repository):
        """Test listing multiple sessions for a user."""
        user_id = "user_test_multi"

        for _ in range(5):
            await repository.create(create_sample_session(user_id=user_id))

        result = await repository.list_by_user_id(user_id)

        assert len(result) == 5

    @pytest.mark.asyncio
    async def test_list_by_user_id_pagination(self, repository):
        """Test pagination with limit and offset."""
        user_id = "user_test_page"

        for i in range(10):
            await repository.create(create_sample_session(user_id=user_id))

        page1 = await repository.list_by_user_id(user_id, limit=3, offset=0)
        page2 = await repository.list_by_user_id(user_id, limit=3, offset=3)
        page3 = await repository.list_by_user_id(user_id, limit=3, offset=6)
        page4 = await repository.list_by_user_id(user_id, limit=3, offset=9)

        assert len(page1) == 3
        assert len(page2) == 3
        assert len(page3) == 3
        assert len(page4) == 1

    @pytest.mark.asyncio
    async def test_list_by_user_id_only_matching(self, repository):
        """Test that only sessions for the specified user are returned."""
        user_id1 = "user_one"
        user_id2 = "user_two"

        await repository.create(create_sample_session(user_id=user_id1))
        await repository.create(create_sample_session(user_id=user_id1))
        await repository.create(create_sample_session(user_id=user_id2))

        result = await repository.list_by_user_id(user_id1)

        assert len(result) == 2
        assert all(s.user_id == user_id1 for s in result)


class TestInMemoryRepositoryCountByCaseId:
    """Tests for count_by_case_id operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryInvestigationSessionRepository()

    @pytest.mark.asyncio
    async def test_count_by_case_id_zero(self, repository):
        """Test counting sessions for case with none."""
        result = await repository.count_by_case_id("case_empty")

        assert result == 0

    @pytest.mark.asyncio
    async def test_count_by_case_id_multiple(self, repository):
        """Test counting multiple sessions."""
        case_id = generate_case_id()

        for _ in range(5):
            await repository.create(create_sample_session(case_id=case_id))

        result = await repository.count_by_case_id(case_id)

        assert result == 5

    @pytest.mark.asyncio
    async def test_count_by_case_id_only_matching(self, repository):
        """Test that count only includes matching case."""
        case_id1 = generate_case_id()
        case_id2 = generate_case_id()

        await repository.create(create_sample_session(case_id=case_id1))
        await repository.create(create_sample_session(case_id=case_id1))
        await repository.create(create_sample_session(case_id=case_id2))

        result = await repository.count_by_case_id(case_id1)

        assert result == 2


class TestInMemoryRepositoryClear:
    """Tests for clear helper method."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryInvestigationSessionRepository()

    @pytest.mark.asyncio
    async def test_clear_removes_all(self, repository):
        """Test that clear removes all sessions."""
        case_id = generate_case_id()

        for _ in range(5):
            await repository.create(create_sample_session(case_id=case_id))

        repository.clear()

        result = await repository.count_by_case_id(case_id)
        assert result == 0


class TestInMemoryRepositoryDeleteSessionsForCase:
    """Tests for delete_sessions_for_case helper method."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryInvestigationSessionRepository()

    @pytest.mark.asyncio
    async def test_delete_sessions_for_case(self, repository):
        """Test deleting all sessions for a case."""
        case_id = generate_case_id()

        for _ in range(3):
            await repository.create(create_sample_session(case_id=case_id))

        count = repository.delete_sessions_for_case(case_id)

        assert count == 3
        assert await repository.count_by_case_id(case_id) == 0

    @pytest.mark.asyncio
    async def test_delete_sessions_for_case_preserves_others(self, repository):
        """Test that deleting for one case preserves other cases."""
        case_id1 = generate_case_id()
        case_id2 = generate_case_id()

        await repository.create(create_sample_session(case_id=case_id1))
        await repository.create(create_sample_session(case_id=case_id2))

        repository.delete_sessions_for_case(case_id1)

        assert await repository.count_by_case_id(case_id1) == 0
        assert await repository.count_by_case_id(case_id2) == 1


class TestInMemoryRepositoryMetadata:
    """Tests for metadata handling."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryInvestigationSessionRepository()

    @pytest.mark.asyncio
    async def test_create_with_metadata(self, repository):
        """Test creating session with metadata."""
        session = create_sample_session(
            metadata={"environment": "production", "priority": "high"}
        )

        result = await repository.create(session)

        assert result.metadata == {"environment": "production", "priority": "high"}

    @pytest.mark.asyncio
    async def test_update_metadata(self, repository):
        """Test updating session metadata."""
        session = create_sample_session(metadata={"key": "value"})
        await repository.create(session)

        session.metadata = {"new_key": "new_value", "another": 123}
        result = await repository.update(session)

        assert result.metadata == {"new_key": "new_value", "another": 123}

    @pytest.mark.asyncio
    async def test_metadata_none(self, repository):
        """Test session with None metadata."""
        session = create_sample_session(metadata=None)
        await repository.create(session)

        result = await repository.get_by_id(session.session_id)

        assert result.metadata is None
