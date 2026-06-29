"""Apply-path wiring: cause hypotheses are anchored on a verified symptom.

Driven through the real ``_apply_investigation_updates`` path:
  - symptom NOT verified -> ``hypotheses_to_add`` are QUEUED as CAPTURED (held out
    of the active differential), never dropped.
  - symptom verified      -> new hypotheses are ACTIVE, AND any previously queued
    (CAPTURED) ones are auto-promoted to ACTIVE — with NO LLM re-emission, even on
    a turn that emits no new hypotheses.
"""

from uuid import uuid4

import pytest

from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import (
    HypothesisToAdd,
    InvestigationResponse_Diagnosis,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    HypothesisCategory,
    HypothesisState,
    InquiryData,
    ProblemVerification,
)

pytestmark = pytest.mark.unit

_DSU = InvestigationResponse_Diagnosis.DiagnosisStateUpdate


def _engine() -> MilestoneEngine:
    """Bare engine — exercises the apply path with just the hypothesis manager
    wired (the matcher step skips gracefully without investigation_tools)."""
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.hypothesis_manager = HypothesisManager()
    return eng


def _case(symptom_verified: bool) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="orders failing",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="orders failing", severity=CaseSeverity.HIGH
        ),
    )
    case.current_turn = 5
    case.progress.symptom_verified = symptom_verified
    return case


def _meta() -> dict:
    return {
        "milestones_completed": [],
        "evidence_added": [],
        "hypotheses_generated": [],
        "hypotheses_validated": [],
        "solutions_proposed": [],
        "progress_made": False,
        "status_transitioned": False,
    }


def _h2a(statement="connection pool exhausted"):
    return [
        HypothesisToAdd(
            statement=statement,
            category=HypothesisCategory.DATABASE,
            likelihood=0.9,
            rationale="initial",
        )
    ]


async def test_unverified_hypotheses_are_queued_as_captured():
    eng, case = _engine(), _case(symptom_verified=False)
    await eng._apply_investigation_updates(
        case, _DSU(hypotheses_to_add=_h2a()), _meta()
    )
    hyps = list(case.hypotheses.values())
    assert len(hyps) == 1
    # queued, not active — held out of the differential pending the anchor
    assert hyps[0].state == HypothesisState.CAPTURED
    assert HypothesisManager.count_active_hypotheses(case) == 0


async def test_verified_hypotheses_are_active():
    eng, case = _engine(), _case(symptom_verified=True)
    await eng._apply_investigation_updates(
        case, _DSU(hypotheses_to_add=_h2a()), _meta()
    )
    hyps = list(case.hypotheses.values())
    assert len(hyps) == 1
    assert hyps[0].state == HypothesisState.ACTIVE


async def test_queued_hypotheses_auto_promote_on_verification_without_reemission():
    """The headline guarantee: a hypothesis queued (CAPTURED) on an
    unverified turn is auto-promoted to ACTIVE the moment the symptom verifies —
    on a LATER turn that emits NO new hypotheses (so the flush cannot depend on
    re-emission)."""
    eng = _engine()

    # Turn N: symptom unverified, LLM emits a hypothesis -> queued (CAPTURED).
    case = _case(symptom_verified=False)
    await eng._apply_investigation_updates(
        case, _DSU(hypotheses_to_add=_h2a()), _meta()
    )
    (hyp_id,) = list(case.hypotheses.keys())
    assert case.hypotheses[hyp_id].state == HypothesisState.CAPTURED

    # Turn N+1: symptom verifies; the LLM emits NO new hypotheses this turn.
    case.progress.symptom_verified = True
    meta = _meta()
    await eng._apply_investigation_updates(case, _DSU(hypotheses_to_add=[]), meta)

    # Auto-applied: promoted to ACTIVE with no re-emission, and counted as
    # generated this turn for progress accounting.
    assert case.hypotheses[hyp_id].state == HypothesisState.ACTIVE
    assert hyp_id in meta["hypotheses_generated"]
