"""Real-LLM verification of what INV-01 still asks of the prompt.

Presentation used to be the LLM's job, and this file used to pin that: the
engine set ``handshake_deferred_at_turn``, ``context_builder`` injected a
``HANDSHAKE_DEFERRED`` block telling the model to re-present the statement,
and a real LLM either read it or did not.

#1607 moved presentation into the engine, so that assumption no longer
exists to pin — the statement is composed into every Gate-1-pending turn
whatever the model does. What is left prompt-dependent is the *opposite*
instruction, and it is the one worth a real call: the model must NOT write
the statement out itself, or the user is shown it twice and asked twice on
the same turn.

Mocked tests cannot catch this by construction — the mock emits whatever
JSON we give it. A real LLM either honours ``ENGINE_PRESENTS_THIS`` or it
does not.

Cost: one LLM call per test (Anthropic Haiku ~$0.001).
"""

from unittest.mock import MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.modules.case.contracts import CaseState

from .helpers import assert_case_status, assert_has_confirmation_suggestions

STATEMENT = (
    "SSH brute-force attack on production server LabSZ — 30 source IPs, "
    "970 failed auth attempts."
)


@pytest.mark.real_llm
@pytest.mark.asyncio
async def test_llm_neither_restates_nor_auto_confirms_on_a_pending_turn(
    real_llm_provider, stub_repo, fresh_inquiry_case
):
    """On a Gate-1-pending turn the model leaves presentation to the engine.

    Setup: INQUIRY, a statement standing unconfirmed from an earlier turn, and
    a passive user message. ``context_builder`` injects ``ENGINE_PRESENTS_THIS``.

    Outcome-based assertions:
      1. Status stays INQUIRY — no premature transition.
      2. ``problem_statement_confirmed`` stays False. A passive "ok, what now?"
         is not consent, and the model must not read it as such.
      3. The engine's statement and its confirm/refine pair are both present
         (structural — true regardless of what the model did).
      4. The statement appears exactly ONCE. Twice means the model restated it
         alongside the engine's copy, which is the drift this file exists for.
    """
    case = fresh_inquiry_case
    case.current_turn = 2
    case.inquiry.proposed_problem_statement = STATEMENT

    engine = MilestoneEngine(
        real_llm_provider,
        stub_repo,
        investigation_tools=MagicMock(),
    )

    result = await engine.process_turn(case, "ok, what now?")
    case_after = result["case_updated"]
    reply = result["agent_response"]

    assert_case_status(case_after, CaseState.INQUIRY, context="pending Gate 1 turn")

    assert not case_after.inquiry.problem_statement_confirmed, (
        "LLM confirmed Gate 1 off a passive message. Check that the INQUIRY "
        "template still says a correction or continued engagement is NOT "
        "confirmation."
    )

    # Structural: the engine composed its presentation and its pair.
    assert STATEMENT in reply, (
        "the engine did not compose the standing statement into a "
        "Gate-1-pending turn — this is engine-owned and should be impossible "
        "to reach through prompt drift."
    )
    assert_has_confirmation_suggestions(result["suggested_follow_ups"])

    # Prompt-dependent: exactly one copy, the engine's.
    assert reply.count(STATEMENT) == 1, (
        f"the problem statement appears {reply.count(STATEMENT)} times — the "
        "model restated it alongside the engine's copy, so the user is shown "
        "it twice. Check that context_builder still emits ENGINE_PRESENTS_THIS "
        "and that the INQUIRY template still tells the model not to present."
    )
