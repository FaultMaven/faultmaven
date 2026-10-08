"""The deferred-implementation disposition, observed through a REAL turn.

The unit tests around ``_maybe_propose_deferred_close`` pin what the proposer
decides. They cannot see what the USER ends up reading, which is where the
defect actually lived: the rationale key was published and never rendered, so
the turn shipped a bare confirm/decline pair with no stated reason (#1122,
case_fa29e0023b85 turns 11-15).

These drive ``MilestoneEngine.process_turn`` end to end and assert on the
composed ``agent_response`` and ``suggested_follow_ups`` — the actual wire
payload. Only the LLM seam is replaced, and it returns a REAL schema instance
rather than a Mock, so every engine stage downstream processes genuine typed
objects (a Mock would accept any shape and hide drift).
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.schemas import (
    InvestigationResponse_Diagnosis,
    MilestoneUpdates,
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

LLM_ANALYSIS = (
    "The provider audience mismatch remains the active cause: the pod projects "
    "sts.amazonaws.com while the IAM OIDC provider carries sts.amazonaws.com.cn."
)


def _engine(response):
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(return_value=response)
    return engine


def _make_repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    return repo


def _deferred_response():
    """A real response whose milestones declare the fix out-of-band."""
    return InvestigationResponse_Diagnosis(
        agent_response=LLM_ANALYSIS,
        state_updates={"milestones": MilestoneUpdates(solution_feasible="deferred")},
    )


def _case(*, causal_absence: bool) -> Case:
    case = Case(
        title="Cross-account AssumeRole failures",
        enterprise_id="org_test",
        user_id="user_test",
        description="data-processor pods cannot assume the cross-account role",
        problem_verification=ProblemVerification(
            symptom_statement="AssumeRoleWithWebIdentity fails for the pods",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.progress.problem_status = ProblemStatus.VERIFIED
    # cause_state is RE-DERIVED from the causal graph every turn, so presetting
    # it is overwritten before the proposer runs (observed: the first draft of
    # this test set IDENTIFIED and the trace still read `cause_state=unknown`,
    # so the proposer bailed and the test passed nothing). Establish the cause
    # the way a real case does — an authored RootCauseConclusion, the `rcc` leg
    # of `cause_identification_leg`, which is anchored by symptom_verified.
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=(
            "The IAM OIDC provider is registered with client ID "
            "sts.amazonaws.com.cn while the projected token audience is "
            "sts.amazonaws.com."
        ),
        mechanism=(
            "AssumeRoleWithWebIdentity rejects the token because the audience "
            "does not match the provider's registered client ID."
        ),
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.85,
    )
    case.solutions = [
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Correct the OIDC provider client ID",
            longterm_fix="Set the provider ClientIDList to sts.amazonaws.com.",
        )
    ]
    if causal_absence:
        case.evidence.append(
            Evidence(
                category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
                primary_purpose="confirm the cause was eliminated",
                summary=(
                    "After the provider client-ID correction the pods obtained "
                    "credentials and the AssumeRole failures stopped."
                ),
                source_type=EvidenceSourceType.USER_DESCRIPTION,
                collected_by="user",
                collected_at_turn=1,
            )
        )
    return case


@pytest.mark.asyncio
async def test_close_turn_renders_the_reason_with_the_llm_analysis():
    """The turn must carry BOTH the model's analysis and the engine's reason.

    Before the fix the rationale key had no reader, so this turn rendered the
    analysis and two unexplained buttons.
    """
    case = _case(causal_absence=False)
    engine = _engine(_deferred_response())

    result = await engine.process_turn(
        case=case, user_message="the platform team ships it"
    )
    text = result["agent_response"]

    assert LLM_ANALYSIS in text, "the model's analysis must survive the gate turn"
    assert (
        "can't be applied or verified during this session" in text
    ), "the close was offered without ever telling the user why"
    assert "---" in text, "the engine reason must be composed below the reply"
    labels = [s["label"] for s in result["suggested_follow_ups"]]
    assert "Yes, close this case" in labels


@pytest.mark.asyncio
async def test_confirmed_case_is_offered_resolve_not_close():
    """With a gone=>gone confirmation the same trigger must offer RESOLVED."""
    case = _case(causal_absence=True)
    engine = _engine(_deferred_response())

    result = await engine.process_turn(
        case=case, user_message="the platform team ships it"
    )
    text = result["agent_response"]
    labels = [s["label"] for s in result["suggested_follow_ups"]]

    assert "Yes, mark as resolved" in labels
    assert "Yes, close this case" not in labels
    assert case.pending_transition["to_state"] == "resolved"
    assert LLM_ANALYSIS in text
    assert "Closing would" not in text, "pivot-from-close prose on an unprompted offer"
    assert "out-of-band" in text


def _deferred_response_with(model_proposes: str):
    """The deferred turn, with the model's own transition beside it."""
    return InvestigationResponse_Diagnosis(
        agent_response=LLM_ANALYSIS,
        state_updates={
            "milestones": MilestoneUpdates(solution_feasible="deferred"),
            "proposed_transition": ProposedTransition(to_state=model_proposes),
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("model_proposes", ["closed", "resolved"])
async def test_the_models_same_turn_proposal_leaves_the_signed_offer(model_proposes):
    """#1885: the engine's offer is what the user answers. Replaced by the
    model's, it lost its signature, a decline recorded nothing, and the engine
    offered the same close again on the next ordinary turn (fm#1122's re-nag).
    A model RESOLVED on this case is not ready, and used to stand in for the
    engine's close as a resolve offer."""
    case = _case(causal_absence=False)
    engine = _engine(_deferred_response_with(model_proposes))

    result = await engine.process_turn(
        case=case, user_message="the platform team ships it"
    )
    pending = case.pending_transition
    assert pending["to_state"] == "closed"
    assert "justifying_signature" in pending
    labels = [s["label"] for s in result["suggested_follow_ups"]]
    assert "Yes, close this case" in labels

    case.current_turn += 1
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="ok", state_updates={}
        )
    )
    await engine.process_turn(case=case, user_message="no")
    assert case.progress.deferred_disposition_declined_signatures == [
        pending["justifying_signature"]
    ]

    case.current_turn += 1
    engine.generator.generate_structured_output = AsyncMock(
        return_value=_deferred_response()
    )
    await engine.process_turn(
        case=case, user_message="what should the platform team change first?"
    )
    assert case.pending_transition is None, "the declined offer came back"
