"""Tests for the rca_infeasible propose-closure flow.

When the LLM marks rca_infeasible=True on ProblemVerification and the
``mitigation_verified`` mitigation gate later fires, the engine must
propose closing the case as stabilized (User-Agent Handshake).
Reference: investigation-lifecycle-logic.md §2.4.

Post-redesign (unified opportunistic flow, no path fork): there is no
Gate 3 and no ``path_selection``. Closing from INVESTIGATING yields the
closure reason ``closed_rca_infeasible``: the cause is structurally
unreachable (declared via rca_infeasible + rationale), which outranks the
``mitigation_sufficient`` that also holds here — this path only fires on
mitigation_verified, so both always apply and the more informative label wins.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _apply_stage_gate_side_effects,
    _close_confirmation_suggestions,
)
from faultmaven.core.investigation.schemas import (
    EvidenceToAdd,
    EvidenceTrail,
    InvestigationResponse_Mitigation,
    MilestoneJustifications,
    MilestoneUpdates,
    ProposedTransition,
)
from faultmaven.core.investigation.terminal_transitions import (
    cancel_pending_transition,
    confirm_pending_transition,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    InvestigationProgress,
    MitigationRecord,
    ProblemStatus,
    ProblemVerification,
)


def _make_case(
    *,
    rca_infeasible: bool,
    rationale: str | None = "third-party API outage",
    mitigation_verified: bool = True,
    no_problem_verification: bool = False,
) -> Case:
    """Build a Case with a verified mitigation and an optional
    rca_infeasible signal.

    The unified flow tracks the mitigation via
    ``progress.mitigation`` (a forward-only record) rather than the
    legacy path-coupled mitigation gates.
    """
    pv = (
        None
        if no_problem_verification
        else ProblemVerification(
            symptom_statement="Test symptom",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
            rca_infeasible=rca_infeasible,
            rca_infeasible_rationale=rationale,
        )
    )
    mitigation = MitigationRecord(
        proposed_at_turn=1,
        accepted=True,
        verified=mitigation_verified,
        completed_at_turn=1 if mitigation_verified else None,
    )
    return Case(
        case_id="case_1234567890ab",
        title="Test Case",
        state=CaseState.INVESTIGATING,
        user_id="user_123",
        enterprise_id="org_123",
        description="Test description",
        problem_verification=pv,
        progress=InvestigationProgress(mitigation=mitigation),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            thread_id="thread_123",
            proposed_problem_statement="Test symptom",
        ),
    )


def test_rca_infeasible_creates_pending_closure():
    """mitigation_verified gate + rca_infeasible=True → pending_transition
    to CLOSED.

    closure_reason is engine-derived from case state: closing from
    INVESTIGATING → ``closed_rca_infeasible``.
    """
    case = _make_case(rca_infeasible=True, rationale="third-party API outage")
    metadata: dict = {}

    _apply_stage_gate_side_effects(
        case, {"mitigation_verified"}, "mitigation worked", metadata
    )

    assert case.pending_transition is not None
    assert case.pending_transition["to_state"] == "closed"
    assert case.pending_transition["closure_reason"] == "closed_rca_infeasible"
    assert "third-party API outage" in case.pending_transition["summary"]
    assert (
        "shall we close this case as stabilized?" in case.pending_transition["summary"]
    )

    assert metadata["transition_proposed_this_turn"] is True
    assert metadata["override_suggestions"] == _close_confirmation_suggestions(case)
    assert (
        metadata["rca_infeasible_closure_message"] == case.pending_transition["summary"]
    )

    # The mitigation stays verified (forward-only).
    assert case.progress.mitigation.verified is True
    assert case.progress.mitigation.accepted is True


def test_rca_infeasible_false_does_not_propose_closure():
    """mitigation_verified gate + rca_infeasible=False → no pending_transition."""
    case = _make_case(rca_infeasible=False)
    metadata: dict = {}

    _apply_stage_gate_side_effects(
        case, {"mitigation_verified"}, "mitigation worked", metadata
    )

    assert case.pending_transition is None
    assert "rca_infeasible_closure_message" not in metadata
    assert case.progress.mitigation.verified is True
    assert case.progress.mitigation.accepted is True


def test_no_problem_verification_does_not_propose_closure():
    """Missing problem_verification must not crash and must not propose closure."""
    case = _make_case(rca_infeasible=False, no_problem_verification=True)
    metadata: dict = {}

    _apply_stage_gate_side_effects(
        case, {"mitigation_verified"}, "mitigation worked", metadata
    )

    assert case.pending_transition is None
    assert "rca_infeasible_closure_message" not in metadata


def test_confirm_pending_transition_closes_rca_infeasible():
    """User confirmation drives CLOSED with
    closure_reason=closed_rca_infeasible."""
    case = _make_case(rca_infeasible=True, rationale="deprecated legacy system")
    _apply_stage_gate_side_effects(case, {"mitigation_verified"}, "ok", {})

    confirmed = confirm_pending_transition(case, "user_123")

    assert confirmed is True
    assert case.state == CaseState.CLOSED
    assert case.closure_reason == "closed_rca_infeasible"
    assert case.pending_transition is None


def test_decline_clears_pending_and_keeps_case_investigating():
    """User decline clears pending_transition; case remains INVESTIGATING for RCA."""
    case = _make_case(rca_infeasible=True)
    _apply_stage_gate_side_effects(case, {"mitigation_verified"}, "ok", {})

    cancelled = cancel_pending_transition(case)

    assert cancelled is True
    assert case.pending_transition is None
    assert case.state == CaseState.INVESTIGATING


@pytest.mark.parametrize(
    "rationale,expected_phrase",
    [
        ("uncontrollable external dependency", "uncontrollable external dependency"),
        (None, "root cause analysis is not feasible for this problem"),
    ],
)
def test_closure_message_uses_rationale_or_fallback(rationale, expected_phrase):
    """Rationale text appears in the closure message; fallback used when missing."""
    case = _make_case(rca_infeasible=True, rationale=rationale)
    metadata: dict = {}

    _apply_stage_gate_side_effects(
        case, {"mitigation_verified"}, "mitigation worked", metadata
    )

    assert expected_phrase in metadata["rca_infeasible_closure_message"]


def _turn_engine(model_proposes: str | None) -> MilestoneEngine:
    """A real engine whose stubbed model verifies the mitigation and, when
    asked, proposes a transition beside it."""
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
    engine.kb_prefetcher.prefetch_kb_context = AsyncMock(return_value=None)
    state_updates = {
        "milestones": MilestoneUpdates(mitigation_verified=True),
        "evidence_to_add": [
            EvidenceToAdd(
                summary="error rate 0% for 30 minutes after failover",
                extract="gateway 5xx rate: 0.0 at 14:30-15:00",
                category=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
                source_type=EvidenceSourceType.USER_DESCRIPTION,
            )
        ],
    }
    if model_proposes:
        state_updates["proposed_transition"] = ProposedTransition(
            to_state=model_proposes
        )
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Mitigation(
            agent_response="The failover held.",
            state_updates=state_updates,
            evidence_trail=EvidenceTrail(
                evidence_analyzed=[],
                milestone_justifications=MilestoneJustifications(
                    mitigation_verified="error rate back to 0 after the failover"
                ),
            ),
        )
    )
    return engine


def _turn_case() -> Case:
    case = _make_case(rca_infeasible=True, mitigation_verified=False)
    case.progress.problem_status = ProblemStatus.VERIFIED
    return case


@pytest.mark.parametrize("model_proposes", ["closed", "resolved"])
async def test_the_models_same_turn_proposal_leaves_the_engines_close(model_proposes):
    """#1885, through a real turn: the engine's stabilized-close offer is what
    the user answers, whatever the model proposed beside it. Replaced, the
    offer the user confirmed was the model's, not the one whose reason the
    reply shows."""
    case = _turn_case()
    result = await _turn_engine(model_proposes).process_turn(
        case=case, user_message="the failover held, error rate is 0"
    )

    assert case.progress.mitigation.verified is True
    pending = case.pending_transition
    assert pending["to_state"] == "closed"
    assert pending["closure_reason"] == "closed_rca_infeasible"
    assert pending["summary"] == (
        "The mitigation is verified and stable. Since third-party API outage, "
        "shall we close this case as stabilized?"
    )
    assert pending["summary"] in result["agent_response"]


@pytest.mark.parametrize("model_proposes", [None, "resolved", "closed"])
async def test_a_resolvable_case_is_never_offered_the_stabilized_close(
    model_proposes,
):
    """The stabilized close reads closure readiness like every engine opener
    (#1885 review): on a case whose cause is confirmed eliminated it is not
    offered, so the resolve offer the readiness bar calls for is what the user
    answers — the model's own, or the INV-43 backstop's — and one "yes"
    resolves it."""
    case = _turn_case()
    case.evidence.append(
        Evidence(
            category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            primary_purpose="confirm the cause was eliminated",
            summary="after the vendor rolled back their change the 5xx stopped",
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_by="user",
            collected_at_turn=1,
        )
    )
    engine = _turn_engine(model_proposes)
    result = await engine.process_turn(
        case=case, user_message="the failover held, error rate is 0"
    )

    assert case.progress.mitigation.verified is True
    assert case.pending_transition["to_state"] == "resolved"
    assert "stabilized" not in result["agent_response"]
    labels = [s["label"] for s in result["suggested_follow_ups"]]
    assert "Yes, mark as resolved" in labels
    assert "Yes, close this case" not in labels

    case.current_turn += 1
    result = await engine.process_turn(case=case, user_message="yes")
    assert result["case_updated"].state == CaseState.RESOLVED
