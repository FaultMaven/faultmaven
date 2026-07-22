"""Unit tests for ``classify_solution_outcome``.

A ``Solution`` and its ``ProposedAction`` are created together from one LLM
proposal (identical ``description``/``commands``). The engine never stamps
``applied_at``/``verified_at``/``effectiveness`` on the Solution, so the live
outcome signal is the matching action's ``state``. These tests pin that
correlation so the runbook-conversion boundary can drop a failed fix's commands.
"""

from __future__ import annotations

from faultmaven.modules.case.contracts import (
    InvestigationActionType,
    ProposedAction,
    Solution,
    SolutionOutcome,
    SolutionType,
    classify_solution_outcome,
)


def _solution(desc: str | None, commands: list[str] | None = None) -> Solution:
    return Solution(
        solution_type=SolutionType.CONFIG_CHANGE,
        title="Solution: fix",
        immediate_action=desc,
        commands=commands or [],
    )


def _action(
    desc: str,
    commands: list[str],
    state: str,
    *,
    action_type: InvestigationActionType = InvestigationActionType.SOLUTION,
    turn: int = 1,
) -> ProposedAction:
    action = ProposedAction(
        case_id="case-1",
        action_type=action_type,
        description=desc,
        commands=commands,
        proposed_in_turn=turn,
    )
    action.state = state
    return action


class TestClassifySolutionOutcome:
    def test_accepted_action_is_applied(self):
        sol = _solution("bump memory", ["kubectl set resources y"])
        actions = [_action("bump memory", ["kubectl set resources y"], "accepted")]
        assert classify_solution_outcome(sol, actions) == SolutionOutcome.APPLIED

    def test_superseded_action_is_failed(self):
        sol = _solution("restart pod", ["kubectl delete pod x"])
        actions = [_action("restart pod", ["kubectl delete pod x"], "superseded")]
        assert classify_solution_outcome(sol, actions) == SolutionOutcome.FAILED

    def test_rejected_action_is_failed(self):
        # Defensive: no engine path currently sets a ProposedAction to "rejected",
        # but the state is in the model's vocabulary, so a rejected fix must never
        # surface as remediation if one is ever introduced.
        sol = _solution("restart pod", ["kubectl delete pod x"])
        actions = [_action("restart pod", ["kubectl delete pod x"], "rejected")]
        assert classify_solution_outcome(sol, actions) == SolutionOutcome.FAILED

    def test_earlier_executed_solution_stays_applied(self):
        # Regression guard: an earlier executed (accepted) SOLUTION must NOT be
        # demoted just because a later SOLUTION exists — executed-then-failed is
        # indistinguishable from one step of a compound remediation, so both
        # executed fixes stay APPLIED (never wrongly drop a fix the user ran).
        a = _solution("restart pod", ["kubectl delete pod x"])
        b = _solution("bump memory", ["kubectl set resources y"])
        actions = [
            _action("restart pod", ["kubectl delete pod x"], "accepted", turn=1),
            _action("bump memory", ["kubectl set resources y"], "accepted", turn=3),
        ]
        assert classify_solution_outcome(a, actions) == SolutionOutcome.APPLIED
        assert classify_solution_outcome(b, actions) == SolutionOutcome.APPLIED

    def test_executed_solution_with_later_superseded_offer_stays_applied(self):
        # Regression guard: an executed fix A plus a later never-run (superseded)
        # SOLUTION offer B — A must stay APPLIED (not blocked/dropped), B is FAILED.
        a = _solution("restart pod", ["kubectl delete pod x"])
        b = _solution("bump memory", ["kubectl set resources y"])
        actions = [
            _action("restart pod", ["kubectl delete pod x"], "accepted", turn=1),
            _action("bump memory", ["kubectl set resources y"], "superseded", turn=3),
        ]
        assert classify_solution_outcome(a, actions) == SolutionOutcome.APPLIED
        assert classify_solution_outcome(b, actions) == SolutionOutcome.FAILED

    def test_diagnostic_downgraded_accepted_is_failed(self):
        # A SOLUTION the engine downgraded to DIAGNOSTIC (M5/3D) still creates a
        # Solution carrying the commands; if its DIAGNOSTIC action is accepted on
        # resolution it must NOT be laundered as remediation.
        sol = _solution("tail the logs", ["kubectl logs x"])
        actions = [
            _action(
                "tail the logs",
                ["kubectl logs x"],
                "accepted",
                action_type=InvestigationActionType.DIAGNOSTIC,
                turn=1,
            )
        ]
        assert classify_solution_outcome(sol, actions) == SolutionOutcome.FAILED

    def test_pending_action_is_proposed(self):
        sol = _solution("restart pod", ["kubectl delete pod x"])
        actions = [_action("restart pod", ["kubectl delete pod x"], "pending")]
        assert classify_solution_outcome(sol, actions) == SolutionOutcome.PROPOSED

    def test_no_matching_action_is_proposed(self):
        sol = _solution("restart pod", ["kubectl delete pod x"])
        actions = [_action("something else", ["true"], "superseded")]
        assert classify_solution_outcome(sol, actions) == SolutionOutcome.PROPOSED

    def test_empty_actions_is_proposed(self):
        sol = _solution("restart pod", ["kubectl delete pod x"])
        assert classify_solution_outcome(sol, []) == SolutionOutcome.PROPOSED

    def test_accepted_wins_over_superseded_sibling(self):
        # Two same-content actions (a re-proposal): if any was accepted, the
        # solution counts as applied — the classifier biases to inclusion so a
        # real fix is never wrongly dropped.
        sol = _solution("bump memory", ["cmd"])
        actions = [
            _action("bump memory", ["cmd"], "superseded"),
            _action("bump memory", ["cmd"], "accepted"),
        ]
        assert classify_solution_outcome(sol, actions) == SolutionOutcome.APPLIED

    def test_commands_must_match_not_just_description(self):
        # Same description, different commands → not the same proposal.
        sol = _solution("restart pod", ["kubectl delete pod A"])
        actions = [_action("restart pod", ["kubectl delete pod B"], "superseded")]
        assert classify_solution_outcome(sol, actions) == SolutionOutcome.PROPOSED

    def test_solution_without_correlatable_content_is_proposed(self):
        # A longterm-fix-only solution carries no description/commands to match
        # an action on → surfaced as an unconfirmed proposal, never dropped.
        sol = Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Solution: fix",
            longterm_fix="Migrate to a connection pool",
        )
        actions = [_action("anything", ["cmd"], "superseded")]
        assert classify_solution_outcome(sol, actions) == SolutionOutcome.PROPOSED
