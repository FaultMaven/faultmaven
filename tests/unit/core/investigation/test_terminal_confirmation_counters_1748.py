"""#1748 - which token confirmed a terminal transition, and who kept talking after.

#723 defers two confirmation-model notes until either is observed. The first (a
terminal transition confirmed by a bare weak token that proved spurious) needs
the confirming channel recorded and the follow-up counted; both are pinned here
from the classifier, through the executor, to the turn record and the counters.
The second (a dropdown INQUIRY -> INVESTIGATING that did not transition) cannot
occur: ``USER_SELECTABLE_ACTIONS`` offers only CLOSED from INQUIRY and
``earned_edge_refusal`` refuses any other ``status_transition`` at engine entry.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import faultmaven.core.investigation.milestone_engine.terminal_turns as terminal_turns
import faultmaven.core.investigation.terminal_transitions as tt
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    _EXPLICIT_CONFIRM_TOKENS,
    _WEAK_CONFIRM_TOKENS,
    _user_confirms_transition,
    confirmation_token_class,
)
from faultmaven.core.investigation.terminal_transitions import (
    TERMINAL_CONFIRMED_VIA,
    confirm_pending_transition,
    propose_transition,
)
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.problem import ProblemVerification
from faultmaven.modules.case.domain.models.progress import InvestigationProgress

pytestmark = pytest.mark.unit

NEGATIVES = ["ok but what is the root cause?", "yesterday", "hmm", ""]


class TestTheClassifier:
    @pytest.mark.parametrize("token", _WEAK_CONFIRM_TOKENS)
    def test_each_weak_token_alone_is_weak(self, token):
        assert confirmation_token_class(token) == "weak_token"

    @pytest.mark.parametrize("token", _EXPLICIT_CONFIRM_TOKENS)
    def test_each_explicit_token_alone_is_explicit(self, token):
        assert confirmation_token_class(token) == "explicit_token"

    def test_an_explicit_token_beside_a_weak_one_is_explicit(self):
        assert confirmation_token_class("yes ok") == "explicit_token"

    def test_a_substantive_reply_is_not_a_confirmation(self):
        assert confirmation_token_class("ok but what is the root cause?") is None

    def test_a_word_sharing_a_prefix_is_not_a_confirmation(self):
        assert confirmation_token_class("yesterday") is None

    @pytest.mark.parametrize(
        "message",
        [*_WEAK_CONFIRM_TOKENS, *_EXPLICIT_CONFIRM_TOKENS, "yes ok", *NEGATIVES],
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

    @pytest.mark.parametrize("message", ["ok", "OK.", "Sure!", "lgtm"])
    def test_the_bare_weak_replies_read_as_weak(self, message):
        assert confirmation_token_class(message) == "weak_token"

    def test_the_two_sets_do_not_overlap(self):
        assert not set(_WEAK_CONFIRM_TOKENS) & set(_EXPLICIT_CONFIRM_TOKENS)


def _investigating_case(pending: str = "resolved") -> Case:
    case = Case(
        case_id="case_1748a1748a17",
        title="Terminal confirmation counters",
        state=CaseState.INQUIRY,
        user_id="user_test",
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


class TestTheExecutor:
    def test_a_missing_confirmed_via_raises(self):
        with pytest.raises(TypeError):
            confirm_pending_transition(_investigating_case(), "user_test")

    def test_an_unknown_confirmed_via_raises(self):
        with pytest.raises(ValueError):
            confirm_pending_transition(
                _investigating_case(), "user_test", confirmed_via="typed_text"
            )

    @pytest.mark.parametrize("via", TERMINAL_CONFIRMED_VIA)
    def test_an_executed_resolved_counts_once_under_its_labels(self, via):
        with patch.object(tt, "terminal_confirmation_total") as counter:
            executed = confirm_pending_transition(
                _investigating_case(), "user_test", confirmed_via=via
            )

        assert executed is True
        counter.labels.assert_called_once_with(via=via, to_state="resolved")
        counter.labels.return_value.inc.assert_called_once()

    def test_an_executed_close_counts_as_closed(self):
        with patch.object(tt, "terminal_confirmation_total") as counter:
            confirm_pending_transition(
                _investigating_case("closed"), "user_test", confirmed_via="weak_token"
            )

        counter.labels.assert_called_once_with(via="weak_token", to_state="closed")

    def test_the_inv37_pivot_counts_nothing(self):
        case = _investigating_case("closed")
        pivot = MagicMock(verdict=tt.ClosureReadiness.SUGGEST_RESOLVE, message="r")
        with (
            patch.object(tt, "assess_closure_readiness", return_value=pivot),
            patch.object(tt, "terminal_confirmation_total") as counter,
        ):
            executed = confirm_pending_transition(
                case, "user_test", confirmed_via="weak_token"
            )

        assert executed is False
        assert case.pending_transition["to_state"] == "resolved"
        counter.labels.assert_not_called()


def _engine() -> MilestoneEngine:
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        side_effect=AssertionError("the gate must not reach the LLM")
    )
    return engine


def _record_terminal_reply(case: Case) -> None:
    """The service's consumed-turn backstop writes the record of a terminal
    turn, which the engine's terminal short-circuit does not."""
    from faultmaven.modules.agent.domain.services.investigation_service.turn_bookkeeping import (
        _backfill_consumed_turn,
    )

    _backfill_consumed_turn(
        case, user_message="msg", agent_response="reply", metadata={}
    )


class TestDrivenThroughTheEngine:
    async def _confirm(
        self, *, message, intent_type=None, intent_data=None, pending="resolved"
    ):
        engine = _engine()
        case = _investigating_case(pending)
        with (
            patch.object(tt, "terminal_confirmation_total") as confirmation,
            patch.object(terminal_turns, "terminal_followup_total") as followup,
        ):
            result = await engine.process_turn(
                case=case,
                user_message=message,
                intent_type=intent_type,
                intent_data=intent_data,
            )
        return engine, case, result, confirmation, followup

    async def test_a_typed_ok_resolves_and_is_recorded_as_weak(self):
        _, case, _, confirmation, _ = await self._confirm(message="ok")

        assert case.state == CaseState.RESOLVED
        confirmation.labels.assert_called_once_with(
            via="weak_token", to_state="resolved"
        )
        confirmation.labels.return_value.inc.assert_called_once()
        assert case.turn_history[-1].terminal_confirmed_via == "weak_token"

    async def test_a_typed_yes_is_recorded_as_explicit(self):
        _, case, _, confirmation, _ = await self._confirm(message="yes")

        confirmation.labels.assert_called_once_with(
            via="explicit_token", to_state="resolved"
        )
        assert case.turn_history[-1].terminal_confirmed_via == "explicit_token"

    async def test_a_decide_click_is_recorded_as_intent(self):
        _, case, _, confirmation, _ = await self._confirm(
            message="Yes, resolve it",
            intent_type="confirmation",
            intent_data={"value": True},
        )

        confirmation.labels.assert_called_once_with(via="intent", to_state="resolved")
        assert case.turn_history[-1].terminal_confirmed_via == "intent"

    async def test_a_repeated_dropdown_click_is_recorded_as_intent(self):
        _, case, _, confirmation, _ = await self._confirm(
            message="Close",
            intent_type="status_transition",
            intent_data={"to_state": "closed"},
            pending="closed",
        )

        confirmation.labels.assert_called_once_with(via="intent", to_state="closed")

    async def test_only_the_first_message_after_is_a_followup(self):
        engine, case, _, _, followup = await self._confirm(message="ok")
        followup.labels.assert_not_called()  # the confirming turn itself
        _record_terminal_reply_seam = _record_terminal_reply

        async def _next(message):
            with (
                patch.object(terminal_turns, "terminal_followup_total") as counter,
                patch.object(
                    terminal_turns.TerminalTurnHandler,
                    "_process_terminal_qa",
                    new=AsyncMock(return_value={"agent_response": "reply"}),
                ),
            ):
                await engine.process_turn(case=case, user_message=message)
            # the service's backstop records the terminal turn
            case.current_turn += 1
            _record_terminal_reply_seam(case)
            return counter

        first = await _next("thanks")
        first.labels.assert_called_once_with(via="weak_token")
        first.labels.return_value.inc.assert_called_once()

        second = await _next("one more question")
        second.labels.assert_not_called()

    async def test_the_transitions_path_records_the_same(self):
        """``_check_automatic_transitions`` is reached directly: the engine gate
        consumes every pending confirmation before it, so no ``process_turn``
        message arrives here (see the PR body)."""
        engine = _engine()
        case = _investigating_case()
        metadata: dict = {}
        with patch.object(tt, "terminal_confirmation_total") as confirmation:
            await engine.transitions.check_automatic_transitions(
                case=case, metadata=metadata, user_message="sure"
            )

        assert case.state == CaseState.RESOLVED
        confirmation.labels.assert_called_once_with(
            via="weak_token", to_state="resolved"
        )
        assert metadata["terminal_confirmed_via"] == "weak_token"
