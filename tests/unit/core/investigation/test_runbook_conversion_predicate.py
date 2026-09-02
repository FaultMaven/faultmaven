"""Gate-coherence tests for the canonical runbook-conversion readiness predicate.

The produce path had the same "may this case become a runbook?" bar re-derived
inline at several sites, which drifted (a CONFIRMED-but-content-thin case was
offered by one site and refused by another). ``runbook_conversion_ready`` is now
the single source of truth. These tests are mechanical and LLM-agnostic: they
pin that every gate resolves to the *same* verdict for the same case, across the
grade × problem-definition × actionable-solution matrix, so the offer boundary
and the enforcement boundary cannot drift apart (#698).

The load-bearing equivalence:

    _runbook_suggestion(case) is not None
        ⟺ runbook_conversion_ready(case)
        ⟺ assess_runbook_readiness(case).verdict != NOT_SUITABLE
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.core.investigation import cause_assurance, terminal_transitions
from faultmaven.core.investigation.cause_assurance import (
    CauseAssuranceGrade,
    has_actionable_solution,
    has_problem_definition,
    has_root_cause_record,
    runbook_conversion_ready,
)
from faultmaven.core.investigation.milestone_engine import _runbook_suggestion
from faultmaven.core.investigation.terminal_transitions import (
    RunbookReadiness,
    assess_runbook_readiness,
)


def _solution(*, actionable: bool):
    """A Solution-like object. Actionable ⟺ it carries commands/steps/longterm."""
    return SimpleNamespace(
        commands=["kubectl rollout restart"] if actionable else None,
        implementation_steps=None,
        longterm_fix=None,
        immediate_action=None,
        verification_method=None,
    )


def _make_case(*, problem_def: bool, rcc: bool, actionable: bool):
    """A minimal Case-like object exercising exactly the three content axes the
    predicate reads. The assurance grade is controlled by monkeypatch, not by
    building a real causal graph — these tests pin the *gate wiring*, not the
    grade computation (which has its own tests)."""
    return SimpleNamespace(
        case_id="case-1",
        problem_verification=(
            SimpleNamespace(symptom_statement="API returns 500s")
            if problem_def
            else None
        ),
        root_cause_conclusion=(
            SimpleNamespace(root_cause="connection pool exhausted") if rcc else None
        ),
        solutions=[_solution(actionable=actionable)],
        evidence=[],
        hypotheses=[],
        action_attempts=[],
    )


def _pin_grade(monkeypatch, grade: CauseAssuranceGrade) -> None:
    """Pin ``grade_cause_assurance`` in both modules that consume it, so the
    predicate (cause_assurance) and the content gate (terminal_transitions) see
    the same grade for the same case."""
    monkeypatch.setattr(cause_assurance, "grade_cause_assurance", lambda case: grade)
    monkeypatch.setattr(
        terminal_transitions, "grade_cause_assurance", lambda case: grade
    )


# (grade, problem_def, rcc, actionable) → expected runbook_conversion_ready
_MATRIX = [
    # The one ready case: sound cause + both content halves present.
    (CauseAssuranceGrade.CONFIRMED, True, True, True, True),
    # Soundness half fails: a validated-but-unconfirmed / no-root cause never
    # converts, no matter how rich the content.
    (CauseAssuranceGrade.MECHANISTIC, True, True, True, False),
    (CauseAssuranceGrade.NO_ROOT, True, True, True, False),
    # Substance half fails even on a CONFIRMED cause — the cases that used to be
    # offered-then-denied (offer suppressed here, not refused at action time).
    (CauseAssuranceGrade.CONFIRMED, False, True, True, False),  # no problem def
    (CauseAssuranceGrade.CONFIRMED, True, False, True, False),  # no RCC record
    (CauseAssuranceGrade.CONFIRMED, True, True, False, False),  # thin: no solution
]


@pytest.mark.unit
@pytest.mark.parametrize("grade,problem_def,rcc,actionable,expected", _MATRIX)
def test_all_gates_resolve_to_one_verdict(
    monkeypatch, grade, problem_def, rcc, actionable, expected
):
    """The predicate, the content gate's NOT_SUITABLE boundary, and the offer
    affordance agree on every case — the #698 no-drift proof."""
    _pin_grade(monkeypatch, grade)
    case = _make_case(problem_def=problem_def, rcc=rcc, actionable=actionable)

    ready = runbook_conversion_ready(case)
    gate_admits = (
        assess_runbook_readiness(case).verdict != RunbookReadiness.NOT_SUITABLE
    )
    offered = _runbook_suggestion(case) is not None

    assert ready is expected
    # The three boundaries are the same boundary.
    assert gate_admits == ready
    assert offered == ready


@pytest.mark.unit
def test_thin_confirmed_case_is_not_offered_then_denied(monkeypatch):
    """A CONFIRMED cause with no actionable solution: the affordance must be
    suppressed (not offered), matching the gate that would return NOT_SUITABLE —
    the offered-then-denied mode (#695) this unification exists to prevent."""
    _pin_grade(monkeypatch, CauseAssuranceGrade.CONFIRMED)
    case = _make_case(problem_def=True, rcc=True, actionable=False)

    assert runbook_conversion_ready(case) is False
    assert _runbook_suggestion(case) is None
    assert assess_runbook_readiness(case).verdict == RunbookReadiness.NOT_SUITABLE


@pytest.mark.unit
class TestFieldHelpers:
    """The shared field helpers single-source what ``assess_runbook_readiness``
    and ``runbook_conversion_ready`` both read."""

    def test_has_problem_definition(self):
        assert has_problem_definition(
            _make_case(problem_def=True, rcc=True, actionable=True)
        )
        assert not has_problem_definition(
            _make_case(problem_def=False, rcc=True, actionable=True)
        )

    def test_has_root_cause_record(self):
        assert has_root_cause_record(
            _make_case(problem_def=True, rcc=True, actionable=True)
        )
        assert not has_root_cause_record(
            _make_case(problem_def=True, rcc=False, actionable=True)
        )

    def test_has_actionable_solution(self):
        assert has_actionable_solution(
            _make_case(problem_def=True, rcc=True, actionable=True)
        )
        assert not has_actionable_solution(
            _make_case(problem_def=True, rcc=True, actionable=False)
        )

    def test_has_actionable_solution_empty_list(self):
        case = SimpleNamespace(solutions=[])
        assert not has_actionable_solution(case)

    def test_has_actionable_solution_none(self):
        case = SimpleNamespace(solutions=None)
        assert not has_actionable_solution(case)


@pytest.mark.unit
class TestHasActionableSolutionOutcomeAware:
    """A solution whose matching ProposedAction was superseded/rejected is a
    failed/replaced attempt — it must not count as an actionable solution, so a
    case whose only actionable fix failed is not offered for conversion (its
    runbook would launder a failed fix's commands into the remediation slot)."""

    @staticmethod
    def _sol(actionable: bool):
        return SimpleNamespace(
            commands=["kubectl delete pod x"] if actionable else None,
            implementation_steps=None,
            longterm_fix=None,
            immediate_action="restart the pod" if actionable else None,
        )

    @staticmethod
    def _action(state: str, *, action_type="solution", turn: int = 1):
        return SimpleNamespace(
            description="restart the pod",
            commands=["kubectl delete pod x"],
            state=state,
            action_type=action_type,
            proposed_in_turn=turn,
        )

    def test_only_superseded_solution_does_not_count(self):
        # A never-executed offer replaced while pending.
        case = SimpleNamespace(
            solutions=[self._sol(actionable=True)],
            proposed_actions=[self._action("superseded")],
        )
        assert not has_actionable_solution(case)

    def test_only_diagnostic_downgraded_solution_does_not_count(self):
        # A SOLUTION downgraded to DIAGNOSTIC (engine-refused as a fix) still leaves
        # a Solution carrying the commands; accepted-on-resolution must not make the
        # case convertible off an engine-refused fix.
        case = SimpleNamespace(
            solutions=[self._sol(actionable=True)],
            proposed_actions=[self._action("accepted", action_type="diagnostic")],
        )
        assert not has_actionable_solution(case)

    def test_accepted_solution_counts(self):
        case = SimpleNamespace(
            solutions=[self._sol(actionable=True)],
            proposed_actions=[self._action("accepted")],
        )
        assert has_actionable_solution(case)

    def test_failed_plus_applied_counts(self):
        applied = SimpleNamespace(
            commands=["kubectl set resources y"],
            implementation_steps=None,
            longterm_fix=None,
            immediate_action="bump memory",
        )
        case = SimpleNamespace(
            solutions=[self._sol(actionable=True), applied],
            proposed_actions=[
                self._action("superseded"),
                SimpleNamespace(
                    description="bump memory",
                    commands=["kubectl set resources y"],
                    state="accepted",
                ),
            ],
        )
        assert has_actionable_solution(case)

    def test_no_proposed_actions_preserves_prior_behavior(self):
        # No compliance chain (stub case) → PROPOSED → still counts, exactly as
        # before this change.
        case = SimpleNamespace(solutions=[self._sol(actionable=True)])
        assert has_actionable_solution(case)


@pytest.mark.unit
class TestReadyCaseIsOffered:
    """The offer boundary is the readiness predicate and nothing else.

    A sync provenance tier ("the confirmed cause was seeded from runbook X, so
    do not offer generation") sat on top of it while the KB cause seeder
    existed; it went with the seeder (fm#1295). Duplicate protection is the
    async similarity dedup at action time.
    """

    def test_offer_shown_when_ready(self, monkeypatch):
        from faultmaven.core.investigation import milestone_engine

        _pin_grade(monkeypatch, CauseAssuranceGrade.CONFIRMED)
        case = _make_case(problem_def=True, rcc=True, actionable=True)
        assert runbook_conversion_ready(case) is True
        assert milestone_engine._runbook_suggestion(case) is not None
