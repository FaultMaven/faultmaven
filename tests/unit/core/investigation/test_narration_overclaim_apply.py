"""INV-40 apply path — behavioral coverage (closes #983).

The detector (`_narration_overclaim_notice`) has been pinned since #684, but
the APPLY path — compose the corrective notice into the returned reply, then
re-record ``turn_history[-1].agent_response_summary`` via frozen-safe
``model_copy`` (#978) — had zero engine-level coverage. That exact gap is how
the original defect survived: the pre-#978 writes crashed every turn they
were reached while all detector tests stayed green.

These tests drive ``process_turn`` through the real ``_process_turn_impl``
with only the LLM seam stubbed, returning a REAL
``InvestigationResponse_Diagnosis`` — the schema production selects for an
INVESTIGATING case at the default DIAGNOSIS stage (a ``_General`` stand-in
would drift from the dispatch the real turn takes). Assertions use the
imported notice constants, never re-typed fragments, so a wording edit can't
silently detach them from the engine; both notice variants are covered (the
source names the pending shape "the guard's most probable real-world shape").
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    _NARRATION_OVERCLAIM_NOTICE,
    _NARRATION_OVERCLAIM_NOTICE_PENDING,
    MilestoneEngine,
    _narration_asserts_disposition,
)
from faultmaven.core.investigation.schemas import InvestigationResponse_Diagnosis
from faultmaven.modules.case.domain.models import Case, CaseState, InquiryData

pytestmark = pytest.mark.unit

# Must stay in _COMPLETION_PHRASES' narrow scan; the module-scope detector
# guard below turns a phrase-list narrowing into a loud failure here instead
# of a silent false-green on the apply tests.
_OVERCLAIM_PROSE = "Case resolved. The nameserver fix took care of it."
_CLEAN_PROSE = "Let's check the pod events next."


def test_overclaim_prose_still_fires_detector():
    """If _COMPLETION_PHRASES is ever narrowed past this phrase, the apply
    tests below would degrade to vacuous greens — fail loudly here instead."""
    assert _narration_asserts_disposition(_OVERCLAIM_PROSE) is True
    assert _narration_asserts_disposition(_CLEAN_PROSE) is False


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
        inquiry=InquiryData(
            proposed_problem_statement="INV-40 apply-path test",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )
    case.state = CaseState.INVESTIGATING
    # Real callers (investigation_service) increment before handing the case
    # to the engine — a turn-0 record is a shape no production turn has.
    case.current_turn = 3
    return case


def _make_engine(prose: str) -> MilestoneEngine:
    llm = MagicMock()
    # Real strings on the two attributes the turn path reads off the provider:
    # - provider_name feeds the narration_overclaim_total metric label, and
    #   _resolve_chat_provider_name's settings-chain fallback returns a bare
    #   MagicMock for an unspecced mock (verified) — a non-str label.
    #   Deliberately NOT "openai"/"anthropic": those route token estimation to
    #   tiktoken, which downloads its BPE vocab on a cold cache (offline CI).
    # - config.default_model feeds resolve_model_budget, where a MagicMock
    #   makes .startswith() truthy for every registry key.
    llm.provider_name = "test-provider"
    llm.config.default_model = "test-model"
    engine = MilestoneEngine(llm, _make_repo(), investigation_tools=MagicMock())
    engine._generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis.model_validate(
            {"agent_response": prose, "state_updates": {}}
        )
    )
    return engine


@pytest.mark.asyncio
async def test_overclaim_notice_is_appended_to_reply_and_turn_summary():
    engine = _make_engine(_OVERCLAIM_PROSE)
    case = _make_investigating_case()
    assert case.pending_transition is None  # precondition: plain variant fires

    result = await engine.process_turn(
        case=case, user_message="Thanks, everything looks good on my side now."
    )

    # Reply surface: the FULL notice is APPENDED below the preserved prose —
    # composition, never substitution (the DF-4 lesson), via the "---"
    # separator _prose_with_gate_notice owns.
    reply = result["agent_response"]
    assert _OVERCLAIM_PROSE in reply
    assert _NARRATION_OVERCLAIM_NOTICE in reply
    assert reply.index(_OVERCLAIM_PROSE) < reply.index(_NARRATION_OVERCLAIM_NOTICE)
    assert "\n\n---\n\n" in reply

    # Turn-record surface: agent_response_summary reflects the COMPOSED text
    # (this is what the next-turn prompt and turn_outcome heuristics read —
    # the #668 loop breaker). Composed length sits under the 500-char cap, so
    # the full notice must survive.
    summary = result["case_updated"].turn_history[-1].agent_response_summary
    assert summary is not None
    assert _NARRATION_OVERCLAIM_NOTICE in summary


@pytest.mark.asyncio
async def test_overclaim_with_pending_transition_uses_pending_variant():
    """The suggestions-only/pending shape — the source comment's 'most
    probable real-world shape': the SAME turn's LLM response both narrates
    'resolved' and emits proposed_transition (a pre-set pending would be
    withdrawn by the escape lane before the guard runs). Must apply the
    PENDING wording to both surfaces."""
    engine = _make_engine(_OVERCLAIM_PROSE)
    engine._generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis.model_validate(
            {
                "agent_response": _OVERCLAIM_PROSE,
                "state_updates": {"proposed_transition": {"to_state": "resolved"}},
            }
        )
    )
    case = _make_investigating_case()

    result = await engine.process_turn(
        case=case, user_message="Thanks, everything looks good on my side now."
    )

    reply = result["agent_response"]
    assert _NARRATION_OVERCLAIM_NOTICE_PENDING in reply
    assert _NARRATION_OVERCLAIM_NOTICE not in reply
    summary = result["case_updated"].turn_history[-1].agent_response_summary
    assert summary is not None
    assert _NARRATION_OVERCLAIM_NOTICE_PENDING in summary


@pytest.mark.asyncio
async def test_clean_narration_leaves_turn_summary_as_raw_text():
    """Control: no over-claim → no notice on either surface, the summary is
    EXACTLY the raw reply, and the re-record block does not fire (observed
    via the _summarize_text spy: record creation is the only 500-cap call —
    a silent extra re-record on clean turns is the #978 failure class)."""
    engine = _make_engine(_CLEAN_PROSE)
    engine._summarize_text = MagicMock(wraps=engine._summarize_text)
    case = _make_investigating_case()

    result = await engine.process_turn(
        case=case, user_message="What should we look at next?"
    )

    reply = result["agent_response"]
    assert _NARRATION_OVERCLAIM_NOTICE not in reply
    assert _NARRATION_OVERCLAIM_NOTICE_PENDING not in reply
    summary = result["case_updated"].turn_history[-1].agent_response_summary
    assert summary == _CLEAN_PROSE
    agent_summary_calls = [
        c for c in engine._summarize_text.call_args_list if 500 in c.args
    ]
    assert len(agent_summary_calls) == 1, "re-record fired on a clean turn"
