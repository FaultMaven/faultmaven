"""A model proposal dropped for an engine offer that lapsed later in the cycle
(#2636 B3).

Step 2 drops the model's ``proposed_transition`` while the engine's offer is
open (INV-43, #1885) and records ``ENGINE_OFFER_STANDS``. The narration step
reads the offer record: while the offer stands, the existing text and
``transition_superseded_by_engine``; once it lapsed, the ruled lapsed text with
the MODEL's target and ``transition_dropped_for_lapsed_offer``. No step lapses
an offer after step 2 on ``main`` (B1e adds one), so these whole turns inject
the lapse just before the narration step, with the real ``lapse_offers`` on a
false-alarm close whose finding the user withdrew. No live LLM: the generator
is stubbed.
"""

from unittest.mock import patch

import pytest

from faultmaven.core.investigation.milestone_engine import (
    turn_completion,
    turn_narration,
)
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.core.investigation.narration import codes as narration_codes
from faultmaven.core.investigation.narration.codes import DECISIONS_KEY, NoticeCode
from faultmaven.core.investigation.narration.notices import render_notice
from faultmaven.core.investigation.offers import answer_offer, lapse_offers
from faultmaven.core.investigation.problem_status import withdraw_invalidation
from faultmaven.modules.case.contracts import AnswerKind, AnswerVia
from tests.unit.core.investigation import (
    test_problem_statement_revision_and_false_alarm as revision,
)

pytestmark = pytest.mark.unit

#: The ruled text (#2636, ruling of 2026-11-12), the model having proposed
#: ``resolved`` beside the engine's false-alarm close.
LAPSED = (
    "TRANSITION NOT PROPOSED: your 'resolved' proposal was not opened, because "
    "the engine had already opened its own offer this turn. That offer then "
    "lapsed before the user saw it, because what it rested on no longer holds. "
    "Propose a transition again on a later turn if one is still earned."
)
STANDING = "TRANSITION NOT PROPOSED: the engine already offered the user a 'closed'"


def _withdraw_the_finding(case) -> None:
    assert withdraw_invalidation(case, basis="the errors were in another region")
    case.offer_record = lapse_offers(case)


def _withdraw_the_offer(case) -> None:
    case.offer_record = answer_offer(
        case,
        proposal_id=case.offer_record.open.proposal_id,
        answer=AnswerKind.WITHDRAWN,
        via=AnswerVia.ENGINE,
    )


async def _turn(monkeypatch, before_narration=None) -> tuple:
    """The finding turn, the model proposing ``resolved`` beside the engine's
    false-alarm close: the decision step 2 logged, the ``transition_compliance``
    record, and the notices the model reads next turn."""
    decided: list = []
    real = turn_completion.narrate_turn

    def _narrate(case, metadata):
        decided.extend(
            dict(e["params"])
            for e in metadata.get(DECISIONS_KEY) or []
            if e["code"] == NoticeCode.ENGINE_OFFER_STANDS
        )
        if before_narration is not None:
            before_narration(case)
        return real(case, metadata)

    monkeypatch.setattr(turn_completion, "narrate_turn", _narrate)
    engine, case = revision._engine(), revision._case()
    revision._respond(engine, revision._finding(model_proposes="resolved"))
    with patch.object(turn_completion.logger, "info") as info:
        result = await engine.process_turn(
            case=case,
            user_message="the gateway log shows no errors at 14:00",
            redaction_ctx=None,
        )
    (extra,) = [
        c.kwargs["extra"]
        for c in info.call_args_list
        if c.args == ("transition_compliance",)
    ]
    notices = result["case_updated"].turn_history[-1].system_notices
    return decided, extra, notices


async def test_a_lapsed_offer_reports_the_models_proposal_not_opened(monkeypatch):
    (params,), extra, notices = await _turn(monkeypatch, _withdraw_the_finding)
    # The model's target names its proposal; the engine's lapsed close does not.
    assert (params["proposed_to_state"], params["to_state"]) == ("resolved", "closed")
    assert LAPSED in notices
    dropped = [n for n in notices if n.startswith("TRANSITION NOT PROPOSED")]
    assert dropped == [LAPSED]
    assert "'closed'" not in LAPSED
    assert extra["llm_proposed_to_status"] == "resolved"
    assert extra["transition_dropped_for_lapsed_offer"] is True
    assert extra["transition_superseded_by_engine"] is False
    assert extra["transition_pivoted"] is False


async def test_a_standing_offer_reports_the_proposal_superseded(monkeypatch):
    (params,), extra, notices = await _turn(monkeypatch)
    assert params["proposed_to_state"] == "resolved"
    assert "stands" not in params  # the log entry is never written to
    assert LAPSED not in notices
    assert any(n.startswith(STANDING) for n in notices)
    assert extra["transition_superseded_by_engine"] is True
    assert extra["transition_dropped_for_lapsed_offer"] is False


async def test_an_offer_closed_otherwise_fails_the_suite(monkeypatch):
    """Answered, withdrawn or replaced is not lapsed: unreachable after step 2,
    so a defect, which the suite raises on (``TOKENS_FAIL``); the engine
    wraps it as the turn's failure."""
    with pytest.raises(MilestoneEngineError, match="closed after the drop, not lapsed"):
        await _turn(monkeypatch, _withdraw_the_offer)


async def test_an_offer_closed_otherwise_is_logged_in_production(monkeypatch):
    """Production logs it and keeps the standing text and flags: no lapsed
    text for an offer that did not lapse."""
    monkeypatch.setattr(narration_codes, "TOKENS_FAIL", False)
    with patch.object(turn_narration.logger, "error") as error:
        _, extra, notices = await _turn(monkeypatch, _withdraw_the_offer)
    assert "not lapsed" in error.call_args.args[2]
    assert LAPSED not in notices
    assert any(n.startswith(STANDING) for n in notices)
    assert extra["transition_superseded_by_engine"] is True
    assert extra["transition_dropped_for_lapsed_offer"] is False


def test_absent_stands_renders_the_standing_text():
    params = {"proposal_id": "p1", "to_state": "closed", "proposed_to_state": "x"}
    assert render_notice(
        NoticeCode.ENGINE_OFFER_STANDS, params, None, []
    ) == render_notice(
        NoticeCode.ENGINE_OFFER_STANDS, {**params, "stands": True}, None, []
    )
