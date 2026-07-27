"""Tests for the rca_infeasible propose-closure flow.

When the LLM marks rca_infeasible=True on ProblemVerification and the
``mitigation_verified`` mitigation gate later fires, the engine must
propose closing the case as stabilized (User-Agent Handshake).
Reference: investigation-lifecycle-logic.md §2.4.

Post-redesign (unified opportunistic flow, no path fork): there is no
Gate 3 and no ``path_selection``. Closing from INVESTIGATING yields the
unified closure reason ``closed_after_investigation`` (which folds in the
former ``mitigation_sufficient`` reason).
"""

import pytest

from faultmaven.core.investigation.milestone_engine import (
    _apply_stage_gate_side_effects,
    _close_confirmation_suggestions,
)
from faultmaven.core.investigation.terminal_transitions import (
    cancel_pending_transition,
    confirm_pending_transition,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InquiryData,
    InvestigationProgress,
    MitigationRecord,
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
        organization_id="org_123",
        description="Test description",
        problem_verification=pv,
        progress=InvestigationProgress(mitigation=mitigation),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            thread_id="thread_123",
            proposed_problem_statement="Test symptom",
        ),
    )


def test_rca_infeasible_creates_pending_closure():
    """mitigation_verified gate + rca_infeasible=True → pending_transition
    to CLOSED.

    closure_reason is engine-derived from case state: closing from
    INVESTIGATING → ``closed_after_investigation``.
    """
    case = _make_case(rca_infeasible=True, rationale="third-party API outage")
    metadata: dict = {}

    _apply_stage_gate_side_effects(
        case, {"mitigation_verified"}, "mitigation worked", metadata
    )

    assert case.pending_transition is not None
    assert case.pending_transition["to_state"] == "closed"
    assert case.pending_transition["closure_reason"] == "closed_after_investigation"
    assert "third-party API outage" in case.pending_transition["summary"]
    assert (
        "shall we close this case as stabilized?" in case.pending_transition["summary"]
    )

    assert metadata["transition_proposed_this_turn"] is True
    assert metadata["override_suggestions"] == _close_confirmation_suggestions()
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


def test_confirm_pending_transition_closes_after_investigation():
    """User confirmation drives CLOSED with
    closure_reason=closed_after_investigation."""
    case = _make_case(rca_infeasible=True, rationale="deprecated legacy system")
    _apply_stage_gate_side_effects(case, {"mitigation_verified"}, "ok", {})

    confirmed = confirm_pending_transition(case, "user_123")

    assert confirmed is True
    assert case.state == CaseState.CLOSED
    assert case.closure_reason == "closed_after_investigation"
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
