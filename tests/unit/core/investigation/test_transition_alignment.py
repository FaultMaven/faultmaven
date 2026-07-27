"""Tests for state-transition path alignment.

The three triggers for a state transition (UI dropdown click, agent-initiated
proposal, LLM-emitted ``proposed_transition``) must all converge on the same
deterministic confirmation UX: a DECIDE confirm/decline pair carrying
``intent={"type": "confirmation", "confirmation_value": …}`` so the next-turn
click routes through ``IntentResolver`` Tier-1 deterministically.

This file verifies that convergence at the engine level.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InvestigationProgress,
    ProblemVerification,
    RootCauseConclusion,
    Solution,
    SolutionType,
)


def _make_repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    return repo


def _make_inquiry_case():
    return Case(
        case_id="case_a1b2c3d4e5f6",
        title="Alignment test",
        state=CaseState.INQUIRY,
        user_id="user_test",
        organization_id="org_test",
        description="Alignment test description",
        problem_verification=ProblemVerification(
            symptom_statement="Alignment test symptom",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )


def _make_investigating_case():
    case = _make_inquiry_case()
    case.inquiry.proposed_problem_statement = "Alignment test"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
    case.inquiry.decided_to_investigate = True
    case.inquiry.decision_made_at = datetime.now(timezone.utc)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    return case


def _fill_for_resolution_ready(case):
    """Promote an investigating case to assess_resolution_readiness=READY.

    READY now requires the root cause to be confirmed ELIMINATED — recorded
    as a ``causal_absence_evidence`` row — not merely that a solution exists.
    So this attaches root cause + solution AND a causal_absence evidence row.
    A case with only root cause + solution (no causal_absence) is CLOSE-grade
    (stabilized / deferred), not resolution-grade. Mutates and returns ``case``.
    """
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="Alignment test root cause",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        mechanism="Test mechanism",
    )
    case.solutions.append(
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Alignment test solution",
            longterm_fix="Apply correct configuration",
        )
    )
    # The required RESOLVED proof: the root cause is confirmed gone after the fix.
    case.evidence.append(
        Evidence(
            summary="Post-fix verification confirms the root cause is gone",
            category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            collected_at=datetime.now(timezone.utc),
            collected_by="user_test",
            primary_purpose="Confirm root cause eliminated",
            preprocessed_content="cause absent after fix",
            content_size_bytes=80,
            preprocessing_method="manual",
            source_file_id="file_a1b2c3d4e5f6",
            collected_at_turn=2,
        )
    )
    return case


def _assert_canonical_confirm_pair(suggestions, expected_label_substring):
    """Every alignment site must emit exactly two DECIDE suggestions
    carrying confirmation intent metadata."""
    assert len(suggestions) == 2
    assert all(s["action_type"] == "DECIDE" for s in suggestions)
    assert all("intent" in s for s in suggestions)
    assert suggestions[0]["intent"] == {
        "type": "confirmation",
        "confirmation_value": True,
    }
    assert suggestions[1]["intent"] == {
        "type": "confirmation",
        "confirmation_value": False,
    }
    # Confirm at least one suggestion's payload references the action
    # (catches accidental swap of helper call-site with the wrong target).
    assert any(expected_label_substring in s["payload"].lower() for s in suggestions)


# ---------------------------------------------------------------------------
# UI dropdown alignment — every propose_transition site emits canonical pair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ui_dropdown_inquiry_to_closed_emits_canonical_close_pair():
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
    )
    case = _make_inquiry_case()
    result = await engine.process_turn(
        case=case,
        user_message="Close this case.",
        intent_type="status_transition",
        intent_data={
            "from_state": "inquiry",
            "to_state": "closed",
            "user_confirmed": True,
        },
    )
    assert result["case_updated"].pending_transition["to_state"] == "closed"
    _assert_canonical_confirm_pair(result["suggested_follow_ups"], "close")


@pytest.mark.asyncio
async def test_ui_dropdown_investigating_to_closed_emits_canonical_close_pair():
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
    )
    case = _make_investigating_case()
    case.progress.symptom_verified = True
    result = await engine.process_turn(
        case=case,
        user_message="Close this case as unresolved.",
        intent_type="status_transition",
        intent_data={
            "from_state": "investigating",
            "to_state": "closed",
            "user_confirmed": True,
        },
    )
    assert result["case_updated"].pending_transition["to_state"] == "closed"
    _assert_canonical_confirm_pair(result["suggested_follow_ups"], "close")


@pytest.mark.asyncio
async def test_ui_dropdown_investigating_to_resolved_ready_emits_resolve_pair():
    """READY case (root cause + solution): dropdown lands in the READY branch
    and emits the canonical RESOLVED confirm/decline pair."""
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
    )
    case = _make_investigating_case()
    case.progress.symptom_verified = True
    _fill_for_resolution_ready(case)

    result = await engine.process_turn(
        case=case,
        user_message="Mark as resolved.",
        intent_type="status_transition",
        intent_data={
            "from_state": "investigating",
            "to_state": "resolved",
            "user_confirmed": True,
        },
    )
    assert result["case_updated"].pending_transition is not None
    assert result["case_updated"].pending_transition["to_state"] == "resolved"
    assert not result["case_updated"].pending_transition.get("needs_info")
    _assert_canonical_confirm_pair(result["suggested_follow_ups"], "resolved")


@pytest.mark.asyncio
async def test_ui_dropdown_resolve_pivots_to_close_when_thin():
    """SUGGEST_CLOSE pivot: when the user picks Resolve via dropdown but the
    case has no root cause / solution / evidence, the engine must propose
    CLOSED (not RESOLVED) and emit the canonical CLOSE pair so the user's
    confirm click matches what they're actually being asked to do."""
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
    )
    case = _make_investigating_case()
    case.progress.symptom_verified = True
    # Deliberately no root_cause_conclusion, no solutions, no evidence.

    result = await engine.process_turn(
        case=case,
        user_message="Mark as resolved.",
        intent_type="status_transition",
        intent_data={
            "from_state": "investigating",
            "to_state": "resolved",
            "user_confirmed": True,
        },
    )
    pending = result["case_updated"].pending_transition
    assert pending is not None
    assert pending["to_state"] == "closed"
    # closure_reason engine-derived: investigating + no mitigation
    assert pending.get("closure_reason") == "closed_after_investigation"
    _assert_canonical_confirm_pair(result["suggested_follow_ups"], "close")


@pytest.mark.asyncio
async def test_ui_dropdown_close_pivots_to_resolve_when_resolution_grade():
    """Symmetric to ``test_ui_dropdown_resolve_pivots_to_close_when_thin``.
    User clicks Close from INVESTIGATING but the case has root cause +
    solution on record — engine pivots to propose RESOLVED instead and
    emits the canonical RESOLVE pair, so the user's confirm click
    actually preserves resolution attribution rather than discarding it.

    This handles the close-side of the loose-terminology pivot strategy:
    users use "close" and "resolve" interchangeably as "conclude the
    case"; the engine reconciles intent against case content."""
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
    )
    case = _make_investigating_case()
    case.progress.symptom_verified = True
    _fill_for_resolution_ready(case)  # root cause + solution → resolution-grade

    result = await engine.process_turn(
        case=case,
        user_message="Close this case.",
        intent_type="status_transition",
        intent_data={
            "from_state": "investigating",
            "to_state": "closed",
            "user_confirmed": True,
        },
    )
    pending = result["case_updated"].pending_transition
    assert pending is not None
    assert pending["to_state"] == "resolved", (
        "Dropdown SUGGEST_RESOLVE pivot did not fire: the user clicked "
        "Close but the case has root cause + solution. Engine should "
        "have pivoted to propose RESOLVED so the user's confirm "
        "preserves resolution attribution."
    )
    # closure_reason is None for RESOLVED (resolution itself is the categorization)
    assert pending.get("closure_reason") is None
    _assert_canonical_confirm_pair(result["suggested_follow_ups"], "resolved")


@pytest.mark.asyncio
async def test_ui_dropdown_resolve_needs_info_keeps_resolve_pair():
    """NEEDS_INFO branch: case has substance (root cause + evidence) but no
    ``causal_absence_evidence`` row confirming the cause was eliminated.
    Engine keeps the resolve intent so a follow-up turn confirming the cause
    is gone can move forward, and shows the RESOLVED confirm pair while
    flagging needs_info."""
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
    )
    case = _make_investigating_case()
    case.progress.symptom_verified = True
    # Substance (root cause + evidence) but NO causal_absence row → readiness
    # routes to NEEDS_INFO (ask user to confirm elimination) instead of
    # SUGGEST_CLOSE.
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="Alignment test root cause",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        mechanism="Test mechanism",
    )
    case.evidence.append(
        Evidence(
            summary="Test evidence",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_at=datetime.now(timezone.utc),
            collected_by="user_test",
            primary_purpose="Alignment test",
            preprocessed_content="Sample log line",
            content_size_bytes=100,
            preprocessing_method="manual",
            source_file_id=None,
            collected_at_turn=1,
        )
    )

    result = await engine.process_turn(
        case=case,
        user_message="Mark as resolved.",
        intent_type="status_transition",
        intent_data={
            "from_state": "investigating",
            "to_state": "resolved",
            "user_confirmed": True,
        },
    )
    pending = result["case_updated"].pending_transition
    assert pending is not None
    assert pending["to_state"] == "resolved"
    assert pending.get("needs_info") is True
    _assert_canonical_confirm_pair(result["suggested_follow_ups"], "resolved")


# ---------------------------------------------------------------------------
# Change 3 — LLM-emitted proposed_transition path overrides LLM suggestions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_automatic_transitions_sets_override_for_resolved():
    """When ``response_obj.state_updates.proposed_transition`` is set to
    ``resolved``, ``_check_automatic_transitions`` must:

    1. Call ``propose_transition()`` so ``case.pending_transition`` is set.
    2. Populate ``metadata["override_suggestions"]`` with the canonical
       resolution confirm/decline pair.

    This is Change 3 — the routing for the third trigger path (NL→LLM via
    B). Tested directly against ``_check_automatic_transitions`` to avoid
    coupling to the full structured-output pipeline.
    """
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
    )
    case = _make_investigating_case()
    case.progress.symptom_verified = True
    _fill_for_resolution_ready(case)

    fake_response = MagicMock()
    fake_response.state_updates.proposed_transition = MagicMock(
        to_state="resolved",
        evidence_ids=[],
    )
    metadata: dict = {"response_obj": fake_response}

    await engine._check_automatic_transitions(
        case=case, metadata=metadata, user_message="The fix worked."
    )

    assert case.pending_transition is not None
    assert case.pending_transition["to_state"] == "resolved"
    assert metadata.get("transition_proposed_this_turn") is True
    _assert_canonical_confirm_pair(metadata["override_suggestions"], "resolved")


@pytest.mark.asyncio
async def test_check_automatic_transitions_sets_override_for_closed():
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
    )
    case = _make_investigating_case()

    fake_response = MagicMock()
    fake_response.state_updates.proposed_transition = MagicMock(
        to_state="closed",
        evidence_ids=[],
    )
    metadata: dict = {"response_obj": fake_response}

    await engine._check_automatic_transitions(
        case=case, metadata=metadata, user_message="Close as unresolved."
    )

    assert case.pending_transition is not None
    assert case.pending_transition["to_state"] == "closed"
    # Regression guard: closure_reason is engine-derived from
    # (case.state, mitigation_verified). Investigating + no mitigation
    # → closed_after_investigation.
    assert case.pending_transition.get("closure_reason") == "closed_after_investigation"
    _assert_canonical_confirm_pair(metadata["override_suggestions"], "close")


@pytest.mark.asyncio
async def test_check_automatic_transitions_closure_reason_inquiry_only():
    """Regression guard for closure_reason on the inquiry→closed path —
    must be 'inquiry_only', mirroring the UI dropdown branch."""
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
    )
    case = _make_inquiry_case()

    fake_response = MagicMock()
    fake_response.state_updates.proposed_transition = MagicMock(
        to_state="closed",
        evidence_ids=[],
    )
    metadata: dict = {"response_obj": fake_response}

    await engine._check_automatic_transitions(
        case=case, metadata=metadata, user_message="never mind, close this case."
    )

    assert case.pending_transition is not None
    assert case.pending_transition["to_state"] == "closed"
    assert case.pending_transition.get("closure_reason") == "inquiry_only"


@pytest.mark.asyncio
async def test_check_automatic_transitions_closure_reason_stabilized_investigation():
    """A case stabilized then closed from INVESTIGATING yields the unified
    closure reason 'closed_after_investigation' (the redesign folds the
    former 'mitigation_sufficient' reason into this one). The mitigation
    record marks the case as stabilized; closure_reason is engine-derived
    from case.state."""
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
    )
    case = _make_investigating_case()
    from faultmaven.modules.case.domain.models import MitigationRecord

    case.progress.mitigation = MitigationRecord(
        proposed_at_turn=case.current_turn,
        accepted=True,
        verified=True,
        completed_at_turn=case.current_turn,
    )

    fake_response = MagicMock()
    fake_response.state_updates.proposed_transition = MagicMock(
        to_state="closed",
        evidence_ids=[],
    )
    metadata: dict = {"response_obj": fake_response}

    await engine._check_automatic_transitions(
        case=case, metadata=metadata, user_message="ok closing — mitigation worked"
    )

    assert case.pending_transition is not None
    assert case.pending_transition["to_state"] == "closed"
    assert case.pending_transition.get("closure_reason") == "closed_after_investigation"


@pytest.mark.asyncio
async def test_llm_emit_resolved_pivots_to_close_when_thin():
    """LLM proposes RESOLVED on a thin case (no root cause / solution /
    evidence). Engine must pivot to CLOSED, propose closed, and override
    suggestions with the close pair. Mirrors the UI dropdown SUGGEST_CLOSE
    behavior so the user sees a coherent prompt + suggestion pair."""
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
    )
    case = _make_investigating_case()
    case.progress.symptom_verified = True
    # Deliberately thin — no root cause, no solutions, no evidence.

    fake_response = MagicMock()
    fake_response.state_updates.proposed_transition = MagicMock(
        to_state="resolved",
        evidence_ids=[],
    )
    metadata: dict = {"response_obj": fake_response}

    await engine._check_automatic_transitions(
        case=case, metadata=metadata, user_message="The fix worked."
    )

    assert case.pending_transition is not None
    assert case.pending_transition["to_state"] == "closed"
    assert case.pending_transition.get("closure_reason") == "closed_after_investigation"
    # NEEDS_INFO first-pass flag must NOT be set on a SUGGEST_CLOSE pivot —
    # the response builder distinguishes the two paths.
    assert not metadata.get("resolution_needs_info_first_pass")
    _assert_canonical_confirm_pair(metadata["override_suggestions"], "close")


@pytest.mark.asyncio
async def test_llm_emit_closed_pivots_to_resolved_when_resolution_grade():
    """Symmetric to ``test_llm_emit_resolved_pivots_to_close_when_thin``.
    LLM proposes CLOSED on a case that has root cause + solution. Engine
    must pivot to RESOLVED, propose resolved, and override suggestions
    with the resolve pair so the user's confirm click preserves
    resolution attribution rather than discarding it."""
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
    )
    case = _make_investigating_case()
    case.progress.symptom_verified = True
    _fill_for_resolution_ready(case)  # root cause + solution → resolution-grade

    fake_response = MagicMock()
    fake_response.state_updates.proposed_transition = MagicMock(
        to_state="closed",
        evidence_ids=[],
    )
    metadata: dict = {"response_obj": fake_response}

    await engine._check_automatic_transitions(
        case=case, metadata=metadata, user_message="Let's close this."
    )

    assert case.pending_transition is not None
    assert case.pending_transition["to_state"] == "resolved", (
        "LLM-emit SUGGEST_RESOLVE pivot did not fire: the agent proposed "
        "CLOSED but the case has root cause + solution. Engine should "
        "have pivoted to propose RESOLVED so the user's confirm preserves "
        "resolution attribution."
    )
    # closure_reason is None for RESOLVED
    assert case.pending_transition.get("closure_reason") is None
    _assert_canonical_confirm_pair(metadata["override_suggestions"], "resolved")


@pytest.mark.asyncio
async def test_llm_emit_resolved_needs_info_keeps_resolve_with_flag():
    """LLM proposes RESOLVED on a case with substance (root cause + evidence)
    but no ``causal_absence_evidence`` row (NEEDS_INFO). Engine keeps the
    resolve intent, sets needs_info=True, and surfaces the readiness message
    via the resolution_needs_info_first_pass metadata so the response builder
    can override the LLM's agent_response."""
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
    )
    case = _make_investigating_case()
    case.progress.symptom_verified = True
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="Alignment test root cause",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        mechanism="Test mechanism",
    )
    case.evidence.append(
        Evidence(
            summary="Test evidence",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_at=datetime.now(timezone.utc),
            collected_by="user_test",
            primary_purpose="Alignment test",
            preprocessed_content="Sample log line",
            content_size_bytes=100,
            preprocessing_method="manual",
            source_file_id=None,
            collected_at_turn=1,
        )
    )

    fake_response = MagicMock()
    fake_response.state_updates.proposed_transition = MagicMock(
        to_state="resolved",
        evidence_ids=[],
    )
    metadata: dict = {"response_obj": fake_response}

    await engine._check_automatic_transitions(
        case=case, metadata=metadata, user_message="The fix worked."
    )

    assert case.pending_transition is not None
    assert case.pending_transition["to_state"] == "resolved"
    assert case.pending_transition.get("needs_info") is True
    assert metadata.get("resolution_needs_info_first_pass") is True
    assert metadata.get("resolution_needs_info_message")
    _assert_canonical_confirm_pair(metadata["override_suggestions"], "resolved")


@pytest.mark.asyncio
async def test_llm_emit_resolved_ready_keeps_resolve_pair():
    """LLM proposes RESOLVED on a READY case (root cause + solution).
    Engine proposes RESOLVED with the canonical resolve confirmation
    pair, no needs_info, no pivot."""
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
    )
    case = _make_investigating_case()
    case.progress.symptom_verified = True
    _fill_for_resolution_ready(case)

    fake_response = MagicMock()
    fake_response.state_updates.proposed_transition = MagicMock(
        to_state="resolved",
        evidence_ids=[],
    )
    metadata: dict = {"response_obj": fake_response}

    await engine._check_automatic_transitions(
        case=case, metadata=metadata, user_message="The fix worked."
    )

    assert case.pending_transition is not None
    assert case.pending_transition["to_state"] == "resolved"
    assert not case.pending_transition.get("needs_info")
    assert not metadata.get("resolution_needs_info_first_pass")
    _assert_canonical_confirm_pair(metadata["override_suggestions"], "resolved")


@pytest.mark.asyncio
async def test_check_automatic_transitions_no_override_when_no_proposal():
    """Common case: LLM did not emit proposed_transition. Engine must not
    set override_suggestions, leaving the LLM's follow-ups untouched."""
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
    )
    case = _make_investigating_case()

    fake_response = MagicMock()
    fake_response.state_updates.proposed_transition = None
    metadata: dict = {"response_obj": fake_response}

    await engine._check_automatic_transitions(
        case=case, metadata=metadata, user_message="Let me check the logs."
    )

    assert case.pending_transition is None
    assert "override_suggestions" not in metadata
    assert metadata.get("transition_proposed_this_turn") is not True


# ============================================================
# derive_closure_reason — Phase 3 insufficient-evidence capture
# ============================================================


class TestDeriveClosureReasonInsufficientEvidence:
    """``derive_closure_reason`` reads the persisted verification status so a
    case closed from the INSUFFICIENT_EVIDENCE cell is captured distinctly.

    Capture-on-close only: the engine never nudges toward this close; it just
    labels it honestly when the user closes such a case (§5.4 / D4).
    """

    def test_insufficient_evidence_cell_yields_distinct_reason(self):
        from faultmaven.core.investigation.terminal_transitions import (
            derive_closure_reason,
        )
        from faultmaven.modules.case.domain.models import VerificationStatus

        case = _make_investigating_case()
        case.progress.verification_status = VerificationStatus.INSUFFICIENT_EVIDENCE

        assert derive_closure_reason(case) == "closed_insufficient_evidence"

    def test_other_investigating_status_yields_after_investigation(self):
        from faultmaven.core.investigation.terminal_transitions import (
            derive_closure_reason,
        )
        from faultmaven.modules.case.domain.models import VerificationStatus

        case = _make_investigating_case()
        # Any non-insufficient status closes as the generic investigated reason.
        case.progress.verification_status = VerificationStatus.OPEN
        assert derive_closure_reason(case) == "closed_after_investigation"

    def test_inquiry_still_wins_over_status(self):
        from faultmaven.core.investigation.terminal_transitions import (
            derive_closure_reason,
        )
        from faultmaven.modules.case.domain.models import VerificationStatus

        case = _make_inquiry_case()
        case.progress = InvestigationProgress(
            verification_status=VerificationStatus.INSUFFICIENT_EVIDENCE
        )
        # INQUIRY has no investigation — the state gate takes precedence.
        assert derive_closure_reason(case) == "inquiry_only"
