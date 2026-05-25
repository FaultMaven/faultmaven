"""Tests for Gate 2 (path-selection) semantics.

Pins three invariants:

- INV-19: INQUIRY -> INVESTIGATING requires ``case.path_selection`` to exist
  (existence IS the commit signal — the path is created exclusively at the
  Gate 2 click handler).
- ``case.path_selection`` is None during INQUIRY until the user clicks a
  Gate 2 COOPERATIVE button.
- The Gate 2 recommendation is computed on-demand at button-render time via
  ``recommend_investigation_path_for_case`` — pure helper, no mutation.
"""

from __future__ import annotations

from faultmaven.core.investigation.milestone_engine import (
    _gate2_is_pending,
    _path_selection_suggestions,
)
from faultmaven.modules.case.contracts import CaseStatus
from faultmaven.modules.case.domain.models import (
    Case,
    InquiryData,
    InvestigationPath,
    PathSelection,
    PreliminaryUrgency,
    ProblemConfirmation,
    UrgencyLevel,
)
from faultmaven.modules.case.domain.services.investigation_router import (
    recommend_investigation_path_for_case,
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
    status: CaseStatus = CaseStatus.INQUIRY,
    symptom_verified: bool = False,
) -> Case:
    """Build a Case with controllable gate inputs. Defaults to INQUIRY
    state; pass ``status=CaseStatus.INVESTIGATING`` to construct a
    post-transition case (used for the new Gate-2-in-INVESTIGATING
    state-machine timing).
    """
    from faultmaven.modules.case.contracts import InvestigationProgress

    inquiry = InquiryData(
        problem_statement_confirmed=problem_statement_confirmed,
        proposed_problem_statement=proposed_statement,
        problem_confirmation=ProblemConfirmation(
            problem_type="unavailability",
            severity_guess="high",
            preliminary_guidance="API down",
        ),
        decided_to_investigate=(status == CaseStatus.INVESTIGATING),
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
        status=status,
        description=proposed_statement,
        inquiry=inquiry,
        progress=InvestigationProgress(symptom_verified=symptom_verified),
    )
    if path_selection is not None:
        case.path_selection = path_selection
    return case


# ---------------------------------------------------------------------------
# recommend_investigation_path_for_case — pure helper
# ---------------------------------------------------------------------------


class TestRecommendInvestigationPathForCase:
    """`recommend_investigation_path_for_case` returns a PathSelection
    derived from current case state (preliminary_urgency + temporal). It
    is PURE — does not write to case.path_selection. Used at Gate 2
    button-render time and inside the Gate 2 click handler."""

    def test_returns_recommendation_when_prerequisites_met(self):
        case = _make_case(
            problem_statement_confirmed=True,
            urgency=UrgencyLevel.CRITICAL,
            is_ongoing=True,
        )
        assert case.path_selection is None  # pre-condition: not yet committed

        rec = recommend_investigation_path_for_case(case)

        assert rec is not None
        assert rec.path == InvestigationPath.MITIGATION_FIRST
        # The recommendation has not been committed to the case
        assert case.path_selection is None

    def test_returns_none_when_gate1_not_passed(self):
        case = _make_case(problem_statement_confirmed=False)
        assert recommend_investigation_path_for_case(case) is None
        assert case.path_selection is None

    def test_returns_none_when_no_urgency_signals(self):
        case = _make_case(problem_statement_confirmed=True, urgency=None)
        assert recommend_investigation_path_for_case(case) is None
        assert case.path_selection is None

    def test_does_not_mutate_case(self):
        """Calling the helper twice yields the same result without mutating
        case state. This is the property the on-demand surface relies on."""
        case = _make_case(
            problem_statement_confirmed=True,
            urgency=UrgencyLevel.CRITICAL,
            is_ongoing=True,
        )
        rec1 = recommend_investigation_path_for_case(case)
        rec2 = recommend_investigation_path_for_case(case)

        assert case.path_selection is None
        assert rec1.path == rec2.path
        assert rec1.rationale == rec2.rationale

    def test_handles_unknown_urgency_via_router_default(self):
        case = _make_case(
            problem_statement_confirmed=True, urgency=UrgencyLevel.UNKNOWN
        )
        rec = recommend_investigation_path_for_case(case)
        assert rec is not None
        assert rec.path == InvestigationPath.ROOT_CAUSE
        assert rec.auto_selected is False


# ---------------------------------------------------------------------------
# _gate2_is_pending
# ---------------------------------------------------------------------------


class TestGate2IsPending:
    """`_gate2_is_pending` returns True when the user needs to click a
    path button. Post-redesign, the gate fires in INVESTIGATING after
    ``symptom_verified`` — the path choice is made on data-verified
    urgency, not on user-claimed urgency at INQUIRY."""

    def test_pending_when_investigating_symptom_verified_and_no_path(self):
        case = _make_case(
            status=CaseStatus.INVESTIGATING,
            symptom_verified=True,
            path_selection=None,
        )
        assert _gate2_is_pending(case) is True

    def test_not_pending_when_symptom_not_yet_verified(self):
        """Pre-symptom-verified, the agent is still in Zone 1 — Gate 2
        affordance has not yet opened. The agent's job is symptom
        verification first."""
        case = _make_case(
            status=CaseStatus.INVESTIGATING,
            symptom_verified=False,
            path_selection=None,
        )
        assert _gate2_is_pending(case) is False

    def test_not_pending_when_still_in_inquiry(self):
        """Pre-redesign Gate 2 fired in INQUIRY; post-redesign it does
        not. Confirms the migration: INQUIRY-stage cases never trip the
        Gate 2 predicate."""
        case = _make_case(problem_statement_confirmed=True)
        assert _gate2_is_pending(case) is False

    def test_not_pending_when_path_committed(self):
        """Path_selection existence IS the commit. If it exists, Gate 2
        has passed and the gate is no longer pending."""
        committed = PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale="Ongoing critical",
            alternate_path=InvestigationPath.ROOT_CAUSE,
            selected_by="u1",
        )
        case = _make_case(
            status=CaseStatus.INVESTIGATING,
            symptom_verified=True,
            path_selection=committed,
        )
        assert _gate2_is_pending(case) is False


# ---------------------------------------------------------------------------
# _path_selection_suggestions — on-demand recommendation render
# ---------------------------------------------------------------------------


class TestPathSelectionSuggestions:
    """`_path_selection_suggestions` emits two COOPERATIVE suggestions for
    Gate 2, computing the recommendation on-demand. After the refactor it
    no longer reads ``case.path_selection`` — it calls the pure helper."""

    def test_mitigation_first_recommended_for_ongoing_critical(self):
        case = _make_case(
            problem_statement_confirmed=True,
            urgency=UrgencyLevel.CRITICAL,
            is_ongoing=True,
        )
        suggs = _path_selection_suggestions(case)
        assert len(suggs) == 2
        assert suggs[0]["action_type"] == "COOPERATIVE"
        assert suggs[0]["intent"]["type"] == "path_selection"
        assert suggs[0]["intent"]["investigation_path"] == "mitigation_first"
        assert suggs[1]["intent"]["investigation_path"] == "root_cause"

    def test_root_cause_recommended_for_low_priority(self):
        case = _make_case(
            problem_statement_confirmed=True,
            urgency=UrgencyLevel.LOW,
            is_ongoing=True,
        )
        suggs = _path_selection_suggestions(case)
        assert suggs[0]["intent"]["investigation_path"] == "root_cause"
        assert suggs[1]["intent"]["investigation_path"] == "mitigation_first"

    def test_rationale_appears_in_recommended_body(self):
        case = _make_case(
            problem_statement_confirmed=True,
            urgency=UrgencyLevel.CRITICAL,
            is_ongoing=True,
        )
        rec = recommend_investigation_path_for_case(case)
        suggs = _path_selection_suggestions(case)
        assert rec.rationale in suggs[0]["body"]

    def test_returns_empty_when_prerequisites_not_met(self):
        """No preliminary_urgency → no recommendation → no suggestions."""
        case = _make_case(problem_statement_confirmed=True, urgency=None)
        assert _path_selection_suggestions(case) == []

    def test_returns_empty_when_gate1_not_passed(self):
        case = _make_case(problem_statement_confirmed=False)
        assert _path_selection_suggestions(case) == []

    def test_does_not_write_to_case(self):
        """Rendering buttons must not mutate case.path_selection. Only the
        Gate 2 click handler writes."""
        case = _make_case(
            problem_statement_confirmed=True,
            urgency=UrgencyLevel.CRITICAL,
            is_ongoing=True,
        )
        _path_selection_suggestions(case)
        assert case.path_selection is None


# ---------------------------------------------------------------------------
# INV-19: path_selection existence IS the commit signal
# ---------------------------------------------------------------------------


class TestINV19PathSelectionAsCommitSignal:
    """``case.path_selection is not None`` means "Gate 2 has been
    committed." No separate user_confirmed field — existence IS the
    commit. Post-redesign, the commit happens in INVESTIGATING after
    ``symptom_verified`` (the path choice is data-grounded)."""

    def test_investigating_pre_symptom_verified_is_pre_commit(self):
        """Just-transitioned INVESTIGATING case: path_selection is
        None; the Gate 2 affordance is not yet open (waiting for
        symptom_verified)."""
        case = _make_case(
            status=CaseStatus.INVESTIGATING,
            symptom_verified=False,
        )
        assert case.path_selection is None
        # Gate 2 is closed; the agent is still in Zone 1.
        assert _gate2_is_pending(case) is False

    def test_investigating_post_symptom_verified_is_gate2_pending(self):
        """Symptom verified, path not yet committed → Gate 2 open."""
        case = _make_case(
            status=CaseStatus.INVESTIGATING,
            symptom_verified=True,
        )
        assert case.path_selection is None
        assert _gate2_is_pending(case) is True

    def test_committed_path_selection_satisfies_gate2(self):
        """After Gate 2 click creates path_selection, _gate2_is_pending
        returns False."""
        committed = PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale="Ongoing critical",
            alternate_path=InvestigationPath.ROOT_CAUSE,
            selected_by="u1",
        )
        case = _make_case(
            status=CaseStatus.INVESTIGATING,
            symptom_verified=True,
            path_selection=committed,
        )
        assert case.path_selection is not None
        assert _gate2_is_pending(case) is False
