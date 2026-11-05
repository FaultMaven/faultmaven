"""#2482: the runbook-fix confirmation path is deleted (ruling (a),
2026-11-03).

Its INQUIRY schema field, both writers, the INVESTIGATING branch and the
case's record are gone. A model that still sends the old field, and a stored
case that still carries the old record, are tolerated: the INQUIRY schema and
``InquiryData`` ignore keys they do not declare (``extra='ignore'``), so the
stray key is dropped rather than refused. No LLM.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.schemas import InquiryResponse
from faultmaven.modules.case.contracts import Case, CaseState
from tests.case_builders import inquiry_case

pytestmark = pytest.mark.unit

_OLD_KEY = "knowledge_resolution"
_OLD_RECORD = {
    "match_id": "pg-connection-pool-exhaustion",
    "match_type": "runbook",
    "solution_applied": "Raised max_connections to 200",
    "user_confirmation": "That fixed it",
}


def _engine() -> MilestoneEngine:
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    return MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())


async def test_an_inquiry_output_carrying_the_old_field_is_applied():
    """Before #2482 the INQUIRY writer built the case's record from this
    field without the ``resolution_turn`` the record requires, so the turn
    failed in post-processing with ``ValidationError: resolution_turn``."""
    response = InquiryResponse.model_validate(
        {
            "agent_response": "Glad the runbook fix worked.",
            "state_updates": {
                "proposed_problem_statement": "Checkout returns 503 since 09:00.",
                _OLD_KEY: _OLD_RECORD,
            },
        }
    )
    case = inquiry_case()

    updated, _ = await _engine().responses.process_response_structured(
        case, "That fixed it", response
    )

    assert _OLD_KEY not in response.state_updates.model_dump()
    assert updated.state == CaseState.INQUIRY
    # The rest of the output was applied.
    assert updated.inquiry.proposed_problem_statement == (
        "Checkout returns 503 since 09:00."
    )
    assert _OLD_KEY not in updated.inquiry.model_dump()


def test_a_stored_case_carrying_the_old_record_loads():
    """The load is a forward pin: it also succeeds before #2482, where the
    record was a declared field. With the field deleted, a case stored with
    it still loads, and the key is dropped."""
    stored = inquiry_case(statement="Checkout returns 503").model_dump(mode="json")
    stored["inquiry"][_OLD_KEY] = {**_OLD_RECORD, "resolution_turn": 3}

    loaded = Case.model_validate(stored)

    assert loaded.inquiry.proposed_problem_statement == "Checkout returns 503"
    assert _OLD_KEY not in loaded.inquiry.model_dump()
