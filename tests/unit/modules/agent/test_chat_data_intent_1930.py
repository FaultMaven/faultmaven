"""The user's answer to an offer of what they told FaultMaven in a chat message (#1930).

These pin the server's half through ``InvestigationService.process_turn``:

- the choices for output in the message: "save it" and "re-submit", first in
  ``suggested_actions``, the re-submit choice carrying ``prefill`` and nothing
  to send, none stored for typed matching, and no "no" to click — ignoring the
  offer leaves the message a claim;
- the one choice for a reported state with no data ("it's stable"): "Submit the
  output", naming what to submit;
- a save: the offered blocks become ONE pasted file, exactly as the user typed
  them, and the turn runs on it as a paste turn through the engine;
- a click naming no offer is refused before the turn is consumed; a click that
  saves nothing (already saved, or nothing to save) is answered without the
  engine.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.chat_data import (
    TURN_METADATA_CHAT_DATA_OFFER,
    mint_chat_data_offer,
)
from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.core.preprocessing.models import UnifiedDataType
from faultmaven.exceptions import ValidationException
from faultmaven.models.api import DataType
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service.chat_data import (
    RESUBMIT_LABEL,
    SAVE_LABEL,
    SUBMIT_LABEL,
)
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    _INTENT_DISPATCH,
    InvestigationService,
    _IntentDispatchKind,
)
from faultmaven.modules.case.contracts import ChatDataOffer, ChatDataOfferState

from .conftest import MockCaseRepository, MockMilestoneEngine, create_sample_case

pytestmark = pytest.mark.unit

_OUTPUT = "NAME          READY   STATUS\napi-0         0/1     CrashLoopBackOff"
_MESSAGE = f"The pods won't start:\n\n{_OUTPUT}\n\nWhat's wrong?"


class _OfferingEngine(MockMilestoneEngine):
    """Mints an offer as the real engine does when the model names the pod
    listing (and, when given, a state report)."""

    def __init__(self, state_request=None):
        super().__init__()
        self.state_request = state_request

    async def _process_turn(self, case, user_message, *args, **kwargs):
        result = await super()._process_turn(case, user_message, *args, **kwargs)
        lines = _OUTPUT.splitlines()
        offer = mint_chat_data_offer(
            case,
            user_message=user_message,
            named_blocks=(
                [SimpleNamespace(first_line=lines[0], last_line=lines[-1])]
                if _OUTPUT in user_message
                else None
            ),
            state_report=(
                SimpleNamespace(request=self.state_request)
                if self.state_request
                else None
            ),
        )
        result["metadata"][TURN_METADATA_CHAT_DATA_OFFER] = (
            offer.offer_id if offer else None
        )
        return result


def _preprocessed(content_hash: str):
    result = MagicMock()
    result.summary = "pod listing"
    result.structural_index = "index"
    result.data_type = UnifiedDataType.LOGS
    result.detailed_data_type = DataType.LOGS_AND_ERRORS
    result.content_hash = content_hash
    result.extraction_method = "rule_based"
    result.extraction_metadata = {}
    result.coverage_start_ts = None
    result.coverage_end_ts = None
    return result


@pytest.fixture
def repo_and_case():
    repo = MockCaseRepository()
    case = create_sample_case(user_id="user_owner")
    repo._storage[case.case_id] = case
    return repo, case


@pytest.fixture
def file_storage():
    svc = MagicMock()
    svc.store_file = AsyncMock(return_value={"file_path": "evidence/x/blob.txt"})
    svc.mark_linked = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def preprocessing():
    svc = MagicMock()
    svc.classify_and_extract = AsyncMock(return_value=_preprocessed("c" * 64))
    return svc


def _service(engine, repo, preprocessing, file_storage) -> InvestigationService:
    return InvestigationService(
        milestone_engine=engine,
        case_repository=repo,
        preprocessing_service=preprocessing,
        file_storage_service=file_storage,
    )


def _click(offer_id: str) -> TurnPayload:
    return TurnPayload(
        query="Save that output as data.",
        intent=QueryIntent(type=IntentType.SAVE_CHAT_DATA, offer_id=offer_id),
    )


def _with_offer(
    case, state=ChatDataOfferState.OFFERED, blocks=(_OUTPUT,), request=None
):
    offer = ChatDataOffer(
        turn=case.current_turn, blocks=list(blocks), state=state, request=request
    )
    case.chat_data_offers.append(offer)
    return offer


class TestTheChoices:
    async def test_output_in_the_message_gets_save_and_re_submit_only(
        self, repo_and_case, preprocessing, file_storage
    ):
        repo, case = repo_and_case
        service = _service(_OfferingEngine(), repo, preprocessing, file_storage)

        response = await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(query=_MESSAGE),
        )

        saved = await repo.get(case.case_id)
        (offer,) = saved.chat_data_offers
        offered = [
            a
            for a in response.suggested_actions
            if a.label in (SAVE_LABEL, RESUBMIT_LABEL, SUBMIT_LABEL)
        ]
        save, resubmit = response.suggested_actions[:2]
        assert offered == [save, resubmit], "two choices, first, and no 'no'"
        assert (save.label, save.type) == (SAVE_LABEL, "DECIDE")
        assert save.intent == {
            "type": IntentType.SAVE_CHAT_DATA.value,
            "offer_id": offer.offer_id,
        }
        # Re-submitting is the client's own action: nothing to send, the
        # blocks to put in its data box.
        assert (resubmit.label, resubmit.type) == (RESUBMIT_LABEL, "EVIDENCE")
        assert resubmit.intent is None and resubmit.payload is None
        assert resubmit.prefill == _OUTPUT
        assert "It isn't evidence yet" in response.agent_response
        # Optional, and said so: the choices are for using it as evidence.
        assert "otherwise ignore this" in response.agent_response
        # Both ends named, so a block that took in a line the user wrote shows.
        assert "starting `NAME          READY   STATUS`" in response.agent_response
        assert "ending `api-0         0/1     CrashLoopBackOff`" in (
            response.agent_response
        )
        # Answered by a click, never by typed matching.
        assert not any(
            (s.get("intent") or {}).get("type") == IntentType.SAVE_CHAT_DATA.value
            for s in saved.last_suggestions or []
        )

    async def test_a_reported_state_with_no_data_gets_one_request(
        self, repo_and_case, preprocessing, file_storage
    ):
        """ "It's stable" is the user's account. FaultMaven asks for the output
        that shows it; there is nothing in the message to save."""
        repo, case = repo_and_case
        request = "the output of kubectl get pods -n payments"
        service = _service(
            _OfferingEngine(state_request=request), repo, preprocessing, file_storage
        )

        response = await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(query="it's stable now"),
        )

        (submit,) = [
            a
            for a in response.suggested_actions
            if a.label in (SAVE_LABEL, RESUBMIT_LABEL, SUBMIT_LABEL)
        ]
        assert submit is response.suggested_actions[0]
        assert (submit.label, submit.type) == (SUBMIT_LABEL, "EVIDENCE")
        assert submit.prefill == "" and submit.intent is None
        assert request in submit.body
        assert request in response.agent_response
        assert "If you'd rather not, ignore this." in response.agent_response

    async def test_a_turn_that_found_nothing_offers_nothing(
        self, repo_and_case, preprocessing, file_storage
    ):
        repo, case = repo_and_case
        service = _service(_OfferingEngine(), repo, preprocessing, file_storage)

        response = await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(query="the pods won't start"),
        )

        assert not any(
            a.label in (SAVE_LABEL, RESUBMIT_LABEL, SUBMIT_LABEL)
            for a in response.suggested_actions
        )
        assert "It isn't evidence yet" not in response.agent_response


class TestSave:
    async def test_the_blocks_become_one_paste_and_the_turn_runs_on_it(
        self, repo_and_case, preprocessing, file_storage
    ):
        repo, case = repo_and_case
        offer = _with_offer(case)
        engine = MockMilestoneEngine()
        service = _service(engine, repo, preprocessing, file_storage)

        await service.process_turn(
            case_id=case.case_id, user_id="user_owner", payload=_click(offer.offer_id)
        )

        # Stored exactly as the user typed it.
        stored = file_storage.store_file.await_args.kwargs
        assert stored["file_data"] == _OUTPUT.encode()
        # The engine ran a paste turn on it.
        engine.process_turn.assert_awaited_once()
        kwargs = engine.process_turn.await_args.kwargs
        assert kwargs["intent_type"] == IntentType.CONVERSATION.value
        (attachment,) = kwargs["attachments"]
        saved = await repo.get(case.case_id)
        (uploaded,) = saved.uploaded_files
        assert attachment["file_id"] == uploaded.file_id
        (settled,) = saved.chat_data_offers
        assert settled.state == ChatDataOfferState.SAVED
        assert settled.file_ids == [uploaded.file_id]
        assert settled.decided_at_turn == saved.current_turn
        # The user row keeps what they clicked.
        (user_row,) = [m for m in saved.messages if m["role"] == "user"]
        assert user_row["metadata"]["intent_type"] == IntentType.SAVE_CHAT_DATA.value

    @pytest.mark.parametrize(
        ("state", "blocks", "reply"),
        [
            (ChatDataOfferState.SAVED, (_OUTPUT,), "already saved as data"),
            (ChatDataOfferState.OFFERED, (), "no output in that message to save"),
        ],
    )
    async def test_a_click_that_saves_nothing_is_answered_without_the_engine(
        self, repo_and_case, preprocessing, file_storage, state, blocks, reply
    ):
        repo, case = repo_and_case
        offer = _with_offer(case, state=state, blocks=blocks, request="the pod list")
        engine = MockMilestoneEngine()
        service = _service(engine, repo, preprocessing, file_storage)

        response = await service.process_turn(
            case_id=case.case_id, user_id="user_owner", payload=_click(offer.offer_id)
        )

        engine.process_turn.assert_not_awaited()
        file_storage.store_file.assert_not_awaited()
        assert reply in response.agent_response


class TestRefusals:
    async def test_a_click_naming_no_offer_is_refused_before_the_turn(
        self, repo_and_case, preprocessing, file_storage, monkeypatch
    ):
        repo, case = repo_and_case
        engine = MockMilestoneEngine()
        service = _service(engine, repo, preprocessing, file_storage)
        admitted = AsyncMock(side_effect=AssertionError("the turn was admitted"))
        monkeypatch.setattr(service, "_verify_access_and_reserve", admitted)

        with pytest.raises(ValidationException):
            await service.process_turn(
                case_id=case.case_id,
                user_id="user_owner",
                payload=_click("cdo_000000000000"),
            )

        admitted.assert_not_awaited()
        engine.process_turn.assert_not_awaited()

    def test_the_intent_needs_its_offer(self):
        with pytest.raises(ValueError):
            QueryIntent(type=IntentType.SAVE_CHAT_DATA)

    def test_the_intent_is_answered_by_the_service(self):
        assert (
            _INTENT_DISPATCH[IntentType.SAVE_CHAT_DATA] == _IntentDispatchKind.SERVICE
        )
