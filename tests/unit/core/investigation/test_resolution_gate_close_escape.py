"""Regression: the resolution gate's CLOSE-escape must not be clobbered.

project-resolution-gate-stuck-loop (Run 36, case_95d86b7daf8c): a case
with a root cause but no Solution record returns NEEDS_INFO. On the repeat
turn the handshake block correctly pivots to proposing CLOSE — but the LLM
re-proposes RESOLVED every turn the user confirms, and the LLM-proposal
block's ``propose_transition`` overwrote that CLOSE with a fresh
RESOLVED+needs_info, looping forever to max_turns.

Fix: the engine's same-turn offer stands (INV-43, #1885). When the request's
re-check has already opened the CLOSE this turn, the LLM's same-turn
``proposed_transition`` is ignored (the record holds one offer), so it can't
clobber the escape. Since #2408 the first ask is a remembered request, not a
RESOLVED offer: no chips, and the next turn re-checks it.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation.milestone_engine import turn_completion
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.turn_commit import TurnCommitPlan
from faultmaven.core.investigation.offers import remember_resolve_request
from faultmaven.core.investigation.schemas import (
    EvidenceToAdd,
    InvestigationResponse_Diagnosis,
    ProposedTransition,
)
from faultmaven.modules.case.contracts import ProblemStatus, UploadedFile
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

_FILE_ID = "file_0123456789ab"


def _make_repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
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
    case.uploaded_files.append(
        UploadedFile(
            file_id=_FILE_ID, filename="es.log", size_bytes=512, uploaded_at_turn=1
        )
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
            source_type=EvidenceSourceType.LOGS,
            collected_at=datetime.now(timezone.utc),
            collected_by="user_test",
            primary_purpose="regression",
            preprocessed_content="x",
            content_size_bytes=10,
            preprocessing_method="manual",
            source_file_id="file_0123456789ab",
            collected_at_turn=1,
        )
    )
    return case


def _asked_last_turn(case: Case) -> None:
    """The previous turn asked for what the resolution still needs."""
    case.offer_record = remember_resolve_request(case, ["solution"])
    case.current_turn += 1


def _llm_proposes_resolved():
    fake = MagicMock()
    fake.state_updates.proposed_transition = MagicMock(
        to_state="resolved", evidence_ids=[]
    )
    return {"response_obj": fake}


@pytest.mark.asyncio
async def test_repeat_needs_info_escapes_to_close_not_clobbered():
    """The loop bug: case already asked once, user confirms again (LLM
    re-proposes RESOLVED). The escape must win → a CLOSE offer, NOT a
    re-armed RESOLVED."""
    engine = _engine()
    case = _needs_info_case()
    # Turn N+1: we already asked for the solution last turn.
    _asked_last_turn(case)
    metadata = _llm_proposes_resolved()

    await engine.transitions.check_automatic_transitions(
        case=case,
        metadata=metadata,
        user_message="yes, it's resolved",
    )

    # Escape fired and was NOT clobbered by the LLM's re-proposed RESOLVED.
    assert case.offer_record.open is not None
    assert case.offer_record.open.kind == "close", (
        "Repeat resolution NEEDS_INFO must escape to CLOSE; the LLM's "
        "same-turn RESOLVED re-proposal clobbered the pivot (the stuck loop)."
    )
    assert metadata.get("resolution_suggest_close") is True


@pytest.mark.asyncio
async def test_first_ask_is_a_request_not_a_premature_close():
    """Control: on the FIRST ask (no request remembered), the gate asks for
    the solution with no offer and no chips (#2408) — it must NOT jump
    straight to CLOSE."""
    engine = _engine()
    case = _needs_info_case()  # nothing asked yet
    metadata = _llm_proposes_resolved()

    await engine.transitions.check_automatic_transitions(
        case=case,
        metadata=metadata,
        user_message="mark resolved",
    )

    assert case.offer_record.open is None
    assert "solution" in case.offer_record.resolve_request.missing
    assert metadata.get("resolution_needs_info_first_pass") is True
    assert not metadata.get("resolution_suggest_close")


@pytest.mark.asyncio
async def test_ready_case_resolves_guard_does_not_interfere():
    """Control: a genuinely-ready case (the fix confirmed: the cause gone and
    the problem gone with it) still opens the RESOLVED offer — the
    close-escape guard only fires on the pivot."""
    engine = _engine()
    case = _needs_info_case()
    case.solutions.append(
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Create the index",
            longterm_fix="CREATE INDEX ...",
        )
    )
    for category, summary in (
        (EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, "queries use the new index"),
        (EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "p99 back to 40ms"),
    ):
        case.evidence.append(
            Evidence(
                summary=summary,
                category=category,
                source_type=EvidenceSourceType.LOGS,
                collected_by="user_test",
                primary_purpose="confirm the fix",
                source_file_id=_FILE_ID,
                collected_at_turn=2,
            )
        )
    metadata = _llm_proposes_resolved()

    await engine.transitions.check_automatic_transitions(
        case=case,
        metadata=metadata,
        user_message="mark resolved",
    )

    assert case.offer_record.open.kind == "resolve"
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
        case=case,
        metadata=metadata,
        user_message="mark resolved",
    )

    assert metadata.get("resolution_readiness_verdict") == "needs_info"
    assert "solution" in (metadata.get("resolution_readiness_missing") or [])


@pytest.mark.asyncio
async def test_readiness_verdict_recorded_on_needs_info_recheck():
    """The request's re-check (second pass) records the verdict too."""
    engine = _engine()
    case = _needs_info_case()
    _asked_last_turn(case)
    metadata = {
        "response_obj": MagicMock(state_updates=MagicMock(proposed_transition=None))
    }

    await engine.transitions.check_automatic_transitions(
        case=case,
        metadata=metadata,
        user_message="I don't have a solution",
    )

    assert metadata.get("resolution_readiness_verdict") == "needs_info"
    assert metadata.get("resolution_readiness_missing")


@pytest.mark.asyncio
async def test_with_nothing_open_the_models_proposal_is_not_superseded():
    """The same-turn rule needs an offer OPEN (#1885): with none, the model's
    proposal is read on its merits (here, a request)."""
    engine = _engine()
    case = _needs_info_case()
    metadata = _llm_proposes_resolved()

    await engine.transitions.check_automatic_transitions(
        case=case,
        metadata=metadata,
        user_message="mark resolved",
    )

    assert case.offer_record.resolve_request is not None
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
    """Step 0's re-check of a met request opens the resolve offer before
    step 2 reads the model's proposal: the model's same-turn proposal is
    dropped (and said to be), not pivoted. Real ``process_turn``, stubbed
    generator."""
    engine = _engine()
    engine.kb_prefetcher.prefetch_kb_context = AsyncMock(return_value=None)
    case = _needs_info_case()
    case.current_turn = 2
    _asked_last_turn(case)
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Good, the index removed the full scans.",
            state_updates={
                "evidence_to_add": [
                    EvidenceToAdd(
                        summary="Full scans gone since the index was created",
                        extract="events-* queries use idx_events_ts after 10:02",
                        category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
                        source_type=EvidenceSourceType.LOGS,
                        source_file_id=_FILE_ID,
                    ),
                    EvidenceToAdd(
                        summary="p99 back to 40ms since the index was created",
                        extract="events-* p99: 40ms after CREATE INDEX at 10:02",
                        category=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
                        source_type=EvidenceSourceType.LOGS,
                        source_file_id=_FILE_ID,
                    ),
                ],
                "proposed_transition": ProposedTransition(to_state=model_proposes),
            },
        )
    )

    with patch.object(turn_completion.logger, "info") as info:
        result = await engine.process_turn(
            case=case,
            user_message="We created the index and p99 is back to 40ms",
            redaction_ctx=None,
        )

    offer = result["case_updated"].offer_record.open
    assert (offer.kind, offer.basis) == ("resolve", "model")
    extra = _compliance(info)
    assert extra["transition_superseded_by_engine"] is True
    assert extra["transition_pivoted"] is False
    feedback = result["case_updated"].turn_history[-1].system_feedback or ""
    assert "the engine already offered the user a 'resolved' transition" in feedback
