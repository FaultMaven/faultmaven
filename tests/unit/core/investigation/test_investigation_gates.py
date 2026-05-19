"""Tests for INV-19 and INV-20: Gate 2 (path-selection) enforcement.

Pins the two new invariants introduced in slice 2 of the investigation-gates
redesign:

- INV-19: INQUIRY -> INVESTIGATING requires path_selection.user_confirmed=True
  in addition to Gate 1 (problem statement confirmation).
- INV-20: changing preliminary_urgency.level or is_ongoing clears
  path_selection so Gate 2 re-fires with the updated recommendation.

Also covers the Gate 2 helpers (_compute_inquiry_path_selection,
_inquiry_path_signals_changed, _path_selection_suggestions) and the
PATH_SELECTION intent handling shape.

Design reference: docs/working/WIP-investigation-gates-implementation.md
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.milestone_engine import (
    _compute_inquiry_path_selection,
    _inquiry_path_signals_changed,
    _path_selection_suggestions,
)
from faultmaven.modules.case.domain.models import (
    Case,
    InquiryData,
    InvestigationPath,
    PathSelection,
    PreliminaryUrgency,
    ProblemConfirmation,
    UrgencyLevel,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_case(
    *,
    problem_statement_confirmed: bool = True,
    urgency: UrgencyLevel | None = UrgencyLevel.CRITICAL,
    is_ongoing: bool = True,
    proposed_statement: str = "Production API is returning 500s",
    path_selection: PathSelection | None = None,
) -> Case:
    """Build a Case in INQUIRY state with controllable Gate 1/2 inputs."""
    inquiry = InquiryData(
        problem_statement_confirmed=problem_statement_confirmed,
        proposed_problem_statement=proposed_statement,
        problem_confirmation=ProblemConfirmation(
            problem_type="unavailability",
            severity_guess="high",
            preliminary_guidance="API down",
        ),
    )
    if urgency is not None:
        inquiry.preliminary_urgency = PreliminaryUrgency(
            level=urgency,
            is_ongoing=is_ongoing,
            is_incident_report=is_ongoing,
            impact_assessment="prod outage",
            assessed_at_turn=1,
        )
    case = Case(
        user_id="u1",
        organization_id="o1",
        title="Test",
        description=proposed_statement,
        inquiry=inquiry,
    )
    if path_selection is not None:
        case.path_selection = path_selection
    return case


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestComputeInquiryPathSelection:
    """`_compute_inquiry_path_selection` populates path_selection when Gate 1
    has passed and urgency signals are present. Idempotent."""

    def test_computes_path_after_gate1_passed_with_signals(self):
        case = _make_case(
            problem_statement_confirmed=True,
            urgency=UrgencyLevel.CRITICAL,
            is_ongoing=True,
        )
        assert case.path_selection is None

        _compute_inquiry_path_selection(case)

        assert case.path_selection is not None
        assert case.path_selection.path == InvestigationPath.MITIGATION_FIRST
        assert case.path_selection.user_confirmed is False  # Gate 2 still pending
        assert case.path_selection.auto_selected is True

    def test_does_not_compute_before_gate1_passed(self):
        case = _make_case(problem_statement_confirmed=False)
        _compute_inquiry_path_selection(case)
        assert case.path_selection is None

    def test_does_not_compute_without_urgency_signals(self):
        case = _make_case(problem_statement_confirmed=True, urgency=None)
        _compute_inquiry_path_selection(case)
        assert case.path_selection is None

    def test_idempotent_when_path_selection_already_set(self):
        case = _make_case(problem_statement_confirmed=True)
        _compute_inquiry_path_selection(case)
        original_path = case.path_selection.path
        original_rationale = case.path_selection.rationale

        # Manually mark as user-confirmed
        case.path_selection = case.path_selection.model_copy(
            update={"user_confirmed": True, "user_confirmed_at_turn": 3}
        )

        # Re-call should be no-op (idempotent on path_selection is None)
        _compute_inquiry_path_selection(case)

        assert case.path_selection.path == original_path
        assert case.path_selection.rationale == original_rationale
        assert case.path_selection.user_confirmed is True  # preserved

    def test_handles_unknown_urgency_via_router_default(self):
        case = _make_case(
            problem_statement_confirmed=True, urgency=UrgencyLevel.UNKNOWN
        )
        _compute_inquiry_path_selection(case)
        assert case.path_selection is not None
        assert case.path_selection.path == InvestigationPath.ROOT_CAUSE
        assert case.path_selection.auto_selected is False


# ---------------------------------------------------------------------------
# INV-20: Mutation watcher
# ---------------------------------------------------------------------------


class TestINV20MutationWatcher:
    """`_inquiry_path_signals_changed` detects mutations to the two fields
    that drive path selection: `level` and `is_ongoing`. Mutations to other
    fields do NOT trigger invalidation."""

    def _pu(self, level=UrgencyLevel.HIGH, is_ongoing=True, impact="x"):
        return PreliminaryUrgency(
            level=level,
            is_ongoing=is_ongoing,
            is_incident_report=is_ongoing,
            impact_assessment=impact,
            assessed_at_turn=1,
        )

    def test_no_change_returns_false(self):
        pu = self._pu()
        assert _inquiry_path_signals_changed(pu, pu) is False

    def test_level_change_returns_true(self):
        old = self._pu(level=UrgencyLevel.CRITICAL)
        new = self._pu(level=UrgencyLevel.LOW)
        assert _inquiry_path_signals_changed(old, new) is True

    def test_is_ongoing_change_returns_true(self):
        old = self._pu(is_ongoing=True)
        new = self._pu(is_ongoing=False)
        assert _inquiry_path_signals_changed(old, new) is True

    def test_impact_assessment_change_does_not_invalidate(self):
        """Only level + is_ongoing drive the router; other fields are noise."""
        old = self._pu(impact="initial guess")
        new = self._pu(impact="refined description")
        assert _inquiry_path_signals_changed(old, new) is False

    def test_none_to_populated_returns_true(self):
        new = self._pu()
        assert _inquiry_path_signals_changed(None, new) is True

    def test_populated_to_none_returns_true(self):
        old = self._pu()
        assert _inquiry_path_signals_changed(old, None) is True

    def test_both_none_returns_false(self):
        assert _inquiry_path_signals_changed(None, None) is False


# ---------------------------------------------------------------------------
# Gate 2 suggestion pair
# ---------------------------------------------------------------------------


class TestPathSelectionSuggestions:
    """`_path_selection_suggestions` emits two COOPERATIVE suggestions for
    Gate 2, carrying PATH_SELECTION intents for the recommended and alternate
    paths respectively."""

    def test_mitigation_first_recommended(self):
        case = _make_case(urgency=UrgencyLevel.CRITICAL, is_ongoing=True)
        _compute_inquiry_path_selection(case)

        suggs = _path_selection_suggestions(case)
        assert len(suggs) == 2
        assert suggs[0]["action_type"] == "COOPERATIVE"
        assert suggs[0]["intent"]["type"] == "path_selection"
        assert suggs[0]["intent"]["investigation_path"] == "mitigation_first"
        assert suggs[1]["intent"]["investigation_path"] == "root_cause"

    def test_root_cause_recommended(self):
        case = _make_case(urgency=UrgencyLevel.LOW, is_ongoing=True)
        _compute_inquiry_path_selection(case)

        suggs = _path_selection_suggestions(case)
        assert suggs[0]["intent"]["investigation_path"] == "root_cause"
        assert suggs[1]["intent"]["investigation_path"] == "mitigation_first"

    def test_rationale_appears_in_recommended_body(self):
        case = _make_case(urgency=UrgencyLevel.CRITICAL, is_ongoing=True)
        _compute_inquiry_path_selection(case)
        suggs = _path_selection_suggestions(case)
        assert case.path_selection.rationale in suggs[0]["body"]

    def test_returns_empty_when_no_path_selection(self):
        case = _make_case(problem_statement_confirmed=True)
        # path_selection not computed
        assert _path_selection_suggestions(case) == []

    def test_returns_empty_when_no_alternate(self):
        """Defensive: router always sets alternate_path, but if some path
        emerged without one, we shouldn't emit a half-formed suggestion."""
        ps = PathSelection(
            path=InvestigationPath.ROOT_CAUSE,
            auto_selected=True,
            rationale="test",
            alternate_path=None,
        )
        case = _make_case(path_selection=ps)
        assert _path_selection_suggestions(case) == []


# ---------------------------------------------------------------------------
# PathSelection model_copy round-trip — the override pattern slice 2 uses
# ---------------------------------------------------------------------------


class TestPathSelectionOverride:
    """When the user picks the alternate path in Gate 2, the engine builds a
    new PathSelection with path/alternate_path swapped and user_confirmed=True.
    PathSelection is frozen, so model_copy is the canonical mutation path."""

    def test_accept_recommended_sets_user_confirmed(self):
        ps = PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale="Ongoing critical",
            alternate_path=InvestigationPath.ROOT_CAUSE,
        )
        new_ps = ps.model_copy(
            update={"user_confirmed": True, "user_confirmed_at_turn": 4}
        )
        assert new_ps.path == InvestigationPath.MITIGATION_FIRST  # unchanged
        assert new_ps.alternate_path == InvestigationPath.ROOT_CAUSE  # unchanged
        assert new_ps.user_confirmed is True
        assert new_ps.user_confirmed_at_turn == 4

    def test_override_to_alternate_swaps_path_and_alternate(self):
        ps = PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale="Ongoing critical",
            alternate_path=InvestigationPath.ROOT_CAUSE,
        )
        # User picked the alternate
        new_ps = ps.model_copy(
            update={
                "user_confirmed": True,
                "user_confirmed_at_turn": 4,
                "path": InvestigationPath.ROOT_CAUSE,
                "alternate_path": InvestigationPath.MITIGATION_FIRST,
                "selected_by": "u1",
            }
        )
        assert new_ps.path == InvestigationPath.ROOT_CAUSE
        assert new_ps.alternate_path == InvestigationPath.MITIGATION_FIRST
        assert new_ps.user_confirmed is True
        assert new_ps.selected_by == "u1"


# ---------------------------------------------------------------------------
# INV-19: Transition gate
# ---------------------------------------------------------------------------


class TestINV19GateTwoTransitionGate:
    """INV-19 — INQUIRY -> INVESTIGATING requires path_selection.user_confirmed=True.

    Tests the gate semantics at the data-model level (which fields the
    transition condition checks). Full engine-loop integration is exercised
    by the broader investigation-service tests that load this branch via
    `_check_automatic_transitions`.
    """

    def test_gate1_alone_does_not_satisfy_transition_condition(self):
        case = _make_case(problem_statement_confirmed=True)
        # No path_selection — Gate 2 cannot be passed
        assert case.inquiry.problem_statement_confirmed is True
        assert case.path_selection is None

        gate2_passed = (
            case.path_selection is not None and case.path_selection.user_confirmed
        )
        assert gate2_passed is False

    def test_gate1_plus_unconfirmed_path_does_not_satisfy(self):
        case = _make_case(problem_statement_confirmed=True)
        _compute_inquiry_path_selection(case)
        assert case.path_selection is not None
        assert case.path_selection.user_confirmed is False

        gate2_passed = (
            case.path_selection is not None and case.path_selection.user_confirmed
        )
        assert gate2_passed is False

    def test_gate1_plus_confirmed_path_satisfies(self):
        case = _make_case(problem_statement_confirmed=True)
        _compute_inquiry_path_selection(case)
        # Simulate user clicking Gate 2 suggestion
        case.path_selection = case.path_selection.model_copy(
            update={"user_confirmed": True, "user_confirmed_at_turn": 3}
        )

        gate2_passed = (
            case.path_selection is not None and case.path_selection.user_confirmed
        )
        assert gate2_passed is True


# ---------------------------------------------------------------------------
# End-to-end shape: turn-by-turn state advancement
# ---------------------------------------------------------------------------


class TestGateRoundTrip:
    """Walks the case through Gate 1 -> path computation -> Gate 2 confirm,
    using only the engine helpers (no LLM). Verifies the state advances
    correctly without an unexpected transition."""

    def test_full_round_trip_to_ready_for_transition(self):
        # Turn 1: user reports problem; no Gate 1 yet
        case = _make_case(problem_statement_confirmed=False)
        _compute_inquiry_path_selection(case)
        assert case.path_selection is None

        # Turn 2: user confirms problem (Gate 1)
        case.inquiry.problem_statement_confirmed = True
        _compute_inquiry_path_selection(case)
        assert case.path_selection is not None
        assert case.path_selection.user_confirmed is False
        recommended = case.path_selection.path

        # Turn 3: user clicks Gate 2 suggestion (recommended)
        case.path_selection = case.path_selection.model_copy(
            update={"user_confirmed": True, "user_confirmed_at_turn": 3}
        )
        assert case.path_selection.user_confirmed is True
        assert case.path_selection.path == recommended

    def test_user_overrides_to_alternate(self):
        case = _make_case(
            problem_statement_confirmed=True,
            urgency=UrgencyLevel.CRITICAL,
            is_ongoing=True,
        )
        _compute_inquiry_path_selection(case)
        assert case.path_selection.path == InvestigationPath.MITIGATION_FIRST

        # User picked the alternate via Gate 2
        ps = case.path_selection
        case.path_selection = ps.model_copy(
            update={
                "user_confirmed": True,
                "user_confirmed_at_turn": 3,
                "path": ps.alternate_path,
                "alternate_path": ps.path,
                "selected_by": "u1",
            }
        )
        assert case.path_selection.path == InvestigationPath.ROOT_CAUSE
        assert case.path_selection.user_confirmed is True

    def test_revision_after_gate2_passes_clears_path(self):
        """INV-20 scenario: user revises problem characterization (urgency),
        which mutates preliminary_urgency. The mutation watcher (run inside
        _apply_inquiry_updates in the engine) clears path_selection so Gate 2
        re-fires. This test simulates the watcher's effect directly."""
        case = _make_case(
            problem_statement_confirmed=True,
            urgency=UrgencyLevel.CRITICAL,
            is_ongoing=True,
        )
        _compute_inquiry_path_selection(case)
        # User confirms Gate 2
        case.path_selection = case.path_selection.model_copy(
            update={"user_confirmed": True, "user_confirmed_at_turn": 3}
        )

        # Turn 4: user revises — "actually, this isn't critical, it's historical."
        # The engine snapshots old preliminary_urgency, applies updates, then
        # the mutation watcher detects the change and clears path_selection.
        old_pu = case.inquiry.preliminary_urgency
        case.inquiry.preliminary_urgency = PreliminaryUrgency(
            level=UrgencyLevel.LOW,
            is_ongoing=False,
            is_incident_report=False,
            impact_assessment="post-mortem analysis",
            assessed_at_turn=4,
        )
        if _inquiry_path_signals_changed(old_pu, case.inquiry.preliminary_urgency):
            case.path_selection = None

        assert case.path_selection is None

        # Subsequent _compute call recomputes — Gate 2 re-fires next turn
        _compute_inquiry_path_selection(case)
        assert case.path_selection is not None
        assert case.path_selection.path == InvestigationPath.ROOT_CAUSE
        assert case.path_selection.user_confirmed is False  # gate re-opened
