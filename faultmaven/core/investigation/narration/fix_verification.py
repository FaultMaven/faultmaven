"""The ``<fix_verification>`` block rendered from the case outcome record
(#2811 S2b-1a, #2615 stage S2).

``render_fix_verification`` is pure: it reads the record (``CaseOutcome``,
``narration_outcome``), the case's identity (``CaseIdentity``), the text of an
action or a need by id (``content_by_id``, ruling 5's ``content(id)``, which
for this block reads actions and needs only, never an evidence row), the
window verdicts the deciders read (the record's final FAILED verdicts over
the fresh read, ``carried_fix_windows``, #3046 S3a-2), which the call site
computes and passes in as ``FixVerificationFresh``, and the needs pool
(``NeedsView``, ruling 2 of 2026-11-17), read only where the need lines
print. It renders the whole block. The held need, its mark and ``DECLINE_STILL_BINDS`` are the record's
(``resolution.pending_check_need_id`` and ``pending_check_reason``, written
from ``pending_symptom_check`` whatever readiness answered, and
``declined_stands``; S2b-1b-i). A need whose text does not read raises, so
the call site prints the declared ``OMITTED`` line, never an empty need.

Where it may differ from a fresh read, it prints the record's: an attempt's
carried ``accepted_in_turn``, and a carried FAILED attempt whose action no
longer reads (``content`` answers None for it, and its description prints
empty; declared; neither is known to be reachable).
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Mapping, NamedTuple, Optional

from faultmaven.modules.case.contracts import (
    AttemptOutcome,
    CaseOutcome,
    CaseState,
    FixWindowState,
    InvestigationStage,
    NeedPurpose,
    PendingCheckReason,
    RecheckReason,
    ResolutionOutcome,
)

from .needs import NeedsView, _truncate_request_text
from .rechecks import recheck_text, recorded_why

#: The latest attempts shown.
MAX_ATTEMPTS = 3
#: The row ids shown per leg.
MAX_IDS = 6
#: The outstanding needs shown.
MAX_NEEDS = 5
_DESCRIPTION_CHARS = 80

OPEN_TAG = "<fix_verification>"
CLOSE_TAG = "</fix_verification>"

#: The block's one line when the record or the fresh verdicts could not be
#: read this turn (``build_fix_verification_block``).
OMITTED = "The fix-verification state could not be read this turn."

_FIX_HEADER = "The engine's record of the fix (informational; ids name evidence rows):"
_MITIGATION_HEADER = (
    "The engine's record of the mitigation (informational; ids name evidence rows):"
)


class CaseIdentity(NamedTuple):
    """The case fields a renderer may read besides the record (#2615 pin 2)."""

    case_id: str
    state: CaseState
    current_stage: Optional[InvestigationStage]
    current_turn: int

    @classmethod
    def of(cls, case) -> "CaseIdentity":
        return cls(case.case_id, case.state, case.current_stage, case.current_turn)


class Content(NamedTuple):
    """What an id names: its text, and for an evidence row the turn it was
    established (``collected_at_turn``)."""

    text: str
    established_turn: Optional[int] = None


def content_by_id(case, item_id: str, *, row: bool = False) -> Optional[Content]:
    """What an id names (ruling 5 of 2026-11-15), by kind: a proposed
    action's description or a need's current ``request_text`` (which an update
    may rewrite), looked up in that order; with ``row``, an evidence row's
    ``summary or extract or ""`` with its established turn (S2b-1b-ii), and
    only that. None when nothing of that kind has the id."""
    if row:
        for evidence in case.evidence or []:
            if evidence.evidence_id == item_id:
                return Content(
                    evidence.summary or evidence.extract or "",
                    evidence.collected_at_turn,
                )
        return None
    for action in case.proposed_actions or []:
        if action.action_id == item_id:
            return Content(action.description)
    for need in case.evidence_needs or []:
        if need.need_id == item_id:
            return Content(need.request_text)
    return None


class ProblemLeg(StrEnum):
    """What the latest fix window says of the problem while the cause leg is
    missing (readiness's ask)."""

    FAILED = "failed"
    """The latest window FAILED: a new fix is needed."""
    RELIEF = "relief"
    """The problem re-checked gone in it, with no cause re-check."""
    PENDING = "pending"
    """Neither: read once the cause leg stands."""


@dataclass(frozen=True)
class FixFresh:
    """A remediation's window verdict, read fresh (Q1 (a))."""

    state: FixWindowState
    failure_turn: Optional[int]
    relief_turn: Optional[int]
    observed: bool
    owed: bool
    """The latest window, while the legs do not stand, shows the problem gone
    with no cause re-check."""


@dataclass(frozen=True)
class FixVerificationFresh:
    """The window verdicts the deciders read (``carried_fix_windows``, #3046
    S3a-2), computed at the call site. It holds no text: ids, turns, flags
    and codes only."""

    fixes: Mapping[str, FixFresh] = field(default_factory=dict)
    """Each dated attempt's window, by action id, read at the action's
    current execution turn; absent with no window."""
    problem_leg: ProblemLeg = ProblemLeg.PENDING
    problem_leg_turn: Optional[int] = None
    """The failure turn (FAILED) or the relief turn (RELIEF)."""
    gone_row_ids: tuple[str, ...] = ()
    """RELIEF's problem-gone rows in the latest window."""
    mitigation_verify_refused: bool = True
    """Whether the gate would refuse ``mitigation_verified``: read only where
    its ask prints (a holding relief in MITIGATION), True elsewhere."""


def _ids(ids: list[str]) -> str:
    shown = ", ".join(ids[:MAX_IDS])
    more = len(ids) - MAX_IDS
    return f"{shown} (+{more} more)" if more > 0 else shown


def _dated(outcome: CaseOutcome) -> list[AttemptOutcome]:
    """The accepted SOLUTIONs with an execution turn, oldest first."""
    return sorted(
        (
            a
            for a in outcome.attempts
            if a.action_id is not None and a.accepted_in_turn is not None
        ),
        key=lambda a: a.accepted_in_turn,
    )


def _window_state(fix: Optional[FixFresh]) -> str:
    if fix is None:
        return "not yet observed"
    if fix.state == FixWindowState.SUCCEEDED:
        return "succeeded"
    if fix.state == FixWindowState.FAILED:
        return f"failed at turn {fix.failure_turn}"
    if fix.owed:
        return (
            f"problem re-checked gone at turn {fix.relief_turn}; " "cause re-check owed"
        )
    seen = fix.observed or fix.relief_turn is not None
    return "observed, no verdict yet" if seen else "not yet observed"


def _attempt_lines(attempts, content, fresh) -> list[str]:
    lines = [f"Fix attempts (latest {MAX_ATTEMPTS}, oldest first):"]
    if len(attempts) > MAX_ATTEMPTS:
        lines.append(f"  …and {len(attempts) - MAX_ATTEMPTS} earlier")
    for a in attempts[-MAX_ATTEMPTS:]:
        found = content(a.action_id)
        description = " ".join((found.text if found else "").split())
        if len(description) > _DESCRIPTION_CHARS:
            description = description[: _DESCRIPTION_CHARS - 1].rstrip() + "…"
        acted = ", ".join(a.target_ids) or "no node recorded"
        if a.kind == "interception":
            relief, present = a.relief_turn, a.present_turn
            if present is not None and present == relief:
                state = f"problem still present at turn {present}"
            elif present is not None and (relief is None or present > relief):
                state = f"problem seen again at turn {present}"
            elif relief is not None:
                state = f"problem seen gone at turn {relief}"
            else:
                state = "not yet observed"
            label = f", an interception at {acted} (cannot confirm the cause)"
        else:
            state = _window_state(fresh.fixes.get(a.action_id))
            label = f", a fix of {acted}" if a.kind else ""
        lines.append(
            f"  - [{a.action_id}] {description} — executed turn "
            f"{a.accepted_in_turn}{label}: {state}"
        )
    return lines


def _cited_basis_clause(outcome, attempts, fresh, status) -> str:
    """The rows a re-check confirms the cause through (#2557), so the model asks
    for those, not for the relief: only where a re-check can count (a fix of
    the cause on record, its window not FAILED, the cause not held open), and
    not when a pending row's why already names them."""
    basis = outcome.bindable.cause_row_ids
    if (
        not basis
        or not attempts
        or fresh.problem_leg == ProblemLeg.FAILED
        or outcome.resolution.off_cause_turn is not None
        or outcome.cause.identification_held_open
        or any(basis[0] in why for _row, why in status[:MAX_IDS])
    ):
        return ""
    return "; the cited basis to re-check: " + _ids(basis)


def _leg_lines(outcome, attempts, fresh) -> list[str]:
    lines = ["Resolution legs (RESOLVED needs both):"]
    resolution, cause = outcome.resolution, outcome.cause
    if resolution.cause_row_ids:
        lines.append("  - cause: " + _ids(resolution.cause_row_ids))
        problem = "  - problem: " + (_ids(resolution.problem_row_ids) or "missing")
        if resolution.unrechecked_row_ids:
            problem += "; not re-checked since the fix: " + _ids(
                resolution.unrechecked_row_ids
            )
        return [*lines, problem]
    # A bound re-check that does not count yet says why, so the model asks for
    # what is missing rather than for the re-check again.
    status = [
        (row, recheck_text(recorded_why(cause, code)))
        for row, code in cause.recheck.items()
        if code != RecheckReason.COUNTS
    ]
    line = "  - cause: missing"
    if status:
        # The full why once; a later row with it keeps the clause before ";".
        pending, said = [], set()
        for row, why in status:
            shown = why.split(";")[0] if why in said else why
            pending.append(f"{row} ({shown})")
            said.add(why)
        line += "; re-checked, not counted yet: " + _ids(pending)
    lines.append(line + _cited_basis_clause(outcome, attempts, fresh, status))
    if fresh.problem_leg == ProblemLeg.FAILED:
        lines.append(
            f"  - problem: the latest fix was seen failing at turn "
            f"{fresh.problem_leg_turn}; a new fix is needed"
        )
    elif fresh.problem_leg == ProblemLeg.RELIEF:
        # The symptom checked before the cause (#1906): the cause re-check is
        # owed.
        lines.append(
            f"  - problem: re-checked gone at turn {fresh.problem_leg_turn} "
            f"({_ids(list(fresh.gone_row_ids))}); counted once the cause is "
            "re-checked after the fix"
        )
    else:
        lines.append("  - problem: read once the cause leg stands")
    return lines


def _mitigation_lines(outcome: CaseOutcome, fresh) -> list[str]:
    """The mitigation's attempt, what the problem did after it, and the rows a
    re-check can name (#2100); a holding relief is named with the ask for
    ``mitigation_verified`` when the gate would accept it (#2602), and a relief
    and a persistence in one turn is the problem still present (#3053 S3d-6)."""
    seen = outcome.mitigation
    relief, present = seen.relief_turn, seen.present_turn
    unregistered = []
    if relief is not None and seen.holding:
        state = f"problem re-checked relieved at turn {relief}"
        if not fresh.mitigation_verify_refused:
            unregistered = [
                f"  - relief row {seen.relief_row_id} (turn {relief}) is on record "
                "but unregistered: send mitigation_verified to record it"
            ]
    elif present is not None:
        seen_as = "still present" if present == relief else "seen again"
        state = f"problem {seen_as} at turn {present}"
    else:
        state = "not yet re-checked"
    rows = _ids(outcome.bindable.problem_row_ids) or (
        "none on record as having shown the problem"
    )
    return [
        f"Mitigation: executed turn {seen.attempt_turn}: {state}",
        *unregistered,
        f"  - rows a post-mitigation re-check names in rechecks: {rows}",
    ]


#: The held mark's addition while the user's decline of the resolution
#: stands: the answer alone does not lapse it (``offers.decline_stands``).
DECLINE_STILL_BINDS = (
    "; the user's decline of the resolution still binds after it, unless the "
    "answer is a new problem-bound row showing the problem gone"
)


def _held_mark(resolution: ResolutionOutcome) -> str:
    """The held need's mark. Under a standing resolve decline it says the
    decline outlives the answer, so the mark promises no offer the decline
    would refuse."""
    why = (
        ": the rows it cites predate the ask"
        if resolution.pending_check_reason == PendingCheckReason.FULFILLED_BEFORE_ASK
        else ""
    )
    decline = DECLINE_STILL_BINDS if resolution.declined_stands else ""
    return f" (holds the resolution offer until answered{why}{decline})"


def _need_lines(resolution: ResolutionOutcome, needs: NeedsView, content) -> list[str]:
    """The outstanding symptom-verification needs the engine still puts to the
    user; ask-exhausted ones are counted, in ``<evidence_needs>``'s own words,
    not listed as live asks (#1079). The held need
    (``resolution.pending_check_need_id``) is listed first and marked,
    exhausted or FULFILLED on rows from before its ask, so the model is shown
    which need to dispose of (#1906 D5.2)."""
    held = resolution.pending_check_need_id
    listed = [
        n
        for n in needs.needs
        if n.need_id == held
        or (n.outstanding and n.purpose == NeedPurpose.SYMPTOM_VERIFICATION)
    ]
    live = [n for n in listed if n.need_id == held or not n.exhausted]
    live.sort(key=lambda n: n.need_id != held)
    stopped = len(listed) - len(live)
    lines = ["Outstanding symptom-verification needs:"]
    for n in live[:MAX_NEEDS]:
        found = content(n.need_id)
        if found is None:
            raise LookupError(f"no text reads for need {n.need_id}")
        mark = _held_mark(resolution) if n.need_id == held else ""
        lines.append(f"  - [{n.need_id}] {_truncate_request_text(found.text)}{mark}")
    if not live:
        lines = ["Outstanding symptom-verification needs: none"]
    elif len(live) > MAX_NEEDS:
        lines.append(f"  …and {len(live) - MAX_NEEDS} more")
    if stopped:
        lines.append(
            f"  ({stopped} the engine has STOPPED surfacing: see <evidence_needs>)"
        )
    return lines


def render_fix_verification(
    outcome: CaseOutcome,
    identity: CaseIdentity,
    content: Callable[[str], Optional[Content]],
    fresh: FixVerificationFresh,
    needs: Callable[[], NeedsView],
) -> Optional[tuple[str, ...]]:
    """The block's lines, its tags included, or None where no block is shown:
    in TREATMENT, and in MITIGATION once a SOLUTION or the mitigation's
    attempt is dated; with only the mitigation dated, its lines alone and no
    needs (#2100). ``needs`` is called only where the need lines print."""
    stage = (
        identity.current_stage if identity.state == CaseState.INVESTIGATING else None
    )
    attempts = _dated(outcome)
    mitigating = (
        stage == InvestigationStage.MITIGATION
        and outcome.mitigation is not None
        and outcome.mitigation.attempt_turn is not None
    )
    if stage != InvestigationStage.TREATMENT and not (
        stage == InvestigationStage.MITIGATION and attempts
    ):
        if not mitigating:
            return None
        return (
            OPEN_TAG,
            _MITIGATION_HEADER,
            *_mitigation_lines(outcome, fresh),
            CLOSE_TAG,
        )
    return (
        OPEN_TAG,
        _FIX_HEADER,
        *(_mitigation_lines(outcome, fresh) if mitigating else []),
        *(
            _attempt_lines(attempts, content, fresh)
            if attempts
            else ["Fix attempts: none recorded"]
        ),
        *_leg_lines(outcome, attempts, fresh),
        *_need_lines(outcome.resolution, needs(), content),
        CLOSE_TAG,
    )
