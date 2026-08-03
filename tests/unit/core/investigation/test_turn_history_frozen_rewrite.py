"""Regression: turn_history records never carry (or accept) the chat reply.

The closure-summary append (#283) and the INV-40 over-claim correction (#684)
both did ``turn_history[-1].agent_response = <composed text>`` before saving —
a write that could never work twice over: ``TurnProgress`` is frozen (the
assignment raises ``ValidationError``, 500-ing the turn — live-hit 2026-08-03,
sim turn 14 when the INV-40 notice fired), and ``agent_response`` is not a
``TurnProgress`` field anyway (the frozen check fires first and masks that), so
even an unfrozen write would have been dropped by ``model_dump`` on save. The
composed reply reaches chat through the engine's returned ``agent_response``,
which ``investigation_service.process_turn`` step 4 appends to
``case.messages`` and persists. The engine-side writes were deleted; the
composed reply is instead reflected into the record's real summary channel
(``agent_response_summary``) by frozen-safe replacement (``model_copy``), so
the next-turn prompt and turn_outcome heuristics see the corrected text.
These tests pin the two contracts that made the original writes wrong.
"""

import pydantic
import pytest

from faultmaven.modules.case.domain.models import TurnOutcome, TurnProgress

pytestmark = pytest.mark.unit


def _turn(n: int = 1) -> TurnProgress:
    return TurnProgress(
        turn_number=n,
        outcome=TurnOutcome.CONVERSATION,
        progress_made=False,
    )


def test_turn_progress_rejects_attribute_assignment():
    turn = _turn()
    with pytest.raises(pydantic.ValidationError):
        turn.agent_response = "composed chat reply"


def test_chat_reply_is_not_a_turn_progress_field():
    # The chat transcript lives on ``case.messages`` (persisted by the
    # investigation service); turn_history carries only a summary.
    assert "agent_response" not in TurnProgress.model_fields
    assert "agent_response_summary" in TurnProgress.model_fields
