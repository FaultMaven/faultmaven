from datetime import datetime, timezone
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Mapping, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

# ============================================================
# Status & Lifecycle Models (Section 2)
# ============================================================


class CaseState(str, Enum):
    """
    Case lifecycle state — passive label describing a case's current condition.

    Values fall into two categories:
    - **Phases** (active work): INQUIRY, INVESTIGATING
    - **Dispositions** (terminal resolution): RESOLVED, CLOSED

    Case Actions (phase transitions and dispositions):
      INQUIRY → INVESTIGATING  (phase transition)
      INQUIRY → CLOSED         (disposition)
      INVESTIGATING → RESOLVED (disposition; includes the KB-resolution milestone-collapse variant)
      INVESTIGATING → CLOSED   (disposition)

    v3: INQUIRY → RESOLVED edge removed. KB-driven cases collapse INVESTIGATING
    to one or two turns via per-runbook Cause attribution rather than skipping
    the phase. See docs/architecture/investigation-engine/investigation-lifecycle-logic.md
    §1.2 INVESTIGATING → RESOLVED → KB-Resolution Path.
    """

    INQUIRY = "inquiry"
    """
    Phase: Pre-investigation exploration.

    Characteristics:
    - User asking questions
    - Agent providing quick guidance
    - No formal investigation commitment
    - May transition to INVESTIGATING or reach a disposition

    Typical Duration: Minutes to hours
    """

    INVESTIGATING = "investigating"
    """
    Phase: Active formal investigation.

    Characteristics:
    - Working through stages (DIAGNOSIS, MITIGATION, TREATMENT)
    - Gathering evidence
    - Testing hypotheses
    - Applying solutions
    - May reach a disposition (RESOLVED or CLOSED)

    Typical Duration: Hours to days
    """

    RESOLVED = "resolved"
    """
    Disposition: Case closed WITH solution.

    Characteristics:
    - Problem was fixed
    - Solution verified
    - closure_reason is None (resolution is the categorization)
    - No further case actions allowed

    Disposition: Terminal (permanent)
    """

    CLOSED = "closed"
    """
    Disposition: Case closed WITHOUT solution.

    Characteristics:
    - Inquiry-only (no investigation), OR any close from INVESTIGATING
    - closure_reason is a ClosureReason (defined below in this module), set
      by derive_closure_reason
    - No further case actions allowed

    Disposition: Terminal (permanent)
    """

    @property
    def is_terminal(self) -> bool:
        """Check if this state is a disposition (terminal)."""
        return self in [CaseState.RESOLVED, CaseState.CLOSED]

    @property
    def is_active(self) -> bool:
        """Check if this state is a phase (active, not terminal)."""
        return self in [CaseState.INQUIRY, CaseState.INVESTIGATING]

    @property
    def is_phase(self) -> bool:
        """Check if this state represents an active phase (INQUIRY or INVESTIGATING)."""
        return self.is_active

    @property
    def is_disposition(self) -> bool:
        """Check if this state represents a terminal disposition (RESOLVED or CLOSED)."""
        return self.is_terminal


class CaseSeverity(str, Enum):
    """
    Case severity levels for prioritization and filtering.

    Severity indicates the impact and urgency of the issue:
    - LOW: Minor issue, minimal impact on operations
    - MEDIUM: Moderate issue, some impact on operations
    - HIGH: Significant issue, major impact on operations
    - CRITICAL: Severe issue, complete service disruption

    Used by API service layer for case filtering and prioritization.
    """

    LOW = "low"
    """Minor issue with minimal operational impact."""

    MEDIUM = "medium"
    """Moderate issue with some operational impact."""

    HIGH = "high"
    """Significant issue with major operational impact."""

    CRITICAL = "critical"
    """Severe issue causing complete service disruption."""

    @classmethod
    def from_string(cls, value: str) -> "CaseSeverity":
        """Convert string to CaseSeverity, case-insensitive.

        Args:
            value: String value to convert

        Returns:
            CaseSeverity enum value

        Raises:
            ValueError: If value is not a valid severity
        """
        value_lower = value.lower()
        for severity in cls:
            if severity.value == value_lower:
                return severity
        raise ValueError(
            f"Invalid severity: {value}. Must be one of: {[s.value for s in cls]}"
        )


class CaseAction(BaseModel):
    """
    Record of one case action (phase transition or disposition change).
    Provides audit trail for case lifecycle.
    """

    from_state: CaseState = Field(description="Status before the action")

    to_state: CaseState = Field(description="Status after the action")

    triggered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the action occurred",
    )

    triggered_by: str = Field(
        description="Who triggered: user_id or 'system' for automatic actions"
    )

    reason: str = Field(
        description="Human-readable reason for the action", max_length=500
    )

    @model_validator(mode="after")
    def validate_action(self):
        """Ensure case action is valid."""
        if not is_valid_action(self.from_state, self.to_state):
            raise ValueError(
                f"Invalid case action: {self.from_state} -> {self.to_state}"
            )
        return self

    model_config = ConfigDict(
        frozen=True,  # Immutable once created
    )


#: Every edge the state machine PERMITS. Distinct from what a user may pick
#: from the UI — see ``USER_SELECTABLE_ACTIONS`` in ``case_action_manager``,
#: which is a strict subset. TWO edges are where the two graphs differ, and
#: both differ for the same reason: a menu cannot honour an edge whose
#: precondition is a fact about the case.
#:
#: INQUIRY → INVESTIGATING is legal and the Gate 1 handshake performs it, but
#: it is earned by a confirmed problem statement (the DB CHECK
#: ``cases_description_required_for_investigation`` makes that structural).
#:
#: INVESTIGATING → RESOLVED is legal and the disposition handshake performs it,
#: but it is earned by a qualifying ``causal_absence_evidence`` row and a
#: ``symptom_absence_evidence`` row at or after it — the cause confirmed
#: eliminated and the problem gone with it — which the engine reads for itself
#: and offers on (INV-43).
#:
#: v3: INQUIRY → RESOLVED removed. KB-resolution flows through INVESTIGATING via
#: the milestone collapse — state authored in one turn, disposition still
#: confirmed on the next (investigation-lifecycle-logic.md §1.2).
#: Frozen deliberately. The graph this replaced was a function-local dict
#: rebuilt on every call, so it could not be widened at runtime; a plain module
#: dict of lists can be, by any importer — including a test that mutates and
#: forgets to restore, which would leak across the process and simultaneously
#: flip ``is_valid_action``, the ``CaseAction`` validator, the INV-22 guard and
#: ``derive_disposition_eligibility``. This suite already has order-dependent
#: failures (#823); a mutable safety net is not one worth adding to them.
LEGAL_TRANSITIONS: Mapping[CaseState, tuple[CaseState, ...]] = MappingProxyType(
    {
        CaseState.INQUIRY: (CaseState.INVESTIGATING, CaseState.CLOSED),
        CaseState.INVESTIGATING: (CaseState.RESOLVED, CaseState.CLOSED),
        CaseState.RESOLVED: (),  # Disposition — terminal
        CaseState.CLOSED: (),  # Disposition — terminal
    }
)


def is_valid_action(from_state: CaseState, to_state: CaseState) -> bool:
    """
    Validate a case action (phase transition or disposition change).

    Valid Case Actions:
    - INQUIRY → INVESTIGATING (phase transition: start investigation)
    - INQUIRY → CLOSED (disposition: no investigation needed)
    - INVESTIGATING → RESOLVED (disposition: solution verified; includes KB-resolution milestone collapse)
    - INVESTIGATING → CLOSED (disposition: abandoned/escalated)

    Invalid:
    - RESOLVED → * (disposition is terminal)
    - CLOSED → * (disposition is terminal)
    - INVESTIGATING → INQUIRY (no backward phase transition)
    """
    return to_state in LEGAL_TRANSITIONS.get(from_state, ())


# Backward compatibility alias
is_valid_transition = is_valid_action


class ParticipantRole(str, Enum):
    """Participant roles in case collaboration"""

    OWNER = "owner"
    COLLABORATOR = "collaborator"
    VIEWER = "viewer"
    SUPPORT = "support"


class InvestigationStrategy(str, Enum):
    """
    Investigation approach mode.
    Affects decision thresholds, workflow behavior, and agent prompts.
    """

    ACTIVE_INCIDENT = "active_incident"
    """
    Service is down NOW. Priority: Speed over completeness.

    Characteristics:
    - Accept hypothesis with TESTING state for quick mitigation
    - Skip to solution phase even without complete root cause analysis
    - Escalate after 3 failed attempts
    - Evidence threshold: SUPPORTS is sufficient (not STRONGLY_SUPPORTS)
    - Time pressure: Minutes matter

    Use when:
    - temporal_state = ONGOING
    - urgency_level = CRITICAL or HIGH
    - User needs immediate restoration
    """

    POST_MORTEM = "post_mortem"
    """
    Historical analysis. Priority: Thorough understanding.

    Characteristics:
    - Require VALIDATED hypothesis before root cause conclusion
    - Complete all milestones systematically
    - Escalate after hypothesis space exhausted (not time-based)
    - Evidence threshold: STRONGLY_SUPPORTS required
    - Time pressure: Days acceptable

    Use when:
    - temporal_state = HISTORICAL or INTERMITTENT (resolved)
    - No immediate service impact
    - Learning/prevention goal
    """


# ============================================================
# Closure Reason
# ============================================================
#
# Sub-categorization of the CLOSED state, derived by the engine from the state
# the case was closed from (``derive_closure_reason``). Every close carries a
# REASON: why the case ended, never merely when. What each value means is
# contract text, published as the enum's ``x-enum-descriptions`` from
# ``_CLOSURE_REASON_MEANINGS`` below: changing one moves the contract version.
#
# The members are declared in derivation order, most specific first; where
# several hold, the first wins:
#
#   - inquiry_only: the one reason a close from INQUIRY has.
#   - closed_false_alarm first among the INVESTIGATING reasons, because it is
#     decisive: no cause, fix or mitigation can stand on a problem that never
#     existed. The one close the engine itself offers on a finding
#     (problem_status.invalidate_problem).
#   - resolution_declined ahead of solution_deferred, which on such a case would
#     say the fix was never confirmed (#2360).
#   - solution_deferred ahead of the remaining reasons: where several hold, the
#     one carrying the most established knowledge wins.
#   - closed_rca_infeasible ABOVE mitigation_sufficient, because the engine's
#     only path to it fires on `mitigation_verified` (milestone_engine, "propose
#     closure as stabilized rather than push RCA"), so both hold on essentially
#     every such close and the more informative label must win. It holds only
#     WITH a rationale. Distinct from insufficient evidence, which is contingent
#     (someone with more access could get it): here a future reader should know
#     not to re-open the case expecting to find a cause.
#   - mitigation_sufficient: the one closure in the set that is not a failure of
#     any kind, which is why it is not folded into a generic bucket.
#   - closed_restatement_held immediately above the generic bucket, which it
#     only ever displaces (#1195): routing it there would head the closure
#     summary "insufficient evidence to establish the problem or its cause" over
#     a case that gathered enough, and would bucket it with genuine data walls
#     in the flywheel.
#   - closed_insufficient_evidence: the default for any other close from
#     INVESTIGATING. Capture-on-close only: the honest partial (residual
#     candidates + the specific unmet need, already persisted on the case) is
#     signal for calibration and the flywheel, and the engine never nudges
#     toward this close (no SUGGEST_CLOSE); see
#     insufficient-evidence-handling.md §5.4.
#
# REMOVED: closed_after_investigation. It named the state a case closed FROM
# rather than why it ended, and covered four unrelated situations at once — a
# documented-but-deferred fix, a successful mitigation, an intractable cause,
# and a plain failure to establish anything. Its former INSUFFICIENT_EVIDENCE-
# cell restriction also made closed_insufficient_evidence unreachable for a case
# that never verified its symptom: that cell requires work_gate_passed (>=2
# hypotheses across >=2 categories), which is CAUSE work a case stuck at symptom
# verification never does, so every such close fell into the generic bucket.
#
# The LLM does NOT emit closure_reason; user-motivation context (e.g., "we're
# escalating") lives in the LLM-authored persistent Report's free-form summary.
#
# A ``StrEnum``, not the ``(str, Enum)`` of the other enums here: the value is
# persisted and compared as text (``pending_transition["justifying_signature"]``
# is ``f"{reason}|{turn}"``) and interpolated into prompts and the action
# history, and ``str()`` of a ``(str, Enum)`` member renders its NAME.


class ClosureReason(StrEnum):
    """Why a CLOSED case ended: the most specific reason that holds when it is closed. Null for any case that is not CLOSED."""

    INQUIRY_ONLY = "inquiry_only"
    CLOSED_FALSE_ALARM = "closed_false_alarm"
    RESOLUTION_DECLINED = "resolution_declined"
    SOLUTION_DEFERRED = "solution_deferred"
    CLOSED_RCA_INFEASIBLE = "closed_rca_infeasible"
    MITIGATION_SUFFICIENT = "mitigation_sufficient"
    CLOSED_RESTATEMENT_HELD = "closed_restatement_held"
    CLOSED_INSUFFICIENT_EVIDENCE = "closed_insufficient_evidence"

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        schema = handler.resolve_ref_schema(handler(core_schema))
        schema["x-enum-descriptions"] = [_CLOSURE_REASON_MEANINGS[m] for m in cls]
        return schema


_T = TypeVar("_T")


def closure_reason_table(
    mapping: Mapping[ClosureReason, _T],
) -> Mapping[ClosureReason, _T]:
    """``mapping`` as a read-only table with exactly one entry per closure reason.

    Raises ``ValueError`` naming every member it lacks and every key that is not
    a member. A ``dict[ClosureReason, …]`` is tied to the vocabulary by type but
    not complete, and completeness is what a hand-kept copy loses when a value
    is added.
    """
    members = set(ClosureReason)
    missing = [member.value for member in ClosureReason if member not in mapping]
    extra = [repr(key) for key in mapping if key not in members]
    if missing or extra:
        raise ValueError(
            "a closure-reason table needs one entry per ClosureReason: "
            f"missing {missing or 'none'}, not a member {extra or 'none'}"
        )
    return MappingProxyType({member: mapping[member] for member in ClosureReason})


#: Each value's meaning, published as the enum's ``x-enum-descriptions``.
#: Contract text: editing an entry moves the contract version.
_CLOSURE_REASON_MEANINGS: Mapping[ClosureReason, str] = closure_reason_table(
    {
        ClosureReason.INQUIRY_ONLY: (
            "Closed during inquiry, before any investigation started."
        ),
        ClosureReason.CLOSED_FALSE_ALARM: (
            "The evidence showed the reported problem was not present where and "
            "when it was reported, so there was nothing to fix."
        ),
        ClosureReason.RESOLUTION_DECLINED: (
            "The fix was confirmed to work, and the user chose not to mark the "
            "case resolved."
        ),
        ClosureReason.SOLUTION_DEFERRED: (
            "The cause was identified and a fix documented, but no fix was "
            "confirmed to work in this case."
        ),
        ClosureReason.CLOSED_RCA_INFEASIBLE: (
            "The root cause was declared structurally unreachable, with a "
            "rationale (for example an uncontrollable external dependency or an "
            "end-of-life system)."
        ),
        ClosureReason.MITIGATION_SUFFICIENT: (
            "A verified mitigation relieved the symptom, and root-cause analysis "
            "was not pursued further."
        ),
        ClosureReason.CLOSED_RESTATEMENT_HELD: (
            "The evidence supported a cause, but its statement never said more "
            "than the problem itself, so no root cause was established."
        ),
        ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE: (
            "What was needed was never established: the problem could not be "
            "verified, or no cause could be grounded in the evidence."
        ),
    }
)
