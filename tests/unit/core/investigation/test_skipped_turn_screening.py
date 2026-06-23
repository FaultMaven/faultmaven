"""SKIPPED recovery placeholders must be transparent to behavioral analyses.

A backfilled SKIPPED turn represents a turn that was *not recorded* (recovered
after an interruption). It must not be mistaken for real diagnostic work, nor
pad the action-loop / momentum windows.
"""

from faultmaven.core.investigation.progress_monitor import ProgressMonitor
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    TurnOutcome,
    TurnProgress,
)


def _turn(n: int, outcome: TurnOutcome, **kw) -> TurnProgress:
    return TurnProgress(turn_number=n, outcome=outcome, progress_made=False, **kw)


def _case(turns) -> Case:
    c = Case(organization_id="o", title="t", description="d", state=CaseState.INQUIRY)
    c.turn_history = turns
    c.current_turn = turns[-1].turn_number if turns else 0
    return c


def test_skipped_turn_not_counted_as_investigative_progress():
    monitor = ProgressMonitor()
    # one real conversation turn, then a backfilled SKIPPED turn
    case = _case(
        [
            _turn(1, TurnOutcome.CONVERSATION),
            _turn(2, TurnOutcome.SKIPPED),
        ]
    )
    # SKIPPED must not register as investigative work since the last milestone.
    assert monitor._count_investigative_turns_since_milestone(case) == 0


def test_skipped_turns_do_not_trip_action_loop():
    monitor = ProgressMonitor()
    # Several identical SKIPPED placeholders (as a multi-turn gap backfill
    # produces) must NOT be read as an action loop of "identical output".
    case = _case([_turn(n, TurnOutcome.SKIPPED) for n in range(1, 8)])
    assert monitor._detect_action_loop(case) is False


def test_ui_transparency_does_not_count_skipped_turns():
    """case_ui_adapter (UI-surfaced) is the consumer that was missed — SKIPPED
    placeholders must not inflate the 'investigation slow' transparency banner."""

    from faultmaven.modules.case.domain.services.case_ui_adapter import (
        _compute_progress_transparency,
    )

    # 5+ SKIPPED placeholders (≥ the transparency threshold) must NOT activate.
    skipped_case = _case([_turn(n, TurnOutcome.SKIPPED) for n in range(1, 7)])
    assert _compute_progress_transparency(skipped_case) is None

    # Sanity: the same count of REAL investigative turns DOES activate, so the
    # screen is specific to SKIPPED and didn't disable the feature.
    real_case = _case([_turn(n, TurnOutcome.DATA_REQUESTED) for n in range(1, 7)])
    assert _compute_progress_transparency(real_case) is not None
