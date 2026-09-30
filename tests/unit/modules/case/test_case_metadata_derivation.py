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


@pytest.mark.parametrize("numbers, flags, current_turn", _ALL_HISTORIES)
def test_the_numeric_repair_is_the_one_a_case_load_applies(
    numbers, flags, current_turn
):
    """``reconcile_turn_numbers`` places every entry where the case method does,
    backfills the same numbers and leaves the same clock."""
    slots, clock = reconcile_turn_numbers(numbers, current_turn)

    case = _case(numbers, flags, current_turn)
    case.reconcile_turn_sequence()

    assert [number for _, number in slots] == [t.turn_number for t in case.turn_history]
    assert clock == case.current_turn
    # Each slot is filled by the entry it names, or by a SKIPPED placeholder.
    for (source, _), entry in zip(slots, case.turn_history):
        if source is None:
            assert entry.outcome is TurnOutcome.SKIPPED
        else:
            assert entry.is_out_of_band == flags[source]


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


def test_mismatched_turn_arrays_are_refused():
    """One flag per entry. Anything else is a read that lost its alignment, and
    guessing which flag belongs to which number would mislabel asides."""
    with pytest.raises(ValueError, match="out-of-band flags"):
        CaseMetadata.from_stored(
            **_stored(turn_numbers=[1, 2], turn_is_out_of_band=[False])
        )


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
