"""The pending-transition gate must never swallow substantive input.

Regression for #656 (case_5db5417fe445, turns 12-13): with a pending CLOSE
proposal armed, typed messages that matched neither the confirm nor the
decline pattern list were answered with a canned re-present of the proposal
— no LLM call, no state change — indefinitely. The user's "I refuse to do
that. you must continue to investigate" and "what is the root cause?" were
swallowed every turn; the case was unrecoverable from the keyboard.

Contract pinned here (the escape lane):

- A message that is not a gate answer (longer than the substantive-length
  bound, or ANY second non-answer) withdraws the pending proposal and the
  turn proceeds to normal processing — the LLM seam is reached.
- A short ambiguous reply ("why?") re-presents the confirmation at most
  ONCE, then the next non-answer withdraws.
- A bare decline still gets the cheap canned acknowledgment; a decline
  carrying substance ("No. we did not do anything yet. …did you see
  anything wrong?") is processed normally after the cancel so its content
  is not lost.

The LLM seam is patched to raise a sentinel: reaching it proves the gate
fell through instead of bricking; not reaching it proves the deterministic
paths still short-circuit.
"""

from datetime import UTC, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    MilestoneEngineError,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    InvestigationProgress,
    ProblemVerification,
)

# The two messages that were swallowed in the live incident.
INCIDENT_REFUSAL = (
    "what are you talking about? you suggested me to prematurely resolve "
    "or close this case. I refuse to do that. you must continue to "
    "investigate into the issue."
)
INCIDENT_QUESTION = (
    "what is the root cause? you have not helped identified the root cause yet"
)


class _SeamReached(Exception):
    """Raised by the patched LLM seam — proves the gate fell through."""


def _make_repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    return repo


def _engine():
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    engine._generate_structured_output = AsyncMock(side_effect=_SeamReached())
    return engine


def _investigating_case_with_pending_close(re_presented: bool = False) -> Case:
    case = Case(
        case_id="case_5db5417fe445",
        title="Escape-lane regression",
        state=CaseState.INQUIRY,
        user_id="user_test",
        organization_id="org_test",
        description="etcdInsufficientMembers alerts",
        problem_verification=ProblemVerification(
            symptom_statement="recurring etcdInsufficientMembers alerts",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = "etcd connectivity"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.inquiry.decided_to_investigate = True
    case.inquiry.decision_made_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.pending_transition = {
        "to_state": "closed",
        "summary": "You can **close** the case instead.",
        "evidence_ids": [],
        "proposed_at": datetime.now(UTC).isoformat(),
    }
    if re_presented:
        case.pending_transition["re_presented"] = True
    return case


async def _run_expecting_fall_through(engine, case, message):
    """The turn must reach the (sentinel-raising) LLM seam, with the
    pending proposal withdrawn before it. The engine wraps turn errors in
    MilestoneEngineError; the sentinel inside proves the seam was hit."""
    with pytest.raises(MilestoneEngineError):
        await engine.process_turn(case=case, user_message=message)
    assert case.pending_transition is None, (
        "the non-answer must withdraw the pending proposal before normal " "processing"
    )
    assert engine._generate_structured_output.called


@pytest.mark.asyncio
async def test_incident_refusal_withdraws_pending_and_reaches_llm():
    engine = _engine()
    case = _investigating_case_with_pending_close()
    await _run_expecting_fall_through(engine, case, INCIDENT_REFUSAL)


@pytest.mark.asyncio
async def test_incident_question_withdraws_pending_and_reaches_llm():
    engine = _engine()
    case = _investigating_case_with_pending_close()
    await _run_expecting_fall_through(engine, case, INCIDENT_QUESTION)


@pytest.mark.asyncio
async def test_short_ambiguous_reply_re_presents_once_without_llm():
    engine = _engine()
    case = _investigating_case_with_pending_close()

    result = await engine.process_turn(case=case, user_message="why?")

    assert "Please select one of the options above" in result["agent_response"]
    assert case.pending_transition is not None
    assert case.pending_transition.get("re_presented") is True
    assert not engine._generate_structured_output.called


@pytest.mark.asyncio
async def test_second_non_answer_withdraws_even_when_short():
    engine = _engine()
    case = _investigating_case_with_pending_close(re_presented=True)
    await _run_expecting_fall_through(engine, case, "hm?")


@pytest.mark.asyncio
async def test_bare_decline_keeps_cheap_canned_acknowledgment():
    engine = _engine()
    case = _investigating_case_with_pending_close()

    result = await engine.process_turn(case=case, user_message="no")

    assert case.pending_transition is None
    assert "remains open" in result["agent_response"]
    assert not engine._generate_structured_output.called


@pytest.mark.asyncio
async def test_decline_with_substance_is_processed_normally():
    """The #656 turn-11-shaped message: starts with a decline token but
    carries the actual question — the content must reach the LLM."""
    engine = _engine()
    case = _investigating_case_with_pending_close()
    await _run_expecting_fall_through(
        engine,
        case,
        "No. we did not do anything yet. I showed the configmap without "
        "modification. did you see anything wrong?",
    )
