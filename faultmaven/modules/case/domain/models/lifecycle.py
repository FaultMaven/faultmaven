from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    - closure_reason = "resolved"
    - No further case actions allowed

    Disposition: Terminal (permanent)
    """

    CLOSED = "closed"
    """
    Disposition: Case closed WITHOUT solution.

    Characteristics:
    - Investigation abandoned/escalated
    - OR inquiry-only (no investigation)
    - closure_reason = "abandoned" | "escalated" | "inquiry_only" | "duplicate" | "other"
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
#: but it is earned by a qualifying ``causal_absence_evidence`` row — the cause
#: confirmed eliminated — which the engine reads for itself and offers on
#: (INV-43).
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
# Closure Reason Enum
# ============================================================
#
# Sub-categorization of CLOSED state. Engine-derived from case state at
# transition time. None for non-terminal and RESOLVED cases.
#
# All values are programmatic — derived from the state the case was closed from
# (unified opportunistic flow, no path fork). Every close carries a REASON: a
# closure_reason must say why the case ended, never merely when. Derived in the
# order below, most specific first:
#
#   - inquiry_only: INQUIRY → CLOSED (no investigation started)
#   - solution_deferred: the cause is IDENTIFIED and a fix is documented, but
#     implementation happens out-of-band (a change request, a maintenance
#     window, another team) — `solution_feasible == DEFERRED` with a solution on
#     record, the disposition `_maybe_propose_deferred_close` proposes. The most
#     complete non-resolved outcome there is: nothing was missing, the fix
#     simply was not applied this session. Ranked first among the INVESTIGATING
#     reasons for that reason — where several hold, the one carrying the most
#     established knowledge wins.
#   - closed_rca_infeasible: the cause is STRUCTURALLY unreachable — an
#     uncontrollable external dependency, an EOL system, a known intractable
#     condition — as declared by `problem_verification.rca_infeasible` WITH a
#     rationale. Distinct from insufficient evidence, which is contingent: there
#     the evidence may well exist and someone with more access could get it, so
#     it is a signal to improve observability. Here nothing can be improved and
#     the right artefact is a documented workaround; a future reader should know
#     not to re-open this expecting to find a cause. Ranked ABOVE
#     mitigation_sufficient because the engine's only path to it fires on
#     `mitigation_verified` (milestone_engine, "propose closure as stabilized
#     rather than push RCA"), so both hold on essentially every such close and
#     the more informative label must win — it already implies a verified
#     mitigation AND explains why RCA stopped.
#   - mitigation_sufficient: a verified mitigation on record; the symptom is
#     relieved and RCA was deferred BY CHOICE, with the cause still reachable if
#     anyone returns to it. This is the one closure in the set that is not a
#     failure of any kind, which is why it is not folded into a generic bucket.
#   - closed_restatement_held: the evidence DID support a cause, and the §7.1
#     restatement guard held every unsettled root because its statement never
#     added anything over the problem — so the case ended with a cause it could
#     not promote for want of a distinct MECHANISM, not for want of data
#     (#1195). Ranked immediately above the generic bucket, which it only ever
#     displaces: routing it there would head the closure summary "insufficient
#     evidence to establish the problem or its cause" over a case that gathered
#     enough, and would bucket it with genuine data walls in the flywheel.
#   - closed_insufficient_evidence: the default for any other close from
#     INVESTIGATING — what was needed was never established, whether at the
#     SYMPTOM level (the problem could not be verified) or at the CAUSE level
#     (verified, but no cause could be grounded). Capture-on-close only: the
#     honest partial (residual candidates + the specific unmet/unobtainable
#     need, already persisted on the case) is signal for calibration and the
#     flywheel. The engine never nudges toward this close (no SUGGEST_CLOSE);
#     see insufficient-evidence-handling.md §5.4.
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

VALID_CLOSURE_REASONS: set[str] = {
    "inquiry_only",
    "solution_deferred",
    "closed_rca_infeasible",
    "mitigation_sufficient",
    "closed_restatement_held",
    "closed_insufficient_evidence",
}
