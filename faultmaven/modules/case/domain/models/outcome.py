"""The case outcome record (#2615 stage S1): what the case established, held once.

``CaseOutcome`` is computed by ``core/investigation/outcome.compute_case_outcome``
as a fold over the prior record and stored as ``InvestigationProgress.outcome``,
inside the ``progress`` blob, so it needs no migration. A verdict the rules call
final is recorded once, with its rows and the problem statement it was read
under, and carried: a FAILED fix window, an attempt's ``accepted_in_turn``, and
the confirmed rows at RESOLVED. A conflict between a carried verdict and a fresh
derivation is logged and counted, and the carried verdict stands.

The narration step reads it (#2636 B1b): the withdrawal and mitigation notices
state its verdicts. The other narrators (S2), the deciders (S3, #3046, and
S3d, #3053, which declares the in-cycle deciders its sources) and the report
and API (S4) switch to it stage by stage.

Every enum here is APPEND-ONLY. A stored case names its members by value, so a
member renamed or removed would fail every load of that case. The members are
pinned by ``tests/unit/modules/case/test_case_outcome_record_2636.py``.
"""

from enum import StrEnum
from typing import Dict, List, Literal, NamedTuple, Optional

from pydantic import BaseModel, Field, model_validator

from .lifecycle import CaseState, ClosureReason
from .problem import ProblemStatus


class FixWindowState(StrEnum):
    """A fix window's verdict (``cause_assurance.FixWindow.state``)."""

    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PoolEmptyReason(StrEnum):
    """Why the confirmation pool (``cause_assurance._cause_pool``) is empty,
    the first that holds in this order (``cause_pool_empty_reason``)."""

    HELD_OPEN = "held_open"
    """A validated root's why is open, so no root is identified."""
    IDENTIFICATION_CONTESTED = "identification_contested"
    """The MECE contest holds identification."""
    FALLEN_WITH_OTHER_STANDING = "fallen_with_other_standing"
    """The case's cause fell, and another standing hypothesis is rooted elsewhere."""
    FIX_TARGETS_OTHER_CAUSE = "fix_targets_other_cause"
    """The cause fell, and the kinded latest remediation targets none of it."""
    ROOTLESS_VALIDATED_ONLY = "rootless_validated_only"
    """Only rootless VALIDATED hypotheses stand."""
    NO_CAUSE = "no_cause"
    """None of the above: no cause at all."""


class ResolutionVerdict(StrEnum):
    """``ResolutionReadiness.verdict``."""

    READY = "ready"
    NEEDS_INFO = "needs_info"
    SUGGEST_CLOSE = "suggest_close"


class ResolutionReason(StrEnum):
    """Which branch of ``assess_resolution_readiness`` answered: one member per
    return, so no reason falls through to another."""

    FALSE_ALARM = "false_alarm"
    """The problem was invalidated: nothing to resolve."""
    PENDING_CHECK = "pending_check"
    """Confirmed, but a check the model asked for after the fix is open."""
    CONFIRMED = "confirmed"
    """Both legs confirm the resolution."""
    NOTHING_ON_RECORD = "nothing_on_record"
    """No cause, solution or evidence is on record."""
    FIX_OFF_CAUSE = "fix_off_cause"
    """The latest attempt acted on no cause now identified."""
    FIX_FAILED = "fix_failed"
    """The latest fix window FAILED."""
    DOCUMENTATION_GAPS = "documentation_gaps"
    """Not confirmed: ``missing`` and ``confirmation_gaps`` say what is asked."""


class ConfirmationGap(StrEnum):
    """One ask of the documentation-gap branch, as that branch makes it."""

    RELIEF_WITHOUT_CAUSE_RECHECK = "relief_without_cause_recheck"
    """The latest window shows the problem gone, with no cause re-check."""
    PROBLEM_UNRECHECKED = "problem_unrechecked"
    """The cause leg stands; the problem the user reported is unchecked."""
    NO_SYMPTOM_ROW = "no_symptom_row"
    """Nothing on record showed the problem, so no re-check can bind to it."""


class PendingCheckReason(StrEnum):
    """Why the held need (``pending_symptom_check``) still holds the
    resolution (#2811 S2b-1b-i)."""

    UNANSWERED = "unanswered"
    """Outstanding: nothing after the ask answers it."""
    FULFILLED_BEFORE_ASK = "fulfilled_before_ask"
    """FULFILLED, citing only rows from before the ask."""


class MitigationRefusalReason(StrEnum):
    """Why ``mitigation_verified`` does not register
    (``mitigation_refusal_reason``, read by ``_mitigation_verify_refusal``)."""

    ATTEMPT_UNDATED = "attempt_undated"
    REVISION_PENDING = "revision_pending"
    PROBLEM_UNVERIFIED = "problem_unverified"
    NO_BOUND_RELIEF = "no_bound_relief"


class RecheckReason(StrEnum):
    """Why a cause-bound causal_absence row does or does not count toward the
    cause leg (``cause_assurance.cause_recheck_status``), one member per
    branch, in the order they are tried. A code, never prose: the narration
    package renders it (#2811)."""

    COUNTS = "counts"
    HELD_OPEN = "held_open"
    DISCONFIRMATION = "disconfirmation"
    PREDATES_POSITING = "predates_positing"
    PREDATES_FAILED_FIX = "predates_failed_fix"
    NO_CONFIRMING_WINDOW = "no_confirming_window"
    OUTSIDE_WINDOW = "outside_window"
    UNCITED = "uncited"
    OTHER_CAUSE = "other_cause"


class NoWindowKind(StrEnum):
    """Why no fix window confirms (``cause_assurance._no_confirming_window``)."""

    OFF_CAUSE_ATTEMPT = "off_cause_attempt"
    """The latest attempt was no fix of the cause now identified."""
    FAILED = "failed"
    """The latest observed fix was seen failing."""
    NO_LONGER_IDENTIFIED = "no_longer_identified"
    """The latest observed fix acted on a cause no longer identified."""
    LATEST_FAILED = "latest_failed"
    """No fix window was observed."""


class NoConfirmingWindow(BaseModel):
    """The parameters of ``NO_CONFIRMING_WINDOW``. ``fix_turn``: the fix's
    execution turn, None for window 0 (the fix done outside the case)."""

    kind: NoWindowKind
    fix_turn: Optional[int] = None
    failure_turn: Optional[int] = None
    newer_fix_turn: Optional[int] = Field(
        default=None, description="A newer fix nobody has re-checked yet"
    )


class UncitedKind(StrEnum):
    """Why a window-bound cause re-check confirms nothing
    (``cause_assurance.uncited_recheck``)."""

    NO_ROOT = "no_root"
    CITED = "cited"
    NO_CITATION = "no_citation"


class UncitedRecheck(BaseModel):
    """The parameters of ``UNCITED``: its variant, and for ``cited`` up to
    five rows of the root's cited basis."""

    kind: UncitedKind
    cited_row_ids: List[str] = Field(default_factory=list)


class RecheckWhy(NamedTuple):
    """A row's ``RecheckReason`` with the parameters its text states: a turn
    (the positing, the failed fix's, or the fix the row did not re-check),
    or the typed parameters of a missing window or an uncited re-check.
    Returned by ``cause_recheck_status``; the record holds the same
    parameters (``CauseOutcome``)."""

    reason: RecheckReason
    turn: Optional[int] = None
    window: Optional[NoConfirmingWindow] = None
    uncited: Optional[UncitedRecheck] = None


class ClosureEligibility(StrEnum):
    """``closure_verdict``: what a close of the case would be."""

    HAS_SUBSTANCE = "has_substance"
    TRIVIAL = "trivial"
    SUGGEST_RESOLVE = "suggest_resolve"


#: Raised by every change that adds or changes a ``CaseOutcome`` field: a
#: stored record of an older shape is rewritten by the backfill (#2811).
CURRENT_OUTCOME_SHAPE = 5


class WritePoint(StrEnum):
    """Where a record is folded (``record_case_outcome``,
    ``compute_case_outcome``). Only a cycle, a replay, the investigation
    apply's mint before the chain emission (``PRE_CHAIN``, #3046 S3a-2), a
    resolve, a close and the backfill's repair mint a final verdict
    (``MINTING_POINTS``), and any first record."""

    CYCLE = "cycle"
    REPLAY = "replay"
    RESOLVED = "resolved"
    CLOSED = "closed"
    REPAIR = "repair"
    BACKFILL = "backfill"
    COMMIT = "commit"
    WRITE = "write"
    METADATA = "metadata"
    CREATE = "create"
    NARRATION = "narration"
    SHAPE = "shape"
    """Not a fold: a verdict an older shape held final, marked on upgrade."""
    PRE_CHAIN = "pre_chain"
    """The investigation apply's mint before the chain emission (#2582)."""


class FinalAt(BaseModel):
    """Where a verdict became final: the write point that minted it, and the
    case's turn then (#2811)."""

    at: WritePoint
    turn: int


class ProblemOutcome(BaseModel):
    """Where the problem statement stands, and the rows that showed it."""

    status: ProblemStatus
    verifying_row_ids: List[str] = Field(default_factory=list)
    statement: str = Field(
        default="", description="The problem statement the rows verified"
    )


class CauseOutcome(BaseModel):
    """The cause pool's legs (``_pool_legs`` / ``_cause_pool``)."""

    identified_root_ids: List[str] = Field(default_factory=list)
    held_root_ids: List[str] = Field(
        default_factory=list, description="Count-held roots marked terminal"
    )
    fallen_root_ids: List[str] = Field(default_factory=list)
    held_open_root_ids: List[str] = Field(default_factory=list)
    contested_root_ids: List[str] = Field(
        default_factory=list,
        description=(
            "The validated, mutually-exclusive roots that contest "
            "identification (``mece_contested_root_ids``, #2811 S2b-2a), "
            "derived fresh at the write like ``held_open_root_ids``, in the "
            "one computation ``pool_empty_reason``'s IDENTIFICATION_CONTESTED "
            "reads (#3053 S3d-3)"
        ),
    )
    confirmation_root_ids: List[str] = Field(
        default_factory=list, description="The confirmation pool"
    )
    cited_basis: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Root id -> the rows its terminal verdict cites",
    )
    pool_empty_reason: Optional[PoolEmptyReason] = Field(
        default=None,
        description=(
            "Why the confirmation pool is empty. IDENTIFICATION_CONTESTED "
            "is read from ``contested_root_ids``' computation (#3053 S3d-3), "
            "HELD_OPEN from ``identification_held_open``'s (S3d-5)"
        ),
    )
    identification_leg: Optional[Literal["chain", "rcc", "working_conclusion"]] = Field(
        default=None,
        description=(
            "The leg the root cause is identified on "
            "(``cause_identification_leg``), or None"
        ),
    )
    identification_held_open: bool = Field(
        default=False,
        description=(
            "Identification is held open (#2036): a root of "
            "``held_open_root_ids`` stands and none is identified, derived "
            "fresh at the write in the one computation ``pool_empty_reason``'s "
            "HELD_OPEN reads (``identification_is_held_open``, #3053 S3d-5), "
            "the one the cause recompute stores"
        ),
    )
    recheck: Dict[str, RecheckReason] = Field(
        default_factory=dict,
        description="Each cause-bound causal_absence row, and whether it counts",
    )
    posited_turn: int = Field(
        default=0, description="The turn the case's cause was posited (#2036)"
    )
    latest_disconfirmation_turn: int = Field(
        default=-1, description="The newest engine-known failed fix's turn, or -1"
    )
    no_confirming_window: Optional[NoConfirmingWindow] = Field(
        default=None, description="Set with any ``no_confirming_window`` row"
    )
    uncited: Optional[UncitedRecheck] = Field(
        default=None, description="Set with any ``uncited`` row"
    )
    other_cause_fix_turn: Optional[int] = Field(
        default=None, description="Set with any ``other_cause`` row"
    )

    @model_validator(mode="after")
    def _reason_iff_empty(self) -> "CauseOutcome":
        """A reason exactly when the confirmation pool is empty."""
        if (self.pool_empty_reason is None) == (not self.confirmation_root_ids):
            raise ValueError(
                "pool_empty_reason is required when, and only when, the "
                "confirmation pool is empty"
            )
        return self


class DisconfirmationPair(BaseModel):
    """The counterfactual disconfirmation as rows: the cause row observed
    removed and the problem row observed present after it."""

    cause_row_id: Optional[str] = None
    problem_row_id: Optional[str] = None
    cause_turn: int
    problem_turn: int


class FixWindowRecord(BaseModel):
    """One fix window as read (``cause_assurance.FixWindow``). FAILED is
    final once ``final_at`` is set: it is carried with its rows, its
    citations and ``read_under``."""

    fix_turn: Optional[int] = None
    end_turn: Optional[int] = None
    state: FixWindowState
    relief_turn: Optional[int] = None
    failure_turn: Optional[int] = None
    off_cause: bool = False
    observed: bool = Field(default=False, description="The fix's outcome was looked at")
    cause_row_ids: List[str] = Field(default_factory=list)
    confirmed_cause_row_ids: List[str] = Field(default_factory=list)
    problem_row_ids: List[str] = Field(
        default_factory=list,
        description=(
            "The rows that showed the problem gone; on a FAILED window, up "
            "to its failure"
        ),
    )
    cited_basis: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Root id -> the cited rows the window's cause rows re-check",
    )
    disconfirmation: Optional[DisconfirmationPair] = Field(
        default=None,
        description=(
            "The counterfactual pair. One a FAILED window takes later (#2728) "
            "adds its cause row and that row's citation to the window, and "
            "nothing else: problem_row_ids keep the rows the failure was read "
            "on (#2586)"
        ),
    )
    read_under: str = Field(
        default="", description="The problem statement the window was read under"
    )
    final_at: Optional[FinalAt] = None


class AttemptOutcome(BaseModel):
    """A fix attempt, keyed by ``ProposedAction.action_id``. ``action_id``
    None is the fix done outside the case: window 0, recorded once observed."""

    action_id: Optional[str] = None
    kind: Optional[Literal["remediation", "interception"]] = None
    target_ids: List[str] = Field(default_factory=list)
    accepted_in_turn: Optional[int] = Field(
        default=None, description="Final once ``final_at`` is set"
    )
    registered_in_turn: Optional[int] = None
    window: Optional[FixWindowRecord] = Field(
        default=None, description="None for an interception, which opens none"
    )
    relief_turn: Optional[int] = Field(
        default=None, description="An interception's: the problem last seen gone"
    )
    present_turn: Optional[int] = Field(
        default=None, description="An interception's: the problem last seen present"
    )
    final_at: Optional[FinalAt] = None


class Demotion(BaseModel):
    """A cause demoted on a failed fix of it, with the pair it was read on
    (final, #2586): M6's own demotion, or the model's or the user's
    refutation standing on the failed fix (``record_demotion``). One per
    hypothesis and attempt (``action_id``; None for window 0). A demotion on
    the cause's own evidence records none."""

    hypothesis_id: str
    action_id: Optional[str] = None
    root_id: str
    fix_turn: Optional[int] = None
    cause_row_id: Optional[str] = None
    problem_row_id: Optional[str] = None
    turn: int


class ResolutionOutcome(BaseModel):
    """``assess_resolution_readiness`` and the rows a resolution rests on;
    ``pending_check_need_id`` and ``pending_check_reason`` are
    ``pending_symptom_check``'s, whatever readiness answered."""

    verdict: ResolutionVerdict
    reason: ResolutionReason
    missing: List[str] = Field(default_factory=list)
    confirmation_gaps: List[ConfirmationGap] = Field(default_factory=list)
    cause_row_ids: List[str] = Field(default_factory=list)
    problem_row_ids: List[str] = Field(default_factory=list)
    pending_check_need_id: Optional[str] = Field(
        default=None,
        description=(
            "The need ``pending_symptom_check`` holds, whatever readiness "
            "answered (it returns FALSE_ALARM first)"
        ),
    )
    pending_check_reason: Optional[PendingCheckReason] = Field(
        default=None,
        description="Why the held need holds, beside its id (shape 4)",
    )
    declined_stands: bool = False
    off_cause_turn: Optional[int] = Field(
        default=None,
        description=(
            "The latest attempt's turn when no fix of the cause now "
            "identified can confirm it (``off_cause_attempt``), whatever "
            "readiness answered, or None (shape 3)"
        ),
    )
    unrechecked_row_ids: List[str] = Field(
        default_factory=list,
        description=(
            "With the cause leg standing, the rows that showed the problem "
            "which no problem-leg row re-checks; empty without it (shape 3)"
        ),
    )
    confirmed_row_ids: List[str] = Field(
        default_factory=list,
        description="At RESOLVED, the confirmation and the stamp's rows (final)",
    )
    statement: str = Field(default="", description="``resolution_record_lines``")


class UnboundRecheck(BaseModel):
    """A post-fix row bound to nothing (``unbound_post_fix_rows``); what it
    could name instead is the record's ``bindable``."""

    evidence_id: str
    observed_turn: int
    uncited: bool = Field(
        description="Inside a fix window, but not bound by the root's citation"
    )
    after_mitigation: bool = False
    narrated_turn: Optional[int] = Field(
        default=None,
        description=(
            "The turn the row was narrated in this continuous unbound stretch "
            "(#2555), or None while it is still to be told"
        ),
    )


class BindableRows(BaseModel):
    """What an unbound re-check could name, the same for every unbound row:
    the rows that showed the problem, the cited basis of the root cause
    (``SubjectIndex.bindable_rows``), and why an uncited re-check confirms
    nothing (``uncited_recheck_why``)."""

    problem_row_ids: List[str] = Field(default_factory=list)
    cause_row_ids: List[str] = Field(default_factory=list)
    why: str = ""


class MitigationOutcome(BaseModel):
    """The accepted mitigation as the record holds it (``mitigation_observation``)."""

    attempt_turn: Optional[int] = None
    relief_turn: Optional[int] = None
    present_turn: Optional[int] = None
    holding: bool = True
    relief_row_id: Optional[str] = None
    span_seconds: Optional[float] = None
    fix_before_relief: bool = False
    disposition: Optional[str] = None
    disposition_turn: Optional[int] = None
    verified: bool = Field(default=False, description="Verified and still holding")
    declared_stable_effective: bool = False
    settled: bool = False
    refusal_reason: Optional[MitigationRefusalReason] = None


class ClosureOutcome(BaseModel):
    """What a close establishes. No label or short label: their copy is the
    owner's (stage S4)."""

    eligibility: ClosureEligibility
    reason: Optional[ClosureReason] = None
    statement: Optional[str] = Field(
        default=None, description="``mitigation_closure_line``"
    )


class CaseOutcome(BaseModel):
    """The case's outcome, folded over its prior (#2615, *The record*)."""

    shape: int = Field(
        default=1,
        description=(
            "The record's shape (``CURRENT_OUTCOME_SHAPE`` when written); a "
            "record stored before the field reads as shape 1"
        ),
    )
    folded_under: CaseState = Field(
        description=(
            "The case's state at the last fold that may mint (#2811): a "
            "terminal case whose record was folded under an open state was "
            "never written by a terminal write, and the backfill recomputes it"
        )
    )
    problem: ProblemOutcome
    cause: CauseOutcome
    attempts: List[AttemptOutcome] = Field(default_factory=list)
    demotions: List[Demotion] = Field(default_factory=list)
    resolution: ResolutionOutcome
    unbound_rechecks: List[UnboundRecheck] = Field(default_factory=list)
    bindable: Optional[BindableRows] = Field(
        default=None, description="Always set from shape 2; None before"
    )
    mitigation: Optional[MitigationOutcome] = None
    milestones_shown: List[str] = Field(default_factory=list)
    closure: ClosureOutcome
    disagreements: Dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Each carried verdict a fresh derivation disagrees with, by field "
            "and entry, and the turn the disagreement was first read: counted "
            "once, when a saved record first carries it (#2689)"
        ),
    )
