"""#2222: typed text never mints an on-screen resolve choice.

The intent resolver's classifier tier is asked which offered choice the user is
"responding to". A decline of the "Mark it resolved" chip, or a request to close
beside it, is a response to it, so the chip's ``status_transition`` was minted
and re-proposed the resolution the user had just declined (case
``case_05d386b3d295``, turn 14). With a resolve offer pending, "please close the
case" was minted "Yes" and re-asked on every turn (``case_f1aa690028ba``, turn
15).

Driven through ``InvestigationService.process_turn`` over the real engine. The
classifier's router and the model are doubles, so no live LLM is called; the
classifier double answers "1" whenever it is asked. The out-of-band triage is
stubbed to "not an aside", since its verdict is not under test (#1329).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.intent_resolver import IntentResolver
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    DECLINED_RESOLVE_CARD_LABEL,
    declined_close_card,
    declined_resolve_card,
)
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    _resolution_confirmation_suggestions,
)
from faultmaven.core.investigation.schemas import (
    InvestigationResponse_Diagnosis,
    TurnPayload,
)
from faultmaven.core.investigation.terminal_transitions import (
    ClosureReadiness,
    deferred_disposition_signature,
    propose_transition,
)
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.case.contracts import CaseState
from tests.model_content import disabled_redaction
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _confirmed_case,
)

pytestmark = pytest.mark.unit

DECLINE = "I can't call it resolved yet."
CLOSE_REQUEST = "We're out of time on this one — please close the case."
RE_ASK = "Please select one of the options above"


class _Store:
    """Saves a snapshot and reads a fresh copy, as a database does."""

    def __init__(self, case) -> None:
        self.case_id = case.case_id
        self.rows = {case.case_id: case.model_copy(deep=True)}

    async def get(self, case_id):
        row = self.rows.get(case_id)
        return row.model_copy(deep=True) if row is not None else None

    async def save(self, case, *, reports=(), receipt=None):
        self.rows[case.case_id] = case.model_copy(deep=True)
        return case

    def row(self):
        return self.rows[self.case_id]


def _router(answer: str) -> MagicMock:
    router = MagicMock()
    router.route = AsyncMock(return_value=SimpleNamespace(content=answer))
    return router


def _service(case, classifier_answer: str = "1"):
    store = _Store(case)
    engine = MilestoneEngine(MagicMock(), store, investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Noted.", state_updates={}
        )
    )
    svc = InvestigationService(engine, store)
    classifier = _router(classifier_answer)
    svc.intent_resolver = IntentResolver(classifier)
    svc.out_of_band_triage.triage = AsyncMock(return_value=None)
    svc.engine.process_turn = AsyncMock(wraps=svc.engine.process_turn)
    return svc, store, classifier


async def _turn(svc, store, query: str):
    return await svc.process_turn(
        case_id=store.case_id,
        user_id=store.row().user_id,
        payload=TurnPayload(query=query),
    )


def _on_offer(case, cards) -> None:
    case.last_suggestions = [
        {**card, "offered_turn": case.current_turn} for card in cards
    ]


def _declined_with_the_chip():
    """READY, a resolve decline standing, and only the chip on offer."""
    case = _confirmed_case()
    case.progress.deferred_disposition_declined_signatures = [
        deferred_disposition_signature(case, ClosureReadiness.SUGGEST_RESOLVE)
    ]
    chip = declined_resolve_card(case)
    assert chip is not None, "premise: the decline stands"
    _on_offer(case, [chip])
    return case


def _resolve_offer_pending():
    """READY, a resolve offer pending, its confirmation pair on offer."""
    case = _confirmed_case()
    propose_transition(case, to_state="resolved", summary="Shall I resolve it?")
    _on_offer(case, _resolution_confirmation_suggestions(case))
    return case


class TestTheChipIsNeverMintedFromTypedText:
    @pytest.mark.parametrize("message", [DECLINE, CLOSE_REQUEST])
    async def test_no_resolve_proposal_and_no_classifier_call(self, message):
        svc, store, classifier = _service(_declined_with_the_chip())

        response = await _turn(svc, store, message)

        row = store.row()
        assert row.pending_transition is None
        assert row.state == CaseState.INVESTIGATING
        labels = [action.label for action in response.suggested_actions]
        assert labels == [DECLINED_RESOLVE_CARD_LABEL]
        assert svc.engine.process_turn.await_args.kwargs["intent_type"] in (
            None,
            "conversation",
        )
        # Not vacuous: with the chip as the only choice, nothing is asked.
        classifier.route.assert_not_called()


class TestAMintedYesAtAPendingGateNeedsABareConsent:
    async def test_a_close_request_reaches_the_model_not_a_re_ask(self):
        svc, store, classifier = _service(_resolve_offer_pending())

        response = await _turn(svc, store, CLOSE_REQUEST)

        assert RE_ASK not in response.agent_response
        svc.engine.generator.generate_structured_output.assert_awaited_once()
        # The classifier minted "Yes", and the mint was dropped.
        classifier.route.assert_awaited_once()
        assert svc.engine.process_turn.await_args.kwargs["intent_type"] in (
            None,
            "conversation",
        )
        row = store.row()
        assert row.state == CaseState.INVESTIGATING
        assert row.pending_transition is None
        # Withdrawn as a deflection, as with no resolve control on screen.
        assert row.progress.deferred_disposition_declined_signatures

    async def test_a_bare_yes_is_still_adopted_and_confirms(self):
        svc, store, classifier = _service(_resolve_offer_pending())

        await _turn(svc, store, "yes")

        classifier.route.assert_awaited_once()
        assert (
            svc.engine.process_turn.await_args.kwargs["intent_type"] == "confirmation"
        )
        assert store.row().state == CaseState.RESOLVED


class TestTheClassifierIsOfferedNoTerminalStep:
    @staticmethod
    def _terminal_cards():
        case = _declined_with_the_chip()
        return [
            declined_resolve_card(case),
            declined_close_card("false_alarm"),
            declined_close_card("deferred"),
        ]

    async def test_only_the_other_choices_reach_the_prompt(self):
        cards = self._terminal_cards()
        confirm, decline = _resolution_confirmation_suggestions(
            _resolve_offer_pending()
        )
        classifier = _router("1")

        minted = await IntentResolver(classifier).resolve(
            "sounds right", [*cards, decline], redaction_ctx=disabled_redaction()
        )

        prompt = classifier.route.await_args.kwargs["messages"][0]["content"]
        assert decline["label"] in str(prompt)
        for card in cards:
            assert card["label"] not in str(prompt)
        assert minted == decline["intent"]

    async def test_with_only_terminal_choices_the_classifier_is_not_called(self):
        classifier = _router("1")
        resolver = IntentResolver(classifier)
        cards = self._terminal_cards()

        for message in (DECLINE, CLOSE_REQUEST, "close it as a false alarm"):
            assert (
                await resolver.resolve(
                    message, cards, redaction_ctx=disabled_redaction()
                )
                is None
            )
        classifier.route.assert_not_called()

    async def test_the_exact_text_of_a_terminal_choice_still_resolves_to_it(self):
        classifier = _router("1")
        for card in self._terminal_cards():
            minted = await IntentResolver(classifier).resolve(
                card["payload"], [card], redaction_ctx=disabled_redaction()
            )
            assert minted == card["intent"]
        classifier.route.assert_not_called()
