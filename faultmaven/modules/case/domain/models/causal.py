from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from .evidence import EvidenceStance
from .hypothesis import HypothesisCategory

# ============================================================
# Causal Graph Models (Two-Dimensional Hypothesis Methodology)
# ------------------------------------------------------------
# A hypothesis is a causal CHAIN, not a flat statement: an ordered path
# root -> intermediate states -> active problem (D). The case owns ONE causal
# DAG (nodes + edges); a Hypothesis is a named root->D path over it.
# See docs/architecture/investigation-engine/
#     two-dimensional-hypothesis-methodology.md
# ============================================================


class NodeType(str, Enum):
    """Role of a causal node on the chain (methodology §2)."""

    PROBLEM = "problem"
    """The active problem D — the shared terminal node every chain ends at.
    Exactly one per case; seeded from problem_verification.symptom_statement."""

    INTERMEDIATE = "intermediate"
    """An observed/observable effect between a cause and D. Never a root cause."""

    ROOT = "root"
    """A candidate root cause — the terminal node of a chain (M1/M3)."""


class NodeState(str, Enum):
    """Empirical status of a single causal node (methodology M4/M7)."""

    CANDIDATE = "candidate"
    """Posited but not yet empirically established."""

    VALIDATED = "validated"
    """Confirmed by empirical evidence (§7.1) or deduction over an exhaustive
    set (§7.1.1). Never set by assertion (M4)."""

    REFUTED = "refuted"
    """Contradicted by evidence (or, for a root, by counterfactual
    disconfirmation — a failed fix, M6)."""

    INCONCLUSIVE = "inconclusive"
    """Could not be proved or disproved (e.g., the rung is untestable, R5)."""


class ValidationMethod(str, Enum):
    """How a node reached VALIDATED (methodology M4)."""

    NONE = "none"
    """Not validated. The only legal method while node_state != VALIDATED."""

    EMPIRICAL = "empirical"
    """Direct observable facts matched the predicted state (§7.1)."""

    DEDUCTIVE = "deductive"
    """Proof by exclusion over a certified-exhaustive OR-set (§7.1.1)."""


class InterventionQuadrant(str, Enum):
    """Where × how-durable an intervention acts (methodology §7.4)."""

    REMEDIATION = "remediation"
    """Permanent fix at the root cause — the ideal; resolves the case."""

    DEFENSIVE_FIX = "defensive_fix"
    """Permanent fix at an intermediate node — durable, but does not address
    the upstream root (closes on symptom_absence)."""

    MITIGATION = "mitigation"
    """Temporary interception of an intermediate node to suppress D under
    current constraints (the mitigation insert)."""

    LOOP_BREAK = "loop_break"
    """Break a causal cycle when no static root exists (R9)."""


class NodeEvidenceLink(BaseModel):
    """Evidence attached to a specific causal NODE with a stance.

    Methodology §9.1: evidence links target nodes (rungs), not a whole chain —
    a SUPPORTS/REFUTES stance bears on the specific rung it tests, which is
    what makes step-by-step descent (S3) and AND-validation (S1) computable.
    """

    evidence_id: str = Field(description="Evidence bearing on this node")

    stance: EvidenceStance = Field(
        description="How this evidence relates to THIS node (supports/refutes/neutral)"
    )

    reasoning: str = Field(
        description="Explanation of the relationship", max_length=1000
    )

    stance_confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence in the stance assessment (0.0-1.0)",
    )

    linked_at_turn: int = Field(
        default=0, ge=0, description="Turn when the link was established"
    )

    analyzed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this relationship was established",
    )


class CausalNode(BaseModel):
    """A node in the case's causal graph (methodology §2, §3).

    A node is the active problem (PROBLEM), an intermediate state, or a
    candidate root cause (ROOT). Its node_state is established only by
    empirical/deductive validation (M4), never by assertion.
    """

    node_id: str = Field(
        default_factory=lambda: f"cn_{uuid4().hex[:12]}",
        description="Unique causal-node identifier",
        pattern=r"^cn_[a-f0-9]{12}$",
    )

    statement: str = Field(
        description="The state/cause this node asserts",
        min_length=1,
        max_length=500,
    )

    node_type: NodeType = Field(description="problem (D) / intermediate / root")

    node_state: NodeState = Field(
        default=NodeState.CANDIDATE, description="Empirical status of this node"
    )

    validation_method: ValidationMethod = Field(
        default=ValidationMethod.NONE,
        description="How the node reached VALIDATED (M4). NONE unless validated.",
    )

    belief: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Current belief this node holds (0.0-1.0); propagated by gate type",
    )

    signature_consistent: bool = Field(
        default=True,
        description=(
            "F3 signature screening: whether this node's mechanism could produce "
            "the observed signature of D. False = screened out at formation."
        ),
    )

    actionable: bool = Field(
        default=False,
        description=(
            "M1: a ROOT node must be actionable (a performable, independent "
            "remediation can be named) before it can be validated/confirmed."
        ),
    )

    category: Optional[HypothesisCategory] = Field(
        default=None, description="Failure family (for anchoring/diversity)"
    )

    state_epoch: int = Field(
        default=0,
        ge=0,
        description=(
            "M6/§7.3 contamination epoch — bumped when a state-mutating action "
            "may have invalidated evidence collected in a prior epoch."
        ),
    )

    evidence_links: List[NodeEvidenceLink] = Field(
        default_factory=list, description="Evidence bearing on this node"
    )

    generated_at_turn: int = Field(
        ge=0, description="Turn when this node was first posited"
    )
    last_updated_turn: int = Field(default=0, ge=0)
    last_progress_at_turn: int = Field(default=0, ge=0)
    iterations_without_progress: int = Field(
        default=0,
        ge=0,
        description=(
            "Stagnation counter for decay/anchoring. Advances ONLY on "
            "investigation turns where this node was eligible to progress and "
            "didn't (new evidence analyzed, a test result returned, a state "
            "transition attempted) — never on clarifying/awaiting-user turns "
            "(TurnOutcome.CONVERSATION). Engine-maintained in Phase 2; see "
            "methodology §6.1 'Decay counts investigation turns'."
        ),
    )

    refutation_reason: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Why the node was refuted. REQUIRED when node_state=REFUTED.",
    )

    rationale: Optional[str] = Field(
        default=None, max_length=1000, description="Why this node was posited"
    )

    metadata: Dict[str, Any] = Field(default_factory=dict)
    proposed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("statement", mode="after")
    @classmethod
    def _statement_not_empty(cls, v: str) -> str:
        """Mirror of the Hypothesis/DB statement-not-empty rule."""
        if not v.strip():
            raise ValueError("statement must not be whitespace-only")
        return v

    @model_validator(mode="after")
    def _refutation_reason_pairs_with_state(self) -> "CausalNode":
        """node_state=REFUTED and refutation_reason travel together
        (mirror of the Hypothesis rule)."""
        if self.node_state == NodeState.REFUTED and not self.refutation_reason:
            raise ValueError("refutation_reason is required when node_state=REFUTED.")
        if self.refutation_reason and self.node_state != NodeState.REFUTED:
            raise ValueError("refutation_reason is only valid when node_state=REFUTED.")
        return self

    @model_validator(mode="after")
    def _validated_requires_method(self) -> "CausalNode":
        """M4 (schema backstop): a node is VALIDATED only with an actual
        validation method — never by assertion. A VALIDATED node with
        method=NONE cannot exist in memory."""
        if (
            self.node_state == NodeState.VALIDATED
            and self.validation_method == ValidationMethod.NONE
        ):
            raise ValueError(
                "a VALIDATED node requires validation_method EMPIRICAL or "
                "DEDUCTIVE (M4: validation is never asserted)."
            )
        return self

    @model_validator(mode="after")
    def _validated_root_must_be_actionable(self) -> "CausalNode":
        """M1 (schema backstop): a ROOT node cannot be VALIDATED unless it is
        actionable (a performable remediation can be named)."""
        if (
            self.node_type == NodeType.ROOT
            and self.node_state == NodeState.VALIDATED
            and not self.actionable
        ):
            raise ValueError("a VALIDATED ROOT node must be actionable (M1).")
        return self


class CausalEdge(BaseModel):
    """A directed cause -> effect edge in the causal graph (methodology S1).

    cause_node_id produces effect_node_id. Edges sharing the same
    (effect_node_id, and_group) are co-necessary (an AND-set, M7); a null or
    distinct and_group denotes an independent (OR) alternative cause.
    """

    edge_id: str = Field(
        default_factory=lambda: f"ce_{uuid4().hex[:12]}",
        description="Unique causal-edge identifier",
        pattern=r"^ce_[a-f0-9]{12}$",
    )

    cause_node_id: str = Field(description="Upstream cause node")
    effect_node_id: str = Field(description="Downstream effect node (closer to D)")

    and_group: Optional[str] = Field(
        default=None,
        description=(
            "AND-set key (M7): edges with the same (effect_node_id, and_group) "
            "are co-necessary. Null/distinct = independent OR alternative cause."
        ),
    )

    reasoning: Optional[str] = Field(
        default=None, max_length=1000, description="Why this causal link holds"
    )

    created_at_turn: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _no_self_loop(self) -> "CausalEdge":
        if self.cause_node_id == self.effect_node_id:
            raise ValueError("a causal edge cannot connect a node to itself")
        return self
