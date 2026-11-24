"""The prompt's milestone status, exclusion rule, focus selection, the
fallback prompt's milestones and resolution and the state summary's verified
line, read from the case outcome record (#2811 S2b-1c-i, #2615 stage S2).

Each selector is pure: it reads the record (``CaseOutcome``,
``narration_outcome``), the case's identity where it needs the turn, and the
inputs read fresh beside it (``PromptStateFresh``), and returns a typed
value. The prompt's texts stay with their templates
(``prompts/templates/assembly.py``, ``prompts/templates/fallback.py``,
``prompts/context_builder/assembly.py``, ``prompts/context_builder/history.py``):
what moves here is the choice of which text applies, which is the
outcome-bearing decision. The one exception is the closure reason's model
rendering (pin 6's model half, ruling Q2 (a)), which is the value itself.

The fresh inputs are what the record does not hold: ``mitigation.verified``
(ruling Q1 (a) keeps it fresh until stage S3d, #3053; the record drops it after a
bound relapse), the symptom's currency (the wall clock), and the offer facts,
which the offer record owns (``OfferView``, #2811 S2b-2d, ruling (a) of
2026-11-20: whether an offer is open, and each standing decline's turn, a
reader of its own). Each is a reader, asked only where the line it selects
prints, as ``main`` read it; the prompt assembly's ``NarrationFold`` reads
each at most once (``NarrationFold.fresh``).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, NamedTuple, Optional

from faultmaven.modules.case.contracts import (
    CaseOutcome,
    ClosureReason,
    ProblemStatus,
)

from .offer_view import OfferView

#: The milestone status's one line when the record or a fresh input could not
#: be read this turn, in place of both of its tags; and the fallback prompt's
#: milestones.
MILESTONES_OMITTED = "The milestone state could not be read this turn."

#: The focus emphasis's one line when the record or a fresh input could not
#: be read this turn, in place of the problem hold, the zone, the resolve
#: decline and the verifying-rows lines.
PROGRESS_OMITTED = "The case's progress could not be read this turn."

#: The fallback prompt's resolution line when the record could not be read
#: this turn, in place of "Solution verified", the closure reason or "Closed".
RESOLUTION_OMITTED = "The case's resolution could not be read this turn."

#: The problem statuses that put the case on hold, under which the reply
#: carries no reopen chip: ``problem_status.problem_on_hold`` is the
#: deciders' twin of this set (stage S3d, #3053).
ON_HOLD = (ProblemStatus.REVISION_PENDING, ProblemStatus.INVALIDATED)

#: The stage gates, in the order the prompt lists them.
GATES = (
    "mitigation_accepted",
    "mitigation_verified",
    "solution_accepted",
    "solution_verified",
)
#: The progress indicators, in the order the prompt lists them.
INDICATORS = ("symptom_verified", "root_cause_identified", "solution_proposed")


@dataclass(frozen=True)
class PromptStateFresh:
    """The inputs the selectors read fresh beside the record. Each field is a
    reader the selector asks only where its line prints; it holds no text:
    flags and turns only."""

    mitigation_verified: Callable[[], bool]
    """``progress.mitigation.verified`` as stored (ruling Q1 (a))."""
    symptom_stale: Callable[[], bool]
    """The symptom's last observation is STALE (``assess_symptom_currency``)."""
    offers: Callable[[], OfferView]
    """The offer facts (``offers.offer_view``, #2811 S2b-2d); each decline's
    turn is read where its line asks for it."""


class MilestoneStatus(NamedTuple):
    """The stage gates and progress indicators the prompt lists, by name."""

    gates: tuple[str, ...]
    mitigation_disposition: Optional[str]
    """The accepted mitigation's disposition, listed after the gates."""
    indicators: tuple[str, ...]


def progress_indicators(outcome: CaseOutcome) -> tuple[str, ...]:
    """The progress indicators the record shows (``milestones_shown``), in
    the prompt's order."""
    shown = set(outcome.milestones_shown)
    return tuple(i for i in INDICATORS if i in shown)


def milestone_status(outcome: CaseOutcome, fresh: PromptStateFresh) -> MilestoneStatus:
    """The gates and indicators the record shows (``milestones_shown``), but
    ``mitigation_verified``, which is read fresh (Q1 (a))."""
    shown = set(outcome.milestones_shown) - {"mitigation_verified"}
    if fresh.mitigation_verified():
        shown.add("mitigation_verified")
    gates = tuple(gate for gate in GATES if gate in shown)
    mitigation = outcome.mitigation
    return MilestoneStatus(
        gates=gates,
        mitigation_disposition=(
            mitigation.disposition if mitigation is not None else None
        ),
        indicators=progress_indicators(outcome),
    )


def cause_work_rule(outcome: CaseOutcome) -> bool:
    """The problem takes cause work, now or staged on a revision: the
    exclusion rule's action half applies."""
    return outcome.problem.status in (
        ProblemStatus.VERIFIED,
        ProblemStatus.REVISION_PENDING,
    )


class HoldKind(StrEnum):
    """Why the problem statement itself is in question."""

    REVISION = "revision"
    """A revised statement awaits the user's re-confirmation."""
    FALSE_ALARM = "false_alarm"
    """The reported symptom was never present."""


class ProblemHold(NamedTuple):
    kind: HoldKind
    declined_at: Optional[int] = None
    """FALSE_ALARM: the turn the user declined its close."""


def problem_hold(
    outcome: CaseOutcome, fresh: PromptStateFresh
) -> Optional[ProblemHold]:
    """The hold the record's problem status puts the case on, or None."""
    status = outcome.problem.status
    if status == ProblemStatus.REVISION_PENDING:
        return ProblemHold(HoldKind.REVISION)
    if status == ProblemStatus.INVALIDATED:
        return ProblemHold(
            HoldKind.FALSE_ALARM, fresh.offers().false_alarm_decline_turn()
        )
    return None


class FocusZone(StrEnum):
    """Where a diagnosing case stands (Framework §8.5)."""

    VERIFY = "verify"
    """Zone 1: the symptom is not verified."""
    CAUSE = "cause"
    """Zone 2: verified, the cause not identified."""
    CAUSE_STALE = "cause_stale"
    """Zone 2, the symptom observed away from the present."""
    SOLUTION = "solution"
    """Zone 3: the cause identified, no solution proposed."""
    PENDING = "pending"
    """Zone 3: a solution proposed, awaiting execution."""


def focus_zone(outcome: CaseOutcome, fresh: PromptStateFresh) -> FocusZone:
    """The zone the record's indicators place the case in; the symptom's
    currency is asked only in Zone 2."""
    shown = set(outcome.milestones_shown)
    if "symptom_verified" not in shown:
        return FocusZone.VERIFY
    if "root_cause_identified" not in shown:
        return FocusZone.CAUSE_STALE if fresh.symptom_stale() else FocusZone.CAUSE
    if "solution_proposed" not in shown:
        return FocusZone.SOLUTION
    return FocusZone.PENDING


class ResolveDeclined(NamedTuple):
    """A standing decline of the resolution, as the prompt states it."""

    actions: bool
    """The reply carries the chips the line points the user at."""
    held: bool
    """A check the model asked for holds the case: point at the close only."""
    this_turn: bool
    """The decline was given on the current turn."""


def resolve_declined(
    outcome: CaseOutcome, fresh: PromptStateFresh, current_turn: int
) -> Optional[ResolveDeclined]:
    """The standing resolve decline (``resolution.declined_stands``), or
    None; whether a check holds the case is the record's
    (``resolution.pending_check_need_id``). The reply carries the reopen chip
    while no offer is open and the record's problem is not on hold
    (``ON_HOLD``), as ``project_offers`` builds it; the decline's turn is the
    offer record's."""
    resolution = outcome.resolution
    if not resolution.declined_stands:
        return None
    offers = fresh.offers()
    actions = offers.open_proposal_id is None and outcome.problem.status not in ON_HOLD
    return ResolveDeclined(
        actions=actions,
        held=actions and resolution.pending_check_need_id is not None,
        this_turn=offers.resolve_decline_turn() == current_turn,
    )


def verifying_rows_missing(outcome: CaseOutcome) -> bool:
    """The problem is verified and no row is on record as having shown it."""
    return (
        outcome.problem.status == ProblemStatus.VERIFIED
        and not outcome.problem.verifying_row_ids
    )


class FocusSelection(NamedTuple):
    """Which focus-emphasis lines apply, each None or False when none does."""

    hold: Optional[ProblemHold]
    zone: Optional[FocusZone]
    """Selected only where no hold is."""
    resolve_declined: Optional[ResolveDeclined]
    verifying_rows_missing: bool


def focus_selection(
    outcome: CaseOutcome,
    fresh: PromptStateFresh,
    current_turn: int,
    *,
    steered: bool,
    zoned: bool,
) -> FocusSelection:
    """The focus emphasis's lines: on a ``steered`` turn (no knowledge_query
    or agent_meta mode) the problem hold, else, where ``zoned`` (DIAGNOSIS),
    the zone; the standing resolve decline on every turn; on a steered turn,
    the verifying rows missing. Each fresh input is asked only where its line
    is decided, in this order."""
    hold = problem_hold(outcome, fresh) if steered else None
    zone = focus_zone(outcome, fresh) if zoned and hold is None else None
    return FocusSelection(
        hold=hold,
        zone=zone,
        resolve_declined=resolve_declined(outcome, fresh, current_turn),
        verifying_rows_missing=steered and verifying_rows_missing(outcome),
    )


class TerminalResolution(NamedTuple):
    """What a terminal case's resolution line states."""

    solution_confirmed: bool
    """The record shows ``solution_verified``."""
    closure: Optional[ClosureReason]
    """The record's closure reason (``closure.reason``)."""


def terminal_resolution(outcome: CaseOutcome) -> TerminalResolution:
    return TerminalResolution(
        solution_confirmed="solution_verified" in outcome.milestones_shown,
        closure=outcome.closure.reason,
    )


#: Each closure reason as the prompt renders it to the model (#2615 pin 6,
#: its model half, as ruling Q2 (a) reads it): the value it printed before
#: the record, unchanged.
CLOSURE_MODEL_TEXT: dict[ClosureReason, str] = {
    ClosureReason.INQUIRY_ONLY: "inquiry_only",
    ClosureReason.CLOSED_FALSE_ALARM: "closed_false_alarm",
    ClosureReason.RESOLUTION_DECLINED: "resolution_declined",
    ClosureReason.SOLUTION_DEFERRED: "solution_deferred",
    ClosureReason.CLOSED_RCA_INFEASIBLE: "closed_rca_infeasible",
    ClosureReason.MITIGATION_SUFFICIENT: "mitigation_sufficient",
    ClosureReason.CLOSED_RESTATEMENT_HELD: "closed_restatement_held",
    ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE: "closed_insufficient_evidence",
}


def closure_model_text(reason: ClosureReason) -> Optional[str]:
    """``reason``'s model rendering, or None for a member the map lacks, which
    the caller renders as its value and counts."""
    return CLOSURE_MODEL_TEXT.get(reason)
