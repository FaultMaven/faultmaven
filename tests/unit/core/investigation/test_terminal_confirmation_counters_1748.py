"""#1748 - how a terminal transition was confirmed, and who kept talking after.

#723 defers two confirmation-model notes until either is observed. The first (a
terminal transition confirmed by a bare weak token that proved spurious) needs
the confirming channel recorded and the next message counted. The second (a
dropdown INQUIRY -> INVESTIGATING that did not transition) cannot occur:
``USER_SELECTABLE_ACTIONS`` offers only CLOSED from INQUIRY and
``earned_edge_refusal`` refuses any other ``status_transition`` at engine entry.

The channel is written onto the confirming turn's record, and BOTH counters are
read from the saved records by the investigation service after its save
(``turn_messages._save_and_emit_turn``). So everything that decides a count is
driven here through ``InvestigationService.process_turn`` — the path a click or
a typed reply actually takes — with only the LLM and the database doubled.
"""

from datetime import UTC, datetime
from typing import get_args
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import faultmaven.core.investigation.terminal_transitions as tt
import faultmaven.modules.agent.domain.services.investigation_service.turn_messages as turn_messages
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    _resolution_confirmation_suggestions,
)
from faultmaven.core.investigation.milestone_engine.terminal_turns import (
    TerminalTurnHandler,
)
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    _EXPLICIT_CONFIRM_TOKENS,
    _WEAK_CONFIRM_TOKENS,
    _user_confirms_transition,
    confirmation_token_class,
)
from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.core.investigation.terminal_transitions import propose_transition
from faultmaven.models.api_models import QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    TerminalConfirmedVia,
    TurnOutcome,
    TurnProgress,
)
from faultmaven.modules.case.domain.models.problem import ProblemVerification
from faultmaven.modules.case.domain.models.progress import InvestigationProgress
from faultmaven.modules.case.exceptions import StaleCaseException

pytestmark = pytest.mark.unit

NEGATIVES = ["ok but what is the root cause?", "yesterday", "hmm", ""]

#: Replies that OPEN on a weak token and still carry an explicit one. The
#: first-token classifier read every one of these as ``weak_token`` (review F1).
WEAK_OPENING_EXPLICIT = ["ok go ahead", "sure, close it", "ok yes", "lgtm, confirmed"]

#: What "Yes, mark as resolved" sends when clicked.
CONFIRM_CARD = _resolution_confirmation_suggestions()[0]


class TestTheClassifier:
    @pytest.mark.parametrize("token", _WEAK_CONFIRM_TOKENS)
    def test_each_weak_token_alone_is_weak(self, token):
        assert confirmation_token_class(token) == "weak_token"

    @pytest.mark.parametrize("token", _EXPLICIT_CONFIRM_TOKENS)
    def test_each_explicit_token_alone_is_explicit(self, token):
        assert confirmation_token_class(token) == "explicit_token"

    @pytest.mark.parametrize("message", [*WEAK_OPENING_EXPLICIT, "yes ok"])
    def test_an_explicit_token_anywhere_makes_the_reply_explicit(self, message):
        assert confirmation_token_class(message) == "explicit_token"

    @pytest.mark.parametrize(
        "message", ["ok", "OK.", "sure!", "lgtm", "okay, sounds good"]
    )
    def test_a_reply_of_weak_tokens_only_is_weak(self, message):
        assert confirmation_token_class(message) == "weak_token"

    def test_the_search_keeps_the_word_boundaries(self):
        """A token inside a longer word is not carried: "yesterday's" is not "yes"."""
        assert confirmation_token_class("ok, yesterday's fix held") == "weak_token"

    def test_a_substantive_reply_is_not_a_confirmation(self):
        assert confirmation_token_class("ok but what is the root cause?") is None

    def test_a_word_sharing_a_prefix_is_not_a_confirmation(self):
        assert confirmation_token_class("yesterday") is None

    def test_an_explicit_token_later_in_the_reply_does_not_make_it_confirm(self):
        """Which class is read over the whole reply; WHETHER it confirms is not.
        The gate still decides on the opening token, as it always has."""
        assert confirmation_token_class("well, yes") is None

    @pytest.mark.parametrize(
        "message",
        [
            *_WEAK_CONFIRM_TOKENS,
            *_EXPLICIT_CONFIRM_TOKENS,
            *WEAK_OPENING_EXPLICIT,
            "yes ok",
            "well, yes",
            *NEGATIVES,
        ],
    )
    def test_the_split_changes_no_verdict(self, message):
        assert _user_confirms_transition(message) == (
            confirmation_token_class(message) is not None
        )

    def test_the_weak_set_is_exactly_723s_note_1_list(self):
        assert _WEAK_CONFIRM_TOKENS == (
            "ok",
            "okay",
            "sure",
            "sounds good",
            "looks good",
            "lgtm",
        )

    def test_the_two_sets_do_not_overlap(self):
        assert not set(_WEAK_CONFIRM_TOKENS) & set(_EXPLICIT_CONFIRM_TOKENS)


class TestAStoredChannelNeverBreaksALoad:
    """The loaders rebuild every record with ``TurnProgress(**t)``, so a value
    the ``Literal`` rejected would make the whole case unloadable (review F8)."""

    def test_an_unknown_stored_channel_loads_as_none(self):
        record = TurnProgress(
            turn_number=3,
            progress_made=False,
            outcome=TurnOutcome.CONVERSATION,
            terminal_confirmed_via="bare_weak",
        )
        assert record.terminal_confirmed_via is None

    @pytest.mark.parametrize("via", get_args(TerminalConfirmedVia))
    def test_every_known_channel_survives(self, via):
        record = TurnProgress(
            turn_number=3,
            progress_made=True,
            outcome=TurnOutcome.CONVERSATION,
            terminal_confirmed_via=via,
        )
        assert record.terminal_confirmed_via == via


# ---------------------------------------------------------------------------
# Driven through InvestigationService.process_turn
# ---------------------------------------------------------------------------

CASE_ID = "case_1748a1748a17"
USER_ID = "user_test"


def _investigating_case(pending: str = "resolved") -> Case:
    case = Case(
        case_id=CASE_ID,
        title="Terminal confirmation counters",
        state=CaseState.INQUIRY,
        user_id=USER_ID,
        enterprise_id="org_test",
        description="pool exhaustion",
        problem_verification=ProblemVerification(
            symptom_statement="pool exhaustion",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = "pool exhaustion"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.current_turn = 7
    propose_transition(case, to_state=pending, summary="Resolve it?")
    return case


class _Store:
    """The repository as a database behaves: a save stores a SNAPSHOT and a get
    returns a fresh copy. A turn whose save fails therefore leaves nothing for
    its retry to find, which is what makes the retry below a real one."""

    def __init__(self, case: Case) -> None:
        self._rows = {case.case_id: case.model_copy(deep=True)}
        self._failures: list[Exception] = []

    def fail_next_save(self, exc: Exception) -> None:
        self._failures.append(exc)

    async def get(self, case_id: str):
        row = self._rows.get(case_id)
        return row.model_copy(deep=True) if row is not None else None

    async def save(self, case: Case) -> Case:
        if self._failures:
            raise self._failures.pop(0)
        self._rows[case.case_id] = case.model_copy(deep=True)
        return case

    def row(self) -> Case:
        return self._rows[CASE_ID]


def _service(store: _Store) -> InvestigationService:
    engine = MilestoneEngine(MagicMock(), store, investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        side_effect=AssertionError("no route under test reaches the LLM")
    )
    return InvestigationService(engine, store)


async def _turn(svc: InvestigationService, query: str, intent: QueryIntent = None):
    return await svc.process_turn(
        case_id=CASE_ID,
        user_id=USER_ID,
        payload=TurnPayload(query=query, intent=intent),
    )


def _click_confirm() -> QueryIntent:
    return QueryIntent(**CONFIRM_CARD["intent"])


@pytest.fixture
def counters():
    with (
        patch.object(turn_messages, "terminal_confirmation_total") as confirmation,
        patch.object(turn_messages, "terminal_followup_total") as followup,
    ):
        yield confirmation, followup


def _offer_the_resolution_cards(store: _Store) -> None:
    """Put the confirmation pair on offer, as the proposing turn stored it."""
    row = store.row()
    row.last_suggestions = [
        {**card, "offered_turn": row.current_turn}
        for card in _resolution_confirmation_suggestions()
    ]


def _spy_on_the_engine(svc: InvestigationService) -> AsyncMock:
    spy = AsyncMock(wraps=svc.engine.process_turn)
    svc.engine.process_turn = spy
    return spy


class TestTheConfirmationCounter:
    async def test_a_typed_ok_counts_as_weak(self, counters):
        confirmation, followup = counters
        store = _Store(_investigating_case())

        await _turn(_service(store), "ok")

        assert store.row().state == CaseState.RESOLVED
        confirmation.labels.assert_called_once_with(
            via="weak_token", to_state="resolved"
        )
        confirmation.labels.return_value.inc.assert_called_once()
        assert store.row().turn_history[-1].terminal_confirmed_via == "weak_token"
        followup.labels.assert_not_called()  # the confirming turn itself

    @pytest.mark.parametrize("message", WEAK_OPENING_EXPLICIT)
    async def test_a_typed_reply_carrying_an_explicit_token_counts_as_explicit(
        self, counters, message
    ):
        confirmation, _ = counters
        store = _Store(_investigating_case())

        await _turn(_service(store), message)

        confirmation.labels.assert_called_once_with(
            via="explicit_token", to_state="resolved"
        )

    async def test_an_ok_the_resolver_minted_into_an_intent_is_still_weak(
        self, counters
    ):
        """Review F2: a typed "ok" the resolver turns into a confirmation
        intent is a typed "ok", not a click."""
        confirmation, _ = counters
        store = _Store(_investigating_case())
        _offer_the_resolution_cards(store)
        svc = _service(store)
        svc.intent_resolver.resolve = AsyncMock(return_value=CONFIRM_CARD["intent"])
        engine = _spy_on_the_engine(svc)

        await _turn(svc, "ok")

        # The mint happened and reached the gate as an intent — otherwise this
        # measures the plain typed path and cannot tell a click from a mint.
        assert engine.await_args.kwargs["intent_type"] == "confirmation"
        assert engine.await_args.kwargs["intent_data"]["typed"] is True
        confirmation.labels.assert_called_once_with(
            via="weak_token", to_state="resolved"
        )

    async def test_typed_text_the_resolver_accepted_with_no_token_is_typed_other(
        self, counters
    ):
        confirmation, _ = counters
        store = _Store(_investigating_case())
        _offer_the_resolution_cards(store)
        svc = _service(store)
        svc.intent_resolver.resolve = AsyncMock(return_value=CONFIRM_CARD["intent"])
        assert confirmation_token_class("that works") is None

        await _turn(svc, "that works")

        assert store.row().state == CaseState.RESOLVED
        confirmation.labels.assert_called_once_with(
            via="typed_other", to_state="resolved"
        )

    async def test_a_click_counts_as_intent(self, counters):
        confirmation, _ = counters
        store = _Store(_investigating_case())
        svc = _service(store)
        engine = _spy_on_the_engine(svc)

        await _turn(svc, CONFIRM_CARD["payload"], intent=_click_confirm())

        assert "typed" not in engine.await_args.kwargs["intent_data"]
        confirmation.labels.assert_called_once_with(via="intent", to_state="resolved")
        assert store.row().turn_history[-1].terminal_confirmed_via == "intent"

    async def test_a_confirmed_close_counts_as_closed(self, counters):
        confirmation, _ = counters
        store = _Store(_investigating_case("closed"))

        await _turn(_service(store), "yes")

        assert store.row().state == CaseState.CLOSED
        confirmation.labels.assert_called_once_with(
            via="explicit_token", to_state="closed"
        )

    async def test_the_inv37_pivot_counts_nothing(self, counters):
        confirmation, _ = counters
        store = _Store(_investigating_case("closed"))
        pivot = MagicMock(verdict=tt.ClosureReadiness.SUGGEST_RESOLVE, message="r")

        with patch.object(tt, "assess_closure_readiness", return_value=pivot):
            await _turn(_service(store), "ok")

        assert store.row().state == CaseState.INVESTIGATING
        assert store.row().pending_transition["to_state"] == "resolved"
        assert store.row().turn_history[-1].terminal_confirmed_via is None
        confirmation.labels.assert_not_called()

    async def test_a_raising_counter_never_fails_the_turn(self, counters):
        confirmation, _ = counters
        confirmation.labels.side_effect = RuntimeError("registry down")
        store = _Store(_investigating_case())

        response = await _turn(_service(store), "ok")

        confirmation.labels.assert_called_once()  # it did raise, here
        assert response.agent_response
        assert store.row().state == CaseState.RESOLVED


class TestTheFollowUpCounter:
    async def _resolved_on_ok(self, counters) -> tuple[_Store, InvestigationService]:
        store = _Store(_investigating_case())
        svc = _service(store)
        await _turn(svc, "ok")
        assert store.row().state == CaseState.RESOLVED
        counters[1].labels.assert_not_called()
        return store, svc

    async def test_only_the_first_message_after_counts_even_on_the_greeting_route(
        self, counters
    ):
        """Review F4: a greeting never reaches the engine, and is counted."""
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)

        await _turn(svc, "hello")
        # The orientation lane answered it, not the engine's terminal branch.
        assert store.row().messages[-2]["metadata"].get("orientation") == "greeting"
        followup.labels.assert_called_once_with(via="weak_token", to_state="resolved")
        followup.labels.return_value.inc.assert_called_once()

        await _turn(svc, "hello")
        followup.labels.assert_called_once()  # still once: not the second message

    async def test_a_follow_up_whose_save_fails_counts_once_on_its_retry(
        self, counters
    ):
        """Review F3: counted after the save, so a retried turn counts once."""
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)
        store.fail_next_save(StaleCaseException(CASE_ID, 3, 4))

        with pytest.raises(StaleCaseException):
            await _turn(svc, "hello")
        followup.labels.assert_not_called()

        await _turn(svc, "hello")
        followup.labels.assert_called_once_with(via="weak_token", to_state="resolved")
        followup.labels.return_value.inc.assert_called_once()

    async def test_a_click_after_the_confirmation_is_not_a_follow_up(self, counters):
        """Review F10: the confirmation card clicked again is not a user who
        kept talking to the case."""
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)
        answered = AsyncMock(
            side_effect=lambda case, user_message, metadata, user_id=None: {
                "agent_response": "It is resolved.",
                "case_updated": case,
                "metadata": metadata,
            }
        )

        with patch.object(TerminalTurnHandler, "_process_terminal_qa", new=answered):
            await _turn(svc, CONFIRM_CARD["payload"], intent=_click_confirm())

        answered.assert_awaited_once()  # the click reached the terminal case
        followup.labels.assert_not_called()


async def test_the_transitions_path_stamps_the_channel_on_its_metadata():
    """``check_automatic_transitions`` is reached directly: the engine gate
    consumes every pending confirmation before it, so no ``process_turn``
    message arrives here. What it stamps is read onto the turn's record by
    ``_apply_turn_response``, and counted from there like any other."""
    store = _Store(_investigating_case())
    engine = _service(store).engine
    case = await store.get(CASE_ID)
    metadata: dict = {}

    await engine.transitions.check_automatic_transitions(
        case=case, metadata=metadata, user_message="sure"
    )

    assert case.state == CaseState.RESOLVED
    assert metadata["terminal_confirmed_via"] == "weak_token"
