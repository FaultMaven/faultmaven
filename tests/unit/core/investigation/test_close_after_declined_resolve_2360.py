"""#2360: after the user declines the resolution, their Close closes the case.

Close is the user's choice at any time; resolve is offered only when the case
has earned it, and the two are not offered together. On a resolvable case the
status menu hides Close and every close pivots to the resolution (INV-37). Once
the user has declined that resolution (#1895), the menu shows Close again, the
menu close proposes CLOSED, and confirming it closes the case with the reason
``resolution_declined``. The "Mark it resolved" chip stays, the model's close
is still refused (chat is unchanged), and a new confirmation that the fix held
lapses the decline and restores the pivot.

Real ``process_turn`` turns with a stubbed generator: no live LLM.
"""

from unittest.mock import patch

import pytest

from faultmaven.core.investigation.milestone_engine.stage_gates import (
    DECLINED_RESOLVE_CARD_LABEL,
)
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    _terminal_confirmation_response,
)
from faultmaven.core.investigation.terminal_transitions import (
    confirm_pending_transition,
    declined_resolve_entry,
    deferred_disposition_signature,
    derive_closure_reason,
    derive_disposition_eligibility,
    execute_user_closure,
    propose_transition,
)
from faultmaven.infrastructure.observability import funnel_metrics
from faultmaven.modules.case.contracts import CaseState
from faultmaven.modules.case.domain.models.lifecycle import VALID_CLOSURE_REASONS
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _SR,
    _declined,
    _labels,
    _offered_then_declined,
    _turn,
    _user_confirmation,
    _user_problem_gone,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _confirmed_case,
)

pytestmark = pytest.mark.unit

_REASON = "resolution_declined"


async def _menu_close(engine, case) -> dict:
    """The status menu's Close: the intent the copilot and dashboard send."""
    return await _turn(
        engine,
        case,
        "",
        intent_type="status_transition",
        intent_data={"to_state": "closed", "from_state": "investigating"},
    )


def _new_confirmation(case) -> None:
    """A confirmation the decline never saw: both legs, recorded now."""
    case.evidence.extend(
        [
            _user_confirmation(turn=case.current_turn),
            _user_problem_gone(turn=case.current_turn),
        ]
    )
    assert declined_resolve_entry(case) is None, "premise: the decline lapsed"


def _declined_case():
    """A resolvable case with the resolve decline recorded on it."""
    case = _confirmed_case()
    case.progress.deferred_disposition_declined_signatures = [
        deferred_disposition_signature(case, _SR)
    ]
    assert declined_resolve_entry(case) is not None, "premise: the decline stands"
    return case


class TestTheMenuShowsClose:
    async def test_close_is_ready_only_while_the_decline_stands(self):
        assert derive_disposition_eligibility(_confirmed_case()) == {
            "resolved": "ready",
            "closed": "suggests_alternative",
        }
        _, case, _ = await _offered_then_declined()
        assert derive_disposition_eligibility(case)["closed"] == "ready"

        _new_confirmation(case)
        assert derive_disposition_eligibility(case)["closed"] == "suggests_alternative"


class TestTheMenuCloseCloses:
    async def test_it_proposes_a_close_and_confirming_it_closes(self):
        engine, case, _ = await _offered_then_declined()
        result = await _menu_close(engine, case)
        assert case.pending_transition["to_state"] == "closed"
        assert case.pending_transition["closure_reason"] == _REASON
        # Asked as a close, not with the pivot's "mark it resolved instead?".
        assert "close this case without resolution" in result["agent_response"]
        assert "mark it **resolved** instead" not in result["agent_response"]

        result = await _turn(engine, case, "yes")
        assert case.state == CaseState.CLOSED
        assert case.closure_reason == _REASON
        assert result["agent_response"].startswith(
            "Case closed — the fix was confirmed to work; you chose not to mark "
            "it resolved."
        )

    async def test_declining_it_keeps_the_case_the_decline_and_close(self):
        """The reply keys on what was declined: a close, so the plain reply,
        with the chip still attached and nothing recorded."""
        engine, case, _ = await _offered_then_declined()
        declined = list(_declined(case))
        await _menu_close(engine, case)

        result = await _turn(engine, case, "no")
        assert case.state == CaseState.INVESTIGATING
        assert case.pending_transition is None
        assert result["agent_response"] == (
            "Understood. The case remains open for further investigation."
        )
        assert _labels(result) == [DECLINED_RESOLVE_CARD_LABEL]
        assert _declined(case) == declined
        assert derive_disposition_eligibility(case)["closed"] == "ready"

    async def test_after_a_new_confirmation_the_menu_close_pivots_again(self):
        engine, case, _ = await _offered_then_declined()
        _new_confirmation(case)
        await _menu_close(engine, case)
        assert case.pending_transition["to_state"] == "resolved"


class TestTheConfirmTimeGuard:
    """Keyed on the decline alone, whoever proposed the close: here an engine
    close (``justifying_signature``) proposed before the case was resolvable,
    carrying the reason derived then."""

    def _stale_close(self, case):
        propose_transition(case, to_state="closed", summary="Close it?")
        case.pending_transition["closure_reason"] = "solution_deferred"
        case.pending_transition["justifying_signature"] = "has_substance|1|rcc|"

    def test_a_close_confirmed_while_the_decline_stands_closes(self):
        case = _declined_case()
        self._stale_close(case)
        assert confirm_pending_transition(case, "user_test") is True
        assert case.state == CaseState.CLOSED
        assert case.closure_reason == _REASON

    def test_the_same_confirm_after_the_decline_lapsed_pivots(self):
        case = _declined_case()
        self._stale_close(case)
        _new_confirmation(case)
        with patch(
            "faultmaven.core.investigation.terminal_transitions."
            "close_pivoted_to_resolve_total"
        ):
            assert confirm_pending_transition(case, "user_test") is False
        assert case.state == CaseState.INVESTIGATING
        assert case.pending_transition["to_state"] == "resolved"


class TestTheReason:
    def test_it_is_derived_while_the_decline_stands_ahead_of_deferred(self):
        case = _confirmed_case()
        assert derive_closure_reason(case) == "solution_deferred", "premise"
        assert derive_closure_reason(_declined_case()) == _REASON

    def test_the_rest_close_derives_it_too(self):
        case = _declined_case()
        assert execute_user_closure(case, "user_test") == _REASON
        assert case.closure_reason == _REASON

    def test_every_reason_table_names_it(self):
        assert _REASON in VALID_CLOSURE_REASONS
        assert _REASON in ReportGenerationService._CLOSURE_REASON_LABELS
        assert _REASON in funnel_metrics._CLOSED_REASONS
        assert _REASON in funnel_metrics._EFFORT_TO_STATES
        closed = _declined_case().model_copy(
            update={"state": CaseState.CLOSED, "closure_reason": _REASON}
        )
        assert _terminal_confirmation_response(closed) != "Case closed."
