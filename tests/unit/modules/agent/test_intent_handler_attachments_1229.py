"""#1229, service half — a SERVICE-routed intent must not tell the engine
that nothing was attached.

``_handle_status_transition``, ``_handle_confirmation`` and
``_handle_hypothesis_action`` delegate to the very same ``engine.process_turn``
the CONVERSATION route uses, but each passed a literal ``attachments=None``.
By then ``_preprocess_attachment`` has already run and committed an
``UploadedFile`` row for every attachment on the turn — so an upload riding a
Copilot suggestion-chip intent was persisted, dedup-classified, and then the
engine was told nothing arrived. Independent of any gate-semantics question,
that is the engine being lied to.

The engine double is ``create_autospec``'d and its ``side_effect`` is a real
function with the engine's own keyword signature: a bare ``Mock`` advertises
``(*args, **kwargs)`` and would accept a call shape the real engine rejects,
which would make every assertion below unfailable.

Engine-side half (what the engine then does with the signal on a deterministic
branch): ``tests/unit/core/investigation/test_gate_turn_upload_novelty_1229.py``.
"""

import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.models.api import DataType
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.domain.models import (
    CaseState,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
)

pytestmark = pytest.mark.unit

CONTENT = b"2026-08-28T10:00:00Z ERROR pod restart loop\n"
CONTENT_HASH = "d" * 64
HYPOTHESIS_ID = "hyp_111111111111"


class _PreprocessingDouble:
    """Returns a real result object — the row is built by assigning these onto
    a Pydantic ``UploadedFile``, so a Mock would not survive validation."""

    async def classify_and_extract(self, content, filename, source_metadata=None):
        return SimpleNamespace(
            summary="Pod restart loop.",
            structural_index="ERROR x 42 between 10:00 and 10:05",
            detailed_data_type=DataType.LOGS_AND_ERRORS,
            content_hash=CONTENT_HASH,
            coverage_start_ts=None,
            coverage_end_ts=None,
            extraction_method="structure_extraction",
            extraction_metadata={},
        )


@pytest.fixture
def repo(mock_case_repository):
    """The dedup lookup must actually RUN, or every upload reaches the engine
    with ``is_novel=None`` (undetermined) and the novelty half of these
    assertions would be vacuous."""
    mock_case_repository.find_uploaded_file_by_content_hash = AsyncMock(
        return_value=None
    )
    return mock_case_repository


@pytest.fixture
def seen() -> dict:
    return {}


@pytest.fixture
def engine(seen):
    """``create_autospec`` so the call is validated against the REAL
    ``MilestoneEngine.process_turn`` signature."""
    double = create_autospec(MilestoneEngine, instance=True)
    # Set in ``MilestoneEngine.__init__``, so class autospec does not carry it;
    # the service reads it to build its IntentResolver.
    double.llm_provider = MagicMock()

    async def spy(
        *,
        case,
        user_message: str,
        attachments: Optional[list] = None,
        intent_type: Optional[str] = None,
        intent_data: Optional[dict[str, Any]] = None,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        seen["attachments"] = copy.deepcopy(attachments)
        seen["intent_type"] = intent_type
        case.updated_at = datetime.now(timezone.utc)
        return {
            "case_updated": case,
            "agent_response": "ack",
            "metadata": {"milestones_completed": [], "progress_made": False},
        }

    double.process_turn = AsyncMock(side_effect=spy)
    return double


@pytest.fixture
def service(engine, repo):
    svc = InvestigationService(milestone_engine=engine, case_repository=repo)
    svc.preprocessing_service = _PreprocessingDouble()
    return svc


@pytest.fixture
def case(sample_case, sample_user_id):
    sample_case.user_id = sample_user_id
    sample_case.inquiry.proposed_problem_statement = "etcd connectivity"
    sample_case.inquiry.problem_statement_confirmed = True
    sample_case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
    sample_case.inquiry.decided_to_investigate = True
    sample_case.inquiry.decision_made_at = datetime.now(timezone.utc)
    sample_case.state = CaseState.INVESTIGATING
    sample_case.hypotheses = {
        HYPOTHESIS_ID: Hypothesis(
            hypothesis_id=HYPOTHESIS_ID,
            statement="etcd peer certificate expired",
            category=HypothesisCategory.NETWORK,
            generation_mode=HypothesisGenerationMode.SYSTEMATIC,
            generated_at_turn=1,
            rationale="initial",
        )
    }
    return sample_case


def _payload(intent: QueryIntent) -> TurnPayload:
    return TurnPayload(
        query="here it is",
        attachments=[
            Attachment(content=CONTENT, filename="app.log", content_type="text/plain")
        ],
        intent=intent,
    )


async def _run(service, repo, case, intent: QueryIntent):
    await repo.save(case)
    return await service.process_turn(
        case_id=case.case_id, user_id=case.user_id, payload=_payload(intent)
    )


INTENTS = {
    "status_transition": QueryIntent(
        type=IntentType.STATUS_TRANSITION,
        from_state=CaseState.INVESTIGATING,
        to_state=CaseState.CLOSED,
    ),
    "confirmation": QueryIntent(type=IntentType.CONFIRMATION, confirmation_value=False),
    "hypothesis_action": QueryIntent(
        type=IntentType.HYPOTHESIS_ACTION,
        hypothesis_id=HYPOTHESIS_ID,
        action="validate",
    ),
}


@pytest.mark.parametrize("name", sorted(INTENTS))
class TestEachServiceRoutedHandler:
    async def test_the_engine_is_told_about_the_attachment(
        self, name, service, repo, case, seen
    ):
        await _run(service, repo, case, INTENTS[name])

        assert seen["intent_type"] == name
        assert seen["attachments"] is not None, (
            f"{name} handed the engine attachments=None while a row for the "
            "upload was already committed (#1229)"
        )
        assert len(seen["attachments"]) == 1
        assert seen["attachments"][0]["filename"] == "app.log"

    async def test_the_novelty_signal_rides_along(
        self, name, service, repo, case, seen
    ):
        """Not merely "a list arrived": the tri-state the engine's progress arm
        reads has to be on it, or threading the list bought nothing."""
        await _run(service, repo, case, INTENTS[name])

        assert seen["attachments"][0]["is_novel"] is True

    async def test_a_turn_with_no_attachment_still_says_none(
        self, name, service, repo, case, seen
    ):
        """The empty list must not reach the engine as ``[]`` — every caller
        normalises to ``None``, and the engine's ``if attachments:`` reads them
        the same way only by accident."""
        await repo.save(case)

        await service.process_turn(
            case_id=case.case_id,
            user_id=case.user_id,
            payload=TurnPayload(query="here it is", intent=INTENTS[name]),
        )

        assert seen["attachments"] is None
