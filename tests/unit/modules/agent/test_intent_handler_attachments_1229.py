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
import inspect
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
    _INTENT_DISPATCH,
    InvestigationService,
    _IntentDispatchKind,
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
# The stall counter every case below starts on.
STANDING_STALL = 3


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
    # Non-zero on purpose: it is what makes "the counter was not written" a
    # real assertion rather than one that holds at its default.
    sample_case.turns_without_progress = STANDING_STALL
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


def _payload(intent: QueryIntent, query: str = "here it is") -> TurnPayload:
    return TurnPayload(
        query=query,
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


class TestTheGreetingHeuristicDoesNotSwallowData:
    """``_detect_intent_heuristic`` rewrites CONVERSATION->GREETING from the
    message text alone, and ``_handle_greeting`` answers from a static string
    without ever calling the engine. So "hi" plus a genuinely new log was
    persisted and dedup-classified, and the engine was told nothing arrived —
    no upload keys, no progress arm, and the two #1224 degradation warnings
    unreachable. A turn that delivers data is not a greeting."""

    async def test_a_greeting_carrying_a_file_reaches_the_engine(
        self, service, repo, case, seen
    ):
        await repo.save(case)

        await service.process_turn(
            case_id=case.case_id,
            user_id=case.user_id,
            # The exact text the heuristic matches (``^(hi|hello|hey|greetings
            # |help)( faultmaven)?[.!]*$``) — a payload whose query does not
            # match would never reach the heuristic and the test would pass
            # for the wrong reason.
            payload=_payload(QueryIntent(type=IntentType.CONVERSATION), query="hi"),
        )

        assert seen.get("attachments") is not None, (
            "the greeting heuristic swallowed a turn that delivered a file "
            "(#1229) — the engine was never called"
        )
        assert seen["attachments"][0]["is_novel"] is True

    async def test_a_bare_greeting_still_short_circuits(
        self, service, repo, case, seen
    ):
        """The control. Without this, the guard above could be passing because
        the heuristic broke entirely rather than because it was scoped."""
        await repo.save(case)

        response = await service.process_turn(
            case_id=case.case_id,
            user_id=case.user_id,
            payload=TurnPayload(
                query="hi", intent=QueryIntent(type=IntentType.CONVERSATION)
            ),
        )

        assert seen == {}, "a bare greeting must not reach the engine"
        assert "FaultMaven" in response.agent_response


class TestTheNonEngineHandlersStillReportUploads:
    """GREETING and FILE_RECLASSIFICATION are SERVICE-routed and never reach
    the engine, so nothing else can report their uploads for them.

    They report the keys and stop there: no progress flag, no
    ``turns_without_progress`` write. That is self-consistent — the flag says
    False and the counter is unchanged, and those agree —
    where a True flag beside an untouched counter would be the disagreement
    #1229 exists to remove. ``_check_if_progress_made`` is the sole writer of
    that counter and it lives in the engine.
    """

    async def test_greeting_reports_the_upload(self, service, case):
        """Reached only by an explicit client-sent GREETING now that the
        heuristic is scoped — but "normally unreachable" is not "provably
        unreachable", which is the whole lesson of #1229."""
        result = await service._handle_greeting(
            case=case,
            attachments=[
                {"file_id": "file_ffffffffffff", "is_novel": True},
            ],
        )

        assert result["metadata"]["files_uploaded"] == ["file_ffffffffffff"]
        assert result["metadata"]["novel_files_uploaded"] == ["file_ffffffffffff"]
        assert result["metadata"]["progress_made"] is False
        assert case.turns_without_progress == STANDING_STALL, (
            "the counter is engine-owned; a service handler that never calls "
            "the engine must not write it"
        )

    async def test_greeting_omits_the_keys_with_no_upload(self, service, case):
        result = await service._handle_greeting(case=case)

        assert "files_uploaded" not in result["metadata"]

    async def test_the_reclassification_handler_is_told_about_the_attachment(
        self, service, repo, case
    ):
        """The wiring, pinned at the seam: the dispatch built
        ``attachment_metadata`` and then discarded it for this handler.

        ``create_autospec`` of the REAL bound method, so a call naming a
        parameter the handler does not have fails here rather than being
        silently accepted by a permissive Mock.
        """
        await repo.save(case)
        spy = create_autospec(
            service._handle_file_reclassification,
            return_value={
                "agent_response": "ok",
                "case_updated": case,
                "metadata": {"progress_made": False, "milestones_completed": []},
            },
        )
        service._handle_file_reclassification = spy

        await service.process_turn(
            case_id=case.case_id,
            user_id=case.user_id,
            payload=_payload(
                QueryIntent(
                    type=IntentType.FILE_RECLASSIFICATION,
                    file_id="file_dddddddddddd",
                    data_type="logs_and_errors",
                )
            ),
        )

        passed = spy.await_args.kwargs["attachments"]
        assert passed is not None, (
            "file_reclassification was handed attachments=None while a row "
            "for the upload was already committed (#1229)"
        )
        assert passed[0]["is_novel"] is True


class TestEverySeriveRoutedHandlerCanReceiveUploads:
    """The structural guard — the class, not the five instances.

    #1229's two halves were both "a handler that could not be told". The three
    the issue named took ``attachments=None``; ``GREETING`` and
    ``FILE_RECLASSIFICATION`` had no such parameter at all. The dispatch is an
    if/elif chain, so a SERVICE intent added tomorrow drops the signal again by
    simply not mentioning it, and nothing fails.

    Derived from ``_INTENT_DISPATCH`` rather than from a hand-kept list, in the
    same spirit as ``_validate_intent_dispatch_completeness``: a new SERVICE
    entry is checked automatically or the test says why it could not be.
    """

    @staticmethod
    def _service_intents():
        return sorted(
            i
            for i, kind in _INTENT_DISPATCH.items()
            if kind is _IntentDispatchKind.SERVICE
        )

    def test_the_dispatch_table_still_has_service_entries(self):
        """Guards the guard: if SERVICE routing disappears or is renamed, the
        parametrised check below would pass over an empty set."""
        assert len(self._service_intents()) >= 5

    def test_each_handler_accepts_attachments(self, service):
        missing = []
        for intent in self._service_intents():
            handler = getattr(service, f"_handle_{intent.value}", None)
            assert handler is not None, (
                f"{intent.value} is SERVICE-routed but has no "
                f"_handle_{intent.value}; if the handler was renamed, teach "
                "this test the new name — do not delete the check"
            )
            if "attachments" not in inspect.signature(handler).parameters:
                missing.append(intent.value)

        assert not missing, (
            f"SERVICE-routed handlers that cannot be told about the turn's "
            f"uploads: {missing}. Every one of them delegates to, or stands in "
            "for, a turn that already committed an UploadedFile row (#1229)."
        )
