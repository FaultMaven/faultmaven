"""The cross-enterprise metadata read derives what a loaded case derives (ADR-012 D9).

``CaseMetadata.from_stored`` never sees a ``Case``: under multi-tenancy the
operator list reads each case's primitive inputs through a database function and
derives ``stage``, ``investigation_turn`` and the clock itself. These tests hold
it to the loaded case on the same inputs — including the turn-sequence repair a
case load performs, which renumbers a duplicate and moves the clock.

The PostgreSQL parity test (``tests/integration/security/
test_admin_case_metadata_postgres.py``) proves the same end to end, through the
real function and the real loader; this module covers the rules exhaustively,
where a database is not needed to do it.
"""

import random
from datetime import datetime, timezone

import pytest

from faultmaven.modules.case.domain.models.case import (
    Case,
    investigation_turn_at,
    reconcile_turn_numbers,
    stage_while_investigating,
)
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.metadata import CaseMetadata
from faultmaven.modules.case.domain.models.problem import InvestigationStage
from faultmaven.modules.case.domain.models.progress import (
    InvestigationProgress,
    MitigationRecord,
    investigation_stage,
)
from faultmaven.modules.case.domain.models.turn import TurnOutcome, TurnProgress

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

#: Named shapes: healthy, a gap, a duplicate (the #1264 shape), out of order, a
#: clock behind its history, a leading zero, and a gap past the backfill cap.
_HISTORIES = [
    ([], [], 0),
    ([], [], 4),
    ([1], [False], 0),
    ([1, 2, 3], [False, True, False], 3),
    ([1, 3], [True, False], 2),
    ([1, 2, 2, 3], [False, True, True, False], 3),
    ([1, 1], [False, True], 1),
    ([2, 1], [True, False], 2),
    ([1, 2, 3], [False, False, True], 1),
    ([0, 1], [True, False], 1),
    ([1, 250, 251], [False, True, False], 251),
    ([3, 3, 3], [True, True, True], 3),
    ([1, 5, 4, 6], [False, True, True, False], 6),
]


def _random_histories(count: int = 300):
    rng = random.Random(1071)
    for _ in range(count):
        length = rng.randint(0, 8)
        numbers = [rng.randint(0, 12) for _ in range(length)]
        flags = [rng.random() < 0.4 for _ in range(length)]
        yield numbers, flags, rng.randint(0, 12)


_ALL_HISTORIES = _HISTORIES + list(_random_histories())


def _case(numbers, flags, current_turn) -> Case:
    """A case as the loader builds it, BEFORE the load-time repair."""
    case = Case(enterprise_id="ent_1", title="t")
    case.turn_history = [
        TurnProgress(
            turn_number=number,
            outcome=TurnOutcome.OUT_OF_BAND if flag else TurnOutcome.CONVERSATION,
            progress_made=False,
        )
        for number, flag in zip(numbers, flags)
    ]
    case.current_turn = current_turn
    return case


def _stored(**overrides):
    fields = dict(
        case_id="case_000000000001",
        enterprise_id="ent_1",
        organization_id=None,
        user_id="user_1",
        state="inquiry",
        source="copilot",
        closure_reason=None,
        created_at=_NOW,
        updated_at=_NOW,
        last_activity_at=_NOW,
        resolved_at=None,
        closed_at=None,
        current_turn=0,
        turns_without_progress=0,
        mitigation_accepted=False,
        mitigation_verified=False,
        solution_accepted=False,
        solution_verified=False,
        turn_numbers=[],
        turn_is_out_of_band=[],
        shared_team_ids=[],
    )
    fields.update(overrides)
    return fields


def _main_reconcile(numbers, current_turn):
    """A frozen transcription of ``Case.reconcile_turn_sequence`` as it stood
    before the rule was extracted into ``reconcile_turn_numbers``, over
    ``(source index, turn number)`` pairs instead of ``TurnProgress`` entries.

    Deliberately NOT importing the production rule: this is the oracle the
    extraction is held to, so it must not move when the rule does. The backfill
    cap is the literal it was (100).
    """
    history = list(enumerate(numbers))
    if not history:
        return [], current_turn
    if all(history[i][1] + 1 == history[i + 1][1] for i in range(len(history) - 1)):
        last = history[-1][1]
        if current_turn < last:
            current_turn = last
        return history, current_turn
    rebuilt = [history[0]]
    for entry in history[1:]:
        prev = rebuilt[-1][1]
        gap = entry[1] - prev - 1
        if entry[1] <= prev or gap > 100:
            rebuilt.append((entry[0], prev + 1))
            continue
        for missing in range(prev + 1, entry[1]):
            rebuilt.append((None, missing))
        rebuilt.append(entry)
    last = rebuilt[-1][1]
    if last < history[-1][1] or current_turn < last:
        current_turn = last
    return rebuilt, current_turn


#: Hand-derived from the algorithm above — (numbers, clock) → (slots, clock).
#: A slot is (index of the stored entry that fills it, or None for a SKIPPED
#: backfill; its turn number).
_REPAIRS = {
    "empty": (([], 4), ([], 4)),
    "healthy": (([1, 2, 3], 3), ([(0, 1), (1, 2), (2, 3)], 3)),
    "lagging clock is raised": (([1, 2, 3], 1), ([(0, 1), (1, 2), (2, 3)], 3)),
    "leading clock is kept": (([1, 2], 5), ([(0, 1), (1, 2)], 5)),
    "duplicate is renumbered": (
        ([1, 2, 2, 3], 3),
        ([(0, 1), (1, 2), (2, 3), (3, 4)], 4),
    ),
    "out of order is renumbered": (
        ([1, 3, 2], 3),
        ([(0, 1), (None, 2), (1, 3), (2, 4)], 4),
    ),
    "gap within the cap is backfilled": (
        ([1, 4], 4),
        ([(0, 1), (None, 2), (None, 3), (1, 4)], 4),
    ),
    "gap AT the cap is still backfilled": (
        ([1, 102], 102),
        ([(0, 1)] + [(None, n) for n in range(2, 102)] + [(1, 102)], 102),
    ),
    "gap BEYOND the cap is renumbered and lowers the clock": (
        ([1, 103], 103),
        ([(0, 1), (1, 2)], 2),
    ),
    "beyond the cap lowers even a leading clock": (
        ([1, 103], 200),
        ([(0, 1), (1, 2)], 2),
    ),
    "beyond the cap twice": (
        ([1, 150, 151], 151),
        ([(0, 1), (1, 2), (2, 3)], 3),
    ),
    "duplicate keeps a leading clock": (([1, 1], 5), ([(0, 1), (1, 2)], 5)),
}


@pytest.mark.parametrize(
    "stored, expected", list(_REPAIRS.values()), ids=list(_REPAIRS)
)
def test_the_numeric_repair_by_value(stored, expected):
    """Literal expectations, not a comparison with the method that delegates to
    the rule — that comparison could not fail when the rule itself moved."""
    numbers, clock = stored
    slots, repaired_clock = reconcile_turn_numbers(numbers, clock)
    assert (slots, repaired_clock) == expected


@pytest.mark.parametrize(
    "stored, expected", list(_REPAIRS.values()), ids=list(_REPAIRS)
)
def test_the_frozen_oracle_agrees_with_the_table(stored, expected):
    """The oracle below is only as good as its transcription."""
    assert _main_reconcile(*stored) == expected


@pytest.mark.parametrize("numbers, flags, current_turn", _ALL_HISTORIES)
def test_the_repair_matches_the_pre_extraction_algorithm(numbers, flags, current_turn):
    """Randomised differential against the frozen oracle — for the pure rule AND
    for the case method, each on its own, never one against the other."""
    expected_slots, expected_clock = _main_reconcile(numbers, current_turn)

    assert reconcile_turn_numbers(numbers, current_turn) == (
        expected_slots,
        expected_clock,
    )

    case = _case(numbers, flags, current_turn)
    case.reconcile_turn_sequence()
    assert [t.turn_number for t in case.turn_history] == [n for _, n in expected_slots]
    assert case.current_turn == expected_clock
    for (source, _), entry in zip(expected_slots, case.turn_history):
        if source is None:
            assert entry.outcome is TurnOutcome.SKIPPED
        else:
            assert entry.is_out_of_band == flags[source]


def test_a_healthy_history_never_reaches_the_repair(monkeypatch):
    """The load and save hot paths: a consecutive history returns before any
    repair is planned — no allocation — and only raises a lagging clock."""
    from faultmaven.modules.case.domain.models import case as case_module

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("a healthy history reached the repair planner")

    monkeypatch.setattr(case_module, "reconcile_turn_numbers", _must_not_run)
    case = _case([1, 2, 3], [False, True, False], 1)

    assert case.reconcile_turn_sequence() == 0
    assert case.current_turn == 3


@pytest.mark.parametrize("numbers, flags, current_turn", _ALL_HISTORIES)
def test_the_clock_and_investigation_turn_match_a_loaded_case(
    numbers, flags, current_turn
):
    """The two turn fields an operator sees, read without loading the case,
    equal the loaded case's after its load-time repair."""
    metadata = CaseMetadata.from_stored(
        **_stored(
            current_turn=current_turn,
            turn_numbers=numbers,
            turn_is_out_of_band=flags,
        )
    )

    case = _case(numbers, flags, current_turn)
    case.reconcile_turn_sequence()

    assert metadata.current_turn == case.current_turn
    assert metadata.investigation_turn == case.investigation_turn_count


def test_a_duplicate_turn_moves_the_clock_it_reports():
    """The shape that makes reading the raw numbers wrong, pinned by value.

    ``[1, 2(aside), 2(aside), 3]`` at clock 3 loads as ``[1, 2, 3, 4]`` at clock
    4: the duplicate is renumbered and every later entry with it. Counting the
    distinct raw asides would report clock 3 — right investigation turn, wrong
    clock — and ``[1, 1(aside)]`` shows the investigation turn can move too.
    """
    duplicated = CaseMetadata.from_stored(
        **_stored(
            current_turn=3,
            turn_numbers=[1, 2, 2, 3],
            turn_is_out_of_band=[False, True, True, False],
        )
    )
    assert (duplicated.current_turn, duplicated.investigation_turn) == (4, 2)

    renumbered_aside = CaseMetadata.from_stored(
        **_stored(
            current_turn=1, turn_numbers=[1, 1], turn_is_out_of_band=[False, True]
        )
    )
    assert (renumbered_aside.current_turn, renumbered_aside.investigation_turn) == (
        2,
        1,
    )


def _gate_combinations():
    """Every combination the two ordering validators admit, plus no mitigation."""
    for mitigation in (None, (False, False), (True, False), (True, True)):
        for solution in ((False, False), (True, False), (True, True)):
            yield mitigation, solution


@pytest.mark.parametrize("mitigation, solution", list(_gate_combinations()))
def test_the_stage_rule_is_the_progress_rule(mitigation, solution):
    progress = InvestigationProgress(
        mitigation=(
            MitigationRecord(accepted=mitigation[0], verified=mitigation[1])
            if mitigation
            else None
        ),
        solution_accepted=solution[0],
        solution_verified=solution[1],
    )
    gates = dict(
        mitigation_accepted=bool(mitigation and mitigation[0]),
        mitigation_verified=bool(mitigation and mitigation[1]),
        solution_accepted=solution[0],
        solution_verified=solution[1],
    )

    assert investigation_stage(**gates) is progress.current_stage
    for state in CaseState:
        metadata = CaseMetadata.from_stored(**_stored(state=state.value, **gates))
        assert metadata.stage == stage_while_investigating(
            state, progress.current_stage
        )
        assert metadata.is_terminal is state.is_terminal


@pytest.mark.parametrize(
    "accepted_verified, solution, expected",
    [
        ((False, False), (False, False), InvestigationStage.DIAGNOSIS),
        ((True, False), (False, False), InvestigationStage.MITIGATION),
        ((True, True), (False, False), InvestigationStage.DIAGNOSIS),
        ((False, False), (True, False), InvestigationStage.TREATMENT),
        ((False, False), (True, True), InvestigationStage.DIAGNOSIS),
        # An unverified mitigation outranks an unverified solution.
        ((True, False), (True, False), InvestigationStage.MITIGATION),
        ((True, True), (True, False), InvestigationStage.TREATMENT),
    ],
)
def test_the_stage_each_gate_combination_derives(accepted_verified, solution, expected):
    """By value, so a change to the one shared rule cannot pass by moving both
    sides of the comparisons above at once."""
    assert (
        investigation_stage(
            mitigation_accepted=accepted_verified[0],
            mitigation_verified=accepted_verified[1],
            solution_accepted=solution[0],
            solution_verified=solution[1],
        )
        is expected
    )


@pytest.mark.parametrize("state", list(CaseState))
def test_a_stage_is_shown_only_while_investigating(state):
    shown = stage_while_investigating(state, InvestigationStage.TREATMENT)
    if state is CaseState.INVESTIGATING:
        assert shown is InvestigationStage.TREATMENT
    else:
        assert shown is None


def test_the_ordinal_is_bounded_by_the_clock():
    """The pure formula keeps the method's bound: never past the clock."""
    assert investigation_turn_at(9, current_turn=3, asides=[]) == 3
    assert investigation_turn_at(3, current_turn=3, asides=[1, 2, 3]) == 0


def test_mismatched_turn_arrays_leave_the_investigation_turn_unknown(caplog):
    """One flag per entry. Anything else is a read that lost its alignment, and
    guessing which flag belongs to which number would mislabel asides — so the
    turn is not derived, and the case is still served."""
    with caplog.at_level("WARNING"):
        metadata = CaseMetadata.from_stored(
            **_stored(current_turn=2, turn_numbers=[1, 2], turn_is_out_of_band=[False])
        )
    assert metadata.investigation_turn is None
    assert metadata.current_turn == 2
    assert "case_000000000001" in caplog.text and "ent_1" in caplog.text


def test_a_malformed_history_is_served_without_its_investigation_turn(caplog):
    """The database reads a malformed history as NULL. The case is still listed
    — the operator list is how a broken case gets found — with the stored clock,
    no investigation turn, and a warning naming the case and its enterprise."""
    with caplog.at_level("WARNING"):
        metadata = CaseMetadata.from_stored(
            **_stored(
                state="investigating",
                current_turn=7,
                turn_numbers=None,
                turn_is_out_of_band=None,
            )
        )
    assert (metadata.current_turn, metadata.investigation_turn) == (7, None)
    assert metadata.stage is InvestigationStage.DIAGNOSIS
    assert "case_metadata_unreadable" in caplog.text
    assert "case_000000000001" in caplog.text and "ent_1" in caplog.text


@pytest.mark.parametrize(
    "gate",
    [
        "mitigation_accepted",
        "mitigation_verified",
        "solution_accepted",
        "solution_verified",
    ],
)
def test_a_malformed_gate_leaves_the_stage_unknown(gate, caplog):
    """A gate the database could not read as a boolean arrives as None: the
    stage is not guessed, the rest of the row is served."""
    with caplog.at_level("WARNING"):
        metadata = CaseMetadata.from_stored(
            **_stored(
                state="investigating",
                current_turn=1,
                turn_numbers=[1],
                turn_is_out_of_band=[False],
                **{gate: None},
            )
        )
    assert metadata.stage is None
    assert metadata.investigation_turn == 1
    assert "stage" in caplog.text


def test_a_well_formed_row_logs_nothing(caplog):
    with caplog.at_level("WARNING"):
        CaseMetadata.from_stored(
            **_stored(turn_numbers=[1, 2], turn_is_out_of_band=[False, True])
        )
    assert "case_metadata_unreadable" not in caplog.text


def test_a_null_activity_column_loads_as_the_model_default():
    """The full loader leaves a NULL ``last_activity_at`` to the ``Case``
    default; the metadata read does the same rather than failing the list."""
    metadata = CaseMetadata.from_stored(**_stored(last_activity_at=None))
    assert metadata.last_activity_at.tzinfo is not None


def test_metadata_has_no_field_that_can_hold_user_text():
    """``CaseMetadata`` is the domain half of the bound: every string field is a
    system id or a closed vocabulary. A new ``str`` field fails here until it is
    classified."""
    string_fields = {
        name
        for name, field in CaseMetadata.model_fields.items()
        if "str" in str(field.annotation)
    }
    assert string_fields == {
        "case_id",
        "enterprise_id",
        "organization_id",
        "user_id",
        "source",
        "closure_reason",
        "shared_team_ids",
    }
