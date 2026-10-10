"""Code that records an ACTOR records the actor, not the creator (ADR-020 D6).

Only the case's effective driver may submit a turn, so on any turn the driver
is who acted. Each site here used to read ``case.user_id`` — the creator — and
would have credited the creator with the driver's work once the two differ:

- evidence ``collected_by`` (``response_application``);
- the confirming user of a pending terminal transition, both on the engine's
  0b confirm (``transition_turns``) and on step 0 of
  ``check_automatic_transitions`` (``transitions``) — the action history's
  ``triggered_by``;
- the ``solution_applied_by`` fallback on the resolution view
  (``case_ui_adapter``).

What stays with the creator (the runbook destination, the title sequence, the
active-case limit, the Slack auto-share, the billing stamp) is not exercised
here: those describe the case, not the act.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from faultmaven.core.investigation.milestone_engine.transition_consent import (
    terminal_offer_key,
)
from faultmaven.modules.case.contracts import CaseState, EvidenceCategory
from tests.unit.core.investigation.test_cause_work_requires_verified_problem import (
    _DSU,
    _apply,
    _row,
)
from tests.unit.core.investigation.test_cause_work_requires_verified_problem import (
    _case as _verified_case,
)
from tests.unit.core.investigation.test_cause_work_requires_verified_problem import (
    _engine as _bare_engine,
)
from tests.unit.core.investigation.test_every_card_names_its_offer_1812 import (
    _engine,
    _pending,
)

pytestmark = [pytest.mark.unit]

CREATOR = "user_1812"
DRIVER = "user_driver_1898"


def _closed_by(case) -> str:
    closures = [a for a in case.action_history if a.to_state == CaseState.CLOSED]
    assert closures, "premise: the case closed"
    return closures[-1].triggered_by


async def test_a_confirmed_close_credits_the_driver_on_the_engine_confirm():
    case = _pending("closed")
    case.driver_id = DRIVER
    assert case.user_id == CREATOR

    await _engine().process_turn(
        case=case,
        user_message="Yes, close it.",
        intent_type="confirmation",
        intent_data={
            "value": True,
            "proposal_id": terminal_offer_key(case.pending_transition),
        },
        user_id=DRIVER,
    )

    assert case.state == CaseState.CLOSED
    assert _closed_by(case) == DRIVER


async def test_a_typed_yes_credits_the_driver_on_the_automatic_transition():
    """Step 0 of ``check_automatic_transitions``, reached by a direct call
    (0b answers every pending before the model runs on a live turn)."""
    case = _pending("closed")
    case.driver_id = DRIVER

    await _engine().transitions.check_automatic_transitions(case, {}, "yes")

    assert case.state == CaseState.CLOSED
    assert _closed_by(case) == DRIVER


async def test_evidence_the_driver_supplies_is_collected_by_the_driver():
    from faultmaven.modules.case.contracts import ProblemStatus

    eng, case = _bare_engine(), _verified_case(ProblemStatus.VERIFIED)
    case.driver_id = DRIVER

    await _apply(
        eng,
        case,
        _DSU(evidence_to_add=[_row(EvidenceCategory.SYMPTOM_EVIDENCE, "d1")]),
    )

    assert [ev.collected_by for ev in case.evidence] == [DRIVER]


@pytest.mark.parametrize(
    "applied_by, expected",
    [(None, DRIVER), ("user_who_applied", "user_who_applied"), ("none", DRIVER)],
    ids=[
        "unrecorded-falls-back-to-the-driver",
        "recorded-is-kept",
        "no-applied-solution-falls-back-to-the-driver",
    ],
)
def test_the_resolution_view_falls_back_to_the_driver(applied_by, expected):
    from faultmaven.modules.case.contracts import Solution, SolutionType
    from faultmaven.modules.case.domain.services.case_ui_adapter import (
        transform_case_for_ui,
    )
    from tests.unit.modules.case.domain.services.test_case_ui_adapter import (
        _make_resolved_case,
    )

    solutions = (
        []
        if applied_by == "none"
        else [
            Solution(
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Restore the checkout pool size",
                longterm_fix="Set max connections back to 50.",
                applied_at=datetime.now(UTC),
                applied_by=applied_by,
            )
        ]
    )
    case = _make_resolved_case(driver_id=DRIVER, solutions=solutions)
    assert case.user_id != DRIVER

    view = transform_case_for_ui(case)

    assert view.solution_applied.applied_by == expected
