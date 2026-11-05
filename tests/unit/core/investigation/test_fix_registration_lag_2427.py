"""A fix is dated by the user message that reported carrying it out (#2427).

The acceptance registers in the turn the model records it, which can be a turn
after the user reported the fix: the suggestion was prose, so nothing stood to
accept until the model minted the SOLUTION and accepted it together. Dated to
the registration turn, the fix's window opens there; a cause re-check in that
turn observes it (``confirming_window``), and the report turn's problem-gone
row falls before the anchor, so a case that read READY asks for it again.

The model declares the report turn (``accepted_fix_reported_in``, ruled
2026-11-03): ``previous_message`` dates the fix to the previous turn, within a
bound that moves no observation out of the window that judged it. Unset,
nothing changes, and nothing is inferred from the evidence. Everything goes
through ``_apply_investigation_updates`` and the real stage gate, and every
registration names its fix (``accepted_action_ref``), so no negative passes
because nothing registered.
"""

import logging

import pytest

from faultmaven.core.investigation.cause_assurance import fix_windows
from faultmaven.core.investigation.milestone_engine.structured_output import (
    _validate_with_degradation,
)
from faultmaven.core.investigation.prompts.templates.assembly import (
    get_prompt_for_case,
)
from faultmaven.core.investigation.prompts.templates.investigation import (
    INVESTIGATION_BASE,
)
from faultmaven.core.investigation.schemas import (
    InvestigationResponse_Diagnosis,
    MilestoneUpdates,
    RootCauseConclusionUpdate,
    SolutionToAdd,
)
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    CauseState,
    HypothesisState,
    InvestigationActionType,
    ProblemStatus,
    SolutionType,
    UploadedFile,
)
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
from tests.unit.core.investigation.test_solution_attempts_2105 import (
    _confirm,
    _revise,
)

pytestmark = pytest.mark.unit

_FIX = "raise max_connections to 100"
_SECOND = "raise max_connections to 200 and cap the pool per worker"
_PROBLEM_LEG = "Confirmation the original problem is gone"
_PREVIOUS = "previous_message"
_DECLARE = (
    "If the user reported carrying out the fix in their previous message, set "
    "state_updates.milestones.accepted_fix_reported_in to previous_message."
)
_IGNORED = "SYSTEM: accepted_fix_reported_in previous_message was ignored"


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


def _identified(**kwargs):
    """The #1927 case at turn 5: the cause identified, no fix proposed."""
    case, root, hyp = _identified_case(**kwargs)
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
            [(None, "failed"), (11, "unknown")],
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
    problem row is in the fix's own turn, so it is window 0's relapse (its end
    is inclusive, #2501): the cause and the problem gone at 10, then the
    problem present. A relapse demotes nothing."""
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
# The report message's rows are post-fix (the ruled design).
# ---------------------------------------------------------------------------


async def test_declared_the_report_turns_cause_row_and_a_later_problem_disconfirm():
    """Turn 10 the report, with only the cause seen removed; turn 11 the fix
    declared reported at 10, beside the problem present and no cause re-check.
    Dated 10, the report's cause row is post-fix: the cause removed, then the
    problem present, is a disconfirmation (INV-42), and M6 demotes the cause."""
    eng, case, hyp = _identified()
    await _turn(
        eng, case, 10, _ev(_CAUSE_GONE, "max_connections reads 100 after the change")
    )

    await _turn(
        eng,
        case,
        11,
        _ev(_PRESENT, "checkout returns 503 again"),
        solutions_to_add=[_fix()],
        milestones=_accepted(_PREVIOUS),
    )

    assert _stamps(case) == [(10, 11)]
    assert _windows(case) == [(None, "unknown"), (10, "failed")]
    assert fix_windows(case)[-1].disconfirmation == (10, 11)
    assert hyp.state == HypothesisState.REFUTED
    assert case.progress.cause_state == CauseState.UNKNOWN


# ---------------------------------------------------------------------------
# The bound: no observation leaves the window that judged it.
# ---------------------------------------------------------------------------

_REASSERTED = RootCauseConclusionUpdate(
    root_cause="max_connections below the checkout pool size",
    mechanism="the pool waits on connections the limit refuses",
    likelihood=0.8,
)


def _judged(case, index=1):
    """(state, failure turn, disconfirmation) of the ``index``th fix window."""
    window = fix_windows(case)[index]
    return window.state, window.failure_turn, window.disconfirmation


async def _second_fix_declared(eng, case) -> dict:
    """Turn 11: the cause re-asserted and a second fix minted and accepted,
    declared reported at 10."""
    return await _turn(
        eng,
        case,
        11,
        solutions_to_add=[_fix(_SECOND)],
        milestones=_accepted(_PREVIOUS),
        root_cause_conclusion=_REASSERTED,
    )


async def _relapsed_at_8(eng, case):
    """The fix at 6, the cause and the problem seen gone at 7, the problem back
    at 8: FAILED at 8 on a relapse, which demotes nothing."""
    await _turn(eng, case, 6, solutions_to_add=[_fix()], milestones=_accepted())
    await _turn(
        eng,
        case,
        7,
        _ev(_CAUSE_GONE, "max_connections reads 100 after the change"),
        _ev(_GONE, "checkout returns 200, 503 rate 0.0/s"),
    )
    await _turn(eng, case, 8, _ev(_PRESENT, "checkout returns 503 again"))


async def test_a_declaration_after_a_fix_not_seen_failing_is_ignored(caplog):
    """A fix dated 10, not seen failing; a second registered at 11, declared
    reported at 10. Turn 10 belongs to the first fix, so the declaration is
    ignored with a line, and the second fix is dated 11."""
    eng, case, _hyp = _identified()
    await _turn(eng, case, 10, solutions_to_add=[_fix()], milestones=_accepted())

    with caplog.at_level(logging.INFO):
        meta = await _turn(
            eng,
            case,
            11,
            solutions_to_add=[_fix(_SECOND)],
            milestones=_accepted(_PREVIOUS),
        )

    assert _stamps(case) == [(10, None), (11, None)]
    assert (
        f"{_IGNORED}: the fix executed at turn 10 was not seen failing before "
        "turn 10, so turn 10's observations stay with it; the fix is dated to "
        "turn 11." in meta["system_feedback"]
    )
    assert "accepted_fix_reported_in=previous_message ignored at turn 11" in (
        caplog.text
    )


async def test_a_declaration_moving_the_rows_that_failed_the_last_fix_is_ignored():
    """The review's shape: the fix at 6; at 10 the cause re-checked removed and
    the problem present, so it FAILED at 10 and M6 demoted the cause; at 11 the
    cause re-asserted and a second fix declared reported at 10. Dating it 10
    would move the re-check that failed the first fix into the second fix's
    window. Ignored: the first fix stays FAILED on (10, 10), and a problem-gone
    row at 12 cannot confirm the second fix without its own re-check."""
    eng, case, hyp = _identified()
    await _turn(eng, case, 6, solutions_to_add=[_fix()], milestones=_accepted())
    await _turn(
        eng,
        case,
        10,
        _ev(_CAUSE_GONE, "max_connections still reads 100"),
        _ev(_PRESENT, "checkout returns 503 again"),
    )
    assert hyp.state == HypothesisState.REFUTED

    meta = await _second_fix_declared(eng, case)

    assert _stamps(case) == [(6, None), (11, None)]
    assert _judged(case) == ("failed", 10, (10, 10))
    assert f"{_IGNORED}: {_not_failing_before(10)}" in meta["system_feedback"]
    await _turn(eng, case, 12, _ev(_GONE, "checkout returns 200, 503 rate 0.0/s"))
    verdict = assess_resolution_readiness(case).verdict
    assert verdict == ResolutionReadiness.NEEDS_INFO


def _not_failing_before(previous: int) -> str:
    """The bound's reason for a first fix (at 6) not seen failing before the
    report turn, for a declaration at ``previous + 1``."""
    return (
        f"the fix executed at turn 6 was not seen failing before turn {previous}, "
        f"so turn {previous}'s observations stay with it; the fix is dated to turn "
        f"{previous + 1}."
    )


async def test_a_fix_failing_in_the_report_turn_bounds_the_declaration():
    """The boundary: the cause seen removed at 7, the problem present at 10, so
    the fix FAILED at 10 on (7, 10), which reads the same with a second fix
    dated 10 or 11. A failure in the report turn is not one before it, so the
    declaration at 11 is refused by the bound."""
    eng, case, hyp = _identified()
    await _turn(eng, case, 6, solutions_to_add=[_fix()], milestones=_accepted())
    await _turn(
        eng, case, 7, _ev(_CAUSE_GONE, "max_connections reads 100 after the change")
    )
    await _turn(eng, case, 10, _ev(_PRESENT, "checkout returns 503 again"))
    assert _judged(case) == ("failed", 10, (7, 10))

    meta = await _second_fix_declared(eng, case)

    assert _stamps(case) == [(6, None), (11, None)]
    assert _judged(case) == ("failed", 10, (7, 10))
    assert f"{_IGNORED}: {_not_failing_before(10)}" in meta["system_feedback"]


async def test_a_declaration_moving_a_disconfirmation_is_ignored():
    """The fix relapsed at 8, so it FAILED before 10; at 10 the cause
    re-checked removed and the problem present disconfirm it, and M6 demotes
    the cause. Dating the second fix 10 would take that re-check out of the
    first fix's window and erase the disconfirmation M6 acted on, so the
    declaration is ignored."""
    eng, case, hyp = _identified()
    await _relapsed_at_8(eng, case)
    await _turn(
        eng,
        case,
        10,
        _ev(_CAUSE_GONE, "max_connections still reads 100"),
        _ev(_PRESENT, "checkout still returns 503"),
    )
    assert hyp.state == HypothesisState.REFUTED

    meta = await _second_fix_declared(eng, case)

    assert _stamps(case) == [(6, None), (11, None)]
    assert _judged(case) == ("failed", 8, (10, 10))
    assert (
        f"{_IGNORED}: turn 10's observations decide whether the fix executed at "
        "turn 6 was disconfirmed, so they stay with it; the fix is dated to turn "
        "11." in meta["system_feedback"]
    )
    await _turn(eng, case, 12, _ev(_GONE, "checkout returns 200, 503 rate 0.0/s"))
    verdict = assess_resolution_readiness(case).verdict
    assert verdict == ResolutionReadiness.NEEDS_INFO


async def test_a_second_fix_reported_after_the_first_failed_is_dated_to_its_report():
    """The fix relapsed at 8; at 10 the user reports the second fix with its
    output, the cause and the problem seen gone; at 11 it is minted and
    accepted, declared reported at 10. The first fix keeps its verdict, and
    the second is dated 10 and confirmed by the report's rows."""
    eng, case, _hyp = _identified()
    await _relapsed_at_8(eng, case)
    await _turn(
        eng,
        case,
        10,
        _ev(_CAUSE_GONE, "max_connections reads 200, per-worker cap 20"),
        _ev(_GONE, "checkout returns 200, 503 rate 0.0/s"),
    )

    meta = await _second_fix_declared(eng, case)

    assert _stamps(case) == [(6, None), (10, 11)]
    assert _judged(case) == ("failed", 8, None)
    assert _windows(case)[-1] == (10, "succeeded")
    assert _IGNORED not in (meta.get("system_feedback") or "")
    verdict = assess_resolution_readiness(case).verdict
    assert verdict == ResolutionReadiness.READY


# ---------------------------------------------------------------------------
# A declaration that is not applied says so.
# ---------------------------------------------------------------------------


async def test_a_declaration_on_the_first_turn_is_ignored_with_a_line(caplog):
    eng, case, _hyp = _identified()

    with caplog.at_level(logging.INFO):
        meta = await _turn(
            eng, case, 1, solutions_to_add=[_fix()], milestones=_accepted(_PREVIOUS)
        )

    assert _stamps(case) == [(1, None)]
    reason = "there is no message before turn 1; the fix is dated to turn 1"
    assert f"{_IGNORED}: {reason}." in meta["system_feedback"]
    assert f"ignored at turn 1: {reason}" in caplog.text


async def test_a_declaration_without_an_acceptance_is_ignored_with_a_line():
    eng, case, _hyp = _identified()

    meta = await _turn(
        eng, case, 6, milestones=MilestoneUpdates(accepted_fix_reported_in=_PREVIOUS)
    )

    assert _stamps(case) == []
    assert (
        f"{_IGNORED}: it is read only with solution_accepted."
        in meta["system_feedback"]
    )


async def test_a_refused_acceptance_takes_its_declaration_with_it():
    """On a problem found never present the acceptance is refused, and the
    declaration goes with it: one NOT ACCEPTED line, no line on the
    declaration."""
    eng, case, _hyp = _identified()
    await _turn(eng, case, 6, solutions_to_add=[_fix()])
    case.progress.problem_status = ProblemStatus.INVALIDATED

    meta = await _turn(eng, case, 7, milestones=_accepted(_PREVIOUS))

    assert _stamps(case) == []
    assert "solution_accepted NOT ACCEPTED" in meta["system_feedback"]
    assert "accepted_fix_reported_in" not in meta["system_feedback"]


async def test_a_declaration_on_a_revision_hold_turn_is_ignored_with_a_line():
    """A fix carried through a revision hold registers at the replay, dated to
    its hold turn, as the carried path does; the declaration is not carried."""
    eng, case, _hyp = _identified()
    _revise(case)

    meta = await _turn(
        eng, case, 6, solutions_to_add=[_fix()], milestones=_accepted(_PREVIOUS)
    )
    await _confirm(eng, case, 8)

    assert (
        f"{_IGNORED}: a fix carried through a revision hold is dated to its hold "
        "turn, 6." in meta["system_feedback"]
    )
    assert _stamps(case) == [(6, None)]


@pytest.mark.parametrize("value", ["previous", "previous_turn", "PREVIOUS_MESSAGE"])
async def test_an_unknown_report_value_costs_that_field_alone(value):
    """Through the degradation ladder and the apply: an off-enum value is
    dropped by its own validator and recorded as a repair, the model is told,
    and the acceptance and the fix it names stand."""
    eng, case, _hyp = _identified()
    await _turn(eng, case, 6, solutions_to_add=[_fix()])
    (fix,) = [
        a
        for a in case.proposed_actions
        if a.action_type == InvestigationActionType.SOLUTION
    ]
    case.current_turn = 7
    body = {
        "agent_response": "Noted.",
        "evidence_trail": {
            "evidence_analyzed": [],
            "milestone_justifications": {
                "solution_accepted": "the user reports running the change"
            },
        },
        "state_updates": {
            "milestones": {
                "solution_accepted": True,
                "accepted_action_ref": fix.action_id,
                "accepted_fix_reported_in": value,
            }
        },
    }

    parsed = _validate_with_degradation(body, InvestigationResponse_Diagnosis)
    m = parsed.state_updates.milestones
    assert (m.solution_accepted, m.accepted_action_ref) == (True, fix.action_id)
    assert m.accepted_fix_reported_in is None
    _case, meta = await eng.responses.process_response_structured(
        case, "I ran it", parsed
    )

    assert _stamps(case) == [(7, None)]
    assert (
        f"MilestoneUpdates.accepted_fix_reported_in: {value!r} is not "
        "this_message or previous_message; field dropped, the fix is dated to "
        "this turn" in meta["validation_repairs"]
    )
    assert (
        f"SYSTEM: accepted_fix_reported_in {value!r} was ignored: it is "
        "this_message or previous_message; the fix is dated to this turn."
        in meta["system_feedback"]
    )


# ---------------------------------------------------------------------------
# The floor: the registration turn.
# ---------------------------------------------------------------------------


async def test_a_cause_posited_in_the_registration_turn_is_still_the_fixs_to_judge():
    """The cause posited at 10, the turn the fix the user reported at 9
    registers, declared. The fix is dated 9 but registered at 10, when the
    cause existed, so at 11 (the cause seen removed, then the problem present)
    M6 demotes it through the turn (``failed_fix_of``'s registration floor)."""
    eng, case, hyp = _identified(hypothesis_turn=10)
    await _turn(
        eng, case, 10, solutions_to_add=[_fix()], milestones=_accepted(_PREVIOUS)
    )
    assert _stamps(case) == [(9, 10)]

    await _turn(
        eng,
        case,
        11,
        _ev(_CAUSE_GONE, "max_connections reads 100 after the change"),
        _ev(_PRESENT, "checkout returns 503 again"),
    )

    assert fix_windows(case)[-1].disconfirmation == (11, 11)
    assert hyp.state == HypothesisState.REFUTED
    assert case.progress.cause_state == CauseState.UNKNOWN
    assert _engine_rows(case)


# ---------------------------------------------------------------------------
# The prompt names the declaration once.
# ---------------------------------------------------------------------------


def _count(text: str) -> int:
    """Occurrences of the declaration line, the template's line wraps folded."""
    return " ".join(text.split()).count(_DECLARE)


def test_the_investigation_base_teaches_the_declaration_once():
    """The line sits beside ``accepted_action_ref`` in the milestone
    justifications, where a fix minted and accepted in one response
    (``new_index_N``) is taught, and nowhere else in the base."""
    justifications = INVESTIGATION_BASE.split("milestone_justifications:", 1)[1]
    justifications = justifications.split("uncertainties:", 1)[0]

    assert _count(INVESTIGATION_BASE) == 1
    assert _count(justifications) == 1


async def test_a_diagnosis_turn_with_a_pending_fix_renders_the_declaration_once():
    """The fix proposed and awaiting execution: the turn renders the
    awaiting-execution focus and the compliance block together, and the
    declaration once, from the base."""
    eng, case, _hyp = _identified()
    await _turn(eng, case, 6, solutions_to_add=[_fix()])
    case.current_turn = 7

    prompt = get_prompt_for_case(case, user_message="I've applied the change")

    assert "awaiting execution" in prompt
    assert "COMPLIANCE DETECTION" in prompt
    assert _count(prompt) == 1
