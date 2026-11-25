"""The engine's outcome statement under a model turn's reply (#2811 S2c-3a).

``outcome_changes`` compares the record at turn start (``OutcomeBaseline``) with
the turn-final record over the closed field set (rulings of 2026-11-22 Q4 (a)
and 2026-11-24): the problem's status, the identification leg and its fallen
pool, the accepted attempts and their fix windows' verdicts (#3046 S3c).
``outcome_statement`` renders the changes the turn's own prose does not state
(``stated``, ``offer_stated``, read against the turn-final record's attempts)
as one "**Case record:**" line. Both read the two records, the turn-start
restore target and the actions accepted at turn start alone (pin 2); the
restore target's owners are the revision offer's and the invalidation's
``prior_status`` (``problem_status.restores_to``).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, NamedTuple, Optional, Sequence

from faultmaven.modules.case.contracts import (
    AttemptOutcome,
    CaseOutcome,
    CauseOutcome,
    FixWindowRecord,
    FixWindowState,
    PoolEmptyReason,
    ProblemStatus,
    ResolutionReason,
)

from .codes import OfferCode
from .offers import pool_empty_user_line
from .replies import NO_ROOT_ON_RECORD, confirming_attempt


class OutcomeChange(StrEnum):
    """A closed-set fact the turn changed, in the order the statement states
    them."""

    PROBLEM_VERIFIED = "problem_verified"
    PROBLEM_UNCONFIRMED = "problem_unconfirmed"
    PROBLEM_INVALIDATED = "problem_invalidated"
    CAUSE_IDENTIFIED = "cause_identified"
    CAUSE_FALLEN = "cause_fallen"
    CAUSE_STANDS_AGAIN = "cause_stands_again"
    CAUSE_CLEARED = "cause_cleared"
    FIX_CARRIED_OUT = "fix_carried_out"
    FIX_SUCCEEDED = "fix_succeeded"
    FIX_FAILED = "fix_failed"


_WINDOW_KINDS = frozenset({OutcomeChange.FIX_SUCCEEDED, OutcomeChange.FIX_FAILED})


class Change(NamedTuple):
    """One change; a fix's carries its attempt, a window's the first attempt
    of the record holding that window."""

    kind: OutcomeChange
    attempt: Optional[AttemptOutcome] = None

    @property
    def key(self) -> tuple:
        """What ``stated`` names it by: its kind, and a fix's ``action_id`` or
        a window's ``fix_turn`` (window 0: None), which every attempt sharing
        the window shares (ruling of 2026-11-25, Q5)."""
        if self.kind in _WINDOW_KINDS:
            return (self.kind, self.attempt.window.fix_turn)
        return (self.kind, self.attempt.action_id if self.attempt else None)


@dataclass(frozen=True)
class OutcomeBaseline:
    """The record at turn start, before any write of the turn: a deep copy,
    the status its problem's hold or finding restores (``restores_to``),
    whether its problem status is the live one (a failed write at the last
    save leaves them apart, and no problem clause is then stated), and the
    actions already accepted then (``accepted``)."""

    record: Optional[CaseOutcome]
    restores_to: Optional[ProblemStatus]
    problem_comparable: bool
    accepted: frozenset


_HOLDS = frozenset({ProblemStatus.REVISION_PENDING, ProblemStatus.INVALIDATED})
_BACKSTOP_LEGS = frozenset({"rcc", "working_conclusion"})
_FALLEN_REASONS = frozenset(
    {
        PoolEmptyReason.FALLEN_WITH_OTHER_STANDING,
        PoolEmptyReason.FIX_TARGETS_OTHER_CAUSE,
    }
)


def pool_fallen(cause: CauseOutcome) -> bool:
    """The cause identified earlier no longer stands: a fallen root and an
    empty confirmation pool. ``fallen_root_ids`` also holds an identified
    root, so the pool decides (critique of 2026-11-24, 1)."""
    return bool(cause.fallen_root_ids) and not cause.confirmation_root_ids


def _problem(base: OutcomeBaseline, prior: ProblemStatus, now: ProblemStatus) -> list:
    """The problem clause: none when nothing moved, when the baseline is not
    the live status, or when a hold or finding ended where it restores to (a
    declined revision, a withdrawn finding). Otherwise a hold reads as the
    status it restores to (ruling of 2026-11-24, Q3 (a))."""
    if now == prior or not base.problem_comparable:
        return []
    if prior in _HOLDS and base.restores_to is not None:
        if now == base.restores_to:
            return []
        prior = base.restores_to
    if now == ProblemStatus.VERIFIED and prior != ProblemStatus.VERIFIED:
        return [Change(OutcomeChange.PROBLEM_VERIFIED)]
    if now == ProblemStatus.INVALIDATED and prior != ProblemStatus.INVALIDATED:
        return [Change(OutcomeChange.PROBLEM_INVALIDATED)]
    if prior == ProblemStatus.VERIFIED and now not in _HOLDS:
        return [Change(OutcomeChange.PROBLEM_UNCONFIRMED)]
    return []


def _cause(prior: CaseOutcome, now: CaseOutcome) -> list:
    """The cause clause. Across a hold's start or end, a backstop leg
    appearing or going is the hold's verified gate, not the cause: stated
    for a ``chain`` leg only (ruling of 2026-11-24 (a))."""
    was, leg = prior.cause.identification_leg, now.cause.identification_leg
    if (
        {prior.problem.status, now.problem.status} & _HOLDS
        and (was is None) != (leg is None)
        and (was or leg) in _BACKSTOP_LEGS
    ):
        return []
    fell, falls = pool_fallen(prior.cause), pool_fallen(now.cause)
    if was is not None and leg is None:
        return [Change(OutcomeChange.CAUSE_CLEARED)]
    if leg is None:
        return []
    if falls and (was is None or not fell):
        return [Change(OutcomeChange.CAUSE_FALLEN)]
    if was is None and not falls:
        return [Change(OutcomeChange.CAUSE_IDENTIFIED)]
    standing = {*now.cause.confirmation_root_ids, *now.cause.identified_root_ids}
    if (
        fell
        and now.cause.pool_empty_reason is None
        and standing & set(prior.cause.fallen_root_ids)
    ):
        return [Change(OutcomeChange.CAUSE_STANDS_AGAIN)]
    return []


def _fixes(prior: CaseOutcome, now: CaseOutcome, accepted: frozenset) -> list:
    """Each attempt accepted this turn, by ``action_id``: neither the prior
    record nor the turn-start actions held its acceptance, whatever turn it
    is dated to (a fix dated back to the report, #2427, or carried through a
    hold). Window 0 has none."""
    before = {a.action_id: a.accepted_in_turn for a in prior.attempts}
    return [
        Change(OutcomeChange.FIX_CARRIED_OUT, a)
        for a in now.attempts
        if a.action_id is not None
        and a.accepted_in_turn is not None
        and before.get(a.action_id) is None
        and a.action_id not in accepted
    ]


def _windows(prior: CaseOutcome, now: CaseOutcome) -> list:
    """One fact per fix window of the turn-final record, by ``fix_turn`` in
    record order: moved into SUCCEEDED or into FAILED since the turn-start
    record, where it may be absent. A window leaving SUCCEEDED states nothing
    (ruling of 2026-11-25, Q2), nor does a FAILED one turning final; an
    attempt absent from the turn-final record states nothing."""
    before: dict = {}
    for attempt in prior.attempts:
        if attempt.window is not None:
            before.setdefault(attempt.window.fix_turn, attempt.window.state)
    changes, seen = [], set()
    for attempt in now.attempts:
        window = attempt.window
        if window is None or window.fix_turn in seen:
            continue
        seen.add(window.fix_turn)
        if window.state == before.get(window.fix_turn):
            continue
        if window.state == FixWindowState.SUCCEEDED:
            changes.append(Change(OutcomeChange.FIX_SUCCEEDED, attempt))
        elif window.state == FixWindowState.FAILED:
            changes.append(Change(OutcomeChange.FIX_FAILED, attempt))
    return changes


def outcome_changes(base: OutcomeBaseline, now: Optional[CaseOutcome]) -> list:
    """The closed-set facts this turn changed, in the statement's order;
    none on a record minted this turn (no baseline record) or with none now.
    Mitigation and bookkeeping are never read (the mitigation is S3d's,
    #3053)."""
    prior = base.record
    if prior is None or now is None:
        return []
    return [
        *_problem(base, prior.problem.status, now.problem.status),
        *_cause(prior, now),
        *_fixes(prior, now, base.accepted),
        *_windows(prior, now),
    ]


_CLAUSES = {
    OutcomeChange.PROBLEM_VERIFIED: "the problem is confirmed",
    OutcomeChange.PROBLEM_UNCONFIRMED: "the problem is no longer confirmed",
    OutcomeChange.PROBLEM_INVALIDATED: (
        "the data shows the reported problem was not present"
    ),
    OutcomeChange.CAUSE_IDENTIFIED: "a root cause is identified",
    OutcomeChange.CAUSE_FALLEN: "the root cause identified earlier no longer stands",
    OutcomeChange.CAUSE_STANDS_AGAIN: "the root cause identified earlier stands again",
    OutcomeChange.CAUSE_CLEARED: "the root cause is no longer identified",
}


def _window_clause(change: Change) -> str:
    """The ruled copy (ruling of 2026-11-25, Q1): what was observed after the
    fix of the window's turn, or before any fix for window 0, whose FAILED is
    a relapse and so always follows a relief."""
    window = change.attempt.window
    if window.fix_turn is None:
        after = "before any fix was carried out in the case"
    else:
        after = f"after the fix carried out in turn {window.fix_turn}"
    if change.kind == OutcomeChange.FIX_SUCCEEDED:
        return f"{after}, the cause and the problem were both seen gone"
    if window.relief_turn is not None:
        return (
            f"{after}, the problem was seen gone, then seen again "
            f"(turn {window.failure_turn})"
        )
    return f"{after}, the problem was still present (turn {window.failure_turn})"


def _clause(change: Change) -> str:
    if change.kind in _WINDOW_KINDS:
        return _window_clause(change)
    if change.kind != OutcomeChange.FIX_CARRIED_OUT:
        return _CLAUSES[change.kind]
    # An unkinded attempt reads as a fix, as ``replies.closed_line`` reads it.
    what = "a workaround" if change.attempt.kind == "interception" else "a fix"
    return f"{what} was carried out (turn {change.attempt.accepted_in_turn})"


def outcome_statement(changes: Sequence[Change], stated: frozenset) -> Optional[str]:
    """``**Case record:** `` and the clause of each change ``stated`` does not
    name, joined by "; ", ending with "."; None with no clause."""
    clauses = [_clause(c) for c in changes if c.key not in stated]
    if not clauses:
        return None
    return "**Case record:** " + "; ".join(clauses) + "."


# --------------------------------------------------------------------------
# What the turn's own prose states, by (OfferCode, composer) as RECORD_PROSE
# is keyed, from the params the prose was rendered from.
# --------------------------------------------------------------------------

_Stated = Callable[[dict, Sequence[AttemptOutcome]], set]


def _nothing(params: dict, attempts) -> set:
    return set()


def _latest(windows) -> Optional[FixWindowRecord]:
    """The window with the greatest ``fix_turn``, window 0 below every dated
    one; None with none."""
    return max(
        windows,
        key=lambda w: -1 if w.fix_turn is None else w.fix_turn,
        default=None,
    )


def _readiness(params: dict, attempts) -> set:
    """A readiness message: the false alarm says the problem was not present,
    the fix that did not hold says the latest window FAILED, and the "Root
    cause" ask with a fallen pool's user line says the cause no longer
    stands."""
    stated = set()
    if params["reason"] == ResolutionReason.FALSE_ALARM:
        stated.add((OutcomeChange.PROBLEM_INVALIDATED, None))
    latest = _latest([a.window for a in attempts if a.window is not None])
    if params["reason"] == ResolutionReason.FIX_FAILED and latest is not None:
        stated.add((OutcomeChange.FIX_FAILED, latest.fix_turn))
    cause = params.get("cause")
    if (
        params["reason"] == ResolutionReason.DOCUMENTATION_GAPS
        and "root cause" in params["missing"]
        and cause is not None
        and cause.pool_empty_reason in _FALLEN_REASONS
        and pool_empty_user_line(cause) is not None
    ):
        stated.add((OutcomeChange.CAUSE_FALLEN, None))
    return stated


def _unmet_close(params: dict, attempts) -> set:
    return _readiness(params, attempts) if params["suggest_close"] else set()


_CAUSE_LINE = "- **The cause** — "
_PROBLEM_LINE = "- **The problem** — "


def _seen_gone(record_lines: str, attempts) -> set:
    """Record lines holding a cause line and a re-checked problem line say
    the cause and the problem were seen gone in the window the resolution
    confirms through, the latest observed one (ruling of 2026-11-25, Q5)."""
    lines = record_lines.splitlines()
    latest = _latest([a.window for a in attempts if a.window and a.window.observed])
    if latest is None or not (
        any(line.startswith(_CAUSE_LINE) for line in lines)
        and any(line.startswith(_PROBLEM_LINE) for line in lines)
    ):
        return set()
    return {(OutcomeChange.FIX_SUCCEEDED, latest.fix_turn)}


def _deferred_resolve(params: dict, attempts) -> set:
    """The deferred resolve prints the record lines alone: it names a cause
    only on a cause line that is not the no-root label."""
    named = any(
        line.startswith(_CAUSE_LINE)
        and not line[len(_CAUSE_LINE) :].startswith(NO_ROOT_ON_RECORD)
        for line in params["record_lines"].splitlines()
    )
    stated = _seen_gone(params["record_lines"], attempts)
    if named:
        stated.add((OutcomeChange.CAUSE_IDENTIFIED, None))
    return stated


def _backstop(params: dict, attempts) -> set:
    """The backstop prints the record lines, and says it records the
    attribution when a cause is on record."""
    stated = _seen_gone(params["record_lines"], attempts)
    if params["identification_leg"] is not None:
        stated.add((OutcomeChange.CAUSE_IDENTIFIED, None))
    return stated


def _confirmation(params: dict, attempts: Sequence[AttemptOutcome]) -> set:
    """The resolve confirmation: a cause when its root line names one, the
    confirming attempt when it is named as carried out, and, as the backstop,
    the window its record lines show seen gone."""
    stated = _seen_gone(params["record_lines"], attempts)
    if params["root"] != NO_ROOT_ON_RECORD:
        stated.add((OutcomeChange.CAUSE_IDENTIFIED, None))
    attempt = confirming_attempt(attempts)
    if (
        attempt is not None
        and attempt.accepted_in_turn is not None
        and params["solution"].endswith(
            f" (carried out turn {attempt.accepted_in_turn})"
        )
    ):
        stated.add((OutcomeChange.FIX_CARRIED_OUT, attempt.action_id))
    return stated


#: Every offer and request key. A model proposal's prose is its offer's
#: summary, never the reply's, so it states nothing there.
STATED_BY: dict[tuple, _Stated] = {
    (OfferCode.RCA_INFEASIBLE_CLOSE, None): _nothing,
    (OfferCode.DEFERRED_RESOLVE, None): _deferred_resolve,
    (OfferCode.DEFERRED_CLOSE, None): _nothing,
    (OfferCode.BACKSTOP_RESOLVE, None): _backstop,
    (OfferCode.FALSE_ALARM_CLOSE, None): lambda p, a: {
        (OutcomeChange.PROBLEM_INVALIDATED, None)
    },
    (OfferCode.NEEDS_INFO_UNMET_CLOSE, None): _unmet_close,
    (OfferCode.RESOLUTION_NEEDS_INFO, None): _readiness,
    (OfferCode.REQUEST_READY, None): _confirmation,
    (OfferCode.MODEL_PROPOSAL, "readiness"): _nothing,
    (OfferCode.MODEL_PROPOSAL, "confirmation"): _nothing,
    (OfferCode.MODEL_PROPOSAL, "closure"): _nothing,
}


def offer_stated(
    code: str, params: dict, attempts: Sequence[AttemptOutcome] = ()
) -> frozenset:
    """What the prose ``code`` rendered from ``params`` states, as ``Change``
    keys; ``attempts``: the turn-final record's, without which no window
    verdict is stated."""
    return frozenset(STATED_BY[(code, params.get("composer"))](params, attempts))
