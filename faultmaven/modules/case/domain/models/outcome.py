"""The case outcome record (#2615 stage S1): what the case established, held once.

``CaseOutcome`` is computed by ``core/investigation/outcome.compute_case_outcome``
as a fold over the prior record and stored as ``InvestigationProgress.outcome``,
inside the ``progress`` blob, so it needs no migration. A verdict the rules call
final is recorded once, with its rows and the problem statement it was read
under, and carried: a FAILED fix window, an attempt's ``accepted_in_turn``, and
the confirmed rows at RESOLVED. A conflict between a carried verdict and a fresh
derivation is logged and counted, and the carried verdict stands.

The narration step reads it (#2636 B1b): the withdrawal and mitigation notices
state its verdicts. The other narrators (S2), the deciders (S3) and the report
and API (S4) switch to it stage by stage.

Every enum here is APPEND-ONLY. A stored case names its members by value, so a
member renamed or removed would fail every load of that case. The members are
pinned by ``tests/unit/modules/case/test_case_outcome_record_2636.py``.
"""

from enum import StrEnum
from typing import Dict, List, Literal, Optional

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


class MitigationRefusalReason(StrEnum):
    """Why ``mitigation_verified`` does not register
    (``mitigation_refusal_reason``, read by ``_mitigation_verify_refusal``)."""

    ATTEMPT_UNDATED = "attempt_undated"
    REVISION_PENDING = "revision_pending"
    PROBLEM_UNVERIFIED = "problem_unverified"
    NO_BOUND_RELIEF = "no_bound_relief"


class ClosureEligibility(StrEnum):
    """``closure_verdict``: what a close of the case would be."""

    HAS_SUBSTANCE = "has_substance"
    TRIVIAL = "trivial"
    SUGGEST_RESOLVE = "suggest_resolve"


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
    confirmation_root_ids: List[str] = Field(
        default_factory=list, description="The confirmation pool"
    )
    cited_basis: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Root id -> the rows its terminal verdict cites",
    )
    pool_empty_reason: Optional[PoolEmptyReason] = Field(
        default=None, description="Why the confirmation pool is empty"
    )
    identification_held_open: bool = Field(
        default=False,
        description=(
            "Identification is held open: a standing hypothesis's root has its "
            "why open and no root is identified, as the cause recompute wrote "
            "it (``progress.cause_identification_held_open``, #2036)"
        ),
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
    final: it is carried with its rows, its citations and ``read_under``."""

    fix_turn: Optional[int] = None
    end_turn: Optional[int] = None
    state: FixWindowState
    relief_turn: Optional[int] = None
    failure_turn: Optional[int] = None
    off_cause: bool = False
    cause_row_ids: List[str] = Field(default_factory=list)
    confirmed_cause_row_ids: List[str] = Field(default_factory=list)
    problem_row_ids: List[str] = Field(default_factory=list)
    cited_basis: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Root id -> the cited rows the window's cause rows re-check",
    )
    disconfirmation: Optional[DisconfirmationPair] = None
    read_under: str = Field(
        default="", description="The problem statement the window was read under"
    )


class AttemptOutcome(BaseModel):
    """A fix attempt, keyed by ``ProposedAction.action_id``. ``action_id``
    None is the fix done outside the case: window 0, recorded once observed."""

    action_id: Optional[str] = None
    kind: Optional[Literal["remediation", "interception"]] = None
    target_ids: List[str] = Field(default_factory=list)
    accepted_in_turn: Optional[int] = Field(default=None, description="Final")
    registered_in_turn: Optional[int] = None
    window: Optional[FixWindowRecord] = Field(
        default=None, description="None for an interception, which opens none"
    )


class Demotion(BaseModel):
    """An M6 demotion with its pair rows (final). No writer yet: stage S3."""

    hypothesis_id: str
    root_id: str
    fix_turn: Optional[int] = None
    cause_row_id: Optional[str] = None
    problem_row_id: Optional[str] = None
    turn: int


class ResolutionOutcome(BaseModel):
    """``assess_resolution_readiness`` and the rows a resolution rests on."""

    verdict: ResolutionVerdict
    reason: ResolutionReason
    missing: List[str] = Field(default_factory=list)
    confirmation_gaps: List[ConfirmationGap] = Field(default_factory=list)
    cause_row_ids: List[str] = Field(default_factory=list)
    problem_row_ids: List[str] = Field(default_factory=list)
    pending_check_need_id: Optional[str] = None
    declined_stands: bool = False
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
        default=None, description="The turn the row was last narrated"
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

    folded_under: CaseState = Field(
        description=(
            "The case's state when the record was folded: a terminal case "
            "whose record was folded under an open state was never written "
            "by a terminal write, and the backfill recomputes it"
        )
    )
    problem: ProblemOutcome
    cause: CauseOutcome
    attempts: List[AttemptOutcome] = Field(default_factory=list)
    demotions: List[Demotion] = Field(default_factory=list)
    resolution: ResolutionOutcome
    unbound_rechecks: List[UnboundRecheck] = Field(default_factory=list)
    bindable: Optional[BindableRows] = Field(
        default=None, description="Set with any unbound re-check"
    )
    mitigation: Optional[MitigationOutcome] = None
    milestones_shown: List[str] = Field(default_factory=list)
    closure: ClosureOutcome
    disagreements: Dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Each carried verdict a fresh derivation disagrees with, by field "
            "and entry, and the turn the disagreement was first read: counted "
            "once, when it first appears"
        ),
    )
