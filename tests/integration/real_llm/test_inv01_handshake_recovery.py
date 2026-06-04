"""Real-LLM verification of the INV-01 handshake-deferred recovery flow.

INV-01 (see investigation-lifecycle-logic.md §1.3.1) is enforced
Code-guarded, but its UX depends on a *composition seam*: the engine's
deterministic recovery affordance assumes the prompt instructs the LLM
to re-present the proposed_problem_statement on the recovery turn.

This test pins that prompt-side assumption with a real LLM. Mocked unit
tests cannot catch prompt-compliance regressions by construction — the
mock emits whatever JSON we give it. A real LLM either reads the
HANDSHAKE_DEFERRED block and behaves correctly, or it doesn't.

Failure mode this catches: someone edits the prompt template's
HANDSHAKE_DEFERRED block (in ``context_builder.py:inquiry_state_str``)
in a way that makes the real LLM stop re-presenting on the recovery
turn, or starts auto-confirming. Either drift degrades the User-Agent
Handshake even though all mocked tests pass.

Cost: one LLM call per test (Anthropic Haiku ~$0.001).
"""

from unittest.mock import MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.modules.case.contracts import CaseState

from .helpers import assert_case_status, assert_has_confirmation_suggestions


@pytest.mark.real_llm
@pytest.mark.asyncio
async def test_llm_does_not_auto_confirm_on_recovery_turn(
    real_llm_provider, stub_repo, fresh_inquiry_case
):
    """On the handshake-deferred recovery turn, the LLM must not auto-confirm.

    Setup: case in INQUIRY with a proposed_problem_statement persisted from
    the prior turn (where the same-turn guard fired). ``current_turn=2`` and
    ``handshake_deferred_at_turn=1`` together make the next ``process_turn``
    call a recovery turn — context_builder will inject the HANDSHAKE_DEFERRED
    block instructing the LLM to re-present the statement.

    The user message is a passive engagement; the prompt's instructions are
    what should drive the LLM to re-present without auto-confirming.

    Assertions are outcome-based:
      1. Status stays INQUIRY (no premature transition).
      2. ``problem_statement_confirmed`` stays False (the LLM did not violate
         the HANDSHAKE_DEFERRED "Do NOT set user_confirmed_investigation=True
         this turn" rule).
      3. The engine's deterministic confirmation suggestions are emitted
         (the Code-guarded affordance — INV-01 *Composition seam*).
    """
    case = fresh_inquiry_case
    # Recovery-turn state: guard fired on turn 1, this is turn 2.
    case.current_turn = 2
    case.inquiry.proposed_problem_statement = (
        "SSH brute-force attack on production server LabSZ — 30 source IPs, "
        "970 failed auth attempts."
    )
    case.inquiry.handshake_deferred_at_turn = 1

    engine = MilestoneEngine(
        real_llm_provider,
        stub_repo,
        investigation_tools=MagicMock(),
    )

    result = await engine.process_turn(case, "ok, what now?")
    case_after = result["case_updated"]
    follow_ups = result["suggested_follow_ups"]

    # 1. No premature transition.
    assert_case_status(case_after, CaseState.INQUIRY, context="recovery turn")

    # 2. The LLM honored the HANDSHAKE_DEFERRED "do not auto-confirm" rule.
    # If this fails, the prompt's recovery-turn instruction is being
    # ignored or has drifted — exactly the class of regression that mocked
    # tests cannot surface.
    assert not case_after.inquiry.problem_statement_confirmed, (
        "LLM auto-confirmed on the recovery turn — the HANDSHAKE_DEFERRED "
        "prompt instruction was not honored. Check that "
        "context_builder.py:inquiry_state_str still emits the "
        "'Do NOT set user_confirmed_investigation=True this turn' clause."
    )

    # 3. The Code-guarded confirmation affordance fired regardless of LLM
    # compliance — verifies the deterministic emission at
    # milestone_engine.py:_investigation_confirmation_suggestions.
    assert_has_confirmation_suggestions(follow_ups)
