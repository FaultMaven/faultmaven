"""Tests for the rca_infeasible propose-closure flow.

When the LLM marks rca_infeasible=True on ProblemVerification and
mitigation_verified later fires as a stage-gate, the engine must propose
closing the case as mitigated (User-Agent Handshake). Reference:
investigation-lifecycle-logic.md §2.4.
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
    CaseStatus,
    InquiryData,
    InvestigationPath,
    InvestigationProgress,
    PathSelection,
    ProblemVerification,
)


def _make_case(
    *,
    rca_infeasible: bool,
    rationale: str | None = "third-party API outage",
    mitigation_verified: bool = True,
    no_problem_verification: bool = False,
) -> Case:
    """Build a Case with mitigation_verified set and an optional rca_infeasible signal.

    Includes a MITIGATION_FIRST ``path_selection`` so the rca_infeasible
    closure flow can stamp ``mitigation_completed_at_turn`` and so
    ``derive_closure_reason`` recognizes the case as at-Gate-3.
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
    return Case(
        case_id="case_1234567890ab",
        title="Test Case",
        status=CaseStatus.INVESTIGATING,
        user_id="user_123",
        organization_id="org_123",
        description="Test description",
        problem_verification=pv,
        progress=InvestigationProgress(
            mitigation_accepted=True,
            mitigation_verified=mitigation_verified,
        ),
        path_selection=PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale="ongoing critical",
            user_confirmed=True,
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            thread_id="thread_123",
            proposed_problem_statement="Test symptom",
        ),
    )


def test_rca_infeasible_creates_pending_closure():
    """mitigation_verified + rca_infeasible=True → pending_transition to CLOSED.

    closure_reason is derived from ``path_selection`` at-Gate-3 state:
    ``mitigation_completed_at_turn`` is set + ``rca_after_mitigation_confirmed``
    is False → ``mitigation_sufficient``.
    """
    case = _make_case(rca_infeasible=True, rationale="third-party API outage")
    metadata: dict = {}

    _apply_stage_gate_side_effects(
        case, {"mitigation_verified"}, "mitigation worked", metadata
    )

    assert case.pending_transition is not None
    assert case.pending_transition["to_status"] == "closed"
    assert case.pending_transition["closure_reason"] == "mitigation_sufficient"
    assert "third-party API outage" in case.pending_transition["summary"]
    assert (
        "shall we close this case as mitigated?" in case.pending_transition["summary"]
    )

    assert metadata["transition_proposed"] is True
    assert metadata["override_suggestions"] == _close_confirmation_suggestions()
    assert (
        metadata["rca_infeasible_closure_message"] == case.pending_transition["summary"]
    )

    # Mitigation gate flags are set-once under forward-only semantics —
    # they stay True after the side effect runs. The case's at-Gate-3
    # status is read from path_selection, not from these flags.
    assert case.progress.mitigation_verified is True
    assert case.progress.mitigation_accepted is True
    # The Gate 3 boundary marker is stamped on path_selection.
    assert case.path_selection.mitigation_completed_at_turn is not None


def test_rca_infeasible_false_does_not_propose_closure():
    """mitigation_verified + rca_infeasible=False → no pending_transition.

    Mitigation gate flags stay True (set-once under forward-only); the
    Gate 3 boundary marker is stamped on path_selection so the engine can
    later recognize an at-Gate-3 close as ``mitigation_sufficient``.
    """
    case = _make_case(rca_infeasible=False)
    metadata: dict = {}

    _apply_stage_gate_side_effects(
        case, {"mitigation_verified"}, "mitigation worked", metadata
    )

    assert case.pending_transition is None
    assert "rca_infeasible_closure_message" not in metadata
    assert case.progress.mitigation_verified is True
    assert case.progress.mitigation_accepted is True
    assert case.path_selection.mitigation_completed_at_turn is not None


def test_no_problem_verification_does_not_propose_closure():
    """Missing problem_verification must not crash and must not propose closure."""
    case = _make_case(rca_infeasible=False, no_problem_verification=True)
    metadata: dict = {}

    _apply_stage_gate_side_effects(
        case, {"mitigation_verified"}, "mitigation worked", metadata
    )

    assert case.pending_transition is None
    assert "rca_infeasible_closure_message" not in metadata


def test_confirm_pending_transition_closes_with_mitigation_sufficient():
    """User confirmation drives CLOSED with closure_reason=mitigation_sufficient."""
    case = _make_case(rca_infeasible=True, rationale="deprecated legacy system")
    _apply_stage_gate_side_effects(case, {"mitigation_verified"}, "ok", {})

    confirmed = confirm_pending_transition(case, "user_123")

    assert confirmed is True
    assert case.status == CaseStatus.CLOSED
    assert case.closure_reason == "mitigation_sufficient"
    assert case.pending_transition is None


def test_decline_clears_pending_and_keeps_case_investigating():
    """User decline clears pending_transition; case remains INVESTIGATING for RCA."""
    case = _make_case(rca_infeasible=True)
    _apply_stage_gate_side_effects(case, {"mitigation_verified"}, "ok", {})

    cancelled = cancel_pending_transition(case)

    assert cancelled is True
    assert case.pending_transition is None
    assert case.status == CaseStatus.INVESTIGATING


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
