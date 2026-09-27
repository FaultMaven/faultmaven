from datetime import datetime, timezone
from enum import Enum
from typing import List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from .causal import InterventionQuadrant

# ============================================================
# Solution Models (Section 7)
# ============================================================


class SolutionType(str, Enum):
    """Type of solution/mitigation"""

    ROLLBACK = "rollback"
    """Revert to previous version/state"""

    CONFIG_CHANGE = "config_change"
    """Modify configuration settings"""

    RESTART = "restart"
    """Restart service/component"""

    SCALING = "scaling"
    """Scale resources (increase/decrease)"""

    CODE_FIX = "code_fix"
    """Fix code bug (requires deployment)"""

    WORKAROUND = "workaround"
    """Temporary workaround (not root fix)"""

    INFRASTRUCTURE = "infrastructure"
    """Infrastructure changes (servers, networking, etc.)"""

    DATA_FIX = "data_fix"
    """Fix data corruption or inconsistency"""

    OTHER = "other"
    """Does not fit above categories"""


class Solution(BaseModel):
    """
    Proposed or applied solution/mitigation.
    """

    solution_id: str = Field(
        default_factory=lambda: f"sol_{uuid4().hex[:12]}",
        description="Unique solution identifier",
        pattern=r"^sol_[a-f0-9]{12}$",
    )

    # ============================================================
    # Solution Type
    # ============================================================
    solution_type: SolutionType = Field(description="Type of solution")

    # ============================================================
    # Causal-graph linkage (Two-Dimensional Hypothesis Methodology §7.4, §9.1)
    # ------------------------------------------------------------
    # Which causal node this intervention acts on, and which intervention
    # quadrant it occupies. Phase 1 adds these additively; the engine sets and
    # enforces them in Phase 2 (M5: a REMEDIATION solution requires its node's
    # root to be validated; mitigation/defensive interceptions are exempt).
    # ============================================================
    node_id: Optional[str] = Field(
        default=None,
        description="Causal node this solution remediates or intercepts",
    )

    quadrant: Optional[InterventionQuadrant] = Field(
        default=None,
        description=(
            "Intervention quadrant (§7.4): remediation (perm@root) / "
            "defensive_fix (perm@intermediate) / mitigation (temp@intermediate) "
            "/ loop_break."
        ),
    )

    # ============================================================
    # Solution Details
    # ============================================================
    title: str = Field(description="Short solution title", min_length=1, max_length=200)

    immediate_action: Optional[str] = Field(
        default=None,
        description="Quick fix or mitigation (temporary)",
        max_length=2000,
    )

    longterm_fix: Optional[str] = Field(
        default=None, description="Permanent solution (comprehensive)", max_length=2000
    )

    # ============================================================
    # Implementation
    # ============================================================
    implementation_steps: List[str] = Field(
        default_factory=list, description="Step-by-step implementation instructions"
    )

    commands: List[str] = Field(
        default_factory=list, description="Specific commands to execute"
    )

    risks: List[str] = Field(
        default_factory=list, description="Risks or side effects of this solution"
    )

    # ============================================================
    # Lifecycle
    # ============================================================
    proposed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When solution was proposed",
    )

    proposed_by: str = Field(
        default="agent", description="Who proposed: 'agent' or user_id"
    )

    applied_at: Optional[datetime] = Field(
        default=None, description="When solution was applied"
    )

    applied_by: Optional[str] = Field(
        default=None, description="Who applied the solution"
    )

    verified_at: Optional[datetime] = Field(
        default=None, description="When solution effectiveness was verified"
    )

    # ============================================================
    # Verification
    # ============================================================
    verification_method: Optional[str] = Field(
        default=None, description="How effectiveness was verified", max_length=500
    )

    verification_evidence_id: Optional[str] = Field(
        default=None, description="Evidence ID proving solution worked"
    )

    effectiveness: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="How well solution worked (0.0 = failed, 1.0 = perfect)",
    )

    # ============================================================
    # Validation
    # ============================================================
    @model_validator(mode="after")
    def solution_content_required(self):
        """Ensure solution has actionable content"""
        immediate = self.immediate_action
        longterm = self.longterm_fix
        steps = self.implementation_steps
        commands = self.commands

        if not any([immediate, longterm, steps, commands]):
            raise ValueError(
                "Solution must have at least one of: immediate_action, longterm_fix, implementation_steps, or commands"
            )

        return self

    @model_validator(mode="after")
    def verification_consistency(self):
        """Ensure verification fields are consistent"""
        verified_at = self.verified_at
        effectiveness = self.effectiveness

        if verified_at and effectiveness is None:
            raise ValueError("verified_at requires effectiveness score")

        if effectiveness is not None and not verified_at:
            raise ValueError("effectiveness requires verified_at")

        return self


# ============================================================
# Proposed Action Models (Evidence-Driven Framework)
# ============================================================


class InvestigationActionType(str, Enum):
    """Type of action proposed during investigation."""

    MITIGATION = "mitigation"
    """Temporary fix to stop the bleeding."""

    SOLUTION = "solution"
    """Permanent fix based on root cause analysis."""

    DIAGNOSTIC = "diagnostic"
    """Data collection or investigation action (does not trigger stage-gate milestones)."""


class ProposedAction(BaseModel):
    """
    A concrete action proposed by the agent for the user to execute.

    ProposedActions are the mechanism by which the agent communicates
    actionable next steps. User compliance with a proposed action
    triggers stage-gate milestone transitions via compliance detection.
    """

    action_id: str = Field(
        default_factory=lambda: f"act_{uuid4().hex[:12]}",
        description="Unique action identifier",
    )

    case_id: str = Field(description="Case this action belongs to")

    action_type: InvestigationActionType = Field(
        description="Whether this is a mitigation or solution action"
    )

    description: str = Field(
        description="Human-readable description of the proposed action",
        max_length=2000,
    )

    commands: List[str] = Field(
        default_factory=list,
        description="Specific commands for the user to execute",
    )

    proposed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the action was proposed",
    )

    proposed_in_turn: int = Field(
        description="Turn number when this action was proposed"
    )

    state: str = Field(
        default="pending",
        description="pending | accepted | rejected | superseded",
    )

    accepted_in_turn: Optional[int] = Field(
        default=None,
        description=(
            "Turn in which compliance detection observed the user EXECUTE this "
            "action (state='accepted'). Distinct from ``proposed_in_turn``, "
            "which is when the action was OFFERED — the user executes it a "
            "turn later in the ordinary flow (#987). Anything reasoning about "
            "what happened *after the fix* must key on this, not on the "
            "proposal turn, or pre-execution evidence from the offering turn "
            "reads as a post-fix outcome. None while pending/superseded, and "
            "on actions accepted before this field existed."
        ),
    )

    superseded_in_turn: Optional[int] = Field(
        default=None,
        description=(
            "Turn in which this action was superseded (state='superseded'). "
            "None while the action is live or if it left liveness another way."
        ),
    )

    superseded_reason: Optional[
        Literal["reproposal", "license_lost", "stale_pending"]
    ] = Field(
        default=None,
        description=(
            "Why the engine superseded this action: 'reproposal' (a newer "
            "SOLUTION offer replaced it) or 'license_lost' (the established-"
            "cause license that admitted the offer fell — demotion, "
            "retraction, MECE hold, or proxy decay) — both SOLUTION-only and "
            "feeding the solution_offer_superseded_total metric label; or "
            "'stale_pending' (a shadowed DIAGNOSTIC ask retired when a "
            "SOLUTION it predated left pending state — withdrawn or accepted — "
            "so it cannot resurface in <pending_action>; feeds "
            "pending_action_superseded_stale_total instead; MITIGATION is "
            "cause-independent and never retired this way). Closed "
            "vocabulary keeps metric-label cardinality bounded. Forensic "
            "field; the context builder renders only pending actions."
        ),
    )

    downgrade_reason: Optional[str] = Field(
        default=None,
        description=(
            "If the engine downgraded action_type from the LLM's intent "
            "(e.g. MITIGATION → DIAGNOSTIC because no SYMPTOM_EVIDENCE "
            "existed yet), this carries the explanation. Rendered to the "
            "LLM via context_builder on the next turn so the agent can "
            "recover (gather the missing evidence and re-propose). None "
            "when no downgrade occurred."
        ),
        max_length=500,
    )

    @field_validator("state")
    @classmethod
    def valid_action_state(cls, v):
        allowed = ["pending", "accepted", "rejected", "superseded"]
        if v not in allowed:
            raise ValueError(f"state must be one of: {allowed}")
        return v


class ActionAttempt(BaseModel):
    """
    Records a user's attempt to execute a ProposedAction.

    When the user submits results after executing (or attempting to execute)
    a proposed action, an ActionAttempt is created. Compliance detection
    analyzes the attempt to determine if stage-gate milestones should be set.
    """

    attempt_id: str = Field(
        default_factory=lambda: f"att_{uuid4().hex[:12]}",
        description="Unique attempt identifier",
    )

    action_id: str = Field(description="ProposedAction this attempt relates to")

    user_message: str = Field(
        description="The user's message containing attempt results",
        max_length=10000,
    )

    submitted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the attempt was submitted",
    )

    compliance_detected: bool = Field(
        default=False,
        description="Whether the user appears to have executed the proposed action",
    )

    compliance_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence that user complied with the proposed action",
    )


class SolutionOutcome(str, Enum):
    """Outcome of a proposed ``Solution``, derived from the compliance chain.

    The engine never stamps ``applied_at``/``verified_at``/``effectiveness`` on a
    ``Solution`` — those per-solution fields carry no signal. The live signal is the
    ``ProposedAction`` a solution is co-created with (same proposal, identical
    ``description``/``commands``): its ``state`` and ``action_type`` record whether
    the user *executed* the fix. Used at the runbook-conversion boundary so a
    never-run or engine-refused fix's commands don't reach a runbook's remediation
    slot.

    Note the deliberate limit: ``accepted`` means the user *executed* the fix, not
    that it *worked* (that is ``solution_verified`` / the resolution, which is
    case-level, not per-action). The engine records no per-fix success signal, and
    an executed fix that failed is indistinguishable from one step of a compound
    remediation (both are ``accepted``, neither is superseded). So this enum
    distinguishes **executed** from **never-executed/refused** — it does not, and
    cannot, single out which executed fix was decisive.
    """

    APPLIED = "applied"
    """The user executed this fix — an accepted actionable action
    (SOLUTION/MITIGATION, not a downgraded DIAGNOSTIC). Surfaced as remediation
    material; makes no claim that this specific fix alone resolved the case."""

    FAILED = "failed"
    """Never executed or refused: the action was superseded/rejected (replaced while
    pending, never run) OR the engine downgraded it to DIAGNOSTIC (refused as a fix).
    Its commands must not become reusable knowledge."""

    PROPOSED = "proposed"
    """No resolved matching action (still pending or uncorrelated) — surfaced, but
    flagged as unconfirmed rather than as an executed fix."""


def _action_type_value(action) -> Optional[str]:
    """The action's ``InvestigationActionType`` value (``"solution"`` /
    ``"mitigation"`` / ``"diagnostic"``), or None if absent. Enum or raw string or
    missing all read the same, so stub actions without an ``action_type`` are
    treated as un-typed (neither solution nor diagnostic)."""
    at = getattr(action, "action_type", None)
    return getattr(at, "value", at)


def classify_solution_outcome(
    solution: "Solution", proposed_actions: List["ProposedAction"]
) -> SolutionOutcome:
    """Classify a proposed ``Solution`` by the outcome of its ``ProposedAction``.

    A ``Solution`` and its ``ProposedAction`` are created together from one LLM
    proposal, so they carry the same ``description`` (``Solution.immediate_action``)
    and ``commands``. We correlate on that content rather than on the Solution's own
    lifecycle fields, which the engine never populates.

    - ``APPLIED``  — a matching action is ``accepted`` (the user executed it) and
      actionable (not a downgraded DIAGNOSTIC). Biases to inclusion: any accepted
      actionable match wins even if a same-content sibling was superseded.
    - ``FAILED``   — matching action(s) exist but none was executed as a fix: every
      one is superseded/rejected (never run) or an engine-downgraded DIAGNOSTIC.
    - ``PROPOSED`` — no matching resolved action (pending or uncorrelated); the
      safe default so an un-instrumented solution is surfaced, not dropped.

    We deliberately do NOT demote an earlier executed fix just because a later
    SOLUTION exists: an accepted fix that failed is indistinguishable from one step
    of a compound remediation, so inferring failure from ordering would wrongly drop
    real remediation (and could block a legitimate conversion). Excluding only the
    unambiguous never-executed/refused cases keeps the guarantee one-directional —
    we never launder a never-run fix in, and never drop a fix the user actually ran.

    Duck-typed on ``solution``/``proposed_actions`` so stub cases (no
    ``proposed_actions``) classify everything ``PROPOSED`` — the pre-existing
    "surface every solution" behavior.
    """
    desc = getattr(solution, "immediate_action", None)
    commands = list(getattr(solution, "commands", None) or [])
    # Nothing to correlate on — treat as an unconfirmed proposal (surfaced).
    if not desc and not commands:
        return SolutionOutcome.PROPOSED

    saw_terminal = False
    for action in proposed_actions or []:
        if getattr(action, "description", None) != desc:
            continue
        if list(getattr(action, "commands", None) or []) != commands:
            continue
        state = getattr(action, "state", None)
        if state == "accepted":
            if _action_type_value(action) == InvestigationActionType.DIAGNOSTIC.value:
                # Engine refused this as a fix (M5/3D downgrade) — not remediation.
                saw_terminal = True
                continue
            return SolutionOutcome.APPLIED
        if state in ("superseded", "rejected"):
            saw_terminal = True
    return SolutionOutcome.FAILED if saw_terminal else SolutionOutcome.PROPOSED
