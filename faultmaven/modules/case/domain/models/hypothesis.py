from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from .evidence import EvidenceStance

# ============================================================
# Hypothesis Models (Section 6)
# ============================================================


class HypothesisCategory(str, Enum):
    """
    Hypothesis categories for anchoring detection.

    If agent tests 4+ hypotheses in same category without validation,
    it is "anchored" and should try different category.
    """

    CODE = "code"
    """Code bugs, logic errors, null pointers, etc."""

    CONFIG = "config"
    """Configuration issues, misconfigurations, wrong settings"""

    ENVIRONMENT = "environment"
    """Environment issues, resource exhaustion, system limits"""

    NETWORK = "network"
    """Network issues, connectivity, latency, DNS"""

    DATA = "data"
    """Data quality issues, corruption, consistency problems"""

    DATABASE = "database"
    """Database performance, queries, indexes, connections"""

    HARDWARE = "hardware"
    """Hardware failures, disk issues, CPU/memory"""

    SECURITY = "security"
    """Security issues, authentication/authorization failures, access control"""

    EXTERNAL = "external"
    """External dependencies, third-party services"""

    HUMAN = "human"
    """Human errors, operational mistakes"""

    OTHER = "other"
    """Does not fit above categories"""


# This docstring is published: it becomes the schema ``description`` of
# HypothesisState in docs/reference/api/. Keep it consumer-facing. The gate it
# describes is ``core/investigation/problem_status.cause_work_accepted``.
class HypothesisState(str, Enum):
    """Hypothesis lifecycle state.

    A hypothesis is formed ACTIVE, and only once the case's reported problem
    has been verified by evidence.
    """

    ACTIVE = "active"
    """
    Currently being tested.
    Evidence is being gathered.
    """

    VALIDATED = "validated"
    """
    Evidence strongly supports hypothesis.
    Root cause identified.
    """

    REFUTED = "refuted"
    """
    Evidence contradicts hypothesis.
    Not the root cause.
    """

    INCONCLUSIVE = "inconclusive"
    """
    Evidence is ambiguous.
    Cannot determine if hypothesis is correct.
    """

    RETIRED = "retired"
    """
    No longer relevant.
    Investigation moved in different direction.
    """

    @property
    def is_terminal(self) -> bool:
        """True when this state takes the hypothesis out of the differential
        for good (``REFUTED``/``RETIRED``): terminal states are immutable from
        every write path — reviving that theory means opening a NEW hypothesis.
        Single owner of the terminal predicate; consumers route through this
        (or ``TERMINAL_HYPOTHESIS_STATES`` for set operations), never a
        re-spelled state pair.
        """
        return self in TERMINAL_HYPOTHESIS_STATES


TERMINAL_HYPOTHESIS_STATES: frozenset[HypothesisState] = frozenset(
    {HypothesisState.REFUTED, HypothesisState.RETIRED}
)
"""The terminal states backing ``HypothesisState.is_terminal`` — the only
place the pair is spelled."""


class HypothesisGenerationMode(str, Enum):
    """How hypothesis was generated"""

    OPPORTUNISTIC = "opportunistic"
    """
    Generated from strong correlation or obvious clue.
    Example: Deploy immediately preceded errors -> hypothesis: "Bug in new deploy"
    """

    SYSTEMATIC = "systematic"
    """
    Generated methodically when root cause unclear.
    Example: Generic slowness -> generate hypotheses for common causes
    """

    FORCED_ALTERNATIVE = "forced_alternative"
    """
    User requested alternative hypotheses.
    Example: User: "What else could it be?"
    """


class HypothesisEvidenceLink(BaseModel):
    """
    Many-to-many relationship between hypothesis and evidence.

    ONE evidence can have DIFFERENT stances for DIFFERENT hypotheses:
    - Evidence "Pool at 95%" -> STRONGLY_SUPPORTS "pool exhausted" hypothesis
    - Evidence "Pool at 95%" -> REFUTES "network latency" hypothesis
    - Evidence "Pool at 95%" -> IRRELEVANT to "memory leak" hypothesis

    Stored in hypothesis_evidence junction table.
    LLM evaluates evidence against ALL active hypotheses after submission.
    """

    hypothesis_id: str = Field(description="Hypothesis being evaluated")

    evidence_id: str = Field(description="Evidence being evaluated")

    stance: EvidenceStance = Field(
        description="How this evidence relates to THIS hypothesis (including IRRELEVANT)"
    )

    reasoning: str = Field(
        description="LLM's explanation of the relationship", max_length=1000
    )

    stance_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in the stance assessment (0.0-1.0). Use for granularity instead of STRONGLY_ variants.",
    )

    analyzed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this relationship was established",
    )


class Hypothesis(BaseModel):
    """
    Hypothesis for systematic root cause exploration.

    Philosophy: Hypotheses are OPTIONAL. Agent may:
    - Identify root cause directly from evidence (no hypotheses)
    - OR generate hypotheses for systematic testing (when unclear)
    """

    hypothesis_id: str = Field(
        default_factory=lambda: f"hyp_{uuid4().hex[:12]}",
        description="Unique hypothesis identifier",
        pattern=r"^hyp_[a-f0-9]{12}$",
    )

    statement: str = Field(
        description="Hypothesis statement (what we think caused the problem)",
        min_length=1,
        max_length=500,
    )

    @field_validator("statement", mode="after")
    @classmethod
    def _statement_not_empty(cls, v: str) -> str:
        """Mirror of the DB hypotheses_statement_not_empty CHECK: statement
        must not be whitespace-only. Pydantic ``min_length=1`` accepts a
        single space; the DB rejects it. Same rule, two layers — neither
        bypassable independently."""
        if not v.strip():
            raise ValueError("statement must not be whitespace-only")
        return v

    category: HypothesisCategory = Field(
        description="Hypothesis category (for anchoring detection)"
    )

    state: HypothesisState = Field(
        default=HypothesisState.ACTIVE, description="Current hypothesis state"
    )

    likelihood: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Estimated likelihood this hypothesis is correct (0.0-1.0)",
    )

    initial_likelihood: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Original likelihood when hypothesis was generated",
    )

    # ============================================================
    # Causal Chain (Two-Dimensional Hypothesis Methodology)
    # ------------------------------------------------------------
    # A hypothesis is a CHAIN: a root->D path through the case's causal graph
    # (Case.causal_nodes / causal_edges). These fields make it a chain header.
    # Phase 1 adds them additively; the engine populates and validates them in
    # Phase 2 (M3 checkpoint: a chain may not validate / carry a Solution until
    # it terminates in a root node). `statement` and `evidence_links` below are
    # transitional (flat-model fields) until the Phase-2 engine rewire retires
    # them in favor of per-node evidence (CausalNode.evidence_links).
    # See docs/.../two-dimensional-hypothesis-methodology.md §9.1.
    # ============================================================
    root_node_id: Optional[str] = Field(
        default=None,
        description=(
            "The chain's ROOT causal node (its candidate root cause). NULL while "
            "the chain is still being expanded backward toward a root (lazy "
            "expansion). When set, must equal path[0]."
        ),
    )

    path: List[str] = Field(
        default_factory=list,
        description=(
            "Ordered node_ids root -> ... -> D (the active problem). path[0] is "
            "the root node, path[-1] is the PROBLEM node. Empty until the chain "
            "is materialized."
        ),
    )

    # ============================================================
    # Evidence Relationships (Many-to-Many)
    # ============================================================
    evidence_links: List[HypothesisEvidenceLink] = Field(
        default_factory=list,
        description="""
        Relationship rows from the hypothesis_evidence junction table.

        ONE evidence can:
        - SUPPORTS hypothesis A
        - REFUTES hypothesis B
        - Be NEUTRAL to hypothesis C

        Each list entry binds (hypothesis_id, evidence_id, stance, reasoning,
        stance_confidence). LLM evaluates each evidence against ALL active
        hypotheses after submission.
        """,
    )

    # ============================================================
    # Metadata
    # ============================================================
    generated_at_turn: int = Field(
        ge=0, description="Turn number when hypothesis was generated"
    )

    last_updated_turn: int = Field(
        default=0, ge=0, description="Turn number when hypothesis was last updated"
    )

    last_progress_at_turn: int = Field(
        default=0, ge=0, description="Turn number when hypothesis last showed progress"
    )

    iterations_without_progress: int = Field(
        default=0, ge=0, description="Count of consecutive iterations without progress"
    )

    generation_mode: HypothesisGenerationMode = Field(
        description="How hypothesis was generated"
    )

    retirement_reason: Optional[str] = Field(
        default=None,
        description=(
            "Why the hypothesis was set aside without a verdict. Bounded to 200 "
            "characters by TRUNCATION (see the validator below), not by "
            "max_length: it is rendered into the terminal report, and the "
            "user-retire path writes the user's own message here, so an "
            "unbounded value reaches the report and the replayed conversation "
            "as arbitrary user text."
        ),
    )

    refutation_reason: Optional[str] = Field(
        default=None,
        max_length=200,
        description=(
            "Evidence or reasoning that disproves the hypothesis. "
            "REQUIRED when state=REFUTED (enforced via model validator). "
            "Not used for other statuses. state=REFUTED and refutation_reason "
            "travel together — an update carrying one without the other is "
            "rejected at the orchestration layer."
        ),
    )

    rationale: str = Field(
        description="Why this hypothesis was generated", max_length=1000
    )

    # ============================================================
    # Testing History
    # ============================================================
    tested_at: Optional[datetime] = Field(
        default=None, description="When hypothesis testing began"
    )

    concluded_at: Optional[datetime] = Field(
        default=None, description="When hypothesis was validated/refuted/retired"
    )

    # ============================================================
    # Computed Properties
    # ============================================================
    @property
    def supporting_evidence(self) -> List[str]:
        """Get evidence IDs that support this hypothesis"""

        return [
            link.evidence_id
            for link in self.evidence_links
            if link.stance == EvidenceStance.SUPPORTS
        ]

    @property
    def refuting_evidence(self) -> List[str]:
        """Get evidence IDs that refute this hypothesis"""

        return [
            link.evidence_id
            for link in self.evidence_links
            if link.stance == EvidenceStance.REFUTES
        ]

    @property
    def evidence_score(self) -> float:
        """
        Evidence balance score.
        Returns: -1.0 (all refuting) to 1.0 (all supporting)
        """
        total_support = len(self.supporting_evidence)
        total_refute = len(self.refuting_evidence)
        total = total_support + total_refute

        if total == 0:
            return 0.0

        return (total_support - total_refute) / total

    @model_validator(mode="after")
    def _validate_refutation_reason_pairs_with_state(self) -> "Hypothesis":
        """Pair integrity: state=REFUTED requires refutation_reason.

        The two fields travel together — a Hypothesis with state=REFUTED
        cannot exist in memory without a refutation_reason, and vice versa.
        RETIRED has its own ``retirement_reason`` field and is a distinct
        path (abandonment without disproof, no reason required).
        """
        if self.state == HypothesisState.REFUTED and not self.refutation_reason:
            raise ValueError(
                "refutation_reason is required when state=REFUTED. If there "
                "is no disproof evidence, use state=RETIRED instead."
            )
        if self.refutation_reason and self.state != HypothesisState.REFUTED:
            raise ValueError(
                "refutation_reason is only valid when state=REFUTED. "
                f"Current state is {self.state.value}."
            )
        return self

    @field_validator("retirement_reason", mode="before")
    @classmethod
    def _bound_retirement_reason(cls, v: Optional[str]) -> Optional[str]:
        """Truncate rather than reject — this validator also runs on LOAD.

        ``max_length`` here would be a HYDRATION constraint, not just a write
        one: both repositories build ``Hypothesis(**row)`` straight from the
        Text column inside ``get_case``'s blanket ``except`` that raises
        ``RepositoryException``, so a single over-length legacy row would make
        the whole CASE unloadable — not a truncated reason, a dead case. The
        user-retire path has written this field unbounded since 2026-04-09
        (``d10d34e1d``), so such rows can exist in any deployment. (
        ``refutation_reason`` can safely carry ``max_length`` because its only
        writer has always truncated at 200, so it never accumulated long rows.)

        Truncating on read keeps the invariant that matters — nothing over 200
        characters reaches the report or the replayed prompt — while letting
        legacy rows load. Dropping the bound instead would leave exactly the
        unbounded user text in the prompt that bounding this field exists to
        prevent.
        """
        if isinstance(v, str) and len(v) > 200:
            return v[:197] + "..."
        return v

    @model_validator(mode="after")
    def _root_heads_the_path(self) -> "Hypothesis":
        """Chain consistency: when both are set, the root node is the head of
        the root->D path (path[0]). Lenient by design — a chain mid-expansion
        may have root_node_id set with an empty path, or a path with no root
        yet; only an outright head/root mismatch is rejected."""
        if self.root_node_id and self.path and self.path[0] != self.root_node_id:
            raise ValueError(
                "root_node_id must equal path[0] (the chain runs root -> D)."
            )
        return self
