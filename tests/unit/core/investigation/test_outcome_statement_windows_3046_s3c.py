"""The outcome statement's fix-window lines (#3046 S3c).

The statement states a fix window's verdict exactly when the turn moved it
into SUCCEEDED or into FAILED in the record, once per window
(``fix_turn``), after the accepted-attempt clauses, and the turn's own prose
does not already state it. Leaving SUCCEEDED, a FAILED turning final and an
attempt absent from the turn-final record state nothing. Every record here is
built by ``compute_case_outcome`` from a real case; the copy is the ruling's
(comment 7161149979, Q1), pinned verbatim. No live LLM.
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.milestone_engine import turn_completion
from faultmaven.core.investigation.milestone_engine.turn_completion import (
    _outcome_statement_for_turn,
    _statement_record,
)
from faultmaven.core.investigation.milestone_engine.turn_narration import (
    OUTCOME_WRITTEN_KEY,
)
from faultmaven.core.investigation.narration.codes import OfferCode
from faultmaven.core.investigation.narration.fix_verification import Content
from faultmaven.core.investigation.narration.replies import confirmation_params
from faultmaven.core.investigation.narration.statement import (
    OutcomeChange,
    offer_stated,
    outcome_changes,
    outcome_statement,
)
from faultmaven.core.investigation.outcome import compute_case_outcome
from faultmaven.core.investigation.terminal_transitions import (
    resolution_record_lines,
)
from faultmaven.modules.case.contracts import (
    EvidenceCategory,
    ResolutionReason,
    WritePoint,
)
from tests.unit.core.investigation.test_case_outcome_fold_2636 import (
    _cited_but_unlinked,
    _eid,
    _fix,
    _relapse,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _cause_gone,
    _identified_case,
    _problem_gone,
    _row,
    _still_failing,
)
from tests.unit.core.investigation.test_outcome_statement_2811_s2c3a import (
    _CONTENT,
    HEAD,
    SEP,
    _base,
    _confirmed_attempts,
    _kinds,
    _process,
    _readiness,
    _record,
    _response,
    _revision_engine,
)
from tests.unit.core.investigation.test_record_prose_2811_s2b2c import _Poisoned

pytestmark = pytest.mark.unit

_S, _F = OutcomeChange.FIX_SUCCEEDED, OutcomeChange.FIX_FAILED
_X = OutcomeChange.FIX_CARRIED_OUT
_GONE_6 = (
    "after the fix carried out in turn 6, the cause and the problem were both seen gone"
)


def _cause_gone_at(turn: int, label: str):
    return _row(
        label,
        EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        turn,
        "max_connections reads 100 on the re-check",
    )


def _statement(before, now, stated=frozenset()):
    return outcome_statement(outcome_changes(_base(before), now), stated)


def _windows(changes) -> list:
    """Each window change as (kind, fix_turn), asserting that the turn the
    clause prints is the carried-out clause's (``accepted_in_turn``)."""
    out = []
    for change in changes:
        if change.kind in (_S, _F):
            window = change.attempt.window
            assert window.fix_turn == change.attempt.accepted_in_turn
            out.append((change.kind, window.fix_turn))
    return out


# ---------------------------------------------------------------------------
# outcome_changes: each transition, from records the fold builds
# ---------------------------------------------------------------------------


def test_into_succeeded_states_both_seen_gone():
    case, _root, _hyp = _identified_case()
    _fix(case, turn=6)
    before = _record(case)
    case.evidence += [_cause_gone(7), _problem_gone(7)]
    now = _record(case, before)
    assert _windows(outcome_changes(_base(before), now)) == [(_S, 6)]
    assert _statement(before, now) == HEAD + _GONE_6 + "."


def test_into_failed_with_no_relief_states_still_present():
    case, _root, _hyp = _identified_case()
    _fix(case, turn=8)
    before = _record(case)
    case.evidence += [_cause_gone(9), _still_failing(9)]
    now = _record(case, before)
    assert _windows(outcome_changes(_base(before), now)) == [(_F, 8)]
    assert _statement(before, now) == (
        HEAD + "after the fix carried out in turn 8, the problem was still "
        "present (turn 9)."
    )


def test_into_failed_after_a_relief_states_seen_again():
    case, _root, _hyp = _identified_case()
    _fix(case, turn=6)
    before = _record(case)
    _relapse(case, rechecks=_eid("pool_log"))
    assert _statement(before, _record(case, before)) == (
        HEAD + "after the fix carried out in turn 6, the problem was seen gone, "
        "then seen again (turn 9)."
    )


def test_succeeded_into_failed_states_seen_again():
    case, _root, _hyp = _identified_case()
    _fix(case, turn=6)
    case.evidence += [_cause_gone(7), _problem_gone(7)]
    before = _record(case)
    assert before.attempts[0].window.state == "succeeded", "premise"
    case.evidence.append(_still_failing(8))
    assert _statement(before, _record(case, before)) == (
        HEAD + "after the fix carried out in turn 6, the problem was seen gone, "
        "then seen again (turn 8)."
    )


def test_a_window_first_seen_this_turn_follows_its_carried_out_clause():
    case, _root, _hyp = _identified_case()
    before = _record(case)
    _fix(case, turn=8)
    case.evidence += [_cause_gone(9), _still_failing(9)]
    now = _record(case, before)
    changes = outcome_changes(_base(before), now)
    assert _kinds(changes) == [_X, _F]
    assert _windows(changes) == [(_F, 8)]
    assert outcome_statement(changes, frozenset()) == (
        HEAD + "a fix was carried out (turn 8); after the fix carried out in "
        "turn 8, the problem was still present (turn 9)."
    )


def test_window_zero_failed_states_before_any_fix():
    case, root, _hyp = _identified_case()
    before = _record(case)
    _relapse(case, rechecks=_cited_but_unlinked(case, root).evidence_id)
    now = _record(case, before)
    assert [a.action_id for a in now.attempts] == [None], "premise: window 0"
    assert _windows(outcome_changes(_base(before), now)) == [(_F, None)]
    assert _statement(before, now) == (
        HEAD + "before any fix was carried out in the case, the problem was seen "
        "gone, then seen again (turn 9)."
    )


def test_window_zero_succeeded_states_before_any_fix():
    case, _root, _hyp = _identified_case()
    before = _record(case)
    case.evidence += [_cause_gone(7), _problem_gone(7)]
    assert _statement(before, _record(case, before)) == (
        HEAD + "before any fix was carried out in the case, the cause and the "
        "problem were both seen gone."
    )


# ---------------------------------------------------------------------------
# The non-transitions
# ---------------------------------------------------------------------------


def test_unknown_staying_unknown_states_nothing():
    case, _root, _hyp = _identified_case()
    _fix(case, turn=6)
    before = _record(case)
    assert before.attempts[0].window.state == "unknown", "premise"
    assert outcome_changes(_base(before), _record(case, before)) == []
    # Control: the same window seen gone states it.
    case.evidence += [_cause_gone(7), _problem_gone(7)]
    assert _windows(outcome_changes(_base(before), _record(case, before))) == [(_S, 6)]


def test_a_carried_final_failed_states_nothing():
    case, _root, _hyp = _identified_case()
    _fix(case, turn=8)
    start = _record(case)
    case.evidence += [_cause_gone(9), _still_failing(9)]
    before = _record(case, start)
    assert before.attempts[0].window.final_at is not None, "premise: final"
    case.evidence.append(_problem_gone(10))
    now = _record(case, before)
    assert outcome_changes(_base(before), now) == []
    # Control: from before the failure, the same record states it.
    assert _windows(outcome_changes(_base(start), now)) == [(_F, 8)]


def test_a_failed_window_turning_final_states_nothing():
    case, _root, _hyp = _identified_case()
    _fix(case, turn=8)
    start = _record(case)
    case.evidence += [_cause_gone(9), _still_failing(9)]
    saved = compute_case_outcome(start, case, at=WritePoint.COMMIT)
    window = saved.attempts[0].window
    assert window.state == "failed" and window.final_at is None, "premise"
    final = _record(case, saved)
    assert final.attempts[0].window.final_at is not None, "premise: minted"
    assert outcome_changes(_base(saved), final) == []
    # Control: from before the failure, the same final record states it.
    assert _windows(outcome_changes(_base(start), final)) == [(_F, 8)]


def test_leaving_succeeded_states_nothing():
    """Ruling Q2: "were seen gone" stays true of that turn."""
    case, _root, _hyp = _identified_case()
    _fix(case, turn=6)
    gone = _problem_gone(7)
    case.evidence += [_cause_gone(7), gone]
    before = _record(case)
    assert before.attempts[0].window.state == "succeeded", "premise"
    case.evidence.remove(gone)
    now = _record(case, before)
    assert now.attempts[0].window.state == "unknown", "premise"
    assert outcome_changes(_base(before), now) == []
    # Control: back into SUCCEEDED, it is stated again.
    case.evidence.append(gone)
    assert _windows(outcome_changes(_base(now), _record(case, now))) == [(_S, 6)]


def test_an_attempt_absent_from_the_turn_final_record_states_nothing():
    """A fix dated back moves window 0's rows into its own window: window 0
    leaves the record and states nothing; the new window states."""
    case, _root, _hyp = _identified_case()
    case.evidence += [_cause_gone(7), _problem_gone(7)]
    before = _record(case)
    assert [(a.action_id, a.window.state) for a in before.attempts] == [
        (None, "succeeded")
    ], "premise: window 0 seen gone"
    _fix(case, turn=6)
    now = _record(case, before)
    assert None not in [a.action_id for a in now.attempts], "premise: absent"
    assert _windows(outcome_changes(_base(before), now)) == [(_S, 6)]


def test_two_attempts_sharing_a_window_state_one_clause():
    case, _root, _hyp = _identified_case()
    _fix(case, turn=6)
    _fix(case, turn=6)
    before = _record(case)
    case.evidence += [_cause_gone(7), _problem_gone(7)]
    now = _record(case, before)
    assert [a.window.fix_turn for a in now.attempts] == [6, 6], "premise"
    changes = outcome_changes(_base(before), now)
    assert _windows(changes) == [(_S, 6)]
    assert outcome_statement(changes, frozenset()) == HEAD + _GONE_6 + "."
    assert outcome_statement(changes, frozenset({(_S, 6)})) is None


# ---------------------------------------------------------------------------
# stated: what each offer's prose already states
# ---------------------------------------------------------------------------


def _failed_then_failed():
    """Fixes at 6 and 8, each seen failing this turn."""
    case, _root, _hyp = _identified_case()
    _fix(case, turn=6)
    _fix(case, turn=8)
    before = _record(case)
    case.evidence += [
        _cause_gone(7),
        _still_failing(7, label="still_7"),
        _cause_gone_at(9, "cause_gone_9"),
        _still_failing(10, label="still_10"),
    ]
    now = _record(case, before)
    assert _windows(outcome_changes(_base(before), now)) == [(_F, 6), (_F, 8)]
    return before, now


@pytest.mark.parametrize(
    "code, extra, states",
    [
        (OfferCode.RESOLUTION_NEEDS_INFO, {}, True),
        (OfferCode.NEEDS_INFO_UNMET_CLOSE, {"suggest_close": True}, True),
        (OfferCode.NEEDS_INFO_UNMET_CLOSE, {"suggest_close": False}, False),
        (OfferCode.MODEL_PROPOSAL, {"composer": "readiness"}, False),
    ],
    ids=["needs-info", "unmet-close", "unmet-no-readiness", "model-proposal"],
)
def test_the_fix_failed_readiness_states_the_latest_windows_failure(
    code, extra, states
):
    before, now = _failed_then_failed()
    params = _readiness(ResolutionReason.FIX_FAILED, **extra)
    stated = offer_stated(code, params, now.attempts)
    assert ((_F, 8) in stated) is states
    expected_8 = (
        ""
        if states
        else "; after the fix carried out in turn 8, the problem was still present (turn 10)"
    )
    # The earlier window is still stated.
    assert _statement(before, now, stated) == (
        HEAD + "after the fix carried out in turn 6, the problem was still "
        "present (turn 7)" + expected_8 + "."
    )
    # Without the record's attempts no window verdict is stated.
    assert (_F, 8) not in offer_stated(code, params)


def _seen_gone_twice():
    """Fixes at 6 and 8; late data turns the superseded window 6 SUCCEEDED
    in the same turn as the latest, 8."""
    case, _root, _hyp = _identified_case()
    _fix(case, turn=6)
    _fix(case, turn=8)
    before = _record(case)
    case.evidence += [
        _cause_gone(7),
        _problem_gone(7),
        _cause_gone_at(9, "cause_gone_9"),
        _problem_gone(9, label="problem_gone_9"),
    ]
    now = _record(case, before)
    assert _windows(outcome_changes(_base(before), now)) == [(_S, 6), (_S, 8)]
    return case, before, now


@pytest.mark.parametrize(
    "code, params",
    [
        (OfferCode.BACKSTOP_RESOLVE, {"identification_leg": "chain"}),
        (OfferCode.BACKSTOP_RESOLVE, {"identification_leg": None}),
        (OfferCode.DEFERRED_RESOLVE, {}),
    ],
    ids=["backstop", "backstop-no-leg", "deferred"],
)
def test_a_resolve_offers_lines_state_the_window_it_confirms_through(code, params):
    case, before, now = _seen_gone_twice()
    lines = resolution_record_lines(case)
    assert "- **The cause** — " in lines and "- **The problem** — " in lines
    stated = offer_stated(code, {**params, "record_lines": lines}, now.attempts)
    assert (_S, 8) in stated and (_S, 6) not in stated
    assert _statement(before, now, stated) == HEAD + _GONE_6 + "."
    # Lines without a re-checked problem line state no window.
    cause_only = "\n".join(
        line for line in lines.splitlines() if line.startswith("- **The cause**")
    )
    assert (_S, 8) not in offer_stated(
        code, {**params, "record_lines": cause_only}, now.attempts
    )


def test_the_confirmation_naming_its_attempt_states_its_window():
    record, fix = _confirmed_attempts()
    text = {fix.action_id: Content(text="raise max_connections to 100")}.get
    params = confirmation_params(record, _CONTENT, text)
    assert (_S, 6) in offer_stated(OfferCode.REQUEST_READY, params, record.attempts)
    # The latest solution named instead: the window is not stated.
    bare = confirmation_params(record, _CONTENT, lambda _id: None)
    assert (_S, 6) not in offer_stated(OfferCode.REQUEST_READY, bare, record.attempts)


# ---------------------------------------------------------------------------
# Pin 2, and through process_turn
# ---------------------------------------------------------------------------


def test_the_statement_site_reads_the_window_from_the_record_alone():
    case, _root, _hyp = _identified_case()
    _fix(case, turn=6)
    before = _record(case)
    case.evidence += [_cause_gone(7), _problem_gone(7)]
    case.progress.outcome = _record(case, before)
    now = _statement_record(_Poisoned(case), {OUTCOME_WRITTEN_KEY: True}, _base(before))
    assert now is case.progress.outcome
    assert _outcome_statement_for_turn(_base(before), now, frozenset()) == (
        HEAD + _GONE_6 + "."
    )


def _fixed_and_cause_gone():
    case, _root, _hyp = _identified_case()
    _fix(case, turn=8)
    case.evidence.append(_cause_gone(9))
    case.current_turn = 9
    return case


async def test_a_turn_that_sees_the_fix_fail_states_it():
    reply, result = await _process(
        _revision_engine(),
        _fixed_and_cause_gone(),
        "still 503",
        _response(),
        during=lambda c: c.evidence.append(_still_failing(10)),
    )
    assert result["case_updated"].progress.outcome.attempts[0].window.state == "failed"
    assert reply.endswith(
        "after the fix carried out in turn 8, the problem was still present "
        "(turn 10)."
    )


async def test_the_fix_failed_readiness_message_is_not_repeated(monkeypatch):
    """Positive control: the window moved into FAILED this turn."""
    seen = []
    real = turn_completion._outcome_statement_for_turn

    def _spy(baseline, now, stated):
        seen.append(_windows(outcome_changes(baseline, now)))
        return real(baseline, now, stated)

    monkeypatch.setattr(turn_completion, "_outcome_statement_for_turn", _spy)
    reply, _ = await _process(
        _revision_engine(),
        _fixed_and_cause_gone(),
        "still 503, resolve it",
        _response(proposed_transition={"to_state": "resolved"}),
        during=lambda c: c.evidence.append(_still_failing(10)),
    )
    assert "The last fix didn't hold" in reply, "premise: the readiness message"
    assert seen == [[(_F, 8)]]
    assert "after the fix carried out in turn 8" not in reply


async def test_a_backstop_offer_states_the_window_once(monkeypatch):
    """The backstop's record lines say the cause and the problem were seen
    gone: no Case record line repeats it. Positive control: the window moved
    into SUCCEEDED this turn."""
    from tests.unit.core.investigation.test_resolution_backstop_turn import (
        _confirmed_case,
    )

    case = _confirmed_case()
    rows = [
        e
        for e in case.evidence
        if e.category
        in (
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
        )
    ]

    def _before_the_rows(c):
        # The snapshot was folded before the rows that confirm the window.
        kept = list(c.evidence)
        c.evidence = [e for e in kept if e not in rows]
        compute = compute_case_outcome(None, c, at=WritePoint.COMMIT)
        c.progress.outcome = compute
        c.evidence = kept

    seen = []
    real = turn_completion._outcome_statement_for_turn

    def _spy(baseline, now, stated):
        seen.append(_windows(outcome_changes(baseline, now)))
        return real(baseline, now, stated)

    monkeypatch.setattr(turn_completion, "_outcome_statement_for_turn", _spy)
    reply, _ = await _process(
        _revision_engine(),
        case,
        "errors gone",
        _response(),
        after_save=_before_the_rows,
    )
    assert "The record shows:" in reply, "premise: the backstop offer"
    assert seen == [[(_S, None)]]
    assert HEAD not in reply
    assert SEP in reply
