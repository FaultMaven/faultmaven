"""Unit tests for InvestigationService.

Tests the InvestigationService which manages milestone-based troubleshooting workflow.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from faultmaven.modules.agent.domain.services.investigation_service import InvestigationService
from faultmaven.modules.case.domain.models import Case, CaseStatus
from faultmaven.models.api_models import CaseQueryRequest, CaseQueryResponse
from faultmaven.exceptions import NotFoundError, PermissionDeniedException, ServiceException

from .conftest import (
    MockCaseRepository,
    MockMilestoneEngine,
    create_sample_case,
    sample_case,
    sample_user_id,
    sample_case_query_request,
    mock_case_repository,
    mock_milestone_engine,
)


class TestInvestigationServiceProcessTurn:
    """Tests for InvestigationService.process_turn()."""

    @pytest.fixture
    def service(self, mock_milestone_engine, mock_case_repository):
        """Create InvestigationService with mocked dependencies."""
        return InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )

    @pytest.mark.asyncio
    async def test_process_turn_success(
        self, service, mock_case_repository, mock_milestone_engine, sample_case, sample_user_id, sample_case_query_request
    ):
        """Test successful turn processing."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        response = await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            request=sample_case_query_request,
        )

        assert isinstance(response, CaseQueryResponse)
        assert response.agent_response == f"Agent response to: {sample_case_query_request.message}"
        # Mock engine increments turn from 0 to 1, so response should have turn_number = 1
        assert response.turn_number == 1, f"Expected turn_number=1, got {response.turn_number} (initial was {sample_case.current_turn})"
        assert mock_milestone_engine.process_turn.called
        assert mock_case_repository.save.called

    @pytest.mark.asyncio
    async def test_process_turn_case_not_found(
        self, service, mock_case_repository, sample_user_id, sample_case_query_request
    ):
        """Test turn processing with non-existent case."""
        non_existent_case_id = f"case_{uuid4().hex[:12]}"

        with pytest.raises(NotFoundError, match="Case"):
            await service.process_turn(
                case_id=non_existent_case_id,
                user_id=sample_user_id,
                request=sample_case_query_request,
            )

    @pytest.mark.asyncio
    async def test_process_turn_permission_denied(
        self, service, mock_case_repository, sample_case, sample_case_query_request
    ):
        """Test turn processing with unauthorized user."""
        # Pre-populate repository with case owned by different user
        await mock_case_repository.save(sample_case)
        unauthorized_user_id = str(uuid4())

        with pytest.raises(PermissionDeniedException, match="not authorized"):
            await service.process_turn(
                case_id=sample_case.case_id,
                user_id=unauthorized_user_id,
                request=sample_case_query_request,
            )

    @pytest.mark.asyncio
    async def test_process_turn_saves_user_message(
        self, service, mock_case_repository, mock_milestone_engine, sample_case, sample_user_id, sample_case_query_request
    ):
        """Test that user message is saved before processing."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)
        initial_message_count = sample_case.message_count

        await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            request=sample_case_query_request,
        )

        # Verify case was saved with user message and agent response
        saved_case = await mock_case_repository.get(sample_case.case_id)
        assert saved_case.message_count == initial_message_count + 2  # User message + agent response
        assert len(saved_case.messages) >= 2, f"Expected at least 2 messages, got {len(saved_case.messages)}"
        # Messages are added in order: user message first (index 0), then agent response (index 1)
        # So messages[-2] should be the user message if there are exactly 2, or second-to-last otherwise
        user_messages = [m for m in saved_case.messages if m.get("role") == "user"]
        assert len(user_messages) >= 1, "User message should be saved"
        # Check that our test message is in the user messages
        assert any(m["content"] == sample_case_query_request.message for m in user_messages), \
            f"User message '{sample_case_query_request.message}' not found in saved messages"

    @pytest.mark.asyncio
    async def test_process_turn_saves_agent_response(
        self, service, mock_case_repository, mock_milestone_engine, sample_case, sample_user_id, sample_case_query_request
    ):
        """Test that agent response is saved after processing."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        response = await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            request=sample_case_query_request,
        )

        # Verify case was saved with agent response
        saved_case = await mock_case_repository.get(sample_case.case_id)
        agent_messages = [m for m in saved_case.messages if m.get("role") == "agent"]
        assert len(agent_messages) >= 1
        assert agent_messages[-1]["content"] == response.agent_response

    @pytest.mark.asyncio
    async def test_process_turn_increments_turn_number(
        self, service, mock_case_repository, mock_milestone_engine, sample_case, sample_user_id, sample_case_query_request
    ):
        """Test that turn number is incremented."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        initial_turn = sample_case.current_turn  # Should be 0 from fixture
        await mock_case_repository.save(sample_case)

        response = await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            request=sample_case_query_request,
        )

        # Service flow:
        # 1. Gets case (current_turn = initial_turn, e.g., 0)
        # 2. Adds user message with turn_number = current_turn + 1 (e.g., 1)
        # 3. Engine processes and increments current_turn += 1 (e.g., 0 -> 1)
        # 4. Service adds agent message with turn_number = updated_case.current_turn (e.g., 1)
        # 5. Response has turn_number = updated_case.current_turn (e.g., 1)
        expected_turn = initial_turn + 1  # Engine increments by 1
        assert response.turn_number == expected_turn, \
            f"Expected turn_number={expected_turn}, got {response.turn_number} (initial was {initial_turn})"
        
        saved_case = await mock_case_repository.get(sample_case.case_id)
        # The saved case should have current_turn incremented by the engine
        assert saved_case.current_turn == expected_turn, \
            f"Expected saved_case.current_turn={expected_turn}, got {saved_case.current_turn}"

    @pytest.mark.asyncio
    async def test_process_turn_with_attachments(
        self, service, mock_case_repository, mock_milestone_engine, sample_case, sample_user_id
    ):
        """Test turn processing with file attachments."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        attachments = [
            {"file_id": "file1", "filename": "file1.txt"},
            {"file_id": "file2", "filename": "file2.log"},
        ]
        request_with_attachments = CaseQueryRequest(
            message="Test message with attachments",
            attachments=attachments,
        )

        response = await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            request=request_with_attachments,
        )

        assert response.agent_response is not None
        # Verify attachments were passed to engine
        call_args = mock_milestone_engine.process_turn.call_args
        assert call_args[1]["attachments"] == attachments


class TestInvestigationServiceGetProgress:
    """Tests for InvestigationService.get_progress()."""

    @pytest.fixture
    def service(self, mock_milestone_engine, mock_case_repository):
        """Create InvestigationService with mocked dependencies."""
        return InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )

    @pytest.mark.asyncio
    async def test_get_progress_success(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Test successful progress retrieval."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        progress = await service.get_progress(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
        )

        assert progress["case_id"] == sample_case.case_id
        assert progress["status"] == sample_case.status.value
        assert "current_turn" in progress
        assert "milestones_completed" in progress  # InvestigationProgress.completed_milestones property
        assert "pending_milestones" in progress  # InvestigationProgress.pending_milestones property
        assert "completion_percentage" in progress  # InvestigationProgress.completion_percentage property

    @pytest.mark.asyncio
    async def test_get_progress_case_not_found(
        self, service, mock_case_repository, sample_user_id
    ):
        """Test progress retrieval with non-existent case."""
        non_existent_case_id = f"case_{uuid4().hex[:12]}"

        with pytest.raises(NotFoundError, match="Case"):
            await service.get_progress(
                case_id=non_existent_case_id,
                user_id=sample_user_id,
            )

    @pytest.mark.asyncio
    async def test_get_progress_permission_denied(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Test progress retrieval with unauthorized user."""
        # Pre-populate repository - set sample_case to have sample_user_id as owner
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)
        unauthorized_user_id = str(uuid4())  # Different user (not the owner)

        with pytest.raises(PermissionDeniedException, match="not authorized"):
            await service.get_progress(
                case_id=sample_case.case_id,
                user_id=unauthorized_user_id,
            )


class TestInvestigationServiceTransitionToInvestigating:
    """Tests for InvestigationService.transition_to_investigating()."""

    @pytest.fixture
    def service(self, mock_milestone_engine, mock_case_repository):
        """Create InvestigationService with mocked dependencies."""
        return InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )

    @pytest.fixture
    def consulting_case(self):
        """Create a case in CONSULTING status."""
        return create_sample_case(
            status=CaseStatus.CONSULTING,
        )

    @pytest.mark.asyncio
    async def test_transition_to_investigating_success(
        self, service, mock_case_repository, consulting_case, sample_user_id
    ):
        """Test successful transition to INVESTIGATING."""
        # Pre-populate repository
        consulting_case.user_id = sample_user_id
        await mock_case_repository.save(consulting_case)

        confirmed_description = "Confirmed problem description"
        updated_case = await service.transition_to_investigating(
            case_id=consulting_case.case_id,
            user_id=sample_user_id,
            confirmed_description=confirmed_description,
        )

        assert updated_case.status == CaseStatus.INVESTIGATING
        assert updated_case.description == confirmed_description
        assert mock_case_repository.save.called

    @pytest.mark.asyncio
    async def test_transition_to_investigating_case_not_found(
        self, service, mock_case_repository, sample_user_id
    ):
        """Test transition with non-existent case."""
        non_existent_case_id = f"case_{uuid4().hex[:12]}"

        with pytest.raises(NotFoundError, match="Case"):
            await service.transition_to_investigating(
                case_id=non_existent_case_id,
                user_id=sample_user_id,
                confirmed_description="Test description",
            )

    @pytest.mark.asyncio
    async def test_transition_to_investigating_permission_denied(
        self, service, mock_case_repository, consulting_case
    ):
        """Test transition with unauthorized user."""
        # Pre-populate repository
        await mock_case_repository.save(consulting_case)
        unauthorized_user_id = str(uuid4())

        with pytest.raises(PermissionDeniedException, match="not authorized"):
            await service.transition_to_investigating(
                case_id=consulting_case.case_id,
                user_id=unauthorized_user_id,
                confirmed_description="Test description",
            )

    @pytest.mark.asyncio
    async def test_transition_to_investigating_invalid_status(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Test transition from non-CONSULTING status."""
        # Pre-populate repository with case in INVESTIGATING status (cannot transition from non-CONSULTING)
        sample_case.user_id = sample_user_id
        sample_case.status = CaseStatus.INVESTIGATING
        await mock_case_repository.save(sample_case)
        
        # Verify the case is stored with INVESTIGATING status
        stored_case = await mock_case_repository.get(sample_case.case_id)
        assert stored_case is not None, "Case should be stored in repository"
        assert stored_case.status == CaseStatus.INVESTIGATING, \
            f"Case status should be INVESTIGATING, got {stored_case.status}"

        with pytest.raises(ServiceException, match="Cannot transition"):
            await service.transition_to_investigating(
                case_id=sample_case.case_id,
                user_id=sample_user_id,
                confirmed_description="Test description",
            )


class TestInvestigationServiceCloseCase:
    """Tests for InvestigationService.close_case()."""

    @pytest.fixture
    def service(self, mock_milestone_engine, mock_case_repository):
        """Create InvestigationService with mocked dependencies."""
        return InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )

    @pytest.mark.asyncio
    async def test_close_case_success(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Test successful case closure."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        closure_reason = "resolved"
        updated_case = await service.close_case(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            closure_reason=closure_reason,
        )

        assert updated_case.status == CaseStatus.CLOSED
        assert updated_case.closure_reason == closure_reason
        assert updated_case.closed_at is not None
        assert mock_case_repository.save.called

    @pytest.mark.asyncio
    async def test_close_case_case_not_found(
        self, service, mock_case_repository, sample_user_id
    ):
        """Test case closure with non-existent case."""
        non_existent_case_id = f"case_{uuid4().hex[:12]}"

        with pytest.raises(NotFoundError, match="Case"):
            await service.close_case(
                case_id=non_existent_case_id,
                user_id=sample_user_id,
                closure_reason="resolved",
            )

    @pytest.mark.asyncio
    async def test_close_case_permission_denied(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Test case closure with unauthorized user."""
        # Pre-populate repository - set sample_case to have sample_user_id as owner
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)
        unauthorized_user_id = str(uuid4())  # Different user

        with pytest.raises(PermissionDeniedException, match="not authorized"):
            await service.close_case(
                case_id=sample_case.case_id,
                user_id=unauthorized_user_id,
                closure_reason="resolved",
            )

    @pytest.mark.asyncio
    async def test_close_case_sets_closed_at(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Test that closed_at timestamp is set."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        updated_case = await service.close_case(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            closure_reason="resolved",
        )

        assert updated_case.closed_at is not None
        assert isinstance(updated_case.closed_at, datetime)
