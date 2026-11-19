"""The prompt's milestone status, exclusion rule and focus selection, read
from the case outcome record (#2811 S2b-1c-i, #2615 stage S2).

Each selector is pure: it reads the record (``CaseOutcome``,
``narration_outcome``), the case's identity where it needs the turn, and the
inputs read fresh beside it (``PromptStateFresh``), and returns a typed
value. The prompt's texts stay with their templates
(``prompts/templates/assembly.py``, ``prompts/context_builder/assembly.py``):
what moves here is the choice of which text applies, which is the
outcome-bearing decision.

The fresh inputs are what the record does not hold: ``mitigation.verified``
(ruling Q1 (a) keeps it fresh until stage S3; the record drops it after a
bound relapse), the symptom's currency (the wall clock), and the offer facts
until stage S2b-2 moves the offers (the false-alarm close declined, the
resolve decline's turn, the reopen chip). Each is a reader, asked only where
the line it selects prints, as ``main`` read it; the prompt assembly's
``NarrationFold`` reads each at most once (``NarrationFold.fresh``).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, NamedTuple, Optional

from faultmaven.modules.case.contracts import CaseOutcome, ProblemStatus

#: The milestone status's one line when the record or a fresh input could not
#: be read this turn, in place of both of its tags.
MILESTONES_OMITTED = "The milestone state could not be read this turn."

#: The focus emphasis's one line when the record or a fresh input could not
#: be read this turn, in place of the problem hold, the zone, the resolve
#: decline and the verifying-rows lines.
PROGRESS_OMITTED = "The case's progress could not be read this turn."

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
    false_alarm_declined_at: Callable[[], Optional[int]]
    """The turn the false-alarm close was declined, while it binds."""
    resolve_decline_turn: Callable[[], Optional[int]]
    """The turn of the standing resolve decline."""
    reopen_chip: Callable[[], bool]
    """The reply carries the reopen chip (``project_offers``)."""


class MilestoneStatus(NamedTuple):
    """The stage gates and progress indicators the prompt lists, by name."""

    gates: tuple[str, ...]
    disposition: Optional[str]
    """The accepted mitigation's disposition, listed after the gates."""
    indicators: tuple[str, ...]


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
        disposition=mitigation.disposition if mitigation is not None else None,
        indicators=tuple(i for i in INDICATORS if i in shown),
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
        return ProblemHold(HoldKind.FALSE_ALARM, fresh.false_alarm_declined_at())
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
    (``resolution.pending_check_need_id``)."""
    resolution = outcome.resolution
    if not resolution.declined_stands:
        return None
    actions = fresh.reopen_chip()
    return ResolveDeclined(
        actions=actions,
        held=actions and resolution.pending_check_need_id is not None,
        this_turn=fresh.resolve_decline_turn() == current_turn,
    )


def verifying_rows_missing(outcome: CaseOutcome) -> bool:
    """The problem is verified and no row is on record as having shown it."""
    return (
        outcome.problem.status == ProblemStatus.VERIFIED
        and not outcome.problem.verifying_row_ids
    )
