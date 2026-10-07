"""Gate 1 records severity only when the user's problem confirmation gave one.

Severity and urgency are different axes (the user's assessment vs business
impact). The record used to default severity to MEDIUM and then overwrite that
default with the urgency level, so an explicit "medium" could not be told from
"never assessed". #1877.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine.transitions import (
    TransitionManager,
)
from faultmaven.core.investigation.problem_status import edit_statement
from faultmaven.core.investigation.prompts.context_builder.evidence import (
    _render_problem_context,
)
from faultmaven.core.investigation.prompts.fence import PromptFence, mint_token
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InquiryData,
    InvestigationProgress,
)
from faultmaven.modules.case.domain.models.problem import (
    PreliminaryUrgency,
    ProblemConfirmation,
    ProblemVerification,
    TemporalState,
    UrgencyLevel,
)

pytestmark = pytest.mark.unit


def _case(severity_guess=None, urgency=None) -> Case:
    inquiry = InquiryData(
        proposed_problem_statement="Checkout returns 500s",
        problem_statement_confirmed=True,
    )
    if severity_guess is not None:
        inquiry.problem_confirmation = ProblemConfirmation(
            problem_type="unavailability", severity_guess=severity_guess
        )
    if urgency is not None:
        inquiry.preliminary_urgency = PreliminaryUrgency(
            level=urgency,
            is_ongoing=True,
            impact_assessment="checkout down",
            assessed_at_turn=1,
        )
    return Case(
        case_id="case_1234567890ab",
        title="Checkout",
        state=CaseState.INQUIRY,
        user_id="u1",
        enterprise_id="e1",
        description="",
        inquiry=inquiry,
    )


async def _gate1(case: Case) -> ProblemVerification:
    deps = MagicMock()
    deps.checkpoint_service = None
    prefetcher = MagicMock()
    prefetcher.prefetch_kb_context = AsyncMock()
    await TransitionManager(
        deps=deps, kb_prefetcher=prefetcher
    )._transition_to_investigating(case)
    assert case.problem_verification is not None
    return case.problem_verification


async def test_unknown_guess_with_high_urgency_leaves_severity_unassessed():
    pv = await _gate1(_case("unknown", UrgencyLevel.HIGH))
    assert pv.severity is None
    assert pv.urgency_level == UrgencyLevel.HIGH
    assert pv.temporal_state == TemporalState.ONGOING


async def test_an_explicit_medium_is_not_overwritten_by_urgency():
    pv = await _gate1(_case("medium", UrgencyLevel.CRITICAL))
    assert pv.severity == "MEDIUM"
    assert pv.urgency_level == UrgencyLevel.CRITICAL


async def test_an_explicit_guess_is_kept_upper_case():
    pv = await _gate1(_case("high", UrgencyLevel.LOW))
    assert pv.severity == "HIGH"
    assert pv.urgency_level == UrgencyLevel.LOW


async def test_no_problem_confirmation_means_no_severity_and_no_prompt_line():
    case = _case(None, None)
    pv = await _gate1(case)
    assert pv.severity is None
    rendered = _render_problem_context(case, PromptFence(mint_token()))
    assert "SYMPTOM_STATEMENT" in rendered
    assert "SEVERITY" not in rendered


async def test_an_assessed_severity_reaches_the_prompt():
    case = _case("high", None)
    await _gate1(case)
    rendered = _render_problem_context(case, PromptFence(mint_token()))
    assert "SEVERITY: HIGH" in rendered


def test_editing_a_malformed_case_creates_the_record_without_severity():
    case = _case("high", UrgencyLevel.HIGH)
    case.state = CaseState.INQUIRY
    assert case.problem_verification is None
    case.progress = InvestigationProgress()
    edit_statement(case, "Checkout returns 500s on card payments")
    assert case.problem_verification is not None
    assert case.problem_verification.severity is None


def test_a_record_saved_before_the_removal_still_loads():
    """Old persisted JSON carries removed keys and an assessed severity."""
    stored = json.loads(
        json.dumps(
            {
                "symptom_statement": "Checkout returns 500s",
                "severity": "MEDIUM",
                "symptom_indicators": ["Error rate: 15%"],
                "affected_users": "all users",
                "affected_regions": ["eu-west-1"],
                "user_impact": "cannot pay",
                "started_at": "2026-09-01T10:00:00Z",
                "noticed_at": "2026-09-01T10:05:00Z",
                "resolved_naturally_at": None,
                "duration": "PT1H",
                "recent_changes": [],
                "correlations": [],
                "correlation_confidence": 0.0,
                "urgency_factors": ["revenue"],
                "verified_at": None,
                "verification_confidence": 0.5,
                "urgency_level": "high",
                "temporal_state": "ongoing",
            }
        )
    )
    pv = ProblemVerification(**stored)
    assert pv.severity == "MEDIUM"
    assert pv.urgency_level == UrgencyLevel.HIGH
    assert not hasattr(pv, "affected_users")
    assert "affected_users" not in pv.model_dump()


def test_a_record_with_no_severity_loads():
    assert ProblemVerification(symptom_statement="x").severity is None
