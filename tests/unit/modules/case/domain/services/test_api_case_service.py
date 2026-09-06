"""Unit tests for APICaseService class (TASK-011).

Tests the API case service layer functionality including:
- Create case with validation
- Get case with authorization
- Update case with authorization
- Delete case with authorization
- List cases with filters
- Get case with details
- Assign case
- Close case
- Get statistics
- Search cases
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
from faultmaven.modules.case.domain.models import (
    Case,
    CaseSeverity,
    CaseState,
    InvestigationStrategy,
)
from faultmaven.modules.case.domain.services.api_case_service import APICaseService

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def mock_case_repo():
    """Create mock case repository."""
    repo = AsyncMock()
    return repo


@pytest.fixture
def mock_session_repo():
    """Create mock investigation session repository."""
    repo = AsyncMock()
    return repo


@pytest.fixture
def case_service(mock_case_repo, mock_session_repo):
    """Create APICaseService with mocked repositories."""
    return APICaseService(
        case_repo=mock_case_repo,
        session_repo=mock_session_repo,
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


# ============================================================
# Create Case Tests
# ============================================================


class TestCreateCase:
    """Test create_case method."""

    @pytest.mark.asyncio
    async def test_create_case_success(self, case_service, mock_case_repo):
        """Test successful case creation."""
        mock_case_repo.save.return_value = Case(
            case_id="case_abc123def456",
            user_id="user_1",
            enterprise_id="org_1",
            title="New Case",
            description="Description",
            state=CaseState.INQUIRY,
        )

        result = await case_service.create_case(
            user_id="user_1",
            organization_id="org_1",
            title="New Case",
            description="Description",
            severity=CaseSeverity.HIGH,
        )

        assert result is not None
        assert result.case_id == "case_abc123def456"
        mock_case_repo.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_case_generates_uuid(self, case_service, mock_case_repo):
        """Test that create_case generates UUID for case_id."""
        mock_case_repo.save.side_effect = lambda case: case

        result = await case_service.create_case(
            user_id="user_1",
            organization_id="org_1",
            title="Test",
            description="",
            severity=CaseSeverity.LOW,
        )

        assert result.case_id.startswith("case_")
        assert len(result.case_id) == 17  # case_ + 12 hex chars

    @pytest.mark.asyncio
    async def test_create_case_sets_timestamps(self, case_service, mock_case_repo):
        """Test that create_case sets correct timestamps."""
        mock_case_repo.save.side_effect = lambda case: case

        before = datetime.now(timezone.utc)
        result = await case_service.create_case(
            user_id="user_1",
            organization_id="org_1",
            title="Test",
            description="",
            severity=CaseSeverity.MEDIUM,
        )
        after = datetime.now(timezone.utc)

        assert result.created_at >= before
        assert result.created_at <= after

    @pytest.mark.asyncio
    async def test_create_case_empty_title_raises_validation_error(self, case_service):
        """Test that empty title raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            await case_service.create_case(
                user_id="user_1",
                organization_id="org_1",
                title="",
                description="",
                severity=CaseSeverity.LOW,
            )

        assert "title" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_create_case_whitespace_title_raises_validation_error(
        self, case_service
    ):
        """Test that whitespace-only title raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            await case_service.create_case(
                user_id="user_1",
                organization_id="org_1",
                title="   ",
                description="",
                severity=CaseSeverity.LOW,
            )

        assert "title" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_create_case_title_too_long_raises_validation_error(
        self, case_service
    ):
        """Test that title exceeding 200 chars raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            await case_service.create_case(
                user_id="user_1",
                organization_id="org_1",
                title="x" * 201,
                description="",
                severity=CaseSeverity.LOW,
            )

        assert "200" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_case_empty_user_id_raises_validation_error(
        self, case_service
    ):
        """Test that empty user_id raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            await case_service.create_case(
                user_id="",
                organization_id="org_1",
                title="Test",
                description="",
                severity=CaseSeverity.LOW,
            )

        assert "user_id" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_create_case_empty_org_id_raises_validation_error(self, case_service):
        """Test that empty organization_id raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            await case_service.create_case(
                user_id="user_1",
                organization_id="",
                title="Test",
                description="",
                severity=CaseSeverity.LOW,
            )

        assert "organization_id" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_create_case_repo_failure_raises_service_error(
        self, case_service, mock_case_repo
    ):
        """Test that repository failure raises ServiceError."""
        mock_case_repo.save.side_effect = Exception("Database error")

        with pytest.raises(ServiceError):
            await case_service.create_case(
                user_id="user_1",
                organization_id="org_1",
                title="Test",
                description="",
                severity=CaseSeverity.LOW,
            )


# ============================================================
# Get Case Tests
# ============================================================


class TestGetCase:
    """Test get_case method."""

    @pytest.mark.asyncio
    async def test_get_case_success(self, case_service, mock_case_repo, sample_case):
        """Test successful case retrieval."""
        mock_case_repo.get.return_value = sample_case

        result = await case_service.get_case(
            sample_case.case_id, sample_case.organization_id
        )

        assert result is not None
        assert result.case_id == sample_case.case_id
        mock_case_repo.get.assert_called_once_with(sample_case.case_id)

    @pytest.mark.asyncio
    async def test_get_case_not_found_returns_none(self, case_service, mock_case_repo):
        """Test that non-existent case returns None."""
        mock_case_repo.get.return_value = None

        result = await case_service.get_case("nonexistent_id", "org_1")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_case_wrong_org_returns_none(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test that wrong organization returns None (authorization)."""
        mock_case_repo.get.return_value = sample_case

        result = await case_service.get_case(sample_case.case_id, "different_org")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_case_correct_org_returns_case(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test that correct organization returns case."""
        mock_case_repo.get.return_value = sample_case

        result = await case_service.get_case(
            sample_case.case_id, sample_case.organization_id
        )

        assert result is not None
        assert result.organization_id == sample_case.organization_id

    @pytest.mark.asyncio
    async def test_get_case_empty_id_returns_none(self, case_service):
        """Test that empty case_id returns None."""
        result = await case_service.get_case("", "org_1")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_case_whitespace_id_returns_none(self, case_service):
        """Test that whitespace case_id returns None."""
        result = await case_service.get_case("   ", "org_1")

        assert result is None


# ============================================================
# Update Case Tests
# ============================================================


class TestUpdateCase:
    """Test update_case method."""

    @pytest.mark.asyncio
    async def test_update_case_success(self, case_service, mock_case_repo, sample_case):
        """Test successful case update."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.save.side_effect = lambda case: case

        result = await case_service.update_case(
            sample_case.case_id,
            sample_case.organization_id,
            {"title": "Updated Title"},
        )

        assert result.title == "Updated Title"
        mock_case_repo.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_case_updates_description(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test updating case description."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.save.side_effect = lambda case: case

        result = await case_service.update_case(
            sample_case.case_id,
            sample_case.organization_id,
            {"description": "New description"},
        )

        assert result.description == "New description"

    @pytest.mark.asyncio
    async def test_update_case_updates_status(
        self, case_service, mock_case_repo, sample_case
    ):
        """Updating status to INVESTIGATING without required inquiry confirmations should fail."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.save.side_effect = lambda case: case
        with pytest.raises(ValidationException):
            await case_service.update_case(
                sample_case.case_id,
                sample_case.organization_id,
                {"state": CaseState.INVESTIGATING},
            )

    @pytest.mark.asyncio
    async def test_update_case_rejects_terminal_state_resolved(
        self, case_service, mock_case_repo, sample_case
    ):
        """A generic update must not jump a case into RESOLVED (terminal).

        Terminal transitions carry mandatory closure semantics (gating,
        closure_reason, audit) only the dedicated paths apply.
        """
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.save.side_effect = lambda case: case
        with pytest.raises(ValidationException) as exc_info:
            await case_service.update_case(
                sample_case.case_id,
                sample_case.organization_id,
                {"state": CaseState.RESOLVED},
            )
        assert "terminal" in str(exc_info.value).lower()
        mock_case_repo.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_case_rejects_terminal_state_closed(
        self, case_service, mock_case_repo, sample_case
    ):
        """A generic update must not jump a case into CLOSED (terminal).

        Guards the string input form too (value is coerced to CaseState first).
        """
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.save.side_effect = lambda case: case
        with pytest.raises(ValidationException) as exc_info:
            await case_service.update_case(
                sample_case.case_id,
                sample_case.organization_id,
                {"state": "closed"},
            )
        assert "terminal" in str(exc_info.value).lower()
        mock_case_repo.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_case_not_found_raises_not_found_error(
        self, case_service, mock_case_repo
    ):
        """Test that updating non-existent case raises NotFoundError."""
        mock_case_repo.get.return_value = None

        with pytest.raises(NotFoundError):
            await case_service.update_case("nonexistent", "org_1", {"title": "New"})

    @pytest.mark.asyncio
    async def test_update_case_wrong_org_raises_authorization_error(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test that wrong organization raises AuthorizationError."""
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await case_service.update_case(
                sample_case.case_id,
                "different_org",
                {"title": "New"},
            )

    @pytest.mark.asyncio
    async def test_update_case_empty_updates_raises_validation_error(
        self, case_service
    ):
        """Test that empty updates raises ValidationException."""
        with pytest.raises(ValidationException):
            await case_service.update_case("case_1", "org_1", {})

    @pytest.mark.asyncio
    async def test_update_case_empty_title_raises_validation_error(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test that updating to empty title raises ValidationException."""
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(ValidationException) as exc_info:
            await case_service.update_case(
                sample_case.case_id,
                sample_case.organization_id,
                {"title": ""},
            )

        assert "title" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_update_case_invalid_status_raises_validation_error(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test that invalid status string raises ValidationException."""
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(ValidationException) as exc_info:
            await case_service.update_case(
                sample_case.case_id,
                sample_case.organization_id,
                {"state": "invalid_status"},
            )

        assert "state" in str(exc_info.value).lower()


# ============================================================
# Delete Case Tests
# ============================================================


class TestDeleteCase:
    """Test delete_case method."""

    @pytest.mark.asyncio
    async def test_delete_case_success(self, case_service, mock_case_repo, sample_case):
        """Test successful case deletion."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.delete.return_value = True

        result = await case_service.delete_case(
            sample_case.case_id, sample_case.organization_id
        )

        assert result is True
        mock_case_repo.delete.assert_called_once_with(sample_case.case_id)

    @pytest.mark.asyncio
    async def test_delete_case_not_found_returns_false(
        self, case_service, mock_case_repo
    ):
        """Test that deleting non-existent case returns False."""
        mock_case_repo.get.return_value = None

        result = await case_service.delete_case("nonexistent", "org_1")

        assert result is False

    @pytest.mark.asyncio
    async def test_delete_case_wrong_org_raises_authorization_error(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test that wrong organization raises AuthorizationError."""
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await case_service.delete_case(sample_case.case_id, "different_org")

    @pytest.mark.asyncio
    async def test_delete_case_empty_id_returns_false(self, case_service):
        """Test that empty case_id returns False."""
        result = await case_service.delete_case("", "org_1")

        assert result is False


# ============================================================
# List Cases Tests
# ============================================================


class TestListCases:
    """Test list_cases method."""

    @pytest.mark.asyncio
    async def test_list_cases_returns_all_for_org(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test that list_cases returns all cases for organization."""
        mock_case_repo.list.return_value = ([sample_case], 1)

        result = await case_service.list_cases("org_456")

        assert len(result) == 1
        assert result[0].case_id == sample_case.case_id

    @pytest.mark.asyncio
    async def test_list_cases_with_user_filter(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test list_cases filters by user_id."""
        mock_case_repo.list.return_value = ([sample_case], 1)

        await case_service.list_cases("org_456", user_id="user_123")

        call_kwargs = mock_case_repo.list.call_args[1]
        assert call_kwargs["user_id"] == "user_123"

    @pytest.mark.asyncio
    async def test_list_cases_with_status_filter(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test list_cases filters by status."""
        mock_case_repo.list.return_value = ([sample_case], 1)

        await case_service.list_cases("org_456", state=CaseState.INQUIRY)

        call_kwargs = mock_case_repo.list.call_args[1]
        assert call_kwargs["state"] == CaseState.INQUIRY

    @pytest.mark.asyncio
    async def test_list_cases_respects_limit(self, case_service, mock_case_repo):
        """Test list_cases respects limit parameter."""
        mock_case_repo.list.return_value = ([], 0)

        await case_service.list_cases("org_456", limit=10)

        call_kwargs = mock_case_repo.list.call_args[1]
        assert call_kwargs["limit"] == 10

    @pytest.mark.asyncio
    async def test_list_cases_respects_offset(self, case_service, mock_case_repo):
        """Test list_cases respects offset parameter."""
        mock_case_repo.list.return_value = ([], 0)

        await case_service.list_cases("org_456", offset=20)

        call_kwargs = mock_case_repo.list.call_args[1]
        assert call_kwargs["offset"] == 20

    @pytest.mark.asyncio
    async def test_list_cases_empty_org_returns_empty(self, case_service):
        """Test that empty organization_id returns empty list."""
        result = await case_service.list_cases("")

        assert result == []


# ============================================================
# Get Case With Details Tests
# ============================================================


class TestGetCaseWithDetails:
    """Test get_case_with_details method."""

    @pytest.mark.asyncio
    async def test_get_case_with_details_includes_case(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test that get_case_with_details includes the case."""
        mock_case_repo.get.return_value = sample_case

        result = await case_service.get_case_with_details(
            sample_case.case_id, sample_case.organization_id
        )

        assert result is not None
        assert "case" in result
        assert result["case"].case_id == sample_case.case_id

    @pytest.mark.asyncio
    async def test_get_case_with_details_includes_sessions(
        self, case_service, mock_case_repo, mock_session_repo, sample_case
    ):
        """Test that get_case_with_details includes sessions when requested."""
        mock_case_repo.get.return_value = sample_case
        mock_session_repo.list_by_case_id.return_value = []

        result = await case_service.get_case_with_details(
            sample_case.case_id,
            sample_case.organization_id,
            include_sessions=True,
        )

        assert "sessions" in result
        mock_session_repo.list_by_case_id.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_case_with_details_includes_evidence(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test that get_case_with_details surfaces case.evidence when requested.

        Storage redesign 2026-04 phase 2: standalone evidence is gone; the
        method now reads `case.evidence` directly off the loaded case rather
        than calling a separate repository method.
        """
        mock_case_repo.get.return_value = sample_case

        result = await case_service.get_case_with_details(
            sample_case.case_id,
            sample_case.organization_id,
            include_evidence=True,
        )

        assert "evidence" in result
        # The result should mirror whatever sample_case.evidence contains.
        assert result["evidence"] == list(getattr(sample_case, "evidence", []) or [])

    @pytest.mark.asyncio
    async def test_get_case_with_details_not_found_returns_none(
        self, case_service, mock_case_repo
    ):
        """Test that get_case_with_details returns None for non-existent case."""
        mock_case_repo.get.return_value = None

        result = await case_service.get_case_with_details("nonexistent", "org_1")

        assert result is None


# ============================================================
# Assign Case Tests
# ============================================================


class TestAssignCase:
    """Test assign_case method."""

    @pytest.mark.asyncio
    async def test_assign_case_success(self, case_service, mock_case_repo, sample_case):
        """Test successful case assignment."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.save.side_effect = lambda case: case

        result = await case_service.assign_case(
            sample_case.case_id,
            sample_case.organization_id,
            "new_assignee",
        )

        assert result is not None
        mock_case_repo.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_assign_case_not_found_raises_not_found_error(
        self, case_service, mock_case_repo
    ):
        """Test that assigning non-existent case raises NotFoundError."""
        mock_case_repo.get.return_value = None

        with pytest.raises(NotFoundError):
            await case_service.assign_case("nonexistent", "org_1", "assignee")

    @pytest.mark.asyncio
    async def test_assign_case_empty_assignee_raises_validation_error(
        self, case_service
    ):
        """Test that empty assignee raises ValidationException."""
        with pytest.raises(ValidationException) as exc_info:
            await case_service.assign_case("case_1", "org_1", "")

        assert "assigned_to" in str(exc_info.value).lower()


# ============================================================
# Close Case Tests
# ============================================================


class TestCloseCase:
    """Test close_case method."""

    @pytest.mark.asyncio
    async def test_close_case_success(self, case_service, mock_case_repo, sample_case):
        """Test successful case closure."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.save.side_effect = lambda case: case

        result = await case_service.close_case(
            sample_case.case_id,
            sample_case.organization_id,
        )

        assert result.state == CaseState.RESOLVED
        assert result.resolved_at is not None

    @pytest.mark.asyncio
    async def test_close_case_resolved_clears_closure_reason(
        self, case_service, mock_case_repo, sample_case
    ):
        """close_case → RESOLVED leaves closure_reason=None.

        closure_reason is the engine-derived CLOSED sub-category
        ('inquiry_only' / 'closed_insufficient_evidence' / ...)
        and is meaningless for RESOLVED.
        """
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.save.side_effect = lambda case: case

        result = await case_service.close_case(
            sample_case.case_id,
            sample_case.organization_id,
        )

        assert result.state == CaseState.RESOLVED
        assert result.closure_reason is None

    @pytest.mark.asyncio
    async def test_close_case_already_closed_raises_conflict_error(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test that closing already-closed case raises ConflictError."""
        closed_case = sample_case.model_copy(
            update={
                "state": CaseState.RESOLVED,
                "resolved_at": datetime.now(timezone.utc),
                "closure_reason": None,
            }
        )
        mock_case_repo.get.return_value = closed_case

        with pytest.raises(ConflictError) as exc_info:
            await case_service.close_case(
                sample_case.case_id,
                sample_case.organization_id,
            )

        assert "already closed" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_close_case_not_found_raises_not_found_error(
        self, case_service, mock_case_repo
    ):
        """Test that closing non-existent case raises NotFoundError."""
        mock_case_repo.get.return_value = None

        with pytest.raises(NotFoundError):
            await case_service.close_case("nonexistent", "org_1")


# ============================================================
# Get Statistics Tests
# ============================================================


class TestGetCaseStatistics:
    """Test get_case_statistics method."""

    @pytest.mark.asyncio
    async def test_get_statistics_returns_total_cases(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test that statistics includes total_cases."""
        mock_case_repo.list.return_value = ([sample_case], 1)

        result = await case_service.get_case_statistics("org_456")

        assert "total_cases" in result
        assert result["total_cases"] == 1

    @pytest.mark.asyncio
    async def test_get_statistics_returns_by_status(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test that statistics includes by_status breakdown."""
        mock_case_repo.list.return_value = ([sample_case], 1)

        result = await case_service.get_case_statistics("org_456")

        assert "by_status" in result
        assert "inquiry" in result["by_status"]

    @pytest.mark.asyncio
    async def test_get_statistics_returns_avg_resolution_time(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test that statistics includes avg_resolution_time."""
        resolved_case = sample_case.model_copy(
            update={
                "state": CaseState.RESOLVED,
                "resolved_at": sample_case.created_at + timedelta(hours=2),
            }
        )
        mock_case_repo.list.return_value = ([resolved_case], 1)

        result = await case_service.get_case_statistics("org_456")

        assert "avg_resolution_time_seconds" in result
        assert result["avg_resolution_time_seconds"] > 0

    @pytest.mark.asyncio
    async def test_get_statistics_empty_org_returns_zeros(
        self, case_service, mock_case_repo
    ):
        """Test that empty organization returns zero statistics."""
        mock_case_repo.list.return_value = ([], 0)

        result = await case_service.get_case_statistics("org_empty")

        assert result["total_cases"] == 0


# ============================================================
# Search Cases Tests
# ============================================================


class TestSearchCases:
    """Test search_cases method."""

    @pytest.mark.asyncio
    async def test_search_cases_returns_matches(
        self, case_service, mock_case_repo, sample_case
    ):
        """Test that search_cases returns matching cases."""
        mock_case_repo.search.return_value = ([sample_case], 1)

        result = await case_service.search_cases("org_456", "test query")

        assert len(result) == 1
        assert result[0].case_id == sample_case.case_id

    @pytest.mark.asyncio
    async def test_search_cases_empty_query_returns_empty(self, case_service):
        """Test that empty query returns empty list."""
        result = await case_service.search_cases("org_1", "")

        assert result == []

    @pytest.mark.asyncio
    async def test_search_cases_respects_limit(self, case_service, mock_case_repo):
        """Test that search_cases respects limit parameter."""
        mock_case_repo.search.return_value = ([], 0)

        await case_service.search_cases("org_1", "query", limit=5)

        call_kwargs = mock_case_repo.search.call_args[1]
        assert call_kwargs["limit"] == 5
