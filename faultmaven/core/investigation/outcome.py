"""The case outcome record, computed as one fold (#2615 stage S1).

``compute_case_outcome(prior, case)`` reads the case as it stands and folds the
result over ``prior``, the record the last write left (``progress.outcome``).
Each verdict the rules call final is carried from ``prior`` and never
re-derived:

- a FAILED fix window, with its rows, its citations and the problem statement
  it was read under (#2569 / Q1 (b): it stays FAILED against that statement);
- an attempt's ``accepted_in_turn``;
- at RESOLVED, the confirmed rows, the stamp's included;
- a terminal case's closure, once a terminal write folded it.

A fresh derivation that disagrees with a carried verdict is a disagreement:
the carried verdict stands, and the record keeps the turn the disagreement was
first read (``CaseOutcome.disagreements``). The fold logs it when it first
appears and counts nothing. It is counted on
``case_outcome_conflicts_total{field}`` once, when a saved record first
carries it (#2689, ruled 2026-11-13: "stored" means the save has committed):
``report_saved_disagreements`` counts the saved record's (key, first-read
turn) pairs the record loaded before the write did not hold, after the turn's
commit (``turn_settlement.settle_turn``), the REST close's retry helper and
the backfill's save. A dry run, a failed or cancelled commit, a retried
attempt and a commit only the turn receipt vouches for count nothing. A fold
that reads it gone drops it, so its return, read at a later turn, counts
again, within one turn's folds too.

The window deciders read the carried verdicts (#3046 S3a-2): every one reads
``carried_fix_windows``, the fresh read with each final FAILED window laid
over it (``carry_verdicts``), and inside a computation the list its fold
carries, so the record's sections read the windows it holds. An
``attempts.window`` disagreement is then the fresh read the record
overrides. The other deciders still derive fresh until S3d (#3053), so
their disagreement is a place where a decider and the record disagree. A
FAILED window with no disconfirmation pair takes a later one, once, at a
minting point, counted as ``attempts.window.disconfirmation`` (#2728).

Where it is written (``record_case_outcome``): at the end of each decision
cycle (``_apply_turn_response``, after every decider, before the save), and
in each investigation apply before the chain emission (a cycle's, or a
replayed bundle's: the same-cycle mint, ``pre_chain``, #2582), after each
bundle of a
statement-revision replay, at both terminal transitions, and
at the two save helpers (#2811): a turn's commit (``commit_turn_plan``) and
the case service's write (``update_case_with_retry``), each after the save's
offer lapse (``record_before_save``), and at case creation, the out-of-band
reclassification and the metadata write, so every saved case carries a
record of the case as saved. A final verdict (``FinalAt``: a FAILED window,
an ``accepted_in_turn``, the stamp's rows) and a terminal ``folded_under``
are minted only at a cycle, a replay, the mint before the chain emission, a
resolve, a close (``MINTING_POINTS``) or a first record; any other fold refreshes the rest and carries them. The narration
step reads the cycle's record (#2636 B1b); mid-cycle narration reads one
computed in memory (``narration_outcome``). ``closure_outcome`` computes the
closure section on demand at the four closure entries, and the two that
close hand it to ``_execute_closed_transition``, whose record persists it.

``fm-backfill-case-outcome`` (``faultmaven/cli/backfill_case_outcome.py``)
gives every stored case without a record its first, recomputes a terminal
case's record that no terminal write folded (``CaseOutcome.folded_under``),
and rewrites a record of an older shape (``CURRENT_OUTCOME_SHAPE``). No reader
computes a record on ``None``: there is no legacy read path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Optional

from faultmaven.core.investigation.causal_graph.clusters import mece_contested_root_ids
from faultmaven.core.investigation.causal_graph.projection import held_open_root_ids
from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    FIX_INTERCEPTION,
    SUBJECT_PROBLEM,
    _cause_posited_turn,
    _is_remediation,
    _outcome_computation,
    _pool_legs,
    carry_verdicts,
    cause_pool_empty_reason,
    cause_recheck_status,
    confirmation_stamp_rows,
    fix_windows,
    held_check_reason,
    interception_relief,
    latest_disconfirmation_turn,
    mitigation_observation,
    mitigation_refusal_reason,
    mitigation_settled,
    off_cause_attempt,
    pending_symptom_check,
    seed_carried_windows,
    subject_index,
    symptom_elimination_rows,
    unbound_post_fix_rows,
    uncited_recheck_why,
)
from faultmaven.core.investigation.cause_identification import cause_identification_leg
from faultmaven.core.investigation.lifecycle_metrics import (
    case_outcome_conflicts_total,
    case_outcome_write_failures_total,
)
from faultmaven.core.investigation.offers import lapse_offers
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
    CURRENT_OUTCOME_SHAPE,
    AttemptOutcome,
    BindableRows,
    CaseOutcome,
    CaseState,
    CauseOutcome,
    ClosureEligibility,
    ClosureOutcome,
    ClosureReason,
    DisconfirmationPair,
    EvidenceCategory,
    FinalAt,
    FixWindowRecord,
    FixWindowState,
    InvestigationActionType,
    MitigationOutcome,
    ProblemOutcome,
    RecheckReason,
    ResolutionOutcome,
    ResolutionVerdict,
    UnboundRecheck,
    WritePoint,
)

if TYPE_CHECKING:
    from faultmaven.core.investigation.cause_assurance import FixWindow, SubjectIndex
    from faultmaven.modules.case.contracts import Case

logger = logging.getLogger(__name__)

#: The write points that may mint a final verdict (#2811, ruled 2026-11-15),
#: and the backfill's terminal repair, which stands in for a terminal write.
MINTING_POINTS = frozenset(
    {
        WritePoint.CYCLE,
        WritePoint.REPLAY,
        WritePoint.PRE_CHAIN,
        WritePoint.RESOLVED,
        WritePoint.CLOSED,
        WritePoint.REPAIR,
    }
)

#: The terminal transitions: the write points that read the closure off the
#: case when none is handed in.
_TERMINAL_POINTS = frozenset({WritePoint.RESOLVED, WritePoint.CLOSED})


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
    ``execute_user_closure`` (#2615, *Closure on demand*). Always computed
    fresh, so each entry decides on what ``derive_closure_reason`` gives. The
    two entries that close hand it to ``_execute_closed_transition``, whose
    record persists the closure its entry decided on; nothing else keeps it,
    so the cycle-end record computes the closure fresh."""
    return _closure_of(case, derive_closure_reason(case))


def record_case_outcome(
    case: "Case", *, at: WritePoint, closure: Optional[ClosureOutcome] = None
) -> bool:
    """Fold the case's outcome over ``progress.outcome`` and store it there.

    ``at`` names the write point (``WritePoint``): a cycle, a replay, the
    mint before the chain emission or a terminal transition, which mint finals (``MINTING_POINTS``), or a save's
    (``commit``, ``write``, ``metadata``, ``create``), which carry them (a
    first record mints); a
    misspelt one raises, and is counted as a failed write. ``closure``: at a
    terminal transition, the closure its entry computed
    (``closure_outcome``); with none (a resolve, or a close offer accepted on
    its stored reason), the one the case now stands on. Any other fold of a
    terminal case carries the closure a terminal write folded.
    A write that raises fails neither the turn nor the transition: it is
    logged with its traceback and counted on
    ``case_outcome_write_failures_total{at}``, and returns False, so the
    narration step renders from no record this write did not store (#2636).
    Returns whether the record was written."""
    try:
        at = WritePoint(at)
        if case.is_terminal and closure is None and at in _TERMINAL_POINTS:
            closure = _closure_of(case, case.closure_reason)
        case.progress.outcome = compute_case_outcome(
            case.progress.outcome, case, at=at, closure=closure
        )
        return True
    except Exception:  # noqa: BLE001 — the record is written in shadow (S1)
        logger.exception(
            "Case %s: the case outcome record was not written at %s (#2615)",
            getattr(case, "case_id", None),
            at,
        )
        case_outcome_write_failures_total.labels(at=at).inc()
        return False


def record_before_save(case: "Case", *, at: WritePoint) -> Callable[[], None]:
    """A save helper's steps before its save (#2811): the turn-sequence
    repair and the offer lapse the save would run (both idempotent, so the
    save's second run is a no-op), then the record, folded over the case as
    it will be stored. Returns the undo a failed save calls: it puts back the
    offer record and the record this changed (``SAVE_STAMPED_FIELDS``'
    promise), leaving the turn-sequence repair, which is idempotent."""
    offers, outcome = case.offer_record, case.progress.outcome
    case.reconcile_turn_sequence()
    case.offer_record = lapse_offers(case)
    record_case_outcome(case, at=at)

    def undo() -> None:
        object.__setattr__(case, "offer_record", offers)
        object.__setattr__(case.progress, "outcome", outcome)

    return undo


def narration_outcome(case: "Case") -> CaseOutcome:
    """The record for the moment narrated, folded in memory over the stored
    one and stored nowhere; it mints no final verdict (#2811). The turn's
    narration computes it once, at its entry, only when the cycle's write
    failed and a logged code reads the record; the prompt folds it lazily,
    once per prompt assembly (``NarrationFold``), for every block that prints
    an outcome line from it: the fix-verification and evidence-needs blocks,
    the milestone status, the exclusion rule, the focus emphasis, the state
    summary, the causal graph block's recovery notes and the working
    conclusion's contest NOTE (S2b-2a: the block is built on every assembly
    whose case has a non-retired hypothesis or a causal node, the NOTE on
    every one with a working conclusion), and the fallback prompt the
    assembly may return instead (which, called alone, folds once per
    call). The resolve confirmation folds it once per call, and the closed
    reply when the terminal write failed (S2b-2b)."""
    return compute_case_outcome(case.progress.outcome, case, at=WritePoint.NARRATION)


#: The record field a later disconfirmation pair of a FAILED window is
#: counted under (#2728): an addition, never a disagreement.
_LATER_PAIR = "attempts.window.disconfirmation"


class _Disagreements:
    """The disagreements one computation reads, keyed by field and entry: the
    record keeps the turn each was first read, and each is logged when it
    first appears. Nothing is counted here (#2689): a saved record's are
    counted after its save (``report_saved_disagreements``)."""

    def __init__(self, case: "Case", prior: Optional[CaseOutcome]) -> None:
        self.case = case
        self.known = dict(prior.disagreements) if prior is not None else {}
        self.read: dict[str, int] = {}

    def add(self, field: str, entry: Optional[str], carried, fresh) -> None:
        key = f"{field}:{entry}" if entry else field
        if key in self.known:
            self.read[key] = self.known[key]
            return
        self.read[key] = self.case.current_turn
        if field == _LATER_PAIR:
            logger.info(
                "Case %s: the outcome record's FAILED window %s takes the later "
                "disconfirmation pair %s (#2728)",
                getattr(self.case, "case_id", None),
                key,
                fresh,
            )
            return
        logger.warning(
            "Case %s: the outcome record's carried %s (%s) disagrees with a "
            "fresh derivation (%s); the carried verdict stands (#2615)",
            getattr(self.case, "case_id", None),
            key,
            carried,
            fresh,
        )


def disagreement_keys(outcome: Optional[CaseOutcome]) -> frozenset[tuple[str, int]]:
    """The disagreements ``outcome`` carries, as (key, first-read turn) pairs:
    what a write site captures from the record it loaded, before its write. A
    key dropped and read again at another turn is a new pair."""
    if outcome is None:
        return frozenset()
    return frozenset(outcome.disagreements.items())


def report_saved_disagreements(
    case_id: Optional[str],
    loaded: frozenset[tuple[str, int]],
    saved: Optional[CaseOutcome],
    narration: tuple[str, ...] = (),
) -> list[str]:
    """Count, once, each disagreement the saved record carries that the record
    loaded before the write (``loaded``, ``disagreement_keys``) did not, and
    each of the narration's conflicts (``narration.<code>``,
    ``turn_narration``). Called only after the save has committed (#2689).
    Each increment is guarded on its own and logged when it fails, so a
    committed save is never failed by it. Returns the keys counted."""
    found = sorted(key for key, _turn in disagreement_keys(saved) - loaded)
    for field in [key.split(":", 1)[0] for key in found] + list(narration):
        try:
            case_outcome_conflicts_total.labels(field=field).inc()
        except Exception:  # noqa: BLE001 — the rest are still counted
            logger.exception(
                "Case %s: the disagreement %s was not counted (#2689)",
                case_id,
                field,
            )
    if found or narration:
        logger.info(
            "Case %s: counted the saved record's disagreements %s and the "
            "narration's conflicts %s (#2689)",
            case_id,
            found,
            list(narration),
        )
    return found


def compute_case_outcome(
    prior: Optional[CaseOutcome],
    case: "Case",
    *,
    at: WritePoint,
    closure: Optional[ClosureOutcome] = None,
) -> CaseOutcome:
    """The case's outcome, folded over ``prior`` (None: computed once from
    the case as it stands). ``at``: the write point; a final verdict, and a
    terminal ``folded_under``, are minted only at one of ``MINTING_POINTS``
    or on a first record (no ``prior``). ``closure``: the closure a terminal
    transition persists (``record_case_outcome``). Counts nothing: its
    disagreements are counted once a saved record carries them (#2689).
    Computed in one scope (``_outcome_computation``), which builds the
    subject index, the fix windows and the cause pool once for the case
    (#2657); so nothing it calls may mutate the case."""
    with _outcome_computation():
        at = WritePoint(at)
        mint = None
        if prior is None or at in MINTING_POINTS:
            mint = FinalAt(at=at, turn=case.current_turn)
        subjects = subject_index(case)
        windows = fix_windows(case, subjects)
        if prior is not None:
            prior = _upgraded(prior, case, subjects, windows)
        statement = current_statement(case)
        seen = _Disagreements(case, prior)
        fold = _Fold(prior, case, subjects, windows, statement, seen, mint)
        # The attempts fold the fresh read; every other section, and every
        # decider it calls, reads the windows they carry (#3046 S3a-2).
        attempts = _attempts(fold)
        fold.windows = windows = carry_verdicts(case, windows, attempts, subjects)
        seed_carried_windows(case, windows)
        status = cause_recheck_status(case, subjects=subjects, windows=windows)
        unbound = _unbound_rechecks(prior, case, subjects, windows)
        return CaseOutcome(
            shape=CURRENT_OUTCOME_SHAPE,
            # An off-cycle fold carries it: only a minting one moves a record to
            # terminal, so a terminal write that raised is still repaired.
            folded_under=case.state if mint is not None else prior.folded_under,
            problem=ProblemOutcome(
                status=case.progress.problem_status,
                verifying_row_ids=list(subjects.verifying),
                statement=statement,
            ),
            cause=_cause(case, subjects, status),
            attempts=attempts,
            demotions=list(prior.demotions) if prior is not None else [],
            resolution=_resolution(fold, status),
            unbound_rechecks=unbound,
            bindable=_bindable(subjects),
            mitigation=_mitigation(case, subjects),
            milestones_shown=shown_milestones(case),
            closure=_closure(prior, case, closure, seen),
            disagreements=seen.read,
        )


def _shape_2(prior: CaseOutcome, case: "Case", subjects, windows) -> CaseOutcome:
    """Shape 1 -> 2 (#2811): what shape 1 held final, marked so (``FinalAt``
    ``shape``): every ``accepted_in_turn``, and every FAILED window, which
    was observed to fail and takes the problem rows its fresh read holds up
    to its failure, unless it holds rows of its own."""
    mark = FinalAt(at=WritePoint.SHAPE, turn=case.current_turn)
    fresh = {w.fix_turn: w for w in windows}
    attempts = []
    for attempt in prior.attempts:
        update: dict = {}
        if attempt.accepted_in_turn is not None and attempt.final_at is None:
            update["final_at"] = mark
        window = attempt.window
        if window is not None and window.state == FixWindowState.FAILED:
            same = fresh.get(window.fix_turn)
            rows = window.problem_row_ids
            if not rows and same is not None and window.failure_turn is not None:
                rows = [
                    e.evidence_id
                    for e in same.problem_rows
                    if subjects.observed(e) <= window.failure_turn
                ]
            update["window"] = window.model_copy(
                update={
                    "final_at": window.final_at or mark,
                    "observed": True,
                    "problem_row_ids": rows,
                }
            )
        attempts.append(attempt.model_copy(update=update) if update else attempt)
    return prior.model_copy(update={"attempts": attempts})


def _shape_3(prior: CaseOutcome, case: "Case", subjects, windows) -> CaseOutcome:
    """Shape 2 -> 3 (#2811 S2b-1a): the identity. Shape 3 adds
    ``resolution.off_cause_turn`` and ``resolution.unrechecked_row_ids``,
    neither a carried verdict, so every fold derives them fresh and nothing
    stored is rewritten."""
    return prior


def _shape_4(prior: CaseOutcome, case: "Case", subjects, windows) -> CaseOutcome:
    """Shape 3 -> 4 (#2811 S2b-1b-i): the identity. Shape 4 adds
    ``resolution.pending_check_reason``, not a carried verdict, so every fold
    derives it fresh and nothing stored is rewritten."""
    return prior


def _shape_5(prior: CaseOutcome, case: "Case", subjects, windows) -> CaseOutcome:
    """Shape 4 -> 5 (#2811 S2b-2a): the identity. Shape 5 adds
    ``cause.contested_root_ids``, not a carried verdict, so every fold
    derives it fresh and nothing stored is rewritten."""
    return prior


#: Each shape's step from the shape below it, in order; a record is moved one
#: step at a time to ``CURRENT_OUTCOME_SHAPE``, each step run only on a record
#: of exactly the shape below its own. A shape change adds its step here.
SHAPE_STEPS = {2: _shape_2, 3: _shape_3, 4: _shape_4, 5: _shape_5}


def _upgraded(prior: CaseOutcome, case: "Case", subjects, windows) -> CaseOutcome:
    while prior.shape < CURRENT_OUTCOME_SHAPE:
        shape = prior.shape + 1
        prior = SHAPE_STEPS[shape](prior, case, subjects, windows).model_copy(
            update={"shape": shape}
        )
    return prior


class _Fold:
    """What one computation folds the attempts and the resolution with."""

    def __init__(self, prior, case, subjects, windows, statement, seen, mint):
        self.prior, self.case, self.subjects = prior, case, subjects
        self.windows, self.statement, self.seen = windows, statement, seen
        self.mint: Optional[FinalAt] = mint


def _cause(case: "Case", subjects: "SubjectIndex", status: list) -> CauseOutcome:
    whys = [why for _row, why in status if why is not None]
    identified, held, fallen = _pool_legs(case)
    return CauseOutcome(
        identified_root_ids=sorted(identified),
        held_root_ids=sorted(held),
        fallen_root_ids=sorted(fallen),
        # Both derived fresh at the write, unlike ``pool_empty_reason``'s
        # HELD_OPEN and IDENTIFICATION_CONTESTED verdicts and
        # ``identification_held_open`` below. The prompt's recovery notes
        # and working-conclusion NOTE read them (#2811 S2b-2a).
        held_open_root_ids=sorted(held_open_root_ids(case)),
        contested_root_ids=sorted(mece_contested_root_ids(case)),
        confirmation_root_ids=sorted(subjects.pool_roots),
        cited_basis={
            node_id: sorted(rows)
            for node_id, rows in subjects.citations.items()
            if rows
        },
        # ``cause_pool_empty_reason`` is computed at the write; only its
        # HELD_OPEN and IDENTIFICATION_CONTESTED verdicts read flags the cause
        # recompute wrote (``cause_identification_held_open``,
        # ``cause_identification_contested``). ``identification_held_open``
        # carries ``progress.cause_identification_held_open``, the withdrawal
        # notice's variant (#2636), which the next prompt's readers also read.
        # The turn's final recompute (#2636) re-derives these flags after
        # every decider, so the cycle's record reads them agreeing with
        # ``held_open_root_ids`` and ``contested_root_ids``; a replayed
        # bundle's record reads them as that bundle left them, and a prompt
        # folded after a pre-prompt hypothesis intent (retire or refute),
        # which runs no recompute, can read them disagreeing.
        pool_empty_reason=cause_pool_empty_reason(case),
        identification_leg=cause_identification_leg(case),
        identification_held_open=bool(case.progress.cause_identification_held_open),
        recheck={
            e.evidence_id: why.reason if why is not None else RecheckReason.COUNTS
            for e, why in status
        },
        posited_turn=_cause_posited_turn(case),
        latest_disconfirmation_turn=latest_disconfirmation_turn(case),
        # The parameters a row's reason states, the same for every row.
        no_confirming_window=next((w.window for w in whys if w.window), None),
        uncited=next((w.uncited for w in whys if w.uncited), None),
        other_cause_fix_turn=next(
            (w.turn for w in whys if w.reason == RecheckReason.OTHER_CAUSE), None
        ),
    )


def _window_record(
    window: "FixWindow", fold: _Fold, action_id: Optional[str] = None
) -> FixWindowRecord:
    subjects = fold.subjects
    named = {getattr(e, "rechecks_evidence_id", None) for e in window.cause_rows}
    pair = None
    if window.disconfirmation is not None:
        removed, present = window.disconfirmation
        # The row a demotion on this attempt named at the pair's cause turn
        # (#2586): the pool may have lost its root since.
        at = {
            e.evidence_id for e in window.cause_rows if subjects.observed(e) == removed
        }
        demoted = [
            d.cause_row_id
            for d in getattr(fold.prior, "demotions", ())
            if (d.action_id, d.fix_turn) == (action_id, window.fix_turn)
        ]
        pair = DisconfirmationPair(
            cause_row_id=next(
                (i for i in demoted if i in at), window.pair_cause_row_id
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
        observed=window.observed,
        cause_row_ids=[e.evidence_id for e in window.cause_rows],
        confirmed_cause_row_ids=[e.evidence_id for e in window.confirmed_cause_rows],
        problem_row_ids=[e.evidence_id for e in window.problem_rows],
        cited_basis={
            node_id: sorted(rows & named)
            for node_id, rows in subjects.citations.items()
            if rows & named
        },
        disconfirmation=pair,
        read_under=fold.statement,
        final_at=fold.mint if window.state == FIX_FAILED else None,
    )


def _attempts(fold: _Fold) -> list[AttemptOutcome]:
    """Every accepted SOLUTION, keyed by its ``action_id``, with the window
    it opened, or, for an interception, what the problem did after it;
    window 0, the fix done outside the case, as ``action_id`` None once
    observed. Folded over the prior: a FAILED window and an
    ``accepted_in_turn`` are carried once final (``final_at``), and a final
    FAILED window whose attempt no longer reads at all is kept."""
    case, windows, mint, seen = fold.case, fold.windows, fold.mint, fold.seen
    by_turn = {w.fix_turn: w for w in windows[1:]}
    fresh = []
    if windows[0].observed:
        fresh.append(AttemptOutcome(window=_window_record(windows[0], fold)))
    for action in getattr(case, "proposed_actions", None) or []:
        kind = getattr(action.action_type, "value", action.action_type)
        if action.state != "accepted" or kind != InvestigationActionType.SOLUTION:
            continue
        turn = action.accepted_in_turn
        window = by_turn.get(turn) if turn is not None else None
        relief = present = None
        if turn is not None and action.fix_kind == FIX_INTERCEPTION:
            relief, present = interception_relief(case, turn, fold.subjects)
        fresh.append(
            AttemptOutcome(
                action_id=action.action_id,
                kind=action.fix_kind,
                target_ids=list(action.target_ids),
                accepted_in_turn=turn,
                registered_in_turn=action.registered_in_turn,
                window=(
                    _window_record(window, fold, action.action_id)
                    if window is not None and _is_remediation(action)
                    else None
                ),
                relief_turn=relief,
                present_turn=present,
                final_at=mint if turn is not None else None,
            )
        )
    prior = fold.prior
    carried = {a.action_id: a for a in (prior.attempts if prior is not None else [])}
    out = []
    for attempt in fresh:
        old = carried.pop(attempt.action_id, None)
        if old is None:
            out.append(attempt)
            continue
        update: dict = {}
        if old.final_at is not None:
            update["final_at"] = old.final_at
            if attempt.accepted_in_turn != old.accepted_in_turn:
                fold.seen.add(
                    "attempts.accepted_in_turn",
                    attempt.action_id,
                    old.accepted_in_turn,
                    attempt.accepted_in_turn,
                )
                update["accepted_in_turn"] = old.accepted_in_turn
        if old.window is not None and old.window.final_at is not None:
            new = attempt.window
            update["window"] = old.window
            if new is None or new.state != FIX_FAILED:
                fresh_state = new.state if new else None
                seen.add("attempts.window", attempt.action_id, FIX_FAILED, fresh_state)
            elif old.window.disconfirmation is None and new.disconfirmation:
                # A later disconfirmation of the FAILED window (#2728): taken
                # once, at a minting point, and counted as an addition.
                if mint is not None:
                    seen.add(_LATER_PAIR, attempt.action_id, None, new.disconfirmation)
                    update["window"] = _with_later_pair(old.window, new, fold)
            elif f"{_LATER_PAIR}:{attempt.action_id}" in seen.known:
                seen.add(_LATER_PAIR, attempt.action_id, None, None)
        out.append(attempt.model_copy(update=update) if update else attempt)
    for old in carried.values():
        if old.window is not None and old.window.final_at is not None:
            seen.add("attempts.window", old.action_id, FIX_FAILED, None)
            out.append(old)
    return out


def _with_later_pair(
    old: FixWindowRecord, new: FixWindowRecord, fold: _Fold
) -> FixWindowRecord:
    """``old`` holding ``new``'s pair, with the pair's cause row and its
    citation added to its rows, and nothing else (#2586): the record's
    window holds the row its pair names."""
    pair = new.disconfirmation
    row = fold.subjects.rows.get(pair.cause_row_id)
    if row is None:
        return old.model_copy(update={"disconfirmation": pair})
    named = getattr(row, "rechecks_evidence_id", None)
    cited = {node: list(rows) for node, rows in old.cited_basis.items()}
    for node, rows in new.cited_basis.items():
        if named in rows and named not in cited.setdefault(node, []):
            cited[node] = sorted([*cited[node], named])
    return old.model_copy(
        update={
            "disconfirmation": pair,
            "cause_row_ids": list(dict.fromkeys([*old.cause_row_ids, row.evidence_id])),
            "cited_basis": cited,
        }
    )


def _resolution(fold: _Fold, status: list) -> ResolutionOutcome:
    case, subjects, windows, prior = fold.case, fold.subjects, fold.windows, fold.prior
    readiness = assess_resolution_readiness(case)
    cause_rows = [e for e, why in status if why is None]
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
                fold.seen.add("resolution.confirmed_row_ids", None, carried, confirmed)
            confirmed = list(carried)
        elif fold.mint is None:
            confirmed = []  # minted only where finals are (#2811)
    # The held need, and why it holds (ruling 3 of 2026-11-17): FULFILLED
    # holds only on rows from before its ask. Read whatever readiness
    # answered, which returns before it on a false alarm.
    check = pending_symptom_check(case, subjects=subjects, windows=windows)
    reason = held_check_reason(check) if check is not None else None
    # The verifying rows no problem-leg row re-checks: read only once the
    # cause leg stands, as the fix-verification block prints them.
    named = {getattr(e, "rechecks_evidence_id", None) for e in problem_rows}
    unrechecked = [
        i for i in subjects.verifying if i in subjects.rows and i not in named
    ]
    return ResolutionOutcome(
        verdict=ResolutionVerdict(readiness.verdict),
        reason=readiness.reason,
        missing=list(readiness.missing),
        confirmation_gaps=list(readiness.confirmation_gaps),
        cause_row_ids=[e.evidence_id for e in cause_rows],
        problem_row_ids=[e.evidence_id for e in problem_rows],
        pending_check_need_id=check.need_id if check is not None else None,
        pending_check_reason=reason,
        declined_stands=declined_resolve_entry(case) is not None,
        off_cause_turn=off_cause_attempt(case, windows),
        unrechecked_row_ids=unrechecked if cause_rows else [],
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
    whatever the problem's status. ``narrated_turn`` is carried from the
    prior's list while the row stays on it (one continuous unbound stretch):
    the narration step writes it (#2555). Nothing is marked here (ruled
    2026-11-13: mark nothing, tell once): a row no narration has told, in a
    backfilled record or one stored before B2, is told once by the next
    verified narrated turn. A row that leaves the list and returns is told
    again."""
    found = unbound_post_fix_rows(case, subjects, windows)
    if not found:
        return []
    narrated = {
        u.evidence_id: u.narrated_turn
        for u in (prior.unbound_rechecks if prior is not None else [])
    }
    return [
        UnboundRecheck(
            evidence_id=u.row.evidence_id,
            observed_turn=subjects.observed(u.row),
            uncited=u.uncited,
            after_mitigation=u.after_mitigation,
            narrated_turn=narrated.get(u.row.evidence_id),
        )
        for u in found
    ]


def _bindable(subjects: "SubjectIndex") -> BindableRows:
    """What an unbound re-check could name instead, and why an uncited one
    confirms nothing: the same for every unbound row, so held once."""
    problem, cause = subjects.bindable_rows()
    return BindableRows(
        problem_row_ids=[e.evidence_id for e in problem],
        cause_row_ids=[e.evidence_id for e in cause],
        why=uncited_recheck_why(subjects),
    )


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
            else mitigation_refusal_reason(
                case, lambda: (seen.relief_turn, seen.present_turn)
            )
        ),
    )


def _closure(
    prior: Optional[CaseOutcome],
    case: "Case",
    closure: Optional[ClosureOutcome],
    seen: _Disagreements,
) -> ClosureOutcome:
    """The closure a terminal transition hands in; a terminal case's closure
    a terminal write folded (final, carried); a terminal case's own, read off
    the case (the backfill, or a record no terminal write folded); else what
    a close of the case would be now."""
    if closure is not None:
        return closure
    if not case.is_terminal:
        return _closure_of(case, derive_closure_reason(case))
    stored = case.closure_reason
    if prior is None or not prior.folded_under.is_terminal:
        return _closure_of(case, stored)
    if prior.closure.reason != stored:
        seen.add("closure", None, prior.closure.reason, stored)
    return prior.closure
