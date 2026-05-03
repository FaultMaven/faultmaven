"""Tests for state-transition path alignment.

The three triggers for a state transition (UI dropdown click, agent-initiated
proposal, LLM-emitted ``proposed_transition``) must all converge on the same
deterministic confirmation UX: a COOPERATIVE confirm/decline pair carrying
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
    CaseStatus,
    InvestigationProgress,
    ProblemVerification,
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
        status=CaseStatus.INQUIRY,
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
    case.status = CaseStatus.INVESTIGATING
    case.progress = InvestigationProgress()
    return case


def _assert_canonical_confirm_pair(suggestions, expected_label_substring):
    """Every alignment site must emit exactly two COOPERATIVE suggestions
    carrying confirmation intent metadata."""
    assert len(suggestions) == 2
    assert all(s["action_type"] == "COOPERATIVE" for s in suggestions)
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
        evidence_service=MagicMock(),
    )
    case = _make_inquiry_case()
    result = await engine.process_turn(
        case=case,
        user_message="Close this case.",
        intent_type="status_transition",
        intent_data={
            "from_status": "inquiry",
            "to_status": "closed",
            "user_confirmed": True,
        },
    )
    assert result["case_updated"].pending_transition["to_status"] == "closed"
    _assert_canonical_confirm_pair(result["suggested_follow_ups"], "close")


@pytest.mark.asyncio
async def test_ui_dropdown_investigating_to_closed_emits_canonical_close_pair():
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
        evidence_service=MagicMock(),
    )
    case = _make_investigating_case()
    case.progress.symptom_verified = True
    result = await engine.process_turn(
        case=case,
        user_message="Close this case as unresolved.",
        intent_type="status_transition",
        intent_data={
            "from_status": "investigating",
            "to_status": "closed",
            "user_confirmed": True,
        },
    )
    assert result["case_updated"].pending_transition["to_status"] == "closed"
    _assert_canonical_confirm_pair(result["suggested_follow_ups"], "close")


@pytest.mark.asyncio
async def test_ui_dropdown_investigating_to_resolved_emits_canonical_resolve_pair():
    """INVESTIGATING → RESOLVED via dropdown — both ready and not-ready
    branches must emit the canonical confirm/decline pair."""
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
        evidence_service=MagicMock(),
    )
    case = _make_investigating_case()
    case.progress.symptom_verified = True

    result = await engine.process_turn(
        case=case,
        user_message="Mark as resolved.",
        intent_type="status_transition",
        intent_data={
            "from_status": "investigating",
            "to_status": "resolved",
            "user_confirmed": True,
        },
    )
    # In an unfilled case this typically lands in the not-ready (needs_info)
    # branch; both branches return the canonical RESOLVED pair under
    # alignment.
    assert result["case_updated"].pending_transition is not None
    assert result["case_updated"].pending_transition["to_status"] == "resolved"
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
        evidence_service=MagicMock(),
    )
    case = _make_investigating_case()
    case.progress.symptom_verified = True

    fake_response = MagicMock()
    fake_response.state_updates.proposed_transition = MagicMock(
        to_status="resolved",
        reason="User indicated the fix worked",
        summary="Resolution criteria appear to be met.",
        evidence_ids=[],
    )
    metadata: dict = {"response_obj": fake_response}

    await engine._check_automatic_transitions(
        case=case, metadata=metadata, user_message="The fix worked."
    )

    assert case.pending_transition is not None
    assert case.pending_transition["to_status"] == "resolved"
    assert metadata.get("transition_proposed") is True
    _assert_canonical_confirm_pair(metadata["override_suggestions"], "resolved")


@pytest.mark.asyncio
async def test_check_automatic_transitions_sets_override_for_closed():
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
        evidence_service=MagicMock(),
    )
    case = _make_investigating_case()

    fake_response = MagicMock()
    fake_response.state_updates.proposed_transition = MagicMock(
        to_status="closed",
        reason="User asked to close without resolving",
        summary="Closing without solution.",
        evidence_ids=[],
    )
    metadata: dict = {"response_obj": fake_response}

    await engine._check_automatic_transitions(
        case=case, metadata=metadata, user_message="Close as unresolved."
    )

    assert case.pending_transition is not None
    assert case.pending_transition["to_status"] == "closed"
    _assert_canonical_confirm_pair(metadata["override_suggestions"], "close")


@pytest.mark.asyncio
async def test_check_automatic_transitions_no_override_when_no_proposal():
    """Common case: LLM did not emit proposed_transition. Engine must not
    set override_suggestions, leaving the LLM's follow-ups untouched."""
    engine = MilestoneEngine(
        MagicMock(),
        _make_repo(),
        investigation_tools=MagicMock(),
        evidence_service=MagicMock(),
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
    assert metadata.get("transition_proposed") is not True
