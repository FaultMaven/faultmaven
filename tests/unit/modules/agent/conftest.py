"""Test fixtures for Agent module unit tests."""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from faultmaven.exceptions import (
    NotFoundError,
    PermissionDeniedException,
    ServiceException,
)
from faultmaven.modules.case.domain.models import Case, CaseStatus

if TYPE_CHECKING:
    from faultmaven.core.investigation.milestone_engine import MilestoneEngine
    from faultmaven.infrastructure.persistence.case_repository import CaseRepository
    from faultmaven.models.api_models import CaseQueryRequest, CaseQueryResponse


def create_sample_case(
    case_id: Optional[str] = None,
    user_id: Optional[str] = None,
    status: CaseStatus = CaseStatus.CONSULTING,
    current_turn: int = 0,
    message_count: int = 0,
) -> Case:
    """Create a sample Case for testing."""
    from faultmaven.modules.case.domain.models import InvestigationProgress

    case_id = case_id or f"case_{uuid4().hex[:12]}"
    user_id = user_id or str(uuid4())
    now = datetime.now(timezone.utc)

    # Create case with all required fields
    # Case model has default_factory for progress, consulting, etc., so they're auto-initialized
    # Required fields: case_id, user_id, organization_id, title, created_at, updated_at, last_activity_at
    case = Case(
        case_id=case_id,
        user_id=user_id,
        organization_id="org_test123",
        title="Test Case",
        description="Test case description",
        status=status,
        current_turn=current_turn,
        message_count=message_count,
        created_at=now,
        updated_at=now,
        last_activity_at=now,  # Required field
        messages=[],  # Empty list to start
    )
    # Progress, consulting, evidence, hypotheses, solutions, etc. are created by default_factory
    # No need to manually set them

    return case


class MockCaseRepository:
    """Mock CaseRepository for testing InvestigationService."""

    def __init__(self):
        self._storage: dict[str, Case] = {}
        self.get = AsyncMock(side_effect=self._get)
        self.save = AsyncMock(side_effect=self._save)
        self.list = AsyncMock(side_effect=self._list)
        self.delete = AsyncMock(side_effect=self._delete)

    async def _get(self, case_id: str) -> Optional[Case]:
        """Get case by ID - return the stored case (service will mutate it)."""
        # Return reference - service mutates in place, which is fine for testing
        # The actual repository would handle this differently, but for unit tests
        # we want to track mutations
        return self._storage.get(case_id)

    async def _save(self, case: Case) -> Case:
        """Save case - store it (service passes the updated case)."""
        # Store the case object directly - service passes the updated case after mutations
        self._storage[case.case_id] = case
        return case

    async def _list(
        self,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        status: Optional[CaseStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Case], int]:
        """List cases with filters."""
        results = list(self._storage.values())

        if user_id:
            results = [c for c in results if c.user_id == user_id]
        if organization_id:
            results = [c for c in results if c.organization_id == organization_id]
        if status:
            results = [c for c in results if c.status == status]

        total = len(results)
        paginated = results[offset : offset + limit]
        return paginated, total

    async def _delete(self, case_id: str) -> bool:
        """Delete case."""
        if case_id in self._storage:
            del self._storage[case_id]
            return True
        return False


class MockMilestoneEngine:
    """Mock MilestoneEngine for testing InvestigationService.

    Note: Real MilestoneEngine saves case via repository internally.
    Mock doesn't need to save since InvestigationService also saves after adding agent message.
    """

    def __init__(self):
        self.process_turn = AsyncMock(side_effect=self._process_turn)

    async def _process_turn(
        self,
        case: Case,
        user_message: str,
        attachments: Optional[list] = None,
    ) -> dict[str, Any]:
        """Mock turn processing.

        Note: The service adds user message to case BEFORE calling this,
        so case.messages already contains the user message.

        Real engine:
        - Increments current_turn
        - Updates case state
        - Saves case via repository (but we don't need to mock that since service saves again)
        - Returns updated case

        Mock behavior:
        - Increments current_turn to match expected behavior
        - Returns case with updated state
        """
        # Update case state - increment turn (user message was already added at turn current_turn + 1)
        # Note: The service expects the engine to increment current_turn
        case.current_turn += 1
        case.updated_at = datetime.now(timezone.utc)

        # Return the same case object - real engine returns the mutated case
        return {
            "case_updated": case,
            "agent_response": f"Agent response to: {user_message}",
            "metadata": {
                "milestones_completed": [],
                "progress_made": True,
            },
        }


@pytest.fixture
def mock_case_repository() -> MockCaseRepository:
    """Create a mock CaseRepository."""
    return MockCaseRepository()


@pytest.fixture
def mock_milestone_engine() -> MockMilestoneEngine:
    """Create a mock MilestoneEngine."""
    return MockMilestoneEngine()


@pytest.fixture
def sample_case() -> Case:
    """Create a sample case."""
    return create_sample_case()


@pytest.fixture
def sample_user_id() -> str:
    """Create a sample user ID."""
    return str(uuid4())


@pytest.fixture
def sample_case_query_request():
    """Create a sample CaseQueryRequest."""
    from faultmaven.models.api_models import CaseQueryRequest

    return CaseQueryRequest(
        message="Test user message",
        attachments=None,
    )
