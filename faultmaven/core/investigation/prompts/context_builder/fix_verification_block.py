"""The ``<fix_verification>`` block: what the engine holds about the fix, shown
to the model on TREATMENT turns, and on MITIGATION turns once a fix or the
mitigation's attempt is dated (#1906 D7, #2100).

Without it no prompt showed the model its fix attempts or which leg of a
resolution stands, so a model could narrate a readiness the engine did not
hold (#2427, turn 11). It shows, one line each:

- the latest fix attempts (accepted SOLUTIONs, by their execution turn),
  each labelled by its own kind: a remediation with its window state
  (``cause_assurance.fix_windows``), an interception with what the problem
  did after it, since it opens no window (#2036 D2.3);
- the two legs a resolution reads (``cause_elimination_rows`` and
  ``symptom_elimination_rows``), by row id, and the rows that showed the
  problem with no re-check in the problem leg;
- the outstanding symptom-verification needs, the one that holds the resolve
  offer (the record's ``resolution.pending_check_need_id``) first and marked;
- in MITIGATION, the mitigation's attempt, what the problem did after it
  (``mitigation_observation``) and the rows a re-check can name, and a
  holding relief the model has not registered, with the ask for
  ``mitigation_verified`` (#2602); with no fix dated, only these, never the
  resolution legs (#2100).

It is rendered from the case outcome record by ``narration.fix_verification``
(#2811 S2b-1a, S2b-1b-i). Where ``main`` showed a block, this call site asks
the prompt assembly's ``NarrationFold`` for the record (``narration_outcome``,
folded at most once per assembly), then computes the window verdicts ruling
Q1 (a) keeps fresh (``FixVerificationFresh``) and the needs pool
(``NeedsView``) and renders, inside one guard. If any of it raises, the block
is the one declared line ``OMITTED``, each failure is logged, it is counted
once per assembly on ``case_outcome_write_failures_total{at="narration"}``,
and the rest of the prompt is unchanged.

Informational: no gate reads it. It cites ids and never repeats the
re-verification checklist's row texts (``evidence_needs``).

A prompt tag, not a stored key: ``Evidence.metadata["fix_verification"]`` is
the namespace a post-fix row's ``rechecks`` is stored under, a different thing
that shares the name.

The allocator RESERVES it (``assembly._allocate_sections``) within a fixed
budget, ``FIX_VERIFICATION_MAX_TOKENS``: a variable section with a floor of 0,
as the causal graph is, is dropped first under pressure, and pressure is what
the long post-fix cases that need this block bring.
"""

import logging
from typing import Optional

from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    _dated_fixes,
    fix_windows,
    mitigation_attempt_turn,
    subject_index,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    case_outcome_write_failures_total,
)
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _mitigation_verify_refusal,
)
from faultmaven.core.investigation.narration.fix_verification import (
    CLOSE_TAG,
    OMITTED,
    OPEN_TAG,
    CaseIdentity,
    FixFresh,
    FixVerificationFresh,
    ProblemLeg,
    content_by_id,
    render_fix_verification,
)
from faultmaven.core.investigation.narration.needs import NeedsView
from faultmaven.core.investigation.outcome import _problem_rows, narration_outcome
from faultmaven.core.investigation.terminal_transitions import (
    _relief_without_cause_recheck,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseOutcome,
    CaseState,
    FixWindowState,
    InvestigationStage,
    WritePoint,
)

from .budget import TokenBudget

logger = logging.getLogger(__name__)

#: The block's fixed share of the prompt, reserved before any variable
#: section. Its construction bounds keep it well under this; the cap is the
#: backstop.
FIX_VERIFICATION_MAX_TOKENS = 600

_OMITTED_BLOCK = "\n".join([OPEN_TAG, OMITTED, CLOSE_TAG])


class NarrationFold:
    """One prompt assembly's narration fold (#2811 S2b-1a): the record
    ``narration_outcome`` folds, computed at most once, on the first request,
    and cached with its failure. ``get_prompt_for_case`` makes one per
    assembly, so the context it builds up to three times folds once and a
    failure is counted once (``fail``)."""

    def __init__(self, case: Case) -> None:
        self._case = case
        self._outcome: Optional[CaseOutcome] = None
        self._asked = False
        self._failed = False

    def outcome(self) -> Optional[CaseOutcome]:
        """The record, or None when the fold raised."""
        if not self._asked:
            self._asked = True
            try:
                self._outcome = narration_outcome(self._case)
            except Exception:  # noqa: BLE001 — only the block is lost
                self.fail()
        return self._outcome

    def fail(self) -> None:
        """Log the exception being handled, each time, and count it once per
        assembly; a metrics error is logged and swallowed."""
        logger.exception(
            "Case %s: the fix-verification block was not rendered (#2811)",
            getattr(self._case, "case_id", None),
        )
        if self._failed:
            return
        self._failed = True
        try:
            case_outcome_write_failures_total.labels(at=WritePoint.NARRATION).inc()
        except Exception:  # noqa: BLE001 — the block is omitted either way
            logger.exception("The narration failure was not counted (#2811)")


def _shown(case: Case) -> bool:
    """Where ``main`` shows the block: in TREATMENT, and in MITIGATION once a
    SOLUTION or the mitigation's attempt is dated."""
    stage = case.current_stage if case.state == CaseState.INVESTIGATING else None
    return stage == InvestigationStage.TREATMENT or (
        stage == InvestigationStage.MITIGATION
        and (bool(_dated_fixes(case)) or mitigation_attempt_turn(case) is not None)
    )


def _fresh(case: Case, outcome: CaseOutcome, subjects, windows):
    """The verdicts ruling Q1 (a) keeps read fresh until stage S3, by today's
    deciders: each dated attempt's window, joined by action id at the
    action's current execution turn; what the latest window says of the
    problem; and, where its ask prints, whether the gate would refuse
    ``mitigation_verified`` (True elsewhere). A
    cause re-check is owed only where readiness would ask for one: in the
    latest window, while the legs do not stand."""
    latest = windows[-1]
    legs = outcome.resolution.cause_row_ids and outcome.resolution.problem_row_ids
    by_turn = {w.fix_turn: w for w in windows if w.fix_turn is not None}
    fixes = {}
    for action in _dated_fixes(case):
        window = by_turn.get(action.accepted_in_turn)
        if window is not None:
            fixes[action.action_id] = FixFresh(
                state=FixWindowState(window.state),
                failure_turn=window.failure_turn,
                relief_turn=window.relief_turn,
                observed=window.observed,
                owed=window is latest
                and not legs
                and _relief_without_cause_recheck(case, window, subjects),
            )
    leg, turn, gone = ProblemLeg.PENDING, None, ()
    if latest.state == FIX_FAILED:
        leg, turn = ProblemLeg.FAILED, latest.failure_turn
    elif _relief_without_cause_recheck(case, latest, subjects):
        leg, turn = ProblemLeg.RELIEF, latest.relief_turn
        gone = tuple(_problem_rows(latest, subjects))
    # The gate's refusal is read only where its line prints, as ``main`` read
    # it: a holding relief of a dated mitigation, in MITIGATION.
    seen = outcome.mitigation
    asks = (
        case.current_stage == InvestigationStage.MITIGATION
        and seen is not None
        and seen.attempt_turn is not None
        and seen.relief_turn is not None
        and seen.holding
    )
    return FixVerificationFresh(
        fixes=fixes,
        problem_leg=leg,
        problem_leg_turn=turn,
        gone_row_ids=gone,
        mitigation_verify_refused=not asks
        or _mitigation_verify_refusal(case) is not None,
    )


def build_fix_verification_block(
    case: Case, fold: Optional[NarrationFold] = None
) -> str:
    """The block, or ``""``: in TREATMENT, and in MITIGATION once a SOLUTION or
    the mitigation's attempt is dated; with only the mitigation dated, its
    lines alone (#2100). ``fold`` is the prompt assembly's record (a fresh
    one per build without it)."""
    if not _shown(case):
        return ""
    fold = NarrationFold(case) if fold is None else fold
    try:
        outcome = fold.outcome()
        if outcome is None:
            return _OMITTED_BLOCK
        subjects = subject_index(case)
        windows = fix_windows(case, subjects)
        fresh = _fresh(case, outcome, subjects, windows)
        lines = render_fix_verification(
            outcome,
            CaseIdentity.of(case),
            lambda item_id: content_by_id(case, item_id),
            fresh,
            NeedsView.of(case),
        )
    except Exception:  # noqa: BLE001 — only this block is lost (#2811)
        fold.fail()
        return _OMITTED_BLOCK
    return "" if lines is None else "\n".join(lines)


def cap_fix_verification_block(text: str, budget: TokenBudget) -> str:
    """``text`` within ``FIX_VERIFICATION_MAX_TOKENS``: the body cut from its
    tail with a marker, the tags kept, so a cut never leaves the block open."""
    if not text or budget.count(text) <= FIX_VERIFICATION_MAX_TOKENS:
        return text
    body = text[len(OPEN_TAG) + 1 : -(len(CLOSE_TAG) + 1)]
    room = FIX_VERIFICATION_MAX_TOKENS - budget.count(f"{OPEN_TAG}\n\n{CLOSE_TAG}")
    return f"{OPEN_TAG}\n{budget._truncate_to(body, room)}\n{CLOSE_TAG}"
