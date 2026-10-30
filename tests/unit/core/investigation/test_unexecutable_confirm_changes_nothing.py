"""A confirmed proposal the engine cannot carry out ends a turn that changes nothing.

The model may propose INQUIRY → ``investigating``, and the proposal is stored
(#2420). A bare "yes" then confirms a target ``confirm_pending_transition`` does
not execute: it clears the pending and reports that nothing ran. The confirm
turn went on to render a terminal reply for a case that had not moved, which
raised once the replies were keyed by closure reason (#2416).

Real ``process_turn`` turns with a stubbed generator: no live LLM.
"""

import pytest

from faultmaven.core.investigation.milestone_engine.transition_turns import (
    UNCARRIED_PROPOSAL_REPLY,
)
from faultmaven.modules.case.contracts import CaseState
from tests.case_builders import inquiry_case
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _engine,
    _turn,
)

pytestmark = pytest.mark.unit


async def test_yes_to_an_investigating_proposal_keeps_the_case_and_clears_it():
    engine = _engine()
    case = inquiry_case(
        case_id="case_c0bbeef0a514",
        title="Checkout 502s",
        user_id="user_test",
        enterprise_id="org_test",
        statement="Checkout returns 502",
    )
    proposed = await _turn(engine, case, "let's investigate", propose="investigating")
    case = proposed["case_updated"]
    assert case.pending_transition["to_state"] == "investigating", "premise"

    result = await _turn(engine, case, "yes")

    after = result["case_updated"]
    assert after.state is CaseState.INQUIRY
    assert after.pending_transition is None
    assert result["agent_response"] == UNCARRIED_PROPOSAL_REPLY
    assert after.turn_history[-1].progress_made is False
