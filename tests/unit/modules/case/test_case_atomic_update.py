"""Tests for Case.atomic_update — verifies post-update revalidation.

atomic_update bypasses per-field validators (so callers can move multiple
interdependent fields through the transient invalid state) but must still
catch incomplete transitions in its final state. Without this guard, callers
that forget a required field (e.g., status=RESOLVED without resolved_at)
would persist schema-invalid rows.
"""

from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    InquiryData,
    InvestigationProgress,
    ProblemVerification,
)


def _make_investigating_case() -> Case:
    return Case(
        case_id="case_abcdef123456",
        title="Test Case",
        state=CaseState.INVESTIGATING,
        user_id="user_test",
        enterprise_id="org_test",
        description="Investigating something concrete",
        progress=InvestigationProgress(),
        problem_verification=ProblemVerification(
            symptom_statement="Test symptom",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            thread_id="thread_test",
            proposed_problem_statement="Test symptom",
        ),
    )


class TestAtomicUpdateValidation:
    def test_full_resolved_transition_succeeds(self):
        case = _make_investigating_case()
        now = datetime.now(timezone.utc)
        case.atomic_update(
            state=CaseState.RESOLVED,
            resolved_at=now,
            closed_at=now,
        )
        assert case.state == CaseState.RESOLVED
        assert case.resolved_at == now
        assert case.closed_at == now

    def test_resolved_without_timestamps_raises(self):
        case = _make_investigating_case()
        with pytest.raises(ValueError, match="RESOLVED.*resolved_at"):
            case.atomic_update(state=CaseState.RESOLVED)

    def test_resolved_with_only_resolved_at_raises(self):
        case = _make_investigating_case()
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="closed_at"):
            case.atomic_update(state=CaseState.RESOLVED, resolved_at=now)

    def test_failed_update_rolls_back_state(self):
        case = _make_investigating_case()
        original_status = case.state
        original_resolved_at = case.resolved_at
        with pytest.raises(ValueError):
            case.atomic_update(state=CaseState.RESOLVED)
        assert case.state == original_status
        assert case.resolved_at == original_resolved_at

    def test_failed_update_rolls_back_partial_changes(self):
        case = _make_investigating_case()
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError):
            # status=RESOLVED + resolved_at, but no closed_at — invalid
            case.atomic_update(state=CaseState.RESOLVED, resolved_at=now)
        assert case.state == CaseState.INVESTIGATING
        assert case.resolved_at is None

    def test_closed_with_closure_reason_succeeds(self):
        case = _make_investigating_case()
        now = datetime.now(timezone.utc)
        case.atomic_update(
            state=CaseState.CLOSED,
            closed_at=now,
            closure_reason="abandoned",
        )
        assert case.state == CaseState.CLOSED
        assert case.closure_reason == "abandoned"

    def test_closed_without_closure_reason_raises(self):
        case = _make_investigating_case()
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="closure_reason"):
            case.atomic_update(state=CaseState.CLOSED, closed_at=now)

    def test_inverted_timestamps_raise_and_roll_back(self):
        case = _make_investigating_case()
        # Pin created_at far enough back that both candidate timestamps below
        # are after it — isolates the resolved_at-vs-closed_at ordering rule.
        anchor = datetime.now(timezone.utc) - timedelta(days=10)
        object.__setattr__(case, "created_at", anchor)
        object.__setattr__(case, "updated_at", anchor)
        object.__setattr__(case, "last_activity_at", anchor)

        later = anchor + timedelta(days=2)
        earlier = anchor + timedelta(days=1)
        # resolved_at after closed_at violates timestamp ordering
        with pytest.raises(ValueError, match="resolved_at.*cannot be after closed_at"):
            case.atomic_update(
                state=CaseState.RESOLVED,
                resolved_at=later,
                closed_at=earlier,
            )
        assert case.state == CaseState.INVESTIGATING
        assert case.resolved_at is None
        assert case.closed_at is None
