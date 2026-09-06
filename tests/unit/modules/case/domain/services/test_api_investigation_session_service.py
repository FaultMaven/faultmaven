"""Unit tests for APIInvestigationSessionService class (TASK-012).

Tests the API investigation session service layer functionality including:
- Create session with validation and authorization
- Get session with authorization via parent case
- Update session with authorization
- Pause/resume session lifecycle
- Complete session with findings
- Abandon session
- Get active session for a case
- List sessions with filters and pagination
- Get session with executions
- Add execution to session
- Check budget exceeded
- Get session statistics
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceError,
    ValidationException,
)
from faultmaven.models.investigation_session import InvestigationSession, SessionState
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    InvestigationStrategy,
)
from faultmaven.modules.case.domain.services.investigation_session_service import (
    APIInvestigationSessionService,
)

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def mock_session_repo():
    """Create mock investigation session repository."""
    repo = AsyncMock()
    return repo


@pytest.fixture
def mock_case_repo():
    """Create mock case repository."""
    repo = AsyncMock()
    return repo


@pytest.fixture
def session_service(mock_session_repo, mock_case_repo):
    """Create APIInvestigationSessionService with mocked repositories."""
    # Note: execution_repo removed - agent executions now handled by case_repo (ICaseRepository)
    # as part of TD-001 migration
    return APIInvestigationSessionService(
        session_repo=mock_session_repo,
        case_repo=mock_case_repo,
    )


@pytest.fixture
def sample_case():
    """Create sample case for testing."""
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_123",
        enterprise_id="org_456",
        title="Test Case",
        description="Test case description",
        state=CaseState.INQUIRY,
        investigation_strategy=InvestigationStrategy.POST_MORTEM,
    )


@pytest.fixture
def sample_session():
    """Create sample session for testing."""
    return InvestigationSession(
        session_id=f"session_{uuid4().hex[:12]}",
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_123",
        organization_id="org_456",
        state=SessionState.ACTIVE,
        session_goal="Debug authentication issue",
        total_token_usage=1000,
        total_agent_executions=5,
        token_budget_limit=10000,
    )


# ============================================================
# Create Session Tests
# ============================================================


class TestCreateSession:
    """Test create_session method."""

    @pytest.mark.asyncio
    async def test_create_session_success(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """Test successful session creation."""
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.get_active_session.return_value = None
        mock_session_repo.create.side_effect = lambda s: s

        result = await session_service.create_session(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_123",
            session_goal="Debug issue",
        )

        assert result is not None
        assert result.case_id == sample_case.case_id
        assert result.state == SessionState.ACTIVE
        mock_session_repo.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_session_generates_uuid(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """Test that create_session generates UUID for session_id."""
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.get_active_session.return_value = None
        mock_session_repo.create.side_effect = lambda s: s

        result = await session_service.create_session(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_123",
        )

        assert result.session_id.startswith("session_")
        assert len(result.session_id) == 20  # session_ + 12 hex chars

    @pytest.mark.asyncio
    async def test_create_session_sets_active_status(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """Test that create_session sets status to ACTIVE."""
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.get_active_session.return_value = None
        mock_session_repo.create.side_effect = lambda s: s

        result = await session_service.create_session(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_123",
        )

        assert result.state == SessionState.ACTIVE

    @pytest.mark.asyncio
    async def test_create_session_sets_timestamps(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """Test that create_session sets correct timestamps."""
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.get_active_session.return_value = None
        mock_session_repo.create.side_effect = lambda s: s

        before = datetime.now(timezone.utc)
        result = await session_service.create_session(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_123",
        )
        after = datetime.now(timezone.utc)

        assert result.created_at >= before
        assert result.created_at <= after
        assert result.started_at >= before
        assert result.started_at <= after

    @pytest.mark.asyncio
    async def test_create_session_not_found_case_raises_error(
        self, session_service, mock_case_repo
    ):
        """Test that non-existent case raises NotFoundError."""
        mock_case_repo.get.return_value = None

        with pytest.raises(NotFoundError) as exc_info:
            await session_service.create_session(
                case_id="nonexistent_case",
                organization_id="org_1",
                user_id="user_1",
            )

        assert "Case" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_session_wrong_org_raises_authorization_error(
        self, session_service, mock_case_repo, sample_case
    ):
        """Test that wrong organization raises AuthorizationError."""
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await session_service.create_session(
                case_id=sample_case.case_id,
                organization_id="different_org",
                user_id="user_1",
            )

    @pytest.mark.asyncio
    async def test_create_session_active_session_exists_raises_conflict(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_case,
        sample_session,
    ):
        """Test that ConflictError raised if active session exists."""
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.get_active_session.return_value = sample_session

        with pytest.raises(ConflictError) as exc_info:
            await session_service.create_session(
                case_id=sample_case.case_id,
                organization_id=sample_case.organization_id,
                user_id="user_1",
            )

        assert "active session already exists" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_create_session_empty_case_id_raises_validation_error(
        self, session_service
    ):
        """Test that empty case_id raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            await session_service.create_session(
                case_id="",
                organization_id="org_1",
                user_id="user_1",
            )

        assert "case_id" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_create_session_empty_user_id_raises_validation_error(
        self, session_service
    ):
        """Test that empty user_id raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            await session_service.create_session(
                case_id="case_1",
                organization_id="org_1",
                user_id="",
            )

        assert "user_id" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_create_session_empty_org_id_raises_validation_error(
        self, session_service
    ):
        """Test that empty organization_id raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            await session_service.create_session(
                case_id="case_1",
                organization_id="",
                user_id="user_1",
            )

        assert "organization_id" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_create_session_negative_budget_raises_validation_error(
        self, session_service
    ):
        """Test that negative token budget raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            await session_service.create_session(
                case_id="case_1",
                organization_id="org_1",
                user_id="user_1",
                token_budget_limit=-100,
            )

        assert "token_budget_limit" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_create_session_with_token_budget(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """Test creating session with token budget limit."""
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.get_active_session.return_value = None
        mock_session_repo.create.side_effect = lambda s: s

        result = await session_service.create_session(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_123",
            token_budget_limit=5000,
        )

        assert result.token_budget_limit == 5000

    @pytest.mark.asyncio
    async def test_create_session_with_metadata(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """Test creating session with metadata."""
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.get_active_session.return_value = None
        mock_session_repo.create.side_effect = lambda s: s

        metadata = {"priority": "high", "tags": ["debug", "auth"]}
        result = await session_service.create_session(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_123",
            metadata=metadata,
        )

        assert result.metadata == metadata


# ============================================================
# Get Session Tests
# ============================================================


class TestGetSession:
    """Test get_session method."""

    @pytest.mark.asyncio
    async def test_get_session_success(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test successful session retrieval."""
        sample_session.case_id = sample_case.case_id
        sample_session.organization_id = sample_case.organization_id
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        result = await session_service.get_session(
            sample_session.session_id, sample_case.organization_id
        )

        assert result is not None
        assert result.session_id == sample_session.session_id
        mock_session_repo.get_by_id.assert_called_once_with(sample_session.session_id)

    @pytest.mark.asyncio
    async def test_get_session_not_found_returns_none(
        self, session_service, mock_session_repo
    ):
        """Test that non-existent session returns None."""
        mock_session_repo.get_by_id.return_value = None

        result = await session_service.get_session("nonexistent_id", "org_1")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_session_wrong_org_returns_none(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that wrong organization returns None."""
        sample_session.case_id = sample_case.case_id
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        result = await session_service.get_session(
            sample_session.session_id, "different_org"
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_get_session_authorization_via_parent_case(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test authorization check via parent case."""
        sample_session.case_id = sample_case.case_id
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        result = await session_service.get_session(
            sample_session.session_id, sample_case.organization_id
        )

        assert result is not None
        mock_case_repo.get.assert_called_once_with(sample_session.case_id)

    @pytest.mark.asyncio
    async def test_get_session_empty_id_returns_none(self, session_service):
        """Test that empty session_id returns None."""
        result = await session_service.get_session("", "org_1")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_session_whitespace_id_returns_none(self, session_service):
        """Test that whitespace session_id returns None."""
        result = await session_service.get_session("   ", "org_1")

        assert result is None


# ============================================================
# Update Session Tests
# ============================================================


class TestUpdateSession:
    """Test update_session method."""

    @pytest.mark.asyncio
    async def test_update_session_success(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test successful session update."""
        sample_session.case_id = sample_case.case_id
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.update.side_effect = lambda s: s

        result = await session_service.update_session(
            sample_session.session_id,
            sample_case.organization_id,
            {"session_goal": "Updated goal"},
            case_id=sample_session.case_id,
        )

        assert result.session_goal == "Updated goal"
        mock_session_repo.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_session_updates_token_budget(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test updating token budget limit."""
        sample_session.case_id = sample_case.case_id
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.update.side_effect = lambda s: s

        result = await session_service.update_session(
            sample_session.session_id,
            sample_case.organization_id,
            {"token_budget_limit": 20000},
            case_id=sample_session.case_id,
        )

        assert result.token_budget_limit == 20000

    @pytest.mark.asyncio
    async def test_update_session_updates_metadata(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test updating session metadata."""
        sample_session.case_id = sample_case.case_id
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.update.side_effect = lambda s: s

        new_metadata = {"priority": "high"}
        result = await session_service.update_session(
            sample_session.session_id,
            sample_case.organization_id,
            {"metadata": new_metadata},
            case_id=sample_session.case_id,
        )

        assert result.metadata == new_metadata

    @pytest.mark.asyncio
    async def test_update_session_not_found_raises_error(
        self, session_service, mock_session_repo
    ):
        """Test that updating non-existent session raises NotFoundError."""
        mock_session_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundError):
            await session_service.update_session(
                "nonexistent",
                "org_1",
                {"session_goal": "New"},
                case_id="case_1",
            )

    @pytest.mark.asyncio
    async def test_update_session_wrong_org_raises_authorization_error(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that wrong organization raises AuthorizationError."""
        sample_session.case_id = sample_case.case_id
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await session_service.update_session(
                sample_session.session_id,
                "different_org",
                {"session_goal": "New"},
                case_id=sample_session.case_id,
            )

    @pytest.mark.asyncio
    async def test_update_session_empty_updates_raises_validation_error(
        self, session_service
    ):
        """Test that empty updates raises ValidationException."""
        with pytest.raises(ValidationException):
            await session_service.update_session(
                "session_1", "org_1", {}, case_id="case_1"
            )

    @pytest.mark.asyncio
    async def test_update_session_negative_budget_raises_validation_error(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that negative token budget raises ValidationException."""
        sample_session.case_id = sample_case.case_id
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(ValidationException) as exc_info:
            await session_service.update_session(
                sample_session.session_id,
                sample_case.organization_id,
                {"token_budget_limit": -100},
                case_id=sample_session.case_id,
            )

        assert "token_budget_limit" in str(exc_info.value).lower()


# ============================================================
# Pause Session Tests
# ============================================================


class TestPauseSession:
    """Test pause_session method."""

    @pytest.mark.asyncio
    async def test_pause_session_success(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test successful session pause."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.ACTIVE
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.update.side_effect = lambda s: s

        result = await session_service.pause_session(
            sample_session.session_id,
            sample_case.organization_id,
            case_id=sample_session.case_id,
        )

        assert result.state == SessionState.PAUSED
        mock_session_repo.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_pause_session_not_active_raises_validation_error(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that pausing non-active session raises ValidationException."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.COMPLETED
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(ValidationException) as exc_info:
            await session_service.pause_session(
                sample_session.session_id,
                sample_case.organization_id,
                case_id=sample_session.case_id,
            )

        assert "cannot pause" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_pause_session_authorization_check(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test authorization check when pausing."""
        sample_session.case_id = sample_case.case_id
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await session_service.pause_session(
                sample_session.session_id,
                "different_org",
                case_id=sample_session.case_id,
            )


# ============================================================
# Resume Session Tests
# ============================================================


class TestResumeSession:
    """Test resume_session method."""

    @pytest.mark.asyncio
    async def test_resume_session_success(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test successful session resume."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.PAUSED
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.update.side_effect = lambda s: s

        result = await session_service.resume_session(
            sample_session.session_id,
            sample_case.organization_id,
            case_id=sample_session.case_id,
        )

        assert result.state == SessionState.ACTIVE
        mock_session_repo.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_resume_session_not_paused_raises_validation_error(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that resuming non-paused session raises ValidationException."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.ACTIVE
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(ValidationException) as exc_info:
            await session_service.resume_session(
                sample_session.session_id,
                sample_case.organization_id,
                case_id=sample_session.case_id,
            )

        assert "cannot resume" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_resume_session_authorization_check(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test authorization check when resuming."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.PAUSED
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await session_service.resume_session(
                sample_session.session_id,
                "different_org",
                case_id=sample_session.case_id,
            )


# ============================================================
# Complete Session Tests
# ============================================================


class TestCompleteSession:
    """Test complete_session method."""

    @pytest.mark.asyncio
    async def test_complete_session_success(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test successful session completion."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.ACTIVE
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.update.side_effect = lambda s: s

        result = await session_service.complete_session(
            sample_session.session_id,
            sample_case.organization_id,
            "Found root cause: memory leak in auth module",
            case_id=sample_session.case_id,
        )

        assert result.state == SessionState.COMPLETED
        mock_session_repo.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_complete_session_sets_findings_summary(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that complete_session sets findings_summary."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.ACTIVE
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.update.side_effect = lambda s: s

        result = await session_service.complete_session(
            sample_session.session_id,
            sample_case.organization_id,
            "Root cause identified",
            case_id=sample_session.case_id,
        )

        assert result.findings_summary == "Root cause identified"

    @pytest.mark.asyncio
    async def test_complete_session_sets_ended_at(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that complete_session sets ended_at timestamp."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.ACTIVE
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.update.side_effect = lambda s: s

        before = datetime.now(timezone.utc)
        result = await session_service.complete_session(
            sample_session.session_id,
            sample_case.organization_id,
            "Findings",
            case_id=sample_session.case_id,
        )
        after = datetime.now(timezone.utc)

        assert result.ended_at is not None
        assert result.ended_at >= before
        assert result.ended_at <= after

    @pytest.mark.asyncio
    async def test_complete_session_calculates_duration(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that complete_session calculates total_duration_ms."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.ACTIVE
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.update.side_effect = lambda s: s

        result = await session_service.complete_session(
            sample_session.session_id,
            sample_case.organization_id,
            "Findings",
            case_id=sample_session.case_id,
        )

        assert result.total_duration_ms is not None
        assert result.total_duration_ms >= 0

    @pytest.mark.asyncio
    async def test_complete_session_already_completed_raises_validation_error(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that completing already-completed session raises ValidationException."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.COMPLETED
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(ValidationException) as exc_info:
            await session_service.complete_session(
                sample_session.session_id,
                sample_case.organization_id,
                "Findings",
                case_id=sample_session.case_id,
            )

        assert "terminal state" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_complete_session_already_abandoned_raises_validation_error(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that completing abandoned session raises ValidationException."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.ABANDONED
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(ValidationException):
            await session_service.complete_session(
                sample_session.session_id,
                sample_case.organization_id,
                "Findings",
                case_id=sample_session.case_id,
            )

    @pytest.mark.asyncio
    async def test_complete_session_empty_findings_raises_validation_error(
        self, session_service
    ):
        """Test that empty findings raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            await session_service.complete_session(
                "session_1", "org_1", "", case_id="case_1"
            )

        assert "findings_summary" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_complete_session_authorization_check(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test authorization check when completing."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.ACTIVE
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await session_service.complete_session(
                sample_session.session_id,
                "different_org",
                "Findings",
                case_id=sample_session.case_id,
            )


# ============================================================
# Abandon Session Tests
# ============================================================


class TestAbandonSession:
    """Test abandon_session method."""

    @pytest.mark.asyncio
    async def test_abandon_session_success(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test successful session abandonment."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.ACTIVE
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.update.side_effect = lambda s: s

        result = await session_service.abandon_session(
            sample_session.session_id,
            sample_case.organization_id,
            case_id=sample_session.case_id,
        )

        assert result.state == SessionState.ABANDONED
        mock_session_repo.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_abandon_session_already_completed_raises_validation_error(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that abandoning completed session raises ValidationException."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.COMPLETED
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(ValidationException):
            await session_service.abandon_session(
                sample_session.session_id,
                sample_case.organization_id,
                case_id=sample_session.case_id,
            )

    @pytest.mark.asyncio
    async def test_abandon_session_authorization_check(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test authorization check when abandoning."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.ACTIVE
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await session_service.abandon_session(
                sample_session.session_id,
                "different_org",
                case_id=sample_session.case_id,
            )


# ============================================================
# Get Active Session Tests
# ============================================================


class TestGetActiveSession:
    """Test get_active_session method."""

    @pytest.mark.asyncio
    async def test_get_active_session_returns_active(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that get_active_session returns active session."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.ACTIVE
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.get_active_session.return_value = sample_session

        result = await session_service.get_active_session(
            sample_case.case_id, sample_case.organization_id
        )

        assert result is not None
        assert result.state == SessionState.ACTIVE

    @pytest.mark.asyncio
    async def test_get_active_session_returns_none_when_no_active(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """Test that get_active_session returns None when no active session."""
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.get_active_session.return_value = None

        result = await session_service.get_active_session(
            sample_case.case_id, sample_case.organization_id
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_get_active_session_authorization_via_case(
        self, session_service, mock_case_repo, sample_case
    ):
        """Test authorization check via case."""
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await session_service.get_active_session(
                sample_case.case_id, "different_org"
            )

    @pytest.mark.asyncio
    async def test_get_active_session_not_found_case_raises_error(
        self, session_service, mock_case_repo
    ):
        """Test that non-existent case raises NotFoundError."""
        mock_case_repo.get.return_value = None

        with pytest.raises(NotFoundError):
            await session_service.get_active_session("nonexistent", "org_1")


# ============================================================
# List Sessions Tests
# ============================================================


class TestListSessions:
    """Test list_sessions method."""

    @pytest.mark.asyncio
    async def test_list_sessions_returns_all_for_case(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that list_sessions returns all sessions for case."""
        sample_session.case_id = sample_case.case_id
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.list_by_case_id.return_value = [sample_session]

        result = await session_service.list_sessions(
            sample_case.case_id, sample_case.organization_id
        )

        assert len(result) == 1
        assert result[0].session_id == sample_session.session_id

    @pytest.mark.asyncio
    async def test_list_sessions_with_status_filter(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """Test list_sessions filters by status."""
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.list_by_case_id.return_value = []

        await session_service.list_sessions(
            sample_case.case_id,
            sample_case.organization_id,
            state=SessionState.ACTIVE,
        )

        mock_session_repo.list_by_case_id.assert_called_once_with(
            sample_case.case_id, state=SessionState.ACTIVE
        )

    @pytest.mark.asyncio
    async def test_list_sessions_pagination_limit(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """Test list_sessions respects limit."""
        sessions = [
            InvestigationSession(
                session_id=f"session_{i}",
                case_id=sample_case.case_id,
                user_id="user_1",
                organization_id=sample_case.organization_id,
            )
            for i in range(10)
        ]
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.list_by_case_id.return_value = sessions

        result = await session_service.list_sessions(
            sample_case.case_id, sample_case.organization_id, limit=5
        )

        assert len(result) == 5

    @pytest.mark.asyncio
    async def test_list_sessions_pagination_offset(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """Test list_sessions respects offset."""
        sessions = [
            InvestigationSession(
                session_id=f"session_{i}",
                case_id=sample_case.case_id,
                user_id="user_1",
                organization_id=sample_case.organization_id,
            )
            for i in range(10)
        ]
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.list_by_case_id.return_value = sessions

        result = await session_service.list_sessions(
            sample_case.case_id, sample_case.organization_id, offset=3, limit=5
        )

        assert len(result) == 5
        assert result[0].session_id == "session_3"

    @pytest.mark.asyncio
    async def test_list_sessions_authorization_check(
        self, session_service, mock_case_repo, sample_case
    ):
        """Test authorization check."""
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await session_service.list_sessions(sample_case.case_id, "different_org")

    @pytest.mark.asyncio
    async def test_list_sessions_empty_results(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """Test empty results handled correctly."""
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.list_by_case_id.return_value = []

        result = await session_service.list_sessions(
            sample_case.case_id, sample_case.organization_id
        )

        assert result == []


class TestCheckBudgetExceeded:
    """Test check_budget_exceeded method."""

    @pytest.mark.asyncio
    async def test_check_budget_exceeded_returns_correct_status(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that check_budget_exceeded returns correct status."""
        sample_session.case_id = sample_case.case_id
        sample_session.total_token_usage = 5000
        sample_session.token_budget_limit = 10000
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        result = await session_service.check_budget_exceeded(
            sample_session.session_id,
            sample_case.organization_id,
            case_id=sample_session.case_id,
        )

        assert result["is_over_budget"] is False
        assert result["total_token_usage"] == 5000
        assert result["token_budget_limit"] == 10000
        assert result["usage_percentage"] == 50.0

    @pytest.mark.asyncio
    async def test_check_budget_exceeded_when_over_budget(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test returns True when over budget."""
        sample_session.case_id = sample_case.case_id
        sample_session.total_token_usage = 15000
        sample_session.token_budget_limit = 10000
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        result = await session_service.check_budget_exceeded(
            sample_session.session_id,
            sample_case.organization_id,
            case_id=sample_session.case_id,
        )

        assert result["is_over_budget"] is True
        assert result["usage_percentage"] == 150.0

    @pytest.mark.asyncio
    async def test_check_budget_exceeded_handles_no_budget_limit(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test handles None budget limit."""
        sample_session.case_id = sample_case.case_id
        sample_session.total_token_usage = 5000
        sample_session.token_budget_limit = None
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        result = await session_service.check_budget_exceeded(
            sample_session.session_id,
            sample_case.organization_id,
            case_id=sample_session.case_id,
        )

        assert result["is_over_budget"] is False
        assert result["token_budget_limit"] is None
        assert result["usage_percentage"] is None

    @pytest.mark.asyncio
    async def test_check_budget_exceeded_authorization_check(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test authorization check."""
        sample_session.case_id = sample_case.case_id
        mock_session_repo.get_by_id.return_value = sample_session
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await session_service.check_budget_exceeded(
                sample_session.session_id,
                "different_org",
                case_id=sample_session.case_id,
            )


# ============================================================
# Get Session Statistics Tests
# ============================================================


class TestGetSessionStatistics:
    """Test get_session_statistics method."""

    @pytest.mark.asyncio
    async def test_get_statistics_returns_total_sessions(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that statistics includes total_sessions."""
        sample_session.case_id = sample_case.case_id
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.list_by_case_id.return_value = [sample_session]

        result = await session_service.get_session_statistics(
            sample_case.case_id, sample_case.organization_id
        )

        assert "total_sessions" in result
        assert result["total_sessions"] == 1

    @pytest.mark.asyncio
    async def test_get_statistics_returns_by_status(
        self,
        session_service,
        mock_session_repo,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that statistics includes by_status breakdown."""
        sample_session.case_id = sample_case.case_id
        sample_session.state = SessionState.ACTIVE
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.list_by_case_id.return_value = [sample_session]

        result = await session_service.get_session_statistics(
            sample_case.case_id, sample_case.organization_id
        )

        assert "by_status" in result
        assert "active" in result["by_status"]
        assert result["by_status"]["active"] == 1

    @pytest.mark.asyncio
    async def test_get_statistics_returns_total_token_usage(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """Test that statistics includes total token usage."""
        sessions = [
            InvestigationSession(
                session_id=f"session_{i}",
                case_id=sample_case.case_id,
                user_id="user_1",
                organization_id=sample_case.organization_id,
                total_token_usage=1000,
            )
            for i in range(3)
        ]
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.list_by_case_id.return_value = sessions

        result = await session_service.get_session_statistics(
            sample_case.case_id, sample_case.organization_id
        )

        assert result["total_token_usage_all_sessions"] == 3000

    @pytest.mark.asyncio
    async def test_get_statistics_returns_total_agent_executions(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """Test that statistics includes total agent executions."""
        sessions = [
            InvestigationSession(
                session_id=f"session_{i}",
                case_id=sample_case.case_id,
                user_id="user_1",
                organization_id=sample_case.organization_id,
                total_agent_executions=5,
            )
            for i in range(2)
        ]
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.list_by_case_id.return_value = sessions

        result = await session_service.get_session_statistics(
            sample_case.case_id, sample_case.organization_id
        )

        assert result["total_agent_executions_all_sessions"] == 10

    @pytest.mark.asyncio
    async def test_get_statistics_calculates_avg_duration(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """Test that statistics calculates average session duration."""
        sessions = [
            InvestigationSession(
                session_id=f"session_{i}",
                case_id=sample_case.case_id,
                user_id="user_1",
                organization_id=sample_case.organization_id,
                total_duration_ms=60000,  # 1 minute
            )
            for i in range(2)
        ]
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.list_by_case_id.return_value = sessions

        result = await session_service.get_session_statistics(
            sample_case.case_id, sample_case.organization_id
        )

        assert result["avg_session_duration_ms"] == 60000.0

    @pytest.mark.asyncio
    async def test_get_statistics_authorization_check(
        self, session_service, mock_case_repo, sample_case
    ):
        """Test authorization check."""
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await session_service.get_session_statistics(
                sample_case.case_id, "different_org"
            )

    @pytest.mark.asyncio
    async def test_get_statistics_empty_sessions(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """Test statistics with no sessions."""
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.list_by_case_id.return_value = []

        result = await session_service.get_session_statistics(
            sample_case.case_id, sample_case.organization_id
        )

        assert result["total_sessions"] == 0
        assert result["by_status"] == {}
        assert result["total_token_usage_all_sessions"] == 0
        assert result["avg_session_duration_ms"] == 0.0


# ============================================================
# Cross-case session binding (#1044)
# ============================================================


@pytest.mark.security
class TestSessionMustBelongToNamedCase:
    """A session id from another case must be refused before the write lands.

    Every mutating route names a case in its path, and the router gates on the
    caller's access to *that* case. Nothing bound the session to it: the service
    resolved by session id plus organization, mutated, and the route compared
    ``session.case_id != case_id`` only after the call returned — so a caller
    with a case of their own could pause, resume, complete or retitle any
    session in the organization and receive a 404 over a change that had
    already been persisted.

    ``_get_session_with_authorization`` now takes the named case and refuses
    first. These tests assert the refusal *and* that the repository was never
    asked to write, which is the half a status-code assertion would miss.
    """

    @pytest.fixture
    def foreign_session(self, mock_session_repo, mock_case_repo, sample_case):
        """A session in a different case, reachable in the caller's org."""
        session = InvestigationSession(
            session_id=f"session_{uuid4().hex[:12]}",
            case_id=f"case_{uuid4().hex[:12]}",  # NOT the case the caller names
            user_id="user_victim",
            organization_id=sample_case.organization_id,
            state=SessionState.ACTIVE,
            session_goal="Victim's investigation",
        )
        mock_session_repo.get_by_id.return_value = session
        mock_case_repo.get.return_value = sample_case
        return session

    @pytest.mark.asyncio
    async def test_pause_refuses_and_does_not_write(
        self, session_service, mock_session_repo, foreign_session, sample_case
    ):
        with pytest.raises(NotFoundError):
            await session_service.pause_session(
                foreign_session.session_id,
                sample_case.organization_id,
                case_id=sample_case.case_id,
            )

        mock_session_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_refuses_and_does_not_write(
        self, session_service, mock_session_repo, foreign_session, sample_case
    ):
        foreign_session.state = SessionState.PAUSED

        with pytest.raises(NotFoundError):
            await session_service.resume_session(
                foreign_session.session_id,
                sample_case.organization_id,
                case_id=sample_case.case_id,
            )

        mock_session_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_complete_refuses_and_does_not_write(
        self, session_service, mock_session_repo, foreign_session, sample_case
    ):
        with pytest.raises(NotFoundError):
            await session_service.complete_session(
                foreign_session.session_id,
                sample_case.organization_id,
                "Findings supplied by someone else",
                case_id=sample_case.case_id,
            )

        mock_session_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_refuses_and_does_not_write(
        self, session_service, mock_session_repo, foreign_session, sample_case
    ):
        with pytest.raises(NotFoundError):
            await session_service.update_session(
                foreign_session.session_id,
                sample_case.organization_id,
                {"session_goal": "Hijacked"},
                case_id=sample_case.case_id,
            )

        mock_session_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_abandon_refuses_and_does_not_write(
        self, session_service, mock_session_repo, foreign_session, sample_case
    ):
        with pytest.raises(NotFoundError):
            await session_service.abandon_session(
                foreign_session.session_id,
                sample_case.organization_id,
                case_id=sample_case.case_id,
            )

        mock_session_repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_session_in_the_named_case_still_works(
        self, session_service, mock_session_repo, mock_case_repo, sample_case
    ):
        """The binding is a predicate, not a blanket refusal."""
        session = InvestigationSession(
            session_id=f"session_{uuid4().hex[:12]}",
            case_id=sample_case.case_id,
            user_id=sample_case.user_id,
            organization_id=sample_case.organization_id,
            state=SessionState.ACTIVE,
        )
        mock_session_repo.get_by_id.return_value = session
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.update.side_effect = lambda s: s

        result = await session_service.pause_session(
            session.session_id,
            sample_case.organization_id,
            case_id=sample_case.case_id,
        )

        assert result.state == SessionState.PAUSED
