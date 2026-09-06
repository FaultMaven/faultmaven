"""Turn-sequence resilience: reconcile_turn_sequence + non-fatal validator.

A turn-counter anomaly must never wedge a case. These tests pin both halves:
prevention is covered at the repository layer; here we cover the in-model
self-heal (Layer 2) and prove it's inert on healthy cases (no business-logic
impact).
"""

import pytest

from faultmaven.modules.case.domain.models import Case, TurnOutcome, TurnProgress


def _tp(n: int, outcome: TurnOutcome = TurnOutcome.CONVERSATION) -> TurnProgress:
    return TurnProgress(turn_number=n, outcome=outcome, progress_made=False)


def _case(nums: list[int], current_turn: int | None = None) -> Case:
    case = Case(enterprise_id="org1", title="t")
    case.turn_history = [_tp(n) for n in nums]
    case.current_turn = (
        current_turn if current_turn is not None else (nums[-1] if nums else 0)
    )
    return case


def _nums(case: Case) -> list[int]:
    return [t.turn_number for t in case.turn_history]


def _outcomes(case: Case) -> list[str]:
    return [t.outcome.value for t in case.turn_history]


# -- inert on healthy cases (the no-business-logic-impact guarantee) ----------
def test_healthy_history_is_unchanged():
    case = _case([1, 2, 3])
    case.reconcile_turn_sequence()
    assert _nums(case) == [1, 2, 3]
    assert case.current_turn == 3
    assert TurnOutcome.SKIPPED.value not in _outcomes(case)  # no synthetic turns


def test_empty_history_is_noop():
    case = _case([])
    case.reconcile_turn_sequence()
    assert _nums(case) == []
    assert case.current_turn == 0


def test_reconcile_is_idempotent():
    case = _case([1, 3], current_turn=2)
    case.reconcile_turn_sequence()
    once = _nums(case)
    case.reconcile_turn_sequence()
    assert _nums(case) == once


# -- self-heal: gaps backfill SKIPPED, numbers preserved ----------------------
def test_gap_backfills_skipped_and_preserves_existing_numbers():
    case = _case([1, 3], current_turn=2)
    case.reconcile_turn_sequence()
    assert _nums(case) == [1, 2, 3]  # 3 keeps its number → references stay valid
    assert _outcomes(case)[1] == TurnOutcome.SKIPPED.value
    assert case.current_turn == 3


def test_wedged_case_shape_heals_instead_of_bricking():
    # The real failure: history persisted as [1] with current_turn=2, then the
    # next turn appends 3 → [1, 3]. This used to raise and wedge the case forever.
    case = _case([1], current_turn=2)
    case.turn_history.append(_tp(3))
    case.reconcile_turn_sequence()
    assert _nums(case) == [1, 2, 3]
    assert _outcomes(case)[1] == TurnOutcome.SKIPPED.value


def test_multi_gap_backfills_each_missing_number():
    case = _case([1, 4], current_turn=3)
    case.reconcile_turn_sequence()
    assert _nums(case) == [1, 2, 3, 4]
    assert _outcomes(case)[1] == TurnOutcome.SKIPPED.value
    assert _outcomes(case)[2] == TurnOutcome.SKIPPED.value


# -- self-heal: duplicates / out-of-order renumber forward --------------------
def test_duplicate_turn_numbers_renumber_forward():
    case = _case([1, 2, 2])
    case.reconcile_turn_sequence()
    assert _nums(case) == [1, 2, 3]


# -- runaway guard ------------------------------------------------------------
def test_pathological_gap_is_capped_not_backfilled():
    case = _case([1, 500], current_turn=500)
    case.reconcile_turn_sequence()
    assert _nums(case) == [1, 2]  # renumbered, no 498 synthetic records
    # current_turn must follow the truncated history, not stay stranded at 500.
    assert case.current_turn == 2


# -- return value (meterable repair count) ------------------------------------
def test_reconcile_returns_repair_count():
    assert _case([1, 2, 3]).reconcile_turn_sequence() == 0  # healthy → 0
    assert _case([1, 3], current_turn=2).reconcile_turn_sequence() == 1  # 1 backfill
    assert _case([1, 4], current_turn=3).reconcile_turn_sequence() == 2  # 2 backfills
    assert _case([1, 2, 2]).reconcile_turn_sequence() == 1  # 1 renumber


# -- backfilled SKIPPED timestamps stay time-ordered (#6) ---------------------
def test_skipped_placeholder_timestamp_is_not_in_the_future():
    case = _case([1, 3], current_turn=2)
    # give the real turns concrete, ordered timestamps
    from datetime import datetime, timezone

    t1 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc)
    case.turn_history[0] = case.turn_history[0].model_copy(update={"timestamp": t1})
    case.turn_history[1] = case.turn_history[1].model_copy(update={"timestamp": t3})
    case.reconcile_turn_sequence()
    ts = [t.timestamp for t in case.turn_history]
    assert ts == sorted(ts)  # monotonic — backfilled turn not stamped now()
    assert case.turn_history[1].timestamp == t1  # inherits the preceding turn's


# -- effective_current_turn property (the derived, persisted counter) ---------
def test_effective_current_turn_derives_from_history():
    assert _case([1, 2], current_turn=3).effective_current_turn == 2  # never ahead
    assert _case([1, 2, 3]).effective_current_turn == 3
    assert _case([], current_turn=0).effective_current_turn == 0  # empty → counter


# -- in-flight safety: current_turn is never decreased ------------------------
def test_current_turn_is_never_decreased():
    case = _case([1, 2], current_turn=3)  # counter legitimately ahead mid-turn
    case.reconcile_turn_sequence()
    assert _nums(case) == [1, 2]
    assert case.current_turn == 3


def test_repair_path_does_not_lower_an_in_flight_current_turn():
    # A gap is repaired (repair path runs) while current_turn is legitimately
    # ahead (5). The backfill must NOT lower it to the rebuilt last (3).
    case = _case([1, 3], current_turn=5)
    case.reconcile_turn_sequence()
    assert _nums(case) == [1, 2, 3]
    assert case.current_turn == 5  # preserved, not demoted to 3


def test_is_skipped_property():
    assert _tp(2, TurnOutcome.SKIPPED).is_skipped is True
    assert _tp(1, TurnOutcome.CONVERSATION).is_skipped is False


# -- the validator must not raise on a gap (non-fatal) ------------------------
def test_validator_does_not_raise_on_gap():
    case = Case(enterprise_id="o", title="t")
    case.turn_history = [_tp(1), _tp(3)]
    # model_validate runs the (now non-fatal) turn_history validator.
    Case.model_validate(case.model_dump(mode="python"))  # must not raise
