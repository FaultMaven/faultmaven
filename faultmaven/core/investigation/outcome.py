"""The case outcome record, computed as one fold (#2615 stage S1).

``compute_case_outcome(prior, case)`` reads the case as it stands and folds the
result over ``prior``, the record the last write left (``progress.outcome``).
Each verdict the rules call final is carried from ``prior`` and never
re-derived:

- a FAILED fix window, with its rows, its citations and the problem statement
  it was read under (#2569 / Q1 (b): it stays FAILED against that statement);
- an attempt's ``accepted_in_turn``;
- at RESOLVED, the confirmed rows, the stamp's included;
- a terminal case's closure.

A fresh derivation that disagrees with a carried verdict is a conflict: it is
logged and counted on ``case_outcome_conflicts_total{field}``, and the carried
verdict stands. In this stage the deciders still read fresh derivations, so a
conflict is a place where a decider and the record disagree; stage S3 switches
them.

Where it is written (``record_case_outcome``): at the end of each decision
cycle (``_apply_turn_response``, after every decider, before the save), after
each bundle of a statement-revision replay, and at both terminal transitions.
Nothing reads it in this stage. ``closure_outcome`` computes the closure
section on demand at the four closure entries.

Every stored case gets its first record from ``fm-backfill-case-outcome``
(``faultmaven/cli/backfill_case_outcome.py``), computed once from the case as
stored. No reader computes a record on ``None``: there is no legacy read path.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Iterator, Optional

from faultmaven.core.investigation.causal_graph.projection import held_open_root_ids
from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    SUBJECT_PROBLEM,
    _is_remediation,
    _pool_legs,
    cause_elimination_rows,
    cause_pool_empty_reason,
    confirmation_stamp_rows,
    fix_windows,
    mitigation_observation,
    mitigation_refusal_reason,
    mitigation_settled,
    subject_index,
    symptom_elimination_rows,
    unbound_post_fix_rows,
    uncited_recheck_why,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    case_outcome_conflicts_total,
    case_outcome_write_failures_total,
)
from faultmaven.core.investigation.problem_status import current_statement
from faultmaven.core.investigation.terminal_transitions import (
    _mitigation_declared_stable,
    _mitigation_verified,
    assess_resolution_readiness,
    closure_verdict,
    declined_resolve_entry,
    derive_closure_reason,
    mitigation_closure_line,
    resolution_record_lines,
    shown_milestones,
)
from faultmaven.modules.case.contracts import (
    AttemptOutcome,
    CaseOutcome,
    CaseState,
    CauseOutcome,
    ClosureEligibility,
    ClosureOutcome,
    ClosureReason,
    DisconfirmationPair,
    EvidenceCategory,
    FixWindowRecord,
    FixWindowState,
    InvestigationActionType,
    MitigationOutcome,
    ProblemOutcome,
    ResolutionOutcome,
    ResolutionVerdict,
    UnboundRecheck,
)

if TYPE_CHECKING:
    from faultmaven.core.investigation.cause_assurance import FixWindow, SubjectIndex
    from faultmaven.modules.case.contracts import Case

logger = logging.getLogger(__name__)

_TERMINAL = (CaseState.RESOLVED, CaseState.CLOSED)

#: Whether the running code is inside a decision cycle: one application of
#: step 1 plus steps 2-4 (``_apply_turn_response``), or one bundle of a
#: statement-revision replay.
_IN_DECISION_CYCLE: ContextVar[bool] = ContextVar(
    "case_outcome_in_decision_cycle", default=False
)
#: The closure a closure entry computed outside a decision cycle, with the
#: case it was computed on: the one memo, read by the terminal transition's
#: record write.
_ENTRY_CLOSURE: ContextVar[Optional[tuple]] = ContextVar(
    "case_outcome_entry_closure", default=None
)


@contextmanager
def decision_cycle() -> Iterator[None]:
    """Mark the code run inside as one decision cycle: ``closure_outcome``
    memoizes nothing there."""
    token = _IN_DECISION_CYCLE.set(True)
    try:
        yield
    finally:
        _IN_DECISION_CYCLE.reset(token)


def _closure_of(case: "Case", reason: Optional[ClosureReason]) -> ClosureOutcome:
    return ClosureOutcome(
        eligibility=ClosureEligibility(closure_verdict(case)),
        reason=reason,
        statement=mitigation_closure_line(case),
    )


def closure_outcome(case: "Case") -> ClosureOutcome:
    """The closure section of ``compute_case_outcome``, computed on demand at
    the four closure entries: ``open_disposition_offer``,
    ``disposition_refusal``, ``confirm_open_offer`` and
    ``execute_user_closure`` (#2615, *Closure on demand*).

    Always computed fresh, so each entry decides on what
    ``derive_closure_reason`` gives. Outside a decision cycle (step 0's
    decline-then-close, the REST close) the result is also memoized in one
    request-scoped slot, which the terminal transition's record write reads:
    the record persists the closure its entry decided on, not one re-derived
    after the state moved. Inside a cycle nothing is memoized, since an offer
    opener's closure would be stale by the cycle's end; the record computes
    the closure fresh there."""
    closure = _closure_of(case, derive_closure_reason(case))
    if not _IN_DECISION_CYCLE.get():
        _ENTRY_CLOSURE.set((case, closure))
    return closure


def _take_entry_closure(case: "Case") -> Optional[ClosureOutcome]:
    """The memoized entry closure for ``case`` when it names the reason the
    case closed with, emptying the slot; else None."""
    held = _ENTRY_CLOSURE.get()
    _ENTRY_CLOSURE.set(None)
    if held is None or held[0] is not case:
        return None
    closure = held[1]
    return closure if closure.reason == getattr(case, "closure_reason", None) else None


def record_case_outcome(case: "Case", *, at: str) -> None:
    """Fold the case's outcome over ``progress.outcome`` and store it there.

    ``at`` names the write point: ``cycle``, ``replay``, ``resolved`` or
    ``closed``. At a terminal transition the closure is the one its entry
    memoized (``closure_outcome``), else the one the case now stands on.
    Nothing reads the record in this stage, so a write that raises fails
    neither the turn nor the transition: it is logged with its traceback and
    counted on ``case_outcome_write_failures_total{at}``."""
    closure = _take_entry_closure(case) if at in ("resolved", "closed") else None
    try:
        if at in ("resolved", "closed") and closure is None:
            closure = _closure_of(case, getattr(case, "closure_reason", None))
        case.progress.outcome = compute_case_outcome(
            case.progress.outcome, case, closure=closure
        )
    except Exception:  # noqa: BLE001 — the record is written in shadow (S1)
        logger.exception(
            "Case %s: the case outcome record was not written at %s (#2615)",
            getattr(case, "case_id", None),
            at,
        )
        case_outcome_write_failures_total.labels(at=at).inc()


def _conflict(case: "Case", field: str, carried, fresh) -> None:
    logger.warning(
        "Case %s: the outcome record's carried %s (%s) disagrees with a fresh "
        "derivation (%s); the carried verdict stands (#2615)",
        getattr(case, "case_id", None),
        field,
        carried,
        fresh,
    )
    case_outcome_conflicts_total.labels(field=field).inc()


def compute_case_outcome(
    prior: Optional[CaseOutcome],
    case: "Case",
    *,
    closure: Optional[ClosureOutcome] = None,
) -> CaseOutcome:
    """The case's outcome, folded over ``prior`` (None: computed once from
    the case as it stands). ``closure``: the closure a terminal transition
    persists (``record_case_outcome``)."""
    subjects = subject_index(case)
    windows = fix_windows(case, subjects)
    statement = current_statement(case)
    return CaseOutcome(
        problem=ProblemOutcome(
            status=case.progress.problem_status,
            verifying_row_ids=list(subjects.verifying),
            statement=statement,
        ),
        cause=_cause(case, subjects),
        attempts=_attempts(prior, case, subjects, windows, statement),
        demotions=list(prior.demotions) if prior is not None else [],
        resolution=_resolution(prior, case, subjects, windows),
        unbound_rechecks=_unbound_rechecks(prior, case, subjects, windows),
        mitigation=_mitigation(case, subjects),
        milestones_shown=shown_milestones(case),
        closure=_closure(prior, case, closure),
    )


def _cause(case: "Case", subjects: "SubjectIndex") -> CauseOutcome:
    identified, held, fallen = _pool_legs(case)
    return CauseOutcome(
        identified_root_ids=sorted(identified),
        held_root_ids=sorted(held),
        fallen_root_ids=sorted(fallen),
        held_open_root_ids=sorted(held_open_root_ids(case)),
        confirmation_root_ids=sorted(subjects.pool_roots),
        cited_basis={
            node_id: sorted(rows)
            for node_id, rows in subjects.citations.items()
            if rows
        },
        pool_empty_reason=cause_pool_empty_reason(case),
    )


def _window_record(
    window: "FixWindow", subjects: "SubjectIndex", statement: str
) -> FixWindowRecord:
    named = {getattr(e, "rechecks_evidence_id", None) for e in window.cause_rows}
    pair = None
    if window.disconfirmation is not None:
        removed, present = window.disconfirmation
        pair = DisconfirmationPair(
            cause_row_id=next(
                (
                    e.evidence_id
                    for e in window.cause_rows
                    if subjects.observed(e) == removed
                ),
                None,
            ),
            problem_row_id=next(
                (
                    e.evidence_id
                    for e in subjects.rows.values()
                    if e.category == EvidenceCategory.SYMPTOM_EVIDENCE
                    and subjects.observed(e) == present
                    and subjects.subject(e, windows=True) == SUBJECT_PROBLEM
                ),
                None,
            ),
            cause_turn=removed,
            problem_turn=present,
        )
    return FixWindowRecord(
        fix_turn=window.fix_turn,
        end_turn=window.end_turn,
        state=FixWindowState(window.state),
        relief_turn=window.relief_turn,
        failure_turn=window.failure_turn,
        off_cause=window.off_cause,
        cause_row_ids=[e.evidence_id for e in window.cause_rows],
        confirmed_cause_row_ids=[e.evidence_id for e in window.confirmed_cause_rows],
        cited_basis={
            node_id: sorted(rows & named)
            for node_id, rows in subjects.citations.items()
            if rows & named
        },
        disconfirmation=pair,
        read_under=statement,
    )


def _attempts(
    prior: Optional[CaseOutcome],
    case: "Case",
    subjects: "SubjectIndex",
    windows: list,
    statement: str,
) -> list[AttemptOutcome]:
    """Every accepted SOLUTION, keyed by its ``action_id``, with the window
    it opened; window 0, the fix done outside the case, as ``action_id``
    None once observed. Folded over ``prior``: a FAILED window and an
    ``accepted_in_turn`` are carried, and a carried FAILED window whose
    attempt no longer reads at all is kept."""
    by_turn = {w.fix_turn: w for w in windows[1:]}
    fresh = []
    if windows[0].observed:
        fresh.append(
            AttemptOutcome(window=_window_record(windows[0], subjects, statement))
        )
    for action in getattr(case, "proposed_actions", None) or []:
        kind = getattr(action.action_type, "value", action.action_type)
        if action.state != "accepted" or kind != InvestigationActionType.SOLUTION:
            continue
        turn = action.accepted_in_turn
        window = by_turn.get(turn) if turn is not None else None
        fresh.append(
            AttemptOutcome(
                action_id=action.action_id,
                kind=action.fix_kind,
                target_ids=list(action.target_ids),
                accepted_in_turn=turn,
                registered_in_turn=action.registered_in_turn,
                window=(
                    _window_record(window, subjects, statement)
                    if window is not None and _is_remediation(action)
                    else None
                ),
            )
        )
    carried = {a.action_id: a for a in (prior.attempts if prior is not None else [])}
    out = []
    for attempt in fresh:
        old = carried.pop(attempt.action_id, None)
        if old is None:
            out.append(attempt)
            continue
        update = {}
        if (
            old.accepted_in_turn is not None
            and attempt.accepted_in_turn != old.accepted_in_turn
        ):
            _conflict(
                case,
                "attempts.accepted_in_turn",
                old.accepted_in_turn,
                attempt.accepted_in_turn,
            )
            update["accepted_in_turn"] = old.accepted_in_turn
        if old.window is not None and old.window.state == FixWindowState.FAILED:
            if attempt.window is None or attempt.window.state != FIX_FAILED:
                fresh_state = attempt.window.state if attempt.window else None
                _conflict(case, "attempts.window", FIX_FAILED, fresh_state)
            update["window"] = old.window
        out.append(attempt.model_copy(update=update) if update else attempt)
    for old in carried.values():
        if old.window is not None and old.window.state == FixWindowState.FAILED:
            _conflict(case, "attempts.window", FIX_FAILED, None)
            out.append(old)
    return out


def _resolution(
    prior: Optional[CaseOutcome],
    case: "Case",
    subjects: "SubjectIndex",
    windows: list,
) -> ResolutionOutcome:
    readiness = assess_resolution_readiness(case)
    cause_rows = cause_elimination_rows(case, subjects=subjects, windows=windows)
    problem_rows = symptom_elimination_rows(
        case, cause_rows, subjects=subjects, windows=windows
    )
    confirmed: list[str] = []
    if case.state == CaseState.RESOLVED:
        legs = cause_rows + problem_rows if problem_rows else []
        confirmed = list(
            dict.fromkeys(
                e.evidence_id for e in (*legs, *confirmation_stamp_rows(case))
            )
        )
        carried = prior.resolution.confirmed_row_ids if prior is not None else []
        if carried:
            if carried != confirmed:
                _conflict(case, "resolution.confirmed_row_ids", carried, confirmed)
            confirmed = list(carried)
    check = readiness.pending_check
    return ResolutionOutcome(
        verdict=ResolutionVerdict(readiness.verdict),
        reason=readiness.reason,
        missing=list(readiness.missing),
        confirmation_gaps=list(readiness.confirmation_gaps),
        cause_row_ids=[e.evidence_id for e in cause_rows],
        problem_row_ids=[e.evidence_id for e in problem_rows],
        pending_check_need_id=check.need_id if check is not None else None,
        declined_stands=declined_resolve_entry(case) is not None,
        confirmed_row_ids=confirmed,
        statement=resolution_record_lines(case),
    )


def _unbound_rechecks(
    prior: Optional[CaseOutcome],
    case: "Case",
    subjects: "SubjectIndex",
    windows: list,
) -> list[UnboundRecheck]:
    """Every post-fix row bound to nothing, whatever turn recorded it and
    whatever the problem's status. ``narrated_turn`` is carried: stage R's
    renderer writes it."""
    found = unbound_post_fix_rows(case, subjects, windows)
    if not found:
        return []
    narrated = {
        u.evidence_id: u.narrated_turn
        for u in (prior.unbound_rechecks if prior is not None else [])
    }
    problem, cause = subjects.bindable_rows()
    why = uncited_recheck_why(subjects)
    return [
        UnboundRecheck(
            evidence_id=u.row.evidence_id,
            observed_turn=subjects.observed(u.row),
            uncited=u.uncited,
            after_mitigation=u.after_mitigation,
            problem_row_ids=[e.evidence_id for e in problem],
            cause_row_ids=[e.evidence_id for e in cause],
            why=why,
            narrated_turn=narrated.get(u.row.evidence_id),
        )
        for u in found
    ]


def _mitigation(case: "Case", subjects: "SubjectIndex") -> Optional[MitigationOutcome]:
    seen = mitigation_observation(case, subjects)
    if seen is None:
        return None
    return MitigationOutcome(
        attempt_turn=seen.attempt_turn,
        relief_turn=seen.relief_turn,
        present_turn=seen.present_turn,
        holding=seen.holding,
        relief_row_id=seen.relief_row_id,
        span_seconds=seen.span.total_seconds() if seen.span is not None else None,
        fix_before_relief=seen.fix_before_relief,
        disposition=seen.disposition,
        disposition_turn=seen.disposition_turn,
        verified=_mitigation_verified(case),
        declared_stable_effective=_mitigation_declared_stable(case),
        settled=mitigation_settled(case),
        refusal_reason=(
            None
            if case.progress.mitigation.verified
            else mitigation_refusal_reason(case)
        ),
    )


def _closure(
    prior: Optional[CaseOutcome], case: "Case", closure: Optional[ClosureOutcome]
) -> ClosureOutcome:
    """The closure a terminal transition hands in; a terminal case's carried
    closure (final); a terminal case's own (the backfill); else what a close
    of the case would be now."""
    if closure is not None:
        return closure
    if case.state not in _TERMINAL:
        return _closure_of(case, derive_closure_reason(case))
    stored = case.closure_reason if case.state == CaseState.CLOSED else None
    if prior is None:
        return _closure_of(case, stored)
    if prior.closure.reason != stored:
        _conflict(case, "closure", prior.closure.reason, stored)
    return prior.closure
