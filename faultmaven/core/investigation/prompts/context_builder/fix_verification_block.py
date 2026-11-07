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
  offer (``pending_symptom_check``) first and marked;
- in MITIGATION, the mitigation's attempt, what the problem did after it
  (``mitigation_observation``) and the rows a re-check can name, and a
  holding relief the model has not registered, with the ask for
  ``mitigation_verified`` (#2602); with no fix dated, only these, never the
  resolution legs (#2100).

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

from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    FIX_INTERCEPTION,
    FIX_SUCCEEDED,
    SUBJECT_PROBLEM,
    cause_recheck_status,
    fix_windows,
    interception_relief,
    mitigation_attempt_turn,
    mitigation_observation,
    off_cause_attempt,
    pending_symptom_check,
    subject_index,
    symptom_elimination_rows,
)
from faultmaven.core.investigation.evidence_need_surfacing import is_ask_exhausted
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _mitigation_verify_refusal,
)
from faultmaven.core.investigation.terminal_transitions import (
    _relief_without_cause_recheck,
    declined_resolve_entry,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    EvidenceCategory,
    InvestigationActionType,
    InvestigationStage,
    NeedPurpose,
    NeedState,
)

from .budget import TokenBudget
from .history import _truncate_request_text

#: The block's fixed share of the prompt, reserved before any variable
#: section. Its construction bounds (below) keep it well under this; the cap
#: is the backstop.
FIX_VERIFICATION_MAX_TOKENS = 600
#: The latest attempts shown.
MAX_ATTEMPTS = 3
#: The row ids shown per leg, and the outstanding needs shown.
MAX_IDS = 6
MAX_NEEDS = 5
_DESCRIPTION_CHARS = 80

OPEN_TAG = "<fix_verification>"
CLOSE_TAG = "</fix_verification>"


def _ids(ids: list[str]) -> str:
    shown = ", ".join(ids[:MAX_IDS])
    more = len(ids) - MAX_IDS
    return f"{shown} (+{more} more)" if more > 0 else shown


def _dated_fixes(case: Case) -> list:
    """The accepted SOLUTIONs with an execution turn, oldest first: what the
    fix windows date (``cause_assurance._dated_fix_turns``)."""
    return sorted(
        (
            a
            for a in case.proposed_actions or []
            if a.state == "accepted"
            and getattr(a.action_type, "value", a.action_type)
            == InvestigationActionType.SOLUTION.value
            and a.accepted_in_turn is not None
        ),
        key=lambda a: a.accepted_in_turn,
    )


def _window_state(case: Case, window, subjects, owing) -> str:
    """``owing`` is the one window that can owe a cause re-check: the latest,
    and only while the legs do not stand (readiness's ask)."""
    if window is None:
        return "not yet observed"
    if window.state == FIX_SUCCEEDED:
        return "succeeded"
    if window.state == FIX_FAILED:
        return f"failed at turn {window.failure_turn}"
    if window is owing and _relief_without_cause_recheck(case, window, subjects):
        return (
            f"problem re-checked gone at turn {window.relief_turn}; "
            "cause re-check owed"
        )
    seen = window.observed or window.relief_turn is not None
    return "observed, no verdict yet" if seen else "not yet observed"


def _attempt_lines(case: Case, attempts: list, windows, subjects, owing) -> list[str]:
    by_turn = {w.fix_turn: w for w in windows if w.fix_turn is not None}
    lines = [f"Fix attempts (latest {MAX_ATTEMPTS}, oldest first):"]
    if len(attempts) > MAX_ATTEMPTS:
        lines.append(f"  …and {len(attempts) - MAX_ATTEMPTS} earlier")
    for a in attempts[-MAX_ATTEMPTS:]:
        description = " ".join((a.description or "").split())
        if len(description) > _DESCRIPTION_CHARS:
            description = description[: _DESCRIPTION_CHARS - 1].rstrip() + "…"
        kind = getattr(a, "fix_kind", None)
        acted = ", ".join(getattr(a, "target_ids", None) or ()) or "no node recorded"
        if kind == FIX_INTERCEPTION:
            relief, present = interception_relief(case, a.accepted_in_turn, subjects)
            if present is not None and (relief is None or present > relief):
                state = f"problem seen again at turn {present}"
            elif relief is not None:
                state = f"problem seen gone at turn {relief}"
            else:
                state = "not yet observed"
            label = f", an interception at {acted} (cannot confirm the cause)"
        else:
            state = _window_state(
                case, by_turn.get(a.accepted_in_turn), subjects, owing
            )
            label = f", a fix of {acted}" if kind else ""
        lines.append(
            f"  - [{a.action_id}] {description} — executed turn "
            f"{a.accepted_in_turn}{label}: {state}"
        )
    return lines


def _cited_basis_clause(case: Case, windows, subjects, status) -> str:
    """The rows a re-check confirms the cause through (#2557), so the model asks
    for those, not for the relief: only where a re-check can count (a fix of
    the cause on record, its window not FAILED, the cause not held open), and
    not when a pending row's why already names them."""
    _problem, basis = subjects.bindable_rows()
    if (
        not basis
        or not _dated_fixes(case)
        or windows[-1].state == FIX_FAILED
        or off_cause_attempt(case, windows) is not None
        or getattr(case.progress, "cause_identification_held_open", False)
        or any(basis[0].evidence_id in why for _row, why in status[:MAX_IDS])
    ):
        return ""
    return "; the cited basis to re-check: " + _ids([r.evidence_id for r in basis])


def _leg_lines(case: Case, windows, subjects, status, cause_rows, problem_rows):
    lines = ["Resolution legs (RESOLVED needs both):"]
    if cause_rows:
        lines.append("  - cause: " + _ids([r.evidence_id for r in cause_rows]))
    else:
        # A bound re-check that does not count yet says why, so the model asks
        # for what is missing rather than for the re-check again.
        cause = "  - cause: missing"
        if status:
            # The full why once; a later row with it keeps the clause before ";".
            pending, said = [], set()
            for row, why in status:
                shown = why.split(";")[0] if why in said else why
                pending.append(f"{row.evidence_id} ({shown})")
                said.add(why)
            cause += "; re-checked, not counted yet: " + _ids(pending)
        lines.append(cause + _cited_basis_clause(case, windows, subjects, status))
        latest = windows[-1]
        if latest.state == FIX_FAILED:
            # As readiness asks (``assess_resolution_readiness``).
            lines.append(
                f"  - problem: the latest fix was seen failing at turn "
                f"{latest.failure_turn}; a new fix is needed"
            )
        elif _relief_without_cause_recheck(case, latest, subjects):
            # The symptom checked before the cause (#1906): the problem is
            # shown gone after the fix, and the cause re-check is owed.
            gone = [
                e.evidence_id
                for e in subjects.rows.values()
                if e.category == EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
                and subjects.subject(e) == SUBJECT_PROBLEM
                and latest.holds(subjects.observed(e))
            ]
            lines.append(
                f"  - problem: re-checked gone at turn {latest.relief_turn} "
                f"({_ids(gone)}); counted once the cause is re-checked after "
                "the fix"
            )
        else:
            lines.append("  - problem: read once the cause leg stands")
        return lines
    problem = "  - problem: " + (
        _ids([r.evidence_id for r in problem_rows]) or "missing"
    )
    named = {getattr(r, "rechecks_evidence_id", None) for r in problem_rows}
    unchecked = [v for v in subjects.verifying if v in subjects.rows and v not in named]
    if unchecked:
        problem += "; not re-checked since the fix: " + _ids(unchecked)
    lines.append(problem)
    return lines


def _need_lines(case: Case, held) -> list[str]:
    """The outstanding symptom-verification needs the engine still puts to the
    user; ask-exhausted ones are counted, in ``<evidence_needs>``'s own words,
    not listed as live asks (#1079). The ``held`` need
    (``pending_symptom_check``) is listed first and marked, exhausted or
    FULFILLED on rows from before its ask, so the model is shown which need to
    dispose of (#1906 D5.2)."""
    needs = [
        n
        for n in case.evidence_needs
        if n is held
        or (n.is_outstanding and n.purpose == NeedPurpose.SYMPTOM_VERIFICATION)
    ]
    live = [n for n in needs if n is held or not is_ask_exhausted(n, case.current_turn)]
    live.sort(key=lambda n: n is not held)
    stopped = len(needs) - len(live)
    if not live:
        lines = ["Outstanding symptom-verification needs: none"]
    else:
        lines = ["Outstanding symptom-verification needs:"]
        lines += [
            f"  - [{n.need_id}] {_truncate_request_text(n.request_text)}"
            + (_held_mark(case, n) if n is held else "")
            for n in live[:MAX_NEEDS]
        ]
        if len(live) > MAX_NEEDS:
            lines.append(f"  …and {len(live) - MAX_NEEDS} more")
    if stopped:
        lines.append(
            f"  ({stopped} the engine has STOPPED surfacing: see <evidence_needs>)"
        )
    return lines


#: The held mark's addition while the user's decline of the resolution
#: stands: the answer alone does not lapse it (``offers.decline_stands``).
DECLINE_STILL_BINDS = (
    "; the user's decline of the resolution still binds after it, unless the "
    "answer is a new problem-bound row showing the problem gone"
)


def _held_mark(case: Case, need) -> str:
    """The held need's mark. Under a standing resolve decline
    (``declined_resolve_entry``) it says the decline outlives the answer, so
    the mark promises no offer the decline would refuse."""
    why = (
        ": the rows it cites predate the ask"
        if need.state == NeedState.FULFILLED
        else ""
    )
    decline = DECLINE_STILL_BINDS if declined_resolve_entry(case) is not None else ""
    return f" (holds the resolution offer until answered{why}{decline})"


def _mitigation_lines(case: Case, subjects) -> list[str]:
    """The mitigation's attempt, what the problem did after it, and the rows a
    re-check can name (#2100). Rendered only in MITIGATION, where the record
    carries no verdict and no disposition: a holding relief is named with the
    ask for ``mitigation_verified``, the verdict's trigger (#2602, ruled (b)),
    when the gate would accept it (``_mitigation_verify_refusal`` is None)."""
    seen = mitigation_observation(case, subjects)
    relief, present = seen.relief_turn, seen.present_turn
    unregistered = []
    if relief is not None and seen.holding:
        state = f"problem re-checked relieved at turn {relief}"
        if _mitigation_verify_refusal(case) is None:
            unregistered = [
                f"  - relief row {seen.relief_row_id} (turn {relief}) is on record "
                "but unregistered: send mitigation_verified to record it"
            ]
    elif present is not None:
        state = f"problem seen again at turn {present}"
    else:
        state = "not yet re-checked"
    problem, _cause = subjects.bindable_rows()
    rows = _ids([r.evidence_id for r in problem]) or (
        "none on record as having shown the problem"
    )
    return [
        f"Mitigation: executed turn {seen.attempt_turn}: {state}",
        *unregistered,
        f"  - rows a post-mitigation re-check names in rechecks: {rows}",
    ]


def build_fix_verification_block(case: Case) -> str:
    """The block, or ``""``: in TREATMENT, and in MITIGATION once a SOLUTION or
    the mitigation's attempt is dated; with only the mitigation dated, its
    lines alone (#2100). One ``SubjectIndex`` and one ``fix_windows`` list
    serve the attempts, the legs and the held need (``pending_symptom_check``);
    a held need's mark reads the resolve decline (``declined_resolve_entry``,
    through the closure verdict), which builds its own."""
    stage = case.current_stage if case.state == CaseState.INVESTIGATING else None
    attempts = _dated_fixes(case)
    mitigating = (
        stage == InvestigationStage.MITIGATION
        and mitigation_attempt_turn(case) is not None
    )
    if stage != InvestigationStage.TREATMENT and not (
        stage == InvestigationStage.MITIGATION and attempts
    ):
        if not mitigating:
            return ""
        header = "The engine's record of the mitigation (informational; ids name "
        lines = _mitigation_lines(case, subject_index(case))
        return "\n".join([OPEN_TAG, header + "evidence rows):", *lines, CLOSE_TAG])
    subjects = subject_index(case)
    windows = fix_windows(case, subjects)
    status = cause_recheck_status(case, subjects=subjects, windows=windows)
    cause_rows = [row for row, why in status if why is None]
    problem_rows = (
        symptom_elimination_rows(case, cause_rows, subjects=subjects, windows=windows)
        if cause_rows
        else []
    )
    # A cause re-check is owed only where readiness would ask for one: in the
    # latest window, while the legs do not stand.
    owing = None if problem_rows else windows[-1]
    lines = [
        OPEN_TAG,
        "The engine's record of the fix (informational; ids name evidence rows):",
        *(_mitigation_lines(case, subjects) if mitigating else []),
        *(
            _attempt_lines(case, attempts, windows, subjects, owing)
            if attempts
            else ["Fix attempts: none recorded"]
        ),
        *_leg_lines(case, windows, subjects, status, cause_rows, problem_rows),
        *_need_lines(
            case, pending_symptom_check(case, subjects=subjects, windows=windows)
        ),
        CLOSE_TAG,
    ]
    return "\n".join(lines)


def cap_fix_verification_block(text: str, budget: TokenBudget) -> str:
    """``text`` within ``FIX_VERIFICATION_MAX_TOKENS``: the body cut from its
    tail with a marker, the tags kept, so a cut never leaves the block open."""
    if not text or budget.count(text) <= FIX_VERIFICATION_MAX_TOKENS:
        return text
    body = text[len(OPEN_TAG) + 1 : -(len(CLOSE_TAG) + 1)]
    room = FIX_VERIFICATION_MAX_TOKENS - budget.count(f"{OPEN_TAG}\n\n{CLOSE_TAG}")
    return f"{OPEN_TAG}\n{budget._truncate_to(body, room)}\n{CLOSE_TAG}"
