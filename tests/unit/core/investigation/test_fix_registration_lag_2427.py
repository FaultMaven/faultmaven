"""A fix is dated by the user message that reported carrying it out (#2427).

The acceptance registers in the turn the model records it, which can be a turn
after the user reported the fix: the suggestion was prose, so nothing stood to
accept until the model minted the SOLUTION and accepted it together. Dated to
the registration turn, the fix's window opens there; a cause re-check in that
turn observes it (``confirming_window``), and the report turn's problem-gone
row falls before the anchor, so a case that read READY asks for it again.

The model declares the report turn (``accepted_fix_reported_in``, ruled
2026-11-03): ``previous_message`` dates the fix to the previous turn when that
turn is later than every fix already dated. Unset, nothing changes, and nothing
is inferred from the evidence. Everything goes through
``_apply_investigation_updates`` and the real stage gate, and every
registration names its fix (``accepted_action_ref``), so no negative passes
because nothing registered.
"""

import logging

import pytest

from faultmaven.core.investigation.causal_graph.disconfirmation import (
    failed_fix_of,
)
from faultmaven.core.investigation.cause_assurance import fix_windows
from faultmaven.core.investigation.schemas import MilestoneUpdates, SolutionToAdd
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    CauseState,
    HypothesisState,
    InvestigationActionType,
    SolutionType,
    UploadedFile,
)
from tests.unit.core.investigation.bound_rows import bound_row
from tests.unit.core.investigation.fix_turns_2039 import (
    _CAUSE_GONE,
    _GONE,
    _PRESENT,
    _engine_rows,
    _ev,
    _turn,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _FILE_ID,
    _identified_case,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import _engine

pytestmark = pytest.mark.unit

_FIX = "raise max_connections to 100"
_SECOND = "raise max_connections to 200 and cap the pool per worker"
_PROBLEM_LEG = "Confirmation the original problem is gone"
_PREVIOUS = "previous_message"


def _fix(description: str = _FIX) -> SolutionToAdd:
    return SolutionToAdd(
        description=description,
        solution_type=SolutionType.CONFIG_CHANGE,
        estimated_impact="checkout recovers under peak",
        risks="none",
    )


def _accepted(reported_in=None) -> MilestoneUpdates:
    """The fix minted in this response, accepted by name."""
    return MilestoneUpdates(
        solution_accepted=True,
        accepted_action_ref="new_index_0",
        accepted_fix_reported_in=reported_in,
    )


def _identified():
    """The #1927 case at turn 5: the cause identified, no fix proposed."""
    case, root, hyp = _identified_case()
    case.uploaded_files.append(
        UploadedFile(
            file_id=_FILE_ID, filename="c.log", size_bytes=1, uploaded_at_turn=1
        )
    )
    return _engine(), case, hyp


def _stamps(case):
    """(execution turn, registration turn) of every accepted SOLUTION."""
    return [
        (a.accepted_in_turn, a.registered_in_turn)
        for a in case.proposed_actions
        if a.action_type == InvestigationActionType.SOLUTION and a.state == "accepted"
    ]


def _windows(case):
    return [(w.fix_turn, w.state) for w in fix_windows(case)]


async def _reported(eng, case, turn=10):
    """The user reports carrying out the fix and pastes post-fix output: the
    cause and the problem seen gone. The fix was prose, so nothing registers."""
    await _turn(
        eng,
        case,
        turn,
        _ev(_CAUSE_GONE, "max_connections reads 100 after the change"),
        _ev(_GONE, "checkout returns 200, 503 rate 0.0/s"),
    )


async def _registered(eng, case, reported_in, *rows, turn=11):
    """The model mints the fix and accepts it, beside a cause re-check."""
    return await _turn(
        eng,
        case,
        turn,
        _ev(_CAUSE_GONE, "max_connections still reads 100"),
        *rows,
        solutions_to_add=[_fix()],
        milestones=_accepted(reported_in),
    )


# ---------------------------------------------------------------------------
# The issue's sequence.
# ---------------------------------------------------------------------------


async def test_a_fix_declared_reported_in_the_previous_message_stays_ready():
    """Turn 10 the report, with the cause and the problem seen gone; turn 11
    the fix minted and accepted, declared reported at 10, beside a cause
    re-check and no problem row. The fix is dated 10, its window holds the
    turn-10 problem-gone row, and the case stays READY."""
    eng, case, _hyp = _identified()
    await _reported(eng, case)
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY

    await _registered(eng, case, _PREVIOUS)

    assert _stamps(case) == [(10, 11)]
    assert _windows(case) == [(None, "unknown"), (10, "succeeded")]
    readiness = assess_resolution_readiness(case)
    assert readiness.missing == []
    assert readiness.verdict == ResolutionReadiness.READY


@pytest.mark.parametrize("reported_in", [None, "this_message"])
async def test_undeclared_a_cause_recheck_on_the_registration_turn_asks_again(
    reported_in,
):
    """The same sequence (turn 11's cause re-check, no problem row) without
    the declaration reads as on main: the fix is dated 11, its window takes
    the re-check, and the turn-10 problem-gone row falls before it."""
    eng, case, _hyp = _identified()
    await _reported(eng, case)

    await _registered(eng, case, reported_in)

    assert _stamps(case) == [(11, None)]
    assert _windows(case) == [(None, "succeeded"), (11, "unknown")]
    readiness = assess_resolution_readiness(case)
    assert readiness.verdict == ResolutionReadiness.NEEDS_INFO
    assert _PROBLEM_LEG in readiness.message


# ---------------------------------------------------------------------------
# The ruled consequence: the registration turn's paste is post-fix.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reported_in, windows, cause_state",
    [
        (
            _PREVIOUS,
            [(None, "unknown"), (10, "failed")],
            CauseState.UNKNOWN,
        ),
        (
            None,
            [(None, "succeeded"), (11, "unknown")],
            CauseState.IDENTIFIED,
        ),
    ],
)
async def test_declared_a_problem_present_beside_a_cause_recheck_fails_the_fix(
    reported_in, windows, cause_state
):
    """Turn 11 carries a cause re-check and the problem present. Declared
    reported at 10, the fix's window holds both: the cause seen removed, then
    the problem present, so the window FAILED on a disconfirmation and M6
    demotes the cause (INV-42), a turn earlier than main. Undeclared, the
    problem row is in the fix's own turn and reads as on main."""
    eng, case, hyp = _identified()
    await _reported(eng, case)

    await _registered(
        eng, case, reported_in, _ev(_PRESENT, "checkout returns 503 again")
    )

    assert _windows(case) == windows
    assert case.progress.cause_state == cause_state
    if reported_in == _PREVIOUS:
        assert fix_windows(case)[-1].disconfirmation == (11, 11)
        assert hyp.state == HypothesisState.REFUTED
        assert _engine_rows(case)
    else:
        assert hyp.state == HypothesisState.VALIDATED
        assert _engine_rows(case) == []
        verdict = assess_resolution_readiness(case).verdict
        assert verdict == ResolutionReadiness.NEEDS_INFO


# ---------------------------------------------------------------------------
# The critique's shapes, undeclared: main's behaviour.
# ---------------------------------------------------------------------------


async def test_pa_a_fix_reported_with_the_problem_present_demotes_nothing():
    """PA: a cause row at 9 that is not this fix's outcome; at 10 the user
    reports the fix with the problem still present. Undeclared, the fix is
    dated 10, and the paste in its own turn disconfirms nothing."""
    eng, case, hyp = _identified()
    await _turn(eng, case, 9, _ev(_CAUSE_GONE, "max_connections reads 100 on a"))

    await _turn(
        eng,
        case,
        10,
        _ev(_PRESENT, "checkout still returns 503 after the change"),
        solutions_to_add=[_fix()],
        milestones=_accepted(),
    )

    assert _stamps(case) == [(10, None)]
    assert hyp.state == HypothesisState.VALIDATED
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert _engine_rows(case) == []


async def test_pg_a_new_fix_is_not_credited_with_the_recheck_before_it():
    """PG: the fix at 6 confirmed at 7 and relapsed at 8; at 9 the cause
    re-check the failed-fix notice asks for; at 10 a new fix with the problem
    gone. Undeclared, the new fix's window holds no cause re-check, and the
    gate asks for one rather than crediting the turn-9 row."""
    eng, case, _hyp = _identified()
    await _turn(eng, case, 6, solutions_to_add=[_fix()], milestones=_accepted())
    await _turn(
        eng,
        case,
        7,
        _ev(_CAUSE_GONE, "max_connections reads 100 after the change"),
        _ev(_GONE, "checkout returns 200, 503 rate 0.0/s"),
    )
    await _turn(eng, case, 8, _ev(_PRESENT, "checkout returns 503 again"))
    await _turn(eng, case, 9, _ev(_CAUSE_GONE, "max_connections still reads 100"))

    await _turn(
        eng,
        case,
        10,
        _ev(_GONE, "checkout returns 200"),
        solutions_to_add=[_fix(_SECOND)],
        milestones=_accepted(),
    )

    assert _stamps(case) == [(6, None), (10, None)]
    assert _windows(case) == [(None, "unknown"), (6, "failed"), (10, "unknown")]
    verdict = assess_resolution_readiness(case).verdict
    assert verdict == ResolutionReadiness.NEEDS_INFO


# ---------------------------------------------------------------------------
# The bound and the floor.
# ---------------------------------------------------------------------------


async def test_a_declaration_not_after_the_latest_dated_fix_is_ignored(caplog):
    """A fix dated 10; a second registered at 11, declared reported at 10.
    Dating it 10 would collide with the first, so the declaration is logged
    and ignored, and the second fix is dated 11."""
    eng, case, _hyp = _identified()
    await _turn(eng, case, 10, solutions_to_add=[_fix()], milestones=_accepted())

    with caplog.at_level(logging.INFO):
        await _turn(
            eng,
            case,
            11,
            solutions_to_add=[_fix(_SECOND)],
            milestones=_accepted(_PREVIOUS),
        )

    assert _stamps(case) == [(10, None), (11, None)]
    assert [w.fix_turn for w in fix_windows(case)] == [None, 10, 11]
    assert "accepted_fix_reported_in=previous_message ignored at turn 11" in (
        caplog.text
    )


async def test_a_cause_reasserted_in_the_registration_turn_is_still_judged():
    """After a relapse, turn 10 re-asserts the cause on a new hypothesis and
    registers a new fix, which the user reported at 9. The fix is dated 9, but
    it registered at 10, when the re-asserted cause existed: its window's
    disconfirmation is still that hypothesis's failed fix (``failed_fix_of``)."""
    eng, case, hyp = _identified()
    await _turn(eng, case, 6, solutions_to_add=[_fix()], milestones=_accepted())
    await _turn(
        eng,
        case,
        7,
        _ev(_CAUSE_GONE, "max_connections reads 100 after the change"),
        _ev(_GONE, "checkout returns 200, 503 rate 0.0/s"),
    )
    await _turn(eng, case, 8, _ev(_PRESENT, "checkout returns 503 again"))
    await _turn(
        eng,
        case,
        10,
        solutions_to_add=[_fix(_SECOND)],
        milestones=_accepted(_PREVIOUS),
    )
    reasserted = hyp.model_copy(
        update={"hypothesis_id": "hyp_000000002427", "generated_at_turn": 10}
    )
    case.hypotheses[reasserted.hypothesis_id] = reasserted
    # Turn 11's rows, before any reader acts on them: the cause seen removed,
    # then the problem present.
    bound_row(case, _CAUSE_GONE, turn=11, summary="max_connections reads 200")
    bound_row(case, _PRESENT, turn=11, summary="checkout returns 503 again")

    assert _stamps(case) == [(6, None), (9, 10)]
    window = failed_fix_of(case, reasserted)
    assert window is not None
    assert (window.fix_turn, window.disconfirmation) == (9, (11, 11))
