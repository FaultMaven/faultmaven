"""#2360: after the user declines the resolution, their Close closes the case.

Close is the user's choice at any time; resolve is offered only when the case
has earned it, and the two are not offered together. On a resolvable case the
status menu hides Close and every close pivots to the resolution (INV-37). Once
the user has declined that resolution (#1895), the menu shows Close again
(except while the declined resolution is re-offered), the menu close proposes
CLOSED, and confirming it closes the case with the reason
``resolution_declined``. The "Mark it resolved" chip stays, the model's close
is still refused (chat is unchanged), and a change to the state the decline was
given against (a new confirmation, solution or cause) lapses it and restores
the pivot.

Real ``process_turn`` turns with a stubbed generator: no live LLM.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.core.investigation.milestone_engine.stage_gates import (
    DECLINED_RESOLVE_CARD_LABEL,
)
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    _terminal_confirmation_response,
)
from faultmaven.core.investigation.terminal_transitions import (
    ClosureReadiness,
    closure_verdict,
    confirm_pending_transition,
    declined_resolve_entry,
    deferred_disposition_signature,
    derive_closure_reason,
    derive_disposition_eligibility,
    execute_user_closure,
    propose_transition,
)
from faultmaven.infrastructure.observability import funnel_metrics
from faultmaven.modules.case.contracts import CaseState, MitigationRecord
from faultmaven.modules.case.domain.models.lifecycle import VALID_CLOSURE_REASONS
from faultmaven.modules.case.domain.models.solution import (
    InvestigationActionType,
    ProposedAction,
    Solution,
    SolutionType,
)
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _SR,
    _chip_intent,
    _declined,
    _engine_disconfirmation,
    _labels,
    _offered_then_declined,
    _turn,
    _user_confirmation,
    _user_problem_gone,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _confirmed_case,
)
from tests.unit.infrastructure.observability.test_funnel_metrics import (
    _CREATE_CASES,
    _INSERT_CASE,
    _NOW,
    _refresh_on,
)

pytestmark = pytest.mark.unit

_REASON = "resolution_declined"
_REPLY = (
    "Case closed — the fix was confirmed to work; you chose not to mark it resolved."
)


async def _menu_close(engine, case) -> dict:
    """The status menu's Close, as the copilot sends it: its message text and
    its ``status_transition`` intent (``ChatWindow``, ``CASE_ACTION_MESSAGES``)."""
    return await _turn(
        engine,
        case,
        "Close this case as unresolved. Summarize what we found so far.",
        intent_type="status_transition",
        intent_data={
            "from_state": "investigating",
            "to_state": "closed",
            "user_confirmed": True,
        },
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


def _closed_as(case, reason: str):
    return case.model_copy(update={"state": CaseState.CLOSED, "closure_reason": reason})


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

    async def test_not_beside_the_resolve_the_chip_reopens(self):
        """Close and Resolve are never offered together: while the chip's
        resolve proposal is pending, the menu has no Close; declining it brings
        Close back."""
        engine, case, _ = await _offered_then_declined()
        await _turn(
            engine,
            case,
            "Mark this case resolved.",
            intent_type="status_transition",
            intent_data=_chip_intent(case),
        )
        assert case.pending_transition["to_state"] == "resolved"
        assert declined_resolve_entry(case) is not None, "premise: decline stands"
        assert derive_disposition_eligibility(case)["closed"] == "suggests_alternative"

        await _turn(engine, case, "no")
        assert case.pending_transition is None
        assert derive_disposition_eligibility(case)["closed"] == "ready"


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
        assert result["agent_response"].split("\n")[0] == _REPLY

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

    def test_a_failed_fix_before_the_confirm_drops_the_stored_reason(self):
        """A close proposed under the decline stores ``resolution_declined``.
        A failed fix recorded before the confirm means the fix is no longer
        confirmed, so the close must not say it was."""
        case = _declined_case()
        propose_transition(case, to_state="closed", summary="Close it?")
        assert case.pending_transition["closure_reason"] == _REASON, "premise"
        # After the turn-1 confirmation rows, so it disqualifies them.
        case.evidence.append(_engine_disconfirmation(turn=2))
        assert closure_verdict(case) != ClosureReadiness.SUGGEST_RESOLVE, "premise"

        assert confirm_pending_transition(case, "user_test") is True
        assert case.closure_reason == "solution_deferred"


class TestTheReason:
    def test_it_is_derived_while_the_decline_stands_ahead_of_deferred(self):
        case = _confirmed_case()
        assert derive_closure_reason(case) == "solution_deferred", "premise"
        assert derive_closure_reason(_declined_case()) == _REASON

    def test_the_rest_close_derives_it_too(self):
        case = _declined_case()
        assert execute_user_closure(case, "user_test") == _REASON
        assert case.closure_reason == _REASON

    def test_its_terminal_reply(self):
        assert _terminal_confirmation_response(
            _closed_as(_confirmed_case(), _REASON)
        ) == (_REPLY)

    async def test_its_report_names_the_fix_and_what_confirmed_it(self):
        """Rendered as the resolution summary renders them, not in new prose."""
        case = _declined_case()
        execute_user_closure(case, "user_test")
        svc = object.__new__(ReportGenerationService)
        summary = await svc._generate_closure_summary(case, {})

        resolved = case.model_copy(update={"state": CaseState.RESOLVED})
        resolution = await svc._generate_resolution_summary(resolved, {})
        for heading in ("## Solution Applied\n", "## Confirming Evidence\n"):
            assert heading in resolution, "premise: the resolved report has it"
            start = resolution.index(heading)
            section = resolution[start : resolution.index("\n## ", start + 1)]
            assert section in summary

    async def test_a_mitigated_case_lists_its_solution_once(self):
        """Mitigation Status would repeat the solutions the fix section already
        rendered; a ``mitigation_sufficient`` close of the same case keeps it."""
        case = _confirmed_case()
        case.progress.mitigation = MitigationRecord(
            accepted=True, verified=True, completed_at_turn=1
        )
        svc = object.__new__(ReportGenerationService)
        summary = await svc._generate_closure_summary(_closed_as(case, _REASON), {})
        assert "## Mitigation Status" not in summary
        assert "## Solution Applied" in summary
        assert summary.count("**1. Correct the OIDC provider client ID**") == 1

        control = _closed_as(case, "mitigation_sufficient")
        assert "## Mitigation Status" in await svc._generate_closure_summary(
            control, {}
        )

    async def test_an_unexecuted_fix_is_the_proposal_when_the_case_closed(self):
        case = _confirmed_case()
        fix = "Set the provider ClientIDList to sts.amazonaws.com."
        case.solutions = [
            Solution(
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Correct the OIDC provider client ID",
                immediate_action=fix,
            )
        ]
        case.proposed_actions = [
            ProposedAction(
                case_id=case.case_id,
                action_type=InvestigationActionType.SOLUTION,
                description=fix,
                proposed_in_turn=1,
                state="pending",
            )
        ]
        svc = object.__new__(ReportGenerationService)
        summary = await svc._generate_closure_summary(_closed_as(case, _REASON), {})
        assert "## Proposed Solution" in summary
        assert (
            "_No fix was recorded as executed; this was the standing proposal "
            "when the case closed._"
        ) in summary


class TestEveryReasonTableIsTheVocabulary:
    def test_the_report_labels_and_the_funnel_labels(self):
        assert set(ReportGenerationService._CLOSURE_REASON_LABELS) == (
            VALID_CLOSURE_REASONS
        )
        assert set(funnel_metrics._CLOSED_REASONS) == VALID_CLOSURE_REASONS | {
            "unknown"
        }
        assert set(funnel_metrics._EFFORT_TO_STATES) == (
            VALID_CLOSURE_REASONS - {"inquiry_only"}
        ) | {"resolved"}

    def test_every_reason_has_its_own_terminal_reply(self):
        replies = {
            _terminal_confirmation_response(_closed_as(_confirmed_case(), reason))
            for reason in VALID_CLOSURE_REASONS
        }
        assert len(replies) == len(VALID_CLOSURE_REASONS)
        assert "Case closed." not in replies

    async def test_the_effort_query_admits_every_close_but_inquiry_only(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql(_CREATE_CASES)
                for reason in VALID_CLOSURE_REASONS:
                    closed = _NOW - timedelta(hours=1)
                    await conn.execute(
                        _INSERT_CASE,
                        {
                            "id": reason,
                            "st": "closed",
                            "cr": reason,
                            "t": 3,
                            "c": (closed - timedelta(hours=1)).isoformat(" "),
                            "e": closed.isoformat(" "),
                        },
                    )
            refreshed = await _refresh_on(
                async_sessionmaker(engine, expire_on_commit=False), _NOW
            )
        finally:
            await engine.dispose()
        assert {row[1] for row in refreshed.fetched} == (
            VALID_CLOSURE_REASONS - {"inquiry_only"}
        )
