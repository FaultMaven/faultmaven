"""Regression: the resolution gate's CLOSE-escape must not be clobbered.

project-resolution-gate-stuck-loop (Run 36, case_95d86b7daf8c): a case
with a root cause but no Solution record returns NEEDS_INFO. On the repeat
turn the handshake block correctly pivots to proposing CLOSE — but the LLM
re-proposes RESOLVED every turn the user confirms, and the LLM-proposal
block's ``propose_transition`` overwrote that CLOSE with a fresh
RESOLVED+needs_info, looping forever to max_turns.

Fix: the engine's same-turn offer stands (INV-43, #1885). When the handshake
block has already pivoted to CLOSE this turn (``transition_proposed_this_turn``
with the CLOSE pending), the LLM's same-turn ``proposed_transition`` is ignored
so it can't clobber the escape.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation.milestone_engine import turn_completion
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.schemas import (
    EvidenceToAdd,
    InvestigationResponse_Diagnosis,
    ProposedTransition,
)
from faultmaven.modules.case.contracts import ProblemStatus
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.conclusion import (
    ConfidenceLevel,
    RootCauseConclusion,
)
from faultmaven.modules.case.domain.models.evidence import (
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
)
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.problem import ProblemVerification
from faultmaven.modules.case.domain.models.progress import InvestigationProgress
from faultmaven.modules.case.domain.models.solution import Solution, SolutionType


def _make_repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    return repo


def _engine():
    return MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())


def _needs_info_case() -> Case:
    """INVESTIGATING, root cause present, evidence present, NO Solution
    record → assess_resolution_readiness == NEEDS_INFO (missing 'solution')."""
    case = Case(
        case_id="case_95d86b7daf8c",
        title="Close-escape regression",
        state=CaseState.INQUIRY,
        user_id="user_test",
        enterprise_id="org_test",
        description="ES fielddata latency",
        problem_verification=ProblemVerification(
            symptom_statement="p99 latency on events-*",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = "latency"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.progress.problem_status = ProblemStatus.VERIFIED
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="missing index on audit_events(created_at)",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        mechanism="full scans exhaust the connection pool",
    )
    case.evidence.append(
        Evidence(
            summary="symptom row",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_at=datetime.now(timezone.utc),
            collected_by="user_test",
            primary_purpose="regression",
            preprocessed_content="x",
            content_size_bytes=10,
            preprocessing_method="manual",
            source_file_id=None,
            collected_at_turn=1,
        )
    )
    return case


def _llm_proposes_resolved():
    fake = MagicMock()
    fake.state_updates.proposed_transition = MagicMock(
        to_state="resolved", evidence_ids=[]
    )
    return {"response_obj": fake}


@pytest.mark.asyncio
async def test_repeat_needs_info_escapes_to_close_not_clobbered():
    """The loop bug: case already asked once (pending resolved+needs_info),
    user confirms again (LLM re-proposes RESOLVED). The escape must win →
    pending_transition becomes CLOSED, NOT re-armed as RESOLVED."""
    engine = _engine()
    case = _needs_info_case()
    # Simulate turn N+1: we already asked for the solution last turn.
    case.pending_transition = {
        "to_state": "resolved",
        "summary": "Before I can mark this as resolved, I need a bit more detail…",
        "evidence_ids": [],
        "proposed_at": datetime.now(timezone.utc).isoformat(),
        "needs_info": True,
    }
    metadata = _llm_proposes_resolved()

    await engine.transitions.check_automatic_transitions(
        case=case, metadata=metadata, user_message="yes, it's resolved"
    )

    # Escape fired and was NOT clobbered by the LLM's re-proposed RESOLVED.
    assert case.pending_transition is not None
    assert case.pending_transition["to_state"] == "closed", (
        "Repeat resolution NEEDS_INFO must escape to CLOSE; the LLM's "
        "same-turn RESOLVED re-proposal clobbered the pivot (the stuck loop)."
    )
    assert metadata.get("resolution_suggest_close") is True


@pytest.mark.asyncio
async def test_first_needs_info_stays_resolved_not_prematurely_closed():
    """Control: on the FIRST ask (no prior needs_info pending), the gate
    asks for the solution (RESOLVED + needs_info) — it must NOT jump
    straight to CLOSE."""
    engine = _engine()
    case = _needs_info_case()  # no pending_transition yet
    metadata = _llm_proposes_resolved()

    await engine.transitions.check_automatic_transitions(
        case=case, metadata=metadata, user_message="mark resolved"
    )

    assert case.pending_transition["to_state"] == "resolved"
    assert case.pending_transition.get("needs_info") is True
    assert not metadata.get("resolution_suggest_close")


@pytest.mark.asyncio
async def test_ready_case_resolves_guard_does_not_interfere():
    """Control: a genuinely-ready case (root cause + Solution record) still
    proposes RESOLVED — the close-escape guard only fires on the pivot."""
    engine = _engine()
    case = _needs_info_case()
    case.solutions.append(
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Create the index",
            longterm_fix="CREATE INDEX ...",
        )
    )
    metadata = _llm_proposes_resolved()

    await engine.transitions.check_automatic_transitions(
        case=case, metadata=metadata, user_message="mark resolved"
    )

    assert case.pending_transition["to_state"] == "resolved"
    assert not metadata.get("resolution_suggest_close")


@pytest.mark.asyncio
async def test_readiness_verdict_recorded_for_transition_compliance():
    """The readiness verdict + missing list must land in turn metadata so
    the transition_compliance log line explains WHY a proposed transition
    did not transition (a pending handshake previously read as a silent
    gate refusal — #656 triage)."""
    engine = _engine()
    case = _needs_info_case()
    metadata = _llm_proposes_resolved()

    await engine.transitions.check_automatic_transitions(
        case=case, metadata=metadata, user_message="mark resolved"
    )

    assert metadata.get("resolution_readiness_verdict") == "needs_info"
    assert "solution" in (metadata.get("resolution_readiness_missing") or [])


@pytest.mark.asyncio
async def test_readiness_verdict_recorded_on_needs_info_recheck():
    """The needs_info re-check path (second pass) records the verdict too."""
    engine = _engine()
    case = _needs_info_case()
    case.pending_transition = {
        "to_state": "resolved",
        "summary": "Before I can mark this as resolved…",
        "evidence_ids": [],
        "proposed_at": datetime.now(timezone.utc).isoformat(),
        "needs_info": True,
    }
    metadata = {
        "response_obj": MagicMock(state_updates=MagicMock(proposed_transition=None))
    }

    await engine.transitions.check_automatic_transitions(
        case=case, metadata=metadata, user_message="I don't have a solution"
    )

    assert metadata.get("resolution_readiness_verdict") == "needs_info"
    assert metadata.get("resolution_readiness_missing")


@pytest.mark.asyncio
async def test_the_flag_alone_with_nothing_pending_supersedes_nothing():
    """The same-turn rule needs an offer STANDING, not just the flag (#1885):
    a flag with nothing pending lets the model's proposal land."""
    engine = _engine()
    case = _needs_info_case()
    metadata = _llm_proposes_resolved()
    metadata["transition_proposed_this_turn"] = True

    await engine.transitions.check_automatic_transitions(
        case=case, metadata=metadata, user_message="mark resolved"
    )

    assert case.pending_transition["to_state"] == "resolved"
    assert not metadata.get("transition_superseded_by_engine")


def _compliance(info) -> dict:
    (extra,) = [
        c.kwargs["extra"]
        for c in info.call_args_list
        if c.args == ("transition_compliance",)
    ]
    return extra


@pytest.mark.asyncio
@pytest.mark.parametrize("model_proposes", ["resolved", "closed"])
async def test_steps_zero_ready_reproposal_stands_on_a_real_turn(model_proposes):
    """Step 0's needs_info -> READY re-proposal is an engine offer like any
    other: the model's same-turn proposal is dropped (and said to be), not
    pivoted. Real ``process_turn``, stubbed generator."""
    engine = _engine()
    engine.kb_prefetcher.prefetch_kb_context = AsyncMock(return_value=None)
    case = _needs_info_case()
    case.current_turn = 3
    case.pending_transition = {
        "to_state": "resolved",
        "summary": "Before I can mark this as resolved, I need a bit more detail…",
        "evidence_ids": [],
        "proposed_at": datetime.now(timezone.utc).isoformat(),
        "needs_info": True,
    }
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Good, the index removed the full scans.",
            state_updates={
                "evidence_to_add": [
                    EvidenceToAdd(
                        summary="p99 back to 40ms since the index was created",
                        extract="events-* p99: 40ms after CREATE INDEX at 10:02",
                        category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
                        source_type=EvidenceSourceType.USER_DESCRIPTION,
                    )
                ],
                "proposed_transition": ProposedTransition(to_state=model_proposes),
            },
        )
    )

    with patch.object(turn_completion.logger, "info") as info:
        result = await engine.process_turn(
            case=case,
            user_message="We created the index and p99 is back to 40ms",
        )

    pending = result["case_updated"].pending_transition
    assert pending["to_state"] == "resolved"
    assert not pending.get("needs_info")
    extra = _compliance(info)
    assert extra["transition_superseded_by_engine"] is True
    assert extra["transition_pivoted"] is False
    feedback = result["case_updated"].turn_history[-1].system_feedback or ""
    assert "the engine already offered the user a 'resolved' transition" in feedback
