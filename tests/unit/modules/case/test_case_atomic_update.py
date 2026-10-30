"""Tests for Case.atomic_update — the final state is validated as one case.

atomic_update exists for the transitions whose fields cannot be assigned one
at a time (state=RESOLVED needs resolved_at, and resolved_at needs RESOLVED).
It used to set the fields around validation and re-run three model
validators, so a field's own type and validators never ran: a closure reason
outside the vocabulary was written, and the case could not be loaded again
(#1795). It now validates the merged state as one ``Case`` and writes nothing
unless that passes.
"""

from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.lifecycle import CaseState, ClosureReason
from tests.case_builders import investigating_case


def _make_investigating_case() -> Case:
    return investigating_case(
        case_id="case_abcdef123456",
        title="Test Case",
        user_id="user_test",
        enterprise_id="org_test",
        description="Investigating something concrete",
        symptom="Test symptom",
        temporal_state="ongoing",
        urgency="high",
        statement="Test symptom",
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
            closure_reason="closed_insufficient_evidence",
        )
        assert case.state == CaseState.CLOSED
        assert case.closure_reason == "closed_insufficient_evidence"

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


def _fields(case: Case) -> dict:
    return {name: getattr(case, name) for name in Case.model_fields}


class TestAnInvalidFinalStateWritesNothing:
    """#1795: each of these was written, or half-written, before."""

    @pytest.mark.parametrize(
        "updates",
        [
            pytest.param({"closure_reason": "abandoned"}, id="unknown-reason"),
            pytest.param({"closure_reason": 42}, id="wrong-type"),
        ],
    )
    def test_a_close_with_a_bad_reason_raises_and_changes_nothing(self, updates):
        case = _make_investigating_case()
        before = _fields(case)
        with pytest.raises(ValueError, match="closure_reason"):
            case.atomic_update(
                state=CaseState.CLOSED, closed_at=datetime.now(timezone.utc), **updates
            )
        assert _fields(case) == before

    def test_a_blank_title_raises_and_changes_nothing(self):
        case = _make_investigating_case()
        with pytest.raises(ValueError, match="title"):
            case.atomic_update(title="   ")
        assert case.title == "Test Case"

    def test_resolved_with_a_reason_raises_and_changes_nothing(self):
        """A control: the one model rule the old path did re-run."""
        case = _make_investigating_case()
        before = _fields(case)
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="closure_reason"):
            case.atomic_update(
                state=CaseState.RESOLVED,
                resolved_at=now,
                closed_at=now,
                closure_reason=ClosureReason.INQUIRY_ONLY,
            )
        assert _fields(case) == before

    def test_an_invalid_nested_model_raises(self):
        """Nested after-validators re-run on the merged state."""
        case = _make_investigating_case()
        case.inquiry.proposed_problem_statement = ""  # confirmed, now empty
        with pytest.raises(ValueError, match="proposed_problem_statement"):
            case.atomic_update(title="Renamed")
        assert case.title == "Test Case"

    def test_an_unknown_key_is_refused_before_any_field_is_written(self):
        """``status`` is not a field, and the documented example passed it. It
        is named before the merged state is validated, which would ignore it,
        and before any field is copied."""
        case = _make_investigating_case()
        with pytest.raises(TypeError, match="status"):
            case.atomic_update(title="Renamed", status=CaseState.CLOSED)
        assert case.title == "Test Case"
        assert "status" not in vars(case)


class TestAClosedCaseReloads:
    def test_the_reason_is_stored_as_the_enum_and_the_case_reloads(self):
        case = _make_investigating_case()
        case.atomic_update(
            state=CaseState.CLOSED,
            closed_at=datetime.now(timezone.utc),
            closure_reason="mitigation_sufficient",
        )
        assert case.closure_reason is ClosureReason.MITIGATION_SUFFICIENT
        reloaded = Case(**case.model_dump())
        assert reloaded.closure_reason is ClosureReason.MITIGATION_SUFFICIENT
