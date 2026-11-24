"""The engine's outcome statement under a model turn's reply (#2811 S2c-3a).

``outcome_changes`` compares the record at turn start (``OutcomeBaseline``) with
the turn-final record over the closed field set (rulings of 2026-11-22 Q4 (a)
and 2026-11-24): the problem's status, the identification leg and its fallen
pool, and the accepted attempts. ``outcome_statement`` renders the changes the
turn's own prose does not state (``stated``, ``offer_stated``) as one
"**Case record:**" line. Both read the two records and the turn-start restore
target alone (pin 2); the restore target's owners are the revision offer's and
the invalidation's ``prior_status`` (``problem_status.restores_to``).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, NamedTuple, Optional, Sequence

from faultmaven.modules.case.contracts import (
    AttemptOutcome,
    CaseOutcome,
    CauseOutcome,
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


class Change(NamedTuple):
    """One change; a fix's carries its attempt."""

    kind: OutcomeChange
    attempt: Optional[AttemptOutcome] = None

    @property
    def key(self) -> tuple:
        """What ``stated`` names it by: its kind, and a fix's ``action_id``."""
        return (self.kind, self.attempt.action_id if self.attempt else None)


@dataclass(frozen=True)
class OutcomeBaseline:
    """The record at turn start, before any write of the turn: a deep copy,
    the status its problem's hold or finding restores (``restores_to``), and
    whether its problem status is the live one (a failed write at the last
    save leaves them apart, and no problem clause is then stated)."""

    record: Optional[CaseOutcome]
    restores_to: Optional[ProblemStatus]
    problem_comparable: bool


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


def _fixes(prior: CaseOutcome, now: CaseOutcome) -> list:
    """Each attempt accepted this turn, by ``action_id``: one whose
    ``accepted_in_turn`` the prior did not hold. Window 0 has none."""
    before = {a.action_id: a.accepted_in_turn for a in prior.attempts}
    return [
        Change(OutcomeChange.FIX_CARRIED_OUT, a)
        for a in now.attempts
        if a.action_id is not None
        and a.accepted_in_turn is not None
        and before.get(a.action_id) is None
    ]


def outcome_changes(base: OutcomeBaseline, now: Optional[CaseOutcome]) -> list:
    """The closed-set facts this turn changed, in the statement's order;
    none on a record minted this turn (no baseline record) or with none now.
    Windows, mitigation and bookkeeping are never read."""
    prior = base.record
    if prior is None or now is None:
        return []
    return [
        *_problem(base, prior.problem.status, now.problem.status),
        *_cause(prior, now),
        *_fixes(prior, now),
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


def _clause(change: Change) -> str:
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


def _readiness(params: dict, attempts) -> set:
    """A readiness message: the false alarm says the problem was not present,
    and the "Root cause" ask with a fallen pool's user line says the cause
    no longer stands."""
    stated = set()
    if params["reason"] == ResolutionReason.FALSE_ALARM:
        stated.add((OutcomeChange.PROBLEM_INVALIDATED, None))
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


def _identified_if(params: dict, *, lines: bool) -> set:
    if params["identification_leg"] is None or (lines and not params["record_lines"]):
        return set()
    return {(OutcomeChange.CAUSE_IDENTIFIED, None)}


def _confirmation(params: dict, attempts: Sequence[AttemptOutcome]) -> set:
    """The resolve confirmation: a cause when its root line names one, and
    the confirming attempt when it is named as carried out."""
    stated = set()
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
    (OfferCode.DEFERRED_RESOLVE, None): lambda p, a: _identified_if(p, lines=True),
    (OfferCode.DEFERRED_CLOSE, None): _nothing,
    (OfferCode.BACKSTOP_RESOLVE, None): lambda p, a: _identified_if(p, lines=False),
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
    """What the prose ``code`` rendered from ``params`` states, as
    ``(OutcomeChange, action_id | None)`` keys."""
    return frozenset(STATED_BY[(code, params.get("composer"))](params, attempts))
