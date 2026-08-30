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
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    TurnOutcome,
    TurnProgress,
)

if TYPE_CHECKING:
    from faultmaven.core.investigation.milestone_engine import MilestoneEngine
    from faultmaven.modules.case.infrastructure.case_repository import CaseRepository


def create_sample_case(
    case_id: Optional[str] = None,
    user_id: Optional[str] = None,
    state: CaseState = CaseState.INQUIRY,
    current_turn: int = 0,
    message_count: int = 0,
) -> Case:
    """Create a sample Case for testing."""
    from faultmaven.modules.case.domain.models import (
        InvestigationProgress,
        ProblemVerification,
        TemporalState,
        UrgencyLevel,
    )

    case_id = case_id or f"case_{uuid4().hex[:12]}"
    user_id = user_id or str(uuid4())
    now = datetime.now(timezone.utc)

    # Create case with all required fields
    # Case model has default_factory for progress, inquiry, etc., so they're auto-initialized
    # Required fields: case_id, user_id, organization_id, title, created_at, updated_at, last_activity_at
    case = Case(
        case_id=case_id,
        user_id=user_id,
        organization_id="org_test123",
        title="Test Case",
        description="Test case description",
        state=state,
        problem_verification=ProblemVerification(
            symptom_statement="Server verification test",
            severity="HIGH",
            temporal_state=TemporalState.ONGOING,
            urgency_level=UrgencyLevel.HIGH,
        ),
        current_turn=current_turn,
        message_count=message_count,
        created_at=now,
        updated_at=now,
        last_activity_at=now,  # Required field
        messages=[],  # Empty list to start
    )
    # Progress, inquiry, evidence, hypotheses, solutions, etc. are created by default_factory
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
        status: Optional[CaseState] = None,
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
        self.llm_provider = MagicMock()
        self.process_turn = AsyncMock(side_effect=self._process_turn)

    async def _process_turn(
        self,
        case: Case,
        user_message: str,
        attachments: Optional[list] = None,
        intent_type: Optional[str] = None,
        intent_data: Optional[dict[str, Any]] = None,
        user_id: Optional[str] = None,
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
        # case.current_turn += 1
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


class RecordingCaseRepository(MockCaseRepository):
    """A repository double that keeps the turn counter the way the real ones do.

    Both ``SQLiteCaseRepository`` and ``PostgreSQLHybridCaseRepository`` write
    ``"current_turn": case.effective_current_turn`` — the LAST ``turn_history``
    number — rather than the in-flight ``current_turn``. ``MockCaseRepository``
    stores the ``Case`` object as handed to it, so that projection never
    happens, and every test built on it is structurally blind to the class of
    defect where a route consumes a turn number without recording one: the two
    counters simply cannot diverge.

    That blindness is not hypothetical. It is why #1264 survived — a mutation of
    the write-site clock passed through ``MockCaseRepository`` — and it is why a
    fix for it validated on the plain double would be validated against a
    repository that does not model the behaviour being fixed.

    Use this double for anything that asserts on turn numbering, turn history,
    or a value derived from a turn clock. Generalised here from the two doubles
    the #1245 lane (PR #1263) had to build locally, so the third lane to need
    them does not build a third copy.
    """

    async def _save(self, case: Case) -> Case:
        self._storage[case.case_id] = case.model_copy(
            update={"current_turn": case.effective_current_turn}
        )
        return case


class RecordingMilestoneEngine(MockMilestoneEngine):
    """A mock engine that records a turn, as the real one does at its Step 6.

    ``MockMilestoneEngine`` appends nothing, so ``turn_history`` stays empty
    forever — and ``effective_current_turn`` falls back to ``current_turn`` when
    it is empty, which makes :class:`RecordingCaseRepository`'s projection a
    no-op. Pairing the two is what lets the counters diverge at all; either one
    alone still hides the defect.
    """

    async def _process_turn(self, case: Case, *args: Any, **kwargs: Any):
        result = await super()._process_turn(case, *args, **kwargs)
        case.turn_history.append(
            TurnProgress(
                turn_number=case.current_turn,
                progress_made=True,
                outcome=TurnOutcome.CONVERSATION,
            )
        )
        return result


@pytest.fixture
def mock_milestone_engine() -> MockMilestoneEngine:
    """Create a mock MilestoneEngine."""
    return MockMilestoneEngine()


@pytest.fixture
def recording_case_repository() -> RecordingCaseRepository:
    """A repository double that applies the real ``effective_current_turn``
    projection. Required for any assertion about turn numbering (#1264)."""
    return RecordingCaseRepository()


@pytest.fixture
def recording_milestone_engine() -> RecordingMilestoneEngine:
    """An engine double that records a ``TurnProgress``, as the real one does."""
    return RecordingMilestoneEngine()


@pytest.fixture
def sample_case() -> Case:
    """Create a sample case."""
    return create_sample_case()


@pytest.fixture
def sample_user_id() -> str:
    """Create a sample user ID."""
    return str(uuid4())


@pytest.fixture
def sample_turn_payload():
    """Create a sample TurnPayload."""
    from faultmaven.core.investigation.schemas import TurnPayload
    from faultmaven.models.api_models import IntentType, QueryIntent

    return TurnPayload(
        query="Test user message",
        attachments=[],
        intent=QueryIntent(type=IntentType.CONVERSATION),
    )


# ============================================================
# Shared domain-object builders for the reclassification suites
# (test_investigation_service_reclassify.py and
# test_file_reclassification_intent.py) — one definition so the two
# suites exercise the shared handlers against identical fixtures.
# ============================================================

_SOURCE_TYPE_MAP = {
    "logs": "logs",
    "metrics": "metrics",
    "configuration": "configuration",
    "structured_config": "configuration",
    "code": "code",
    "text": "text",
}


def make_uploaded_file(
    file_id: str = "file_aaaaaaaaaaaa",
    filename: str = "server.log",
    storage_ref: Optional[str] = "evidence/case_x/server.log",
    data_type: Optional[str] = None,
    upload_source: str = "file_upload",
):
    """UploadedFile with the reclassify suites' shared defaults."""
    from faultmaven.modules.case.domain.models import UploadedFile

    return UploadedFile(
        file_id=file_id,
        filename=filename,
        size_bytes=100,
        storage_ref=storage_ref,
        uploaded_at_turn=0,
        data_type=data_type,
        upload_source=upload_source,
    )


def make_evidence(
    evidence_id: str = "ev_aaaaaaaaaaaa",
    data_type: str = "metrics",
    metadata: Optional[dict] = None,
    source_file_id: Optional[str] = "file_aaaaaaaaaaaa",
    source_type=None,
):
    """Evidence row with LLM-authored claim fields the handlers must not touch."""
    from faultmaven.modules.case.domain.models import (
        Evidence,
        EvidenceCategory,
        EvidenceSourceType,
    )

    resolved_source_type = (
        source_type
        if source_type is not None
        else EvidenceSourceType(
            _SOURCE_TYPE_MAP.get(data_type, EvidenceSourceType.METRICS.value)
        )
    )
    return Evidence(
        evidence_id=evidence_id,
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        primary_purpose="Test",
        summary="Old summary",
        extract="old index",
        source_type=resolved_source_type,
        source_file_id=source_file_id,
        collected_by="user",
        collected_at=datetime.now(timezone.utc),
        collected_at_turn=0,
        metadata=metadata,
    )


def make_preprocessing_result(new_data_type=None, metadata: Optional[dict] = None):
    """PreprocessingResult as returned by reclassify under user_override."""
    from faultmaven.core.preprocessing.models import (
        PreprocessingResult,
        UnifiedDataType,
    )
    from faultmaven.models.api import DataType

    if new_data_type is None:
        new_data_type = DataType.LOGS_AND_ERRORS
    unified_map = {
        DataType.LOGS_AND_ERRORS: UnifiedDataType.LOGS,
        DataType.METRICS_AND_PERFORMANCE: UnifiedDataType.METRICS,
        DataType.STRUCTURED_CONFIG: UnifiedDataType.CONFIGURATION,
    }
    return PreprocessingResult(
        data_type=unified_map.get(new_data_type, UnifiedDataType.LOGS),
        detailed_data_type=new_data_type,
        summary="new summary",
        structural_index="new index content",
        content_ref=None,
        content_size_bytes=100,
        content_type="text/plain",
        extraction_method="crime_scene",
        compression_ratio=0.1,
        extraction_metadata={"evidence_metadata": metadata or {}},
        content_hash="a" * 64,
        processing_time_ms=5,
    )
