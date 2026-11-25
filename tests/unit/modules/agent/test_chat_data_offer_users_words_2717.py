"""The chat-data offer searches only what the user typed for blocks (#2717).

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

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.case_telemetry import TELEMETRY_LOGGER_NAME
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

    async def test_its_data_answers_a_waiting_request_and_a_new_one_is_asked(self, rig):
        """The composed query is not searched for blocks, but the state request
        is asked whoever wrote the query: the turn's data closes the request
        that was waiting, and the model's new one is minted."""
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

        _, saved = await _turn(rig, TurnPayload(query="", attachments=[_paste()]))

        answered, asked = saved.chat_data_offers
        assert answered.offer_id == waiting.offer_id
        assert answered.decided_at_turn == saved.current_turn
        assert asked.blocks == []
        assert asked.request == "the output of chronyc"
        assert asked.decided_at_turn is None


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


def _reports_a_state(prompt, schema_model, **kwargs):
    """The model reads the message as a report of the system's state, with no
    output showing it, and names the output that would."""
    return InvestigationResponse_Diagnosis(
        agent_response="Good to hear.",
        state_updates={},
        system_state_report=StateReport(request="the output of kubectl get pods"),
    )


def _model_reports_a_state(rig):
    rig.engine.generator.generate_structured_output.side_effect = _reports_a_state


_WITH_OUTPUT = f"The pods won't start:\n\n{_OUTPUT}"


class TestOnlyTypedTextIsSearchedForBlocks:
    """The client says who wrote ``query`` (``query_source``): only a typed
    query is searched for output the user typed."""

    async def test_a_generated_query_naming_a_block_offers_nothing(self, rig):
        response, saved = await _turn(
            rig, TurnPayload(query=_WITH_OUTPUT, query_source="generated")
        )

        given = rig.engine.generator.generate_structured_output.await_args
        assert given.kwargs["user_message"] == _WITH_OUTPUT, "the model saw it"
        assert saved.chat_data_offers == []
        assert _NOTE not in response.agent_response
        assert not _offered(response)

    async def test_a_chosen_query_naming_a_block_offers_no_block(self, rig):
        response, saved = await _turn(
            rig, TurnPayload(query=_WITH_OUTPUT, query_source="chosen")
        )

        assert saved.chat_data_offers == []
        assert not _offered(response)

    async def test_an_empty_query_and_a_paste_sent_as_generated_offers_nothing(
        self, rig
    ):
        response, saved = await _turn(
            rig,
            TurnPayload(query="", attachments=[_paste()], query_source="generated"),
        )

        assert saved.chat_data_offers == []
        assert not _offered(response)


class TestTheStateRequest:
    """The state request is asked whoever wrote the query: only the block
    search reads typed text."""

    async def test_a_chosen_query_reporting_a_state_is_asked_for_it(self, rig):
        _model_reports_a_state(rig)

        await _turn(rig, TurnPayload(query="It's stable now.", query_source="chosen"))

        saved = await rig.repo.get(rig.case.case_id)
        (offer,) = saved.chat_data_offers
        assert offer.blocks == []
        assert offer.request == "the output of kubectl get pods"

    async def test_a_generated_query_reporting_a_state_is_asked_for_it(self, rig):
        _model_reports_a_state(rig)

        _, saved = await _turn(
            rig, TurnPayload(query="It's stable now.", query_source="generated")
        )

        (offer,) = saved.chat_data_offers
        assert offer.blocks == []
        assert offer.request == "the output of kubectl get pods"


class TestTypedTurnsCarryingAnIntentStillMint:
    """No intent backstop: an intent does not make a typed query untyped."""

    async def test_a_typed_turn_the_resolver_minted_an_intent_for(self, rig):
        """The resolver reads the typed text as a chip's confirmation; the
        engine is told the intent was minted from typed text."""
        resolved = {"type": "confirmation", "confirmation_value": True}
        rig.service.intent_resolver.resolve = AsyncMock(return_value=resolved)
        rig.case.last_suggestions = [
            {
                "label": "Yes, check the pods",
                "payload": "Yes, check the pods",
                "action_type": "DECIDE",
                "intent": resolved,
                "offered_turn": rig.case.current_turn,
            }
        ]
        process_turn = AsyncMock(wraps=rig.engine.process_turn)
        rig.engine.process_turn = process_turn

        response, saved = await _turn(rig, TurnPayload(query=_WITH_OUTPUT))

        assert process_turn.await_args.kwargs["typed"] is True, "premise: minted"
        (offer,) = saved.chat_data_offers
        assert offer.blocks == [_OUTPUT]
        assert _offered(response)

    async def test_a_typed_turn_sent_with_an_intent_and_no_query_source(self, rig):
        """The simulator's shape: the persona's own words with a control's
        intent, and no ``query_source``."""
        response, saved = await _turn(
            rig,
            TurnPayload(
                query=_WITH_OUTPUT,
                intent=QueryIntent(type=IntentType.CONVERSATION),
            ),
        )

        (offer,) = saved.chat_data_offers
        assert offer.blocks == [_OUTPUT]
        assert _offered(response)


class TestTheTelemetryCountsTheUsersQuery:
    async def test_a_generated_query_counts_no_chars(self, rig, caplog):
        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            await _turn(rig, TurnPayload(query=_WITH_OUTPUT, query_source="generated"))

        (row,) = [r for r in caplog.records if r.name == TELEMETRY_LOGGER_NAME]
        assert row.user_message_chars == 0

    @pytest.mark.parametrize("source", ["typed", "chosen"])
    async def test_a_typed_or_chosen_query_counts_its_chars(self, rig, caplog, source):
        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            await _turn(rig, TurnPayload(query=_WITH_OUTPUT, query_source=source))

        (row,) = [r for r in caplog.records if r.name == TELEMETRY_LOGGER_NAME]
        assert row.user_message_chars == len(_WITH_OUTPUT)
