"""Outcome copy rendered from the record (#2811 S2c-1, #2615 stage S2).

Five texts said more than the record holds: the no-window re-check line
(#2583), the cause half of the unbound feedback on an empty confirmation pool
(#2496), the closed replies of ``closed_insufficient_evidence`` with a cause
identified (#2419) and of ``solution_deferred`` (#2421), and the solution the
resolve confirmation names. Each now states what ``progress.outcome`` holds,
in the owner's approved copy (S2c plan, copy items 3, 4, 6, 7 and 8). Every
test drives an entry point that exists on ``main`` too, so each fails there.
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.cause_assurance import (
    cause_recheck_status,
    fix_windows,
    subject_index,
)
from faultmaven.core.investigation.milestone_engine import (
    terminal_replies as replies_module,
)
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    _terminal_confirmation_response,
    resolution_confirmation_params,
    resolution_content,
)
from faultmaven.core.investigation.narration.notices import unbound_lines
from faultmaven.core.investigation.narration.rechecks import no_window_text
from faultmaven.core.investigation.outcome import narration_outcome
from faultmaven.modules.case.contracts import (
    AttemptOutcome,
    CaseOutcome,
    CaseState,
    CauseOutcome,
    ClosureEligibility,
    ClosureOutcome,
    ClosureReason,
    FixWindowRecord,
    FixWindowState,
    NoConfirmingWindow,
    NoWindowKind,
    PoolEmptyReason,
    ProblemOutcome,
    ProblemStatus,
    RecheckReason,
    ResolutionOutcome,
    ResolutionReason,
    ResolutionVerdict,
    UnboundRecheck,
)
from tests.unit.core.investigation import reply_cases_2811 as replies
from tests.unit.core.investigation.prompts import (
    fix_verification_cases_2811 as rechecks,
)
from tests.unit.core.investigation.test_reply_record_2811 import _Poisoned

pytestmark = pytest.mark.unit

_SUCCEEDED, _FAILED, _UNKNOWN = (
    FixWindowState.SUCCEEDED,
    FixWindowState.FAILED,
    FixWindowState.UNKNOWN,
)


def _window(fix_turn, state=_UNKNOWN, observed=True) -> FixWindowRecord:
    return FixWindowRecord(fix_turn=fix_turn, state=state, observed=observed)


def _fix(action_id, turn, window=None, kind="remediation") -> AttemptOutcome:
    return AttemptOutcome(
        action_id=action_id, kind=kind, accepted_in_turn=turn, window=window
    )


def _workaround(action_id, turn) -> AttemptOutcome:
    return _fix(action_id, turn, kind="interception")


# ---------------------------------------------------------------------------
# #2583: the no-window line names what the decider found
# ---------------------------------------------------------------------------


def test_no_observed_window_is_never_said_to_have_failed():
    text = no_window_text(NoConfirmingWindow(kind=NoWindowKind.LATEST_FAILED))
    assert text == (
        "no re-check after the latest fix is on record; re-check the cause " "after it"
    )
    assert "failing" not in text


@pytest.mark.parametrize(
    "failure_turn, text",
    [
        (5, "the fix executed at turn 3 was seen failing at turn 5; only a new "
         "fix can confirm"),
        (None, "the fix executed at turn 3 was seen failing; only a new fix can "
         "confirm"),
    ],
)  # fmt: skip
def test_a_failed_fix_with_no_newer_one_names_the_turns_the_record_holds(
    failure_turn, text
):
    window = NoConfirmingWindow(
        kind=NoWindowKind.FAILED, fix_turn=3, failure_turn=failure_turn
    )
    assert no_window_text(window) == text


@pytest.mark.parametrize(
    "window, text",
    [
        (NoConfirmingWindow(kind=NoWindowKind.FAILED),
         "the fix done outside the case was seen failing; only a new fix can "
         "confirm"),
        (NoConfirmingWindow(kind=NoWindowKind.FAILED, fix_turn=3, newer_fix_turn=6),
         "the fix executed at turn 3 was seen failing; the newer fix (executed "
         "turn 6) is not re-checked yet"),
    ],
    ids=["window_zero", "newer_fix_pending"],
)  # fmt: skip
def test_a_failure_turn_the_record_does_not_hold_is_not_printed(window, text):
    assert no_window_text(window) == text


def test_a_failed_window_zero_keeps_its_line():
    window = NoConfirmingWindow(kind=NoWindowKind.FAILED, failure_turn=5)
    assert no_window_text(window) == (
        "the fix done outside the case was seen failing at turn 5; only a new "
        "fix can confirm"
    )


async def _latest_failed_rows():
    """Every re-check row the decider gives ``LATEST_FAILED``, over the
    re-check corpus, with its case."""
    out = []
    for name in rechecks.CASES:
        case = await rechecks.build(name)
        for row, why in cause_recheck_status(case):
            if (
                why is not None
                and why.reason == RecheckReason.NO_CONFIRMING_WINDOW
                and why.window.kind == NoWindowKind.LATEST_FAILED
            ):
                out.append((name, case, row))
    return out


async def test_the_decider_gives_latest_failed_only_with_no_window_observed():
    """The premise #2583's line rests on (the amendment's stop condition):
    ``LATEST_FAILED`` means no fix window was observed, and the row was
    observed before the latest fix."""
    rows = await _latest_failed_rows()
    assert rows, "positive control: the corpus reaches LATEST_FAILED"
    for name, case, row in rows:
        windows = fix_windows(case)
        assert not any(w.observed for w in windows), name
        dated = [w.fix_turn for w in windows if w.fix_turn is not None]
        assert dated, name
        assert subject_index(case).observed(row) < max(dated), name


# ---------------------------------------------------------------------------
# #2496: why the confirmation pool is empty, by the record's reason
# ---------------------------------------------------------------------------

_PARAMS = {
    "problem_row_ids": ["ev_p"],
    "cause_row_ids": [],
    "why": "the why",
    "pool_roots": False,
    "fix_dated": True,
}

_TODAY = "none supports the root cause alone yet."


def _outcome(cause: CauseOutcome, *, uncited: bool = False) -> CaseOutcome:
    return CaseOutcome(
        folded_under=CaseState.INVESTIGATING,
        problem=ProblemOutcome(status=ProblemStatus.VERIFIED),
        cause=cause,
        resolution=ResolutionOutcome(
            verdict=ResolutionVerdict.NEEDS_INFO,
            reason=ResolutionReason.DOCUMENTATION_GAPS,
        ),
        unbound_rechecks=[
            UnboundRecheck(evidence_id="ev_u", observed_turn=7, uncited=uncited)
        ],
        closure=ClosureOutcome(eligibility=ClosureEligibility.HAS_SUBSTANCE),
    )


def _cause_text(cause: CauseOutcome) -> str:
    ((line, ids),) = unbound_lines(_PARAMS, _outcome(cause))
    assert ids == ["ev_u"]
    head = "; causal_absence rows: "
    return line[line.index(head) + len(head) :]


_IDS = ["cn_1", "cn_2", "cn_3", "cn_4", "cn_5", "cn_6"]
_FIVE = "cn_1, cn_2, cn_3, cn_4, cn_5"

#: One record per reason, with the ids the reason names, and its line.
_BY_REASON = {
    PoolEmptyReason.HELD_OPEN: (
        {"held_open_root_ids": _IDS},
        f"none can confirm the cause while what produced {_FIVE} is open; "
        "re-send root_is_terminal true citing why it arose, then re-check it.",
    ),
    PoolEmptyReason.IDENTIFICATION_CONTESTED: (
        {"contested_root_ids": ["cn_a", "cn_b"]},
        "none can confirm the cause while cn_a, cn_b contest it; discriminate "
        "between them first.",
    ),
    PoolEmptyReason.FALLEN_WITH_OTHER_STANDING: (
        {"fallen_root_ids": ["cn_f"]},
        "none can confirm the fallen cause cn_f while another standing "
        "hypothesis is rooted elsewhere; settle or retire it first.",
    ),
    PoolEmptyReason.FIX_TARGETS_OTHER_CAUSE: (
        {"fallen_root_ids": ["cn_f"]},
        "none can confirm the fallen cause cn_f: the latest fix acted on a "
        "different cause.",
    ),
    PoolEmptyReason.ROOTLESS_VALIDATED_ONLY: (
        {},
        "the validated hypothesis has no root node to re-check; attach it to "
        "a root cause first.",
    ),
    PoolEmptyReason.NO_CAUSE: (
        {},
        "no root cause is on record that a re-check can confirm.",
    ),
}


def test_every_pool_empty_reason_has_a_model_line():
    """Pin 6, the model half: every ``PoolEmptyReason`` member is rendered."""
    assert set(_BY_REASON) == set(PoolEmptyReason)


@pytest.mark.parametrize("reason", list(PoolEmptyReason))
def test_the_feedback_says_why_the_pool_is_empty(reason):
    fields, line = _BY_REASON[reason]
    assert _cause_text(CauseOutcome(pool_empty_reason=reason, **fields)) == line


def test_the_uncited_line_says_it_too():
    cause = CauseOutcome(
        pool_empty_reason=PoolEmptyReason.IDENTIFICATION_CONTESTED,
        contested_root_ids=["cn_a", "cn_b"],
    )
    ((line, _ids),) = unbound_lines(_PARAMS, _outcome(cause, uncited=True))
    assert line == (
        "SYSTEM: Post-fix row(s) ev_u confirm nothing: none can confirm the "
        "cause while cn_a, cn_b contest it; discriminate between them first."
    )


@pytest.mark.parametrize(
    "cause",
    [
        # A leg beside NO_CAUSE: the record does not say no cause is on record.
        CauseOutcome(
            pool_empty_reason=PoolEmptyReason.NO_CAUSE, identification_leg="chain"
        ),
        # A reason that names roots, with none recorded.
        CauseOutcome(pool_empty_reason=PoolEmptyReason.HELD_OPEN),
        CauseOutcome(pool_empty_reason=PoolEmptyReason.IDENTIFICATION_CONTESTED),
        # No reason: the record's pool is not empty (the decider's read is).
        CauseOutcome(confirmation_root_ids=["cn_1"]),
    ],
    ids=["leg_with_no_cause", "held_open_no_ids", "contested_no_ids", "no_reason"],
)
def test_where_the_record_says_no_more_the_line_is_unchanged(cause):
    assert _cause_text(cause) == _TODAY


# ---------------------------------------------------------------------------
# #2419 and #2421: the closed replies state the leg and the attempts
# ---------------------------------------------------------------------------

_MITIGATION = "the problem was observed relieved after the mitigation (turn 7)"


async def _closed(reason, *, leg, attempts, statement=None):
    """A closed case whose stored terminal record holds ``reason``, ``leg``,
    ``attempts`` and the mitigation ``statement``; read through a case on
    which every field but the record, the identity and the proposed actions
    raises (pin 2)."""
    case = await replies.build("T:solution_deferred:bare:written")
    stored = case.progress.outcome
    assert stored.folded_under.is_terminal, "premise: the record is terminal"
    case.progress.outcome = stored.model_copy(
        update={
            "closure": stored.closure.model_copy(
                update={"reason": reason, "statement": statement}
            ),
            "cause": stored.cause.model_copy(update={"identification_leg": leg}),
            "attempts": attempts,
        }
    )
    return _terminal_confirmation_response(_Poisoned(case))


_IDENTIFIED = "Case closed — a root cause was identified; no fix of it was confirmed."
_INSUFFICIENT = ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE
_DEFERRED = ClosureReason.SOLUTION_DEFERRED
_ON_RECORD = "Case closed — cause identified and a fix is on record."
_TABLE_INSUFFICIENT = (
    "Case closed — insufficient evidence to ground a cause. Residual "
    "candidates and the missing data are preserved in the closure summary."
)


async def test_2419_case_071b1ff09eb6_states_the_workaround():
    """The report's record: leg ``chain``, one interception accepted at turn
    12, closed ``closed_insufficient_evidence``."""
    reply = await _closed(_INSUFFICIENT, leg="chain", attempts=[_workaround("a", 12)])
    assert reply == f"{_IDENTIFIED} Only a workaround was carried out (turn 12)."


@pytest.mark.parametrize(
    "attempts, done",
    [
        ([], "No fix was carried out in the case."),
        (
            [_fix("a", 9, _window(9, _FAILED))],
            "A fix was carried out (turn 9); it was not confirmed.",
        ),
        # An unkinded attempt reads as a remediation.
        (
            [_fix("a", 9, kind=None), _workaround("b", 11)],
            "A fix was carried out (turn 9); it was not confirmed.",
        ),
        # The latest is the greatest turn, never the list order.
        (
            [_workaround("a", 12), _workaround("b", 4)],
            "Only a workaround was carried out (turn 12).",
        ),
        # No turn on record: no turn clause.
        ([_workaround("a", None)], "Only a workaround was carried out."),
        # Window 0 is no accepted attempt.
        ([AttemptOutcome(window=_window(None, _FAILED))], "No fix was carried out "
         "in the case."),
    ],
    ids=["none", "fix", "unkinded", "latest", "no_turn", "window_zero"],
)  # fmt: skip
async def test_2419_an_identified_cause_states_what_was_carried_out(attempts, done):
    reply = await _closed(_INSUFFICIENT, leg="rcc", attempts=attempts)
    assert reply == f"{_IDENTIFIED} {done}"


async def test_2419_both_branches_carry_the_mitigation_sentence():
    reply = await _closed(
        _INSUFFICIENT,
        leg="chain",
        attempts=[_workaround("a", 12)],
        statement=_MITIGATION,
    )
    assert reply == (
        f"{_IDENTIFIED} Only a workaround was carried out (turn 12). The problem "
        "was observed relieved after the mitigation (turn 7)."
    )


@pytest.mark.parametrize(
    "leg, attempts",
    [
        (None, [_workaround("a", 12)]),
        ("chain", [AttemptOutcome(window=_window(None, _SUCCEEDED))]),
        ("chain", [_fix("a", 9, _window(9, _SUCCEEDED))]),
    ],
    ids=["no_leg", "window_zero_succeeded", "fix_succeeded"],
)
async def test_2419_without_a_leg_or_with_a_succeeded_window_it_is_unchanged(
    leg, attempts
):
    assert await _closed(_INSUFFICIENT, leg=leg, attempts=attempts) == (
        _TABLE_INSUFFICIENT
    )


async def test_2421_case_360fea23ebff_states_the_fix_carried_out():
    """The report's record: a fix accepted at turn 7, closed
    ``solution_deferred``."""
    reply = await _closed(
        _DEFERRED, leg="chain", attempts=[_fix("a", 7, _window(7, _UNKNOWN))]
    )
    assert reply == f"{_ON_RECORD} It was carried out (turn 7)."


@pytest.mark.parametrize(
    "attempts",
    [
        [_fix("a", 5, _window(5, _FAILED))],
        # A failed fix, then a later one that held: still not told apart from
        # a failed fix beside the deferred one.
        [_fix("a", 5, _window(5, _FAILED)), _fix("b", 8, _window(8, _SUCCEEDED))],
    ],
    ids=["failed", "failed_then_succeeded"],
)
async def test_2421_a_failed_fix_carried_out_leaves_the_first_sentence(attempts):
    """The record holds accepted attempts alone: a failed fix and a pending
    deferred one read the same, so the reply claims neither."""
    reply = await _closed(_DEFERRED, leg="chain", attempts=attempts)
    assert reply == _ON_RECORD


@pytest.mark.parametrize(
    "attempts",
    [
        [],
        # An accepted interception beside a pending remediation (no attempt).
        [_workaround("a", 5)],
        [AttemptOutcome(window=_window(None, _UNKNOWN))],
    ],
    ids=["none", "interception_only", "window_zero"],
)
async def test_2421_with_no_fix_carried_out_it_says_so(attempts):
    reply = await _closed(_DEFERRED, leg="chain", attempts=attempts)
    assert reply == f"{_ON_RECORD} It was not carried out in the case."


async def test_2421_both_branches_carry_the_mitigation_sentence():
    reply = await _closed(
        _DEFERRED,
        leg="chain",
        attempts=[_fix("a", None), _fix("b", 7)],
        statement=_MITIGATION,
    )
    assert reply == (
        f"{_ON_RECORD} It was carried out (turn 7). The problem was observed "
        "relieved after the mitigation (turn 7)."
    )


async def test_2421_with_no_record_the_reply_is_its_first_sentence(monkeypatch):
    """The record-free fallback states only what the close reason holds."""
    case = await replies.build("T:solution_deferred:line:copied")

    def _raise(_case):
        raise RuntimeError("probe")

    monkeypatch.setattr(replies_module, "narration_outcome", _raise)
    assert _terminal_confirmation_response(case) == _ON_RECORD


# ---------------------------------------------------------------------------
# The confirming solution
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


async def _two_actions():
    """``held_check`` with a second accepted action beside its own, each with
    its own description: ``(case, first, second)``."""
    case = await replies.build("held_check")
    first = case.proposed_actions[0]
    second = first.model_copy(
        update={"action_id": "act_000000second", "description": "Roll back the unit"}
    )
    case.proposed_actions.append(second)
    assert first.description and first.description != second.description
    return case, first, second


def _solution(monkeypatch, case, attempts) -> str:
    """The solution the confirmation names for a record holding ``attempts``."""
    record = narration_outcome(case).model_copy(update={"attempts": attempts})
    monkeypatch.setattr(replies_module, "narration_outcome", lambda _c: record)
    return resolution_confirmation_params(case)["solution"]


async def test_the_confirming_attempt_is_named(monkeypatch):
    case, first, second = await _two_actions()
    attempts = [
        _fix(second.action_id, 3, _window(3, _FAILED)),
        _fix(first.action_id, 6, _window(6, _SUCCEEDED)),
    ]
    solution = _solution(monkeypatch, case, attempts)
    assert solution == f"{first.description} (carried out turn 6)"
    assert solution != resolution_content(case).solution


@pytest.mark.parametrize(
    "shape",
    ["later_observed", "two_at_the_turn", "window_zero", "none"],
)
async def test_with_no_single_confirming_attempt_the_latest_solution_is_named(
    monkeypatch, shape
):
    case, first, second = await _two_actions()
    attempts = {
        # An older SUCCEEDED window, then a later observed one.
        "later_observed": [
            _fix(first.action_id, 3, _window(3, _SUCCEEDED)),
            _fix(second.action_id, 8, _window(8, _UNKNOWN)),
        ],
        # Two attempts share the confirming window.
        "two_at_the_turn": [
            _fix(first.action_id, 6, _window(6, _SUCCEEDED)),
            _fix(second.action_id, 6, _window(6, _SUCCEEDED)),
        ],
        "window_zero": [AttemptOutcome(window=_window(None, _SUCCEEDED))],
        "none": [],
    }[shape]
    assert _solution(monkeypatch, case, attempts) == resolution_content(case).solution


async def test_a_long_description_is_clipped_as_before(monkeypatch):
    case, first, _second = await _two_actions()
    first.description = "x" * 250
    attempts = [_fix(first.action_id, 6, _window(6, _SUCCEEDED))]
    assert _solution(monkeypatch, case, attempts) == (
        "x" * 200 + "... (carried out turn 6)"
    )
