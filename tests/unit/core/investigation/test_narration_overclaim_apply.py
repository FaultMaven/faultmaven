"""INV-40 apply path — behavioral coverage (closes #983).

The detector (`_narration_overclaim_notice`) has been pinned since #684, but
the APPLY path — compose the corrective notice into the returned reply, then
re-record ``turn_history[-1].agent_response_summary`` via frozen-safe
``model_copy`` (#978) — had zero engine-level coverage. That exact gap is how
the original defect survived: the pre-#978 writes crashed every turn they
were reached while all detector tests stayed green.

This drives ``process_turn`` through the real ``_process_turn_impl`` with the
LLM seam stubbed to return a REAL ``InvestigationResponse_General`` (never a
mock stand-in — a fake response type would pass a dead gate) whose narration
over-claims disposition on a non-terminal case, and asserts both surfaces:

- the returned ``agent_response`` carries the corrective notice APPENDED
  below the LLM prose (composition, never substitution — the DF-4 lesson);
- the turn record's ``agent_response_summary`` reflects the COMPOSED text,
  so the next-turn prompt and turn_outcome heuristics do not replay the
  model its own uncorrected over-claim (the #668 loop).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import InvestigationResponse_General
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    InvestigationProgress,
    ProblemVerification,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# Fires _narration_asserts_disposition (same phrase the detector tests pin).
_OVERCLAIM_PROSE = "Case resolved. The nameserver fix took care of it."
# Stable fragment of _NARRATION_OVERCLAIM_NOTICE.
_NOTICE_FRAGMENT = "has not been resolved or closed"


def _make_repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    return repo


def _make_investigating_case() -> Case:
    case = Case(
        case_id="case_1a2b3c4d5e6f",
        title="INV-40 apply-path test",
        state=CaseState.INQUIRY,
        user_id="user_test",
        organization_id="org_test",
        description="INV-40 apply-path test description",
        problem_verification=ProblemVerification(
            symptom_statement="Pods crashlooping",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = "INV-40 apply-path test"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
    case.inquiry.decided_to_investigate = True
    case.inquiry.decision_made_at = datetime.now(timezone.utc)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    return case


def _make_engine() -> MilestoneEngine:
    llm = MagicMock()
    # Real string: the metric label (narration_overclaim_total) requires one,
    # and a bare MagicMock would leak through _resolve_chat_provider_name.
    llm.provider_name = "test-provider"
    engine = MilestoneEngine(llm, _make_repo(), investigation_tools=MagicMock())
    engine._generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_General.model_validate(
            {"agent_response": _OVERCLAIM_PROSE, "state_updates": {}}
        )
    )
    return engine


async def test_overclaim_notice_is_appended_to_reply_and_turn_summary():
    engine = _make_engine()
    case = _make_investigating_case()

    result = await engine.process_turn(
        case=case, user_message="Thanks, everything looks good on my side now."
    )

    # Reply surface: notice APPENDED below the preserved LLM prose.
    reply = result["agent_response"]
    assert reply.startswith("Case resolved.")
    assert _NOTICE_FRAGMENT in reply

    # Turn-record surface: agent_response_summary reflects the COMPOSED text.
    updated = result["case_updated"]
    assert updated.turn_history, "turn record was not created"
    summary = updated.turn_history[-1].agent_response_summary or ""
    assert _NOTICE_FRAGMENT in summary
    # And the case stayed non-terminal — the notice corrected, not transitioned.
    assert updated.state is CaseState.INVESTIGATING


async def test_clean_narration_leaves_turn_summary_as_raw_text():
    """Control: no over-claim → no notice, and the summary is the raw reply
    (the re-record block must not fire when composition changed nothing)."""
    engine = _make_engine()
    engine._generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_General.model_validate(
            {
                "agent_response": "Let's check the pod events next.",
                "state_updates": {},
            }
        )
    )
    case = _make_investigating_case()

    result = await engine.process_turn(
        case=case, user_message="What should we look at next?"
    )

    assert _NOTICE_FRAGMENT not in result["agent_response"]
    summary = result["case_updated"].turn_history[-1].agent_response_summary or ""
    assert _NOTICE_FRAGMENT not in summary
    assert "pod events" in summary
