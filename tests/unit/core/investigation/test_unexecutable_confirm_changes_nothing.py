"""A transition the engine cannot carry out is never offered.

The model may propose INQUIRY → ``investigating``, a legal edge (#2420). It
used to be stored as a pending proposal, and a bare "yes" then confirmed a
target the confirm does not execute; the confirm turn went on to render a
terminal reply for a case that had not moved, which raised once the replies
were keyed by closure reason (#2416). Only a disposition is an offer (ruling 2,
#2379): the proposal opens nothing and the model is told why, and the
investigation starts the one way it does, on the user's confirmation of the
problem statement (Gate 1).

Real ``process_turn`` turns with a stubbed generator: no live LLM.
"""

import pytest

from faultmaven.modules.case.contracts import CaseState
from tests.case_builders import inquiry_case
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _engine,
    _feedback,
    _turn,
)

pytestmark = pytest.mark.unit


async def test_an_investigating_proposal_opens_nothing_and_says_why():
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
    assert case.state is CaseState.INQUIRY
    assert case.offer_record.open is None
    assert "only resolved or closed is offered" in _feedback(proposed)
