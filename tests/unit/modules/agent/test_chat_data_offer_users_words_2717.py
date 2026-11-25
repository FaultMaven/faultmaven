"""The chat-data offer reads only what the user typed (#2717).

A turn carrying only data has no words of the user's: the service composes its
query ("I've pasted some text … tell me what you find") so the model knows to
analyze the data. That sentence is FaultMaven's own, and offering it back as
system output the user typed would record the engine's wording as evidence.

Driven through the REAL ``InvestigationService`` and the REAL
``MilestoneEngine`` (its model seam stubbed, no provider call). The stub model
names, as a block, the first and last line of the message it was given: on a
turn carrying only data that is the composed query, exactly the block the issue
saw offered.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.schemas import (
    Attachment,
    ChatDataBlock,
    InvestigationResponse_Diagnosis,
    StateReport,
    TurnPayload,
)
from faultmaven.core.preprocessing.models import UnifiedDataType
from faultmaven.models.api import DataType
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service.chat_data import (
    SAVE_LABEL,
)
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.case.contracts import ChatDataOffer, ChatDataOfferState
from tests.case_builders import investigating_case

from .conftest import MockCaseRepository

pytestmark = pytest.mark.unit

_NOTE = "It isn't evidence yet"
_OUTPUT = "NAME          READY   STATUS\napi-0         0/1     CrashLoopBackOff"


def _case():
    return investigating_case(
        case_id="case_000000002717",
        user_id="user_owner",
        enterprise_id="org_test",
        confirm_after_build=True,
        statement="api pods crash",
        current_turn=6,
    )


def _preprocessed():
    result = MagicMock()
    result.summary = "timedatectl output"
    result.structural_index = "index"
    result.data_type = UnifiedDataType.LOGS
    result.detailed_data_type = DataType.UNSTRUCTURED_TEXT
    result.content_hash = "e" * 64
    result.extraction_method = "rule_based"
    result.extraction_metadata = {}
    result.coverage_start_ts = None
    result.coverage_end_ts = None
    return result


def _paste() -> Attachment:
    return Attachment(
        content=b"               Local time: Tue 2026-11-11 10:00:00 UTC\n",
        filename="paste.txt",
        content_type="text/plain",
        source_metadata={"source_type": "text_paste"},
    )


async def _names_the_message(prompt, schema_model, **kwargs):
    """The model names the message it was given as output: the whole of it
    when it holds no pod listing, else the listing."""
    message = kwargs["user_message"]
    lines = (_OUTPUT if _OUTPUT in message else message).splitlines()
    return InvestigationResponse_Diagnosis(
        agent_response="Looking at the data.",
        state_updates={},
        chat_data_blocks=[ChatDataBlock(first_line=lines[0], last_line=lines[-1])],
    )


@pytest.fixture
def rig():
    repo = MockCaseRepository()
    case = _case()
    repo._storage[case.case_id] = case
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        side_effect=_names_the_message
    )
    preprocessing = MagicMock()
    preprocessing.classify_and_extract = AsyncMock(return_value=_preprocessed())
    storage = MagicMock()
    storage.store_file = AsyncMock(return_value={"file_path": "evidence/x/blob.txt"})
    storage.mark_linked = AsyncMock(return_value=True)
    service = InvestigationService(
        milestone_engine=engine,
        case_repository=repo,
        preprocessing_service=preprocessing,
        file_storage_service=storage,
    )
    service.out_of_band_triage.triage = AsyncMock(return_value=None)
    return SimpleNamespace(service=service, repo=repo, case=case, engine=engine)


async def _turn(rig, payload: TurnPayload):
    response = await rig.service.process_turn(
        case_id=rig.case.case_id, user_id="user_owner", payload=payload
    )
    return response, await rig.repo.get(rig.case.case_id)


def _offered(response) -> bool:
    return any(a.label == SAVE_LABEL for a in response.suggested_actions)


class TestATurnCarryingOnlyDataOffersNothing:
    async def test_the_composed_query_is_not_offered_back(self, rig):
        """The issue's reproduction: an empty message and one paste."""
        response, saved = await _turn(
            rig, TurnPayload(query="", attachments=[_paste()])
        )

        # The model was given the composed query and named it as output.
        given = rig.engine.generator.generate_structured_output.await_args
        assert given.kwargs["user_message"].startswith("I've pasted some text")
        assert saved.chat_data_offers == []
        assert _NOTE not in response.agent_response
        assert not _offered(response)

    async def test_a_save_click_with_no_text_offers_nothing_new(self, rig):
        """The save turn is a paste turn: its attachment is added before the
        turn runs, so an empty click composes the query too."""
        offer = ChatDataOffer(turn=5, blocks=[_OUTPUT])
        rig.case.chat_data_offers.append(offer)

        response, saved = await _turn(
            rig,
            TurnPayload(
                query="",
                intent=QueryIntent(
                    type=IntentType.SAVE_CHAT_DATA, offer_id=offer.offer_id
                ),
            ),
        )

        rig.engine.generator.generate_structured_output.assert_awaited_once()
        assert [o.offer_id for o in saved.chat_data_offers] == [offer.offer_id]
        assert saved.chat_data_offers[0].state == ChatDataOfferState.SAVED
        assert _NOTE not in response.agent_response

    async def test_its_data_still_answers_a_waiting_state_request(self, rig):
        """Nothing is read from the composed query, a state report included;
        the data the turn carries still closes the request that was waiting."""
        waiting = ChatDataOffer(turn=5, request="the output of timedatectl")
        rig.case.chat_data_offers.append(waiting)
        rig.engine.generator.generate_structured_output.side_effect = None
        rig.engine.generator.generate_structured_output.return_value = (
            InvestigationResponse_Diagnosis(
                agent_response="Looking at the data.",
                state_updates={},
                system_state_report=StateReport(request="the output of chronyc"),
            )
        )

        response, saved = await _turn(
            rig, TurnPayload(query="", attachments=[_paste()])
        )

        (answered,) = saved.chat_data_offers
        assert answered.offer_id == waiting.offer_id
        assert answered.decided_at_turn == saved.current_turn
        assert "the output of chronyc" not in response.agent_response


class TestTypedOutputIsStillOffered:
    async def test_a_typed_message_holding_output_beside_a_paste(self, rig):
        """The rig's positive control: the same stub, and the user's own
        message, mint the offer and its note."""
        response, saved = await _turn(
            rig,
            TurnPayload(
                query=f"The pods won't start:\n\n{_OUTPUT}", attachments=[_paste()]
            ),
        )

        (offer,) = saved.chat_data_offers
        assert offer.blocks == [_OUTPUT]
        assert _NOTE in response.agent_response
        assert _offered(response)
