"""Investigation Schemas for Structured Output

This module defines the Pydantic models used for LLM structured output generation.
It implements the "Form-Filler" pattern where the LLM returns a structured state update
along with its natural language response.

Design Reference:
- docs/architecture/investigation-engine/agent-behavioral-rules.md
- docs/architecture/investigation-engine/prompt-templates.md
- Investigation Architecture v2.0

Key Features:
- Stage-specific schemas (Dynamic Views) to minimize token usage
- Strict validation fields
- Integration with Case domain models
- Optional[List[T]] pattern for all list fields to handle LLM returning null

IMPORTANT: List Field Pattern (Fix for Turn 3 Pydantic Validation Errors)
===========================================================================
All list fields use the pattern: Optional[List[T]] = Field(default_factory=list)

This handles LLM behavior where empty lists are often returned as null instead of [].
The pattern provides:
- Accept null: Optional[List[T]] allows None values
- Accept []: Direct empty list accepted
- Accept ["item"]: Direct list with items accepted
- Default to []: default_factory=list ensures None becomes []

Without this pattern, LLM returning null causes Pydantic validation errors:
  "Input should be a valid array [type=list_type, input_value=None]"

Applied to 23 list fields across all schemas (see git blame for specific changes).
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from faultmaven.modules.agent.domain.models.agentic import QueryIntent  # noqa: F401
from faultmaven.modules.case.contracts import (
    ConfidenceLevel,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    HypothesisCategory,
    HypothesisState,
    InvestigationStage,
    NeedPriority,
    NeedPurpose,
    NeedState,
    NodeType,
    SolutionType,
    TurnOutcome,
)

# =============================================================================
# Unified Ingestion Pipeline (v4.1)
# =============================================================================


@dataclass
class Attachment:
    """A file or pasted data submitted with a turn."""

    content: bytes
    filename: str
    content_type: str
    source_metadata: Optional[Dict[str, Any]] = None


@dataclass
class TurnPayload:
    """Universal turn payload — canonical input to the investigation pipeline.

    Every user turn is represented as an optional query + optional attachments.
    At least one must be provided. If attachments are present, they are preprocessed
    through Tier 0+1 before the LLM sees them. If no query is provided, an implicit
    system query is injected.
    """

    query: Optional[str] = None
    attachments: List[Attachment] = field(default_factory=list)
    intent: Optional["QueryIntent"] = None

    @property
    def has_attachments(self) -> bool:
        return len(self.attachments) > 0

    @property
    def has_query(self) -> bool:
        return self.query is not None and self.query.strip() != ""


# =============================================================================
# Shared Components
# =============================================================================


class ReasoningConclusion(BaseModel):
    """Single reasoning step in the internal analysis."""

    observation: str = Field(description="What was observed in the evidence")
    inference: str = Field(description="What this implies about the problem")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence in this inference"
    )


class InternalReasoning(BaseModel):
    """
    Internal reasoning that must be completed BEFORE state_updates.
    Forces LLM to justify decisions with evidence trail.

    Reference: Prompt Engineering Guide Section 13
    """

    evidence_analyzed: Optional[List[str]] = Field(
        default_factory=list,
        description="Evidence IDs that were considered in this turn",
    )
    conclusions: Optional[List[ReasoningConclusion]] = Field(
        default_factory=list,
        description="Step-by-step reasoning from evidence to conclusions",
    )
    milestone_justifications: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "MANDATORY: For EVERY milestone set to True in milestones, provide a justification here. "
            "Format: {milestone_name: 'justification citing specific evidence IDs'}. "
            "Example: {'symptom_verified': 'Connection errors confirmed per ev_abc123'}. "
            "DO NOT leave empty {} when completing milestones - validation will reject."
        ),
    )
    uncertainties: Optional[List[str]] = Field(
        default_factory=list,
        description="What remains unclear or uncertain after this analysis",
    )


class ProblemConfirmation(BaseModel):
    """Agent's initial understanding of the problem."""

    problem_type: Literal["error", "slowness", "unavailability", "data_issue", "other"]
    severity_guess: Literal["critical", "high", "medium", "low", "unknown"]
    preliminary_guidance: Optional[str] = None


class PreliminaryUrgency(BaseModel):
    """Early urgency signal based on BUSINESS IMPACT."""

    level: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    is_ongoing: bool
    is_incident_report: bool = Field(
        default=False,
        description=(
            "True ONLY when the user is actively reporting a current problem "
            "affecting their systems. False for informational/how-to questions, "
            "even if the topic involves failures or outages."
        ),
    )
    impact_assessment: str


class KnowledgeMatch(BaseModel):
    """Knowledge base match for potential instant resolution."""

    match_type: Literal["past_case", "runbook", "documentation"]
    match_likelihood: float = Field(ge=0.0, le=1.0)
    match_summary: str
    suggested_solution: Optional[str] = None


class KnowledgeResolution(BaseModel):
    """Records instant resolution via KB match."""

    match_id: str
    match_type: Literal["past_case", "runbook", "documentation"]
    solution_applied: str
    user_confirmation: str


# v3 KB-Resolution schemas (CauseChunk, IndicatorResult, CauseMatch,
# CauseMatchResult) live in `cause_schemas.py` as a leaf module — kept out of
# this file so `agent.tools.kb_qa` can import them without transiting through
# the `QueryIntent` re-export at line 41 above (which would violate the
# Agent module layer contract in .importlinter).


class MilestoneUpdates(BaseModel):
    """Milestones the LLM can set to True (never False).

    Two categories:

    1. PROGRESS INDICATORS — provide context and analytics, do not drive
       stage transitions.
    2. STAGE-GATE MILESTONES — drive stage transitions when set. The LLM is
       the compliance detector: it sets these in structured output when it
       recognizes that the user has complied with a ProposedAction
       (Framework doc §4.1).

    NOTE: solution_verified is NOT settable here. It requires the User-Agent
    Handshake pattern via ProposedTransition.
    """

    # Progress indicators (LLM-settable, non-stage-driving)
    symptom_verified: Optional[bool] = None
    root_cause_identified: Optional[bool] = None
    root_cause_likelihood: Optional[float] = Field(None, ge=0.0, le=1.0)
    # solution_proposed removed (3F) — set programmatically at ProposedAction creation
    root_cause_method: Optional[
        Literal[
            "direct_analysis",
            "hypothesis_validation",
            "single_shot_validation",
            "correlation",
            "user_provided",
            "other",
        ]
    ] = Field(
        None,
        description="How root cause was identified: direct_analysis | hypothesis_validation | single_shot_validation | correlation | user_provided | other",
    )

    # Implementation feasibility of the SELECTED solution (assessment signal).
    # Set to "deferred" when the cause + fix are known but the fix cannot be
    # applied/verified during this session (e.g. it needs an out-of-band change
    # request, a maintenance window, or another team). "deferred" routes the
    # case to CLOSE-with-documented-solution (redesign §3.1 row 3 / §6 Q2).
    solution_feasible: Optional[Literal["now", "deferred"]] = Field(
        None,
        description=(
            "Whether the selected solution can be applied this session: 'now' "
            "(default) or 'deferred' (known fix, but implementation happens "
            "out-of-band / takes time). 'deferred' means the case should be "
            "closed with the solution documented, not held open waiting."
        ),
    )

    # Stage-gate milestones (LLM-settable, drive stage transitions)
    # The LLM sets these when it detects user compliance with a pending
    # ProposedAction — the user's action is the trigger, the LLM recognizes it.
    mitigation_accepted: Optional[bool] = None
    mitigation_verified: Optional[bool] = None
    solution_accepted: Optional[bool] = None


class ProblemVerificationUpdate(BaseModel):
    """Updates to problem verification data."""

    symptom_correction: Optional[str] = None
    scope_impact: Optional[str] = None
    timeline_start: Optional[str] = None
    timeline_duration: Optional[str] = None
    changes_list: Optional[List[str]] = Field(default_factory=list)
    rca_infeasible: Optional[bool] = None
    rca_infeasible_rationale: Optional[str] = None


class EvidenceToAdd(BaseModel):
    """A claim-anchored evidence record the LLM is creating this turn.

    Post-010 strict evidence model: every evidence row is the LLM's
    deliberate decision to record a focused, claim-relevant extract.
    Files are data (``uploaded_files``); evidence is the verbatim
    slice that supports a specific claim.

    Source rule (mirrors the ``evidence_source_invariant`` DB CHECK):
    - If the extract was drawn from an uploaded file → set
      ``source_file_id`` to that file's id (visible in the
      ``<evidence file_id="...">`` and ``<uploaded_file file_id="...">``
      attributes in the prompt context).
    - If the extract was a verbatim system-output quote the user
      typed in their short chat message → leave ``source_file_id``
      ``None`` and set ``source_type=USER_DESCRIPTION``.
    - Every other source_type requires a file_id; the system rejects
      the row otherwise.

    Milestone Attribution (Option 2.5 — Three-Tier Logic):

    Tier 1: MilestoneUpdates drives milestone state (turn-level, LLM specifies)
    Tier 2: System infers advances_milestones from category (automatic, handles 90%)
    Tier 3: LLM overrides via advances_milestones field (optional, handles 10%)

    The advances_milestones field is OPTIONAL. If omitted, the system
    will automatically infer milestone attribution based on the
    evidence category and milestones completed this turn using
    CATEGORY_MILESTONE_MAP.
    """

    summary: str
    extract: Optional[str] = Field(
        default=None,
        description=(
            "Optional verbatim quote drawn from the source. For "
            "file-backed evidence: a focused slice (one or a few lines) "
            "of system output the file contains. For chat-extracted "
            "evidence (source_type=USER_DESCRIPTION): the verbatim "
            "system-output snippet the user typed into chat. Leave "
            "empty when the summary is self-contained. Do NOT put "
            "the user's own description or paraphrase here — the rule "
            "is 'extract is system output, not user words'."
        ),
    )
    category: EvidenceCategory
    source_type: EvidenceSourceType
    source_file_id: Optional[str] = Field(
        default=None,
        description=(
            "FK to the uploaded_files row this extract came from. "
            "REQUIRED unless source_type=USER_DESCRIPTION (the "
            "chat-quote case). Copy the file_id verbatim from the "
            '<evidence file_id="..."> or <uploaded_file file_id="..."> '
            "attributes in the prompt context. Leaving this blank for "
            "a non-USER_DESCRIPTION extract causes the row to be "
            "rejected by the evidence_source_invariant DB CHECK."
        ),
    )
    likelihood: float = Field(0.8, ge=0.0, le=1.0)

    @field_validator("summary", mode="before")
    @classmethod
    def truncate_summary(cls, v):
        # Domain Evidence.content has no length cap, but the summary is the
        # short-form description used for UI. Verbose providers (e.g. DeepSeek
        # on logs-zookeeper) overshoot 500; truncate softly so the turn does
        # not 500. Same graceful-degrade pattern as the binary-decode
        # placeholder. See ISS-057.
        if isinstance(v, str) and len(v) > 500:
            return v[:489] + " [...trunc]"
        return v

    advances_milestones: Optional[List[str]] = Field(
        default=None,
        description=(
            "OPTIONAL: Override system-inferred milestone attribution. "
            "If omitted, system infers from category + milestones completed this turn. "
            "Only specify when automatic inference is incorrect (rare edge case)."
        ),
    )

    @field_validator("category", mode="before")
    @classmethod
    def validate_category(cls, v):
        """Strict category validation.

        Unrecognized categories raise ``ValidationError`` and fail the
        turn — no automatic LLM retry (``LLMErrorHandler`` classifies
        the error as ``UNKNOWN_ERROR`` and returns ``ErrorAction.FAIL``
        on the first attempt). The strict enum constraint is enforced
        at decode time by STRICT / FUNCTION_CALLING modes, so in
        practice this path is rarely hit. Valid values (the verification
        quartet): ``symptom_evidence`` / ``causal_evidence`` /
        ``symptom_absence_evidence`` / ``causal_absence_evidence``.
        """
        if isinstance(v, str):
            return EvidenceCategory(v)
        return v

    @field_validator("likelihood")
    @classmethod
    def validate_likelihood(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @model_validator(mode="after")
    def _source_file_required_unless_user_description(self) -> "EvidenceToAdd":
        """Mirror of the DB ``evidence_source_invariant`` CHECK: every
        evidence row has a known source.

        - ``source_file_id`` set → extract came from a file (any
          source_type is valid)
        - ``source_file_id`` None → only legal when
          ``source_type == USER_DESCRIPTION`` (the chat-quote case)

        Cross-layer defense: same rule applies in the ``Evidence``
        domain model and at the DB level. Catching the violation here
        gives the LLM a clear validation error instead of an opaque
        IntegrityError downstream.
        """
        if (
            self.source_file_id is None
            and self.source_type != EvidenceSourceType.USER_DESCRIPTION
        ):
            raise ValueError(
                "evidence_to_add.source_file_id is required unless "
                "source_type=USER_DESCRIPTION; the LLM should copy the "
                'file_id from the <evidence file_id="..."> or '
                '<uploaded_file file_id="..."> attribute in the prompt '
                "context. Leave source_file_id blank only when the "
                "extract is a verbatim quote from the user's short chat "
                "message (USER_DESCRIPTION)."
            )
        return self


class HypothesisToAdd(BaseModel):
    """New hypothesis to track."""

    statement: str
    category: HypothesisCategory
    likelihood: float = Field(ge=0.0, le=1.0)
    rationale: str
    root_node_ref: Optional[str] = Field(
        default=None,
        description=(
            "Chain mode: the causal node that is this hypothesis's ROOT cause — "
            "an existing node id (cn_...) or 'new_index_N' of a node in "
            "causal_nodes_to_add. When the CAUSAL CHAINS instructions are active, "
            "set this for EVERY hypothesis; it links the hypothesis to its root→D "
            "chain."
        ),
    )


class HypothesisUpdate(BaseModel):
    """Update to an existing hypothesis.

    Pair integrity: when ``status`` is set to ``REFUTED``, ``refutation_reason``
    MUST also be provided (max 200 chars). The orchestration layer rejects
    updates that carry one without the other. If there is no disproof
    evidence, use ``state=RETIRED`` instead (no reason required).
    """

    likelihood: Optional[float] = Field(None, ge=0.0, le=1.0)
    state: Optional[HypothesisState] = None
    refutation_reason: Optional[str] = Field(
        default=None,
        max_length=200,
        description=(
            "Required when setting state=REFUTED. Cite the specific evidence "
            "or reasoning that disproves the hypothesis. Not used for other "
            "statuses. state=REFUTED and refutation_reason travel together."
        ),
    )
    root_node_ref: Optional[str] = Field(
        default=None,
        description=(
            "Chain mode: RE-ROOT this existing hypothesis onto a causal node — an "
            "existing node id (cn_...) or 'new_index_N' of a node in "
            "causal_nodes_to_add. When the CAUSAL CHAINS instructions are active and "
            "you deepen a hypothesis's cause, point it at the chain's deepest ROOT so "
            "it leaves its placeholder root. Not for REFUTED entries."
        ),
    )

    @field_validator("state", mode="before")
    @classmethod
    def validate_status(cls, v):
        """
        Validate hypothesis state against HypothesisState enum.

        Maps known LLM aliases (e.g., "TESTING" -> ACTIVE) and rejects
        unrecognized values by returning None (no status update).
        """
        if v is None:
            return None
        if isinstance(v, HypothesisState):
            return v
        if isinstance(v, str):
            # Map known LLM aliases to valid enum values
            aliases = {"TESTING": "active", "testing": "active"}
            mapped = aliases.get(v, v.lower())
            try:
                return HypothesisState(mapped)
            except ValueError:
                import logging

                logger = logging.getLogger(__name__)
                logger.warning(
                    f"LLM returned unrecognized hypothesis state '{v}', "
                    f"ignoring status update",
                    extra={
                        "status_attempted": v,
                        "alert_team": "llm_integration",
                        "severity": "warning",
                        "metric": "hypothesis.status_fallback",
                    },
                )
                return None
        return v


class HypothesisEvidenceLinkToAdd(BaseModel):
    """Link evidence to a hypothesis."""

    hypothesis_id_ref: str = Field(
        description=(
            "Hypothesis ID string (format: hyp_xxxxxxxxxxxx) or "
            "'new_index_N' if created this turn. MUST be a string — "
            "do not emit a bare integer."
        )
    )
    evidence_id_ref: str = Field(
        description=(
            "Evidence ID string (format: ev_xxxxxxxxxxxx) or "
            "'new_index_N' if created this turn. MUST be a string — "
            "do not emit a bare integer."
        )
    )
    stance: EvidenceStance
    reasoning: str
    stance_confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence in the stance assessment (0.0-1.0)",
    )

    @field_validator("hypothesis_id_ref", "evidence_id_ref", mode="before")
    @classmethod
    def _coerce_bare_int_to_new_index(cls, v):
        """Coerce bare integers to ``new_index_N`` form.

        Gemini's function-calling tool spec is looser on type enforcement
        than its response_schema mode — bare integers can slip through
        where the schema declares ``str``. The schema description names
        two valid string forms (existing ID or ``new_index_N``); a bare
        integer ``N`` almost certainly means "the Nth new item this turn"
        because existing IDs always have ``hyp_xxxxxxxxxxxx`` /
        ``ev_xxxxxxxxxxxx`` prefixes. Coerce at the boundary so a 500
        becomes a normal new-index reference; downstream resolution will
        either find or fail-soft as it would for any new_index lookup.

        Note: this is a tactical patch for a specific shape failure
        variant (int-where-string on ID-ref fields). The strategic fix
        — tightening Gemini's function-calling schema enforcement so
        it rejects the wrong type at decode time — is tracked separately
        (see PR description).
        """
        if isinstance(v, int) and not isinstance(v, bool):
            return f"new_index_{v}"
        return v


# Chain-emission contract (Two-Dimensional Hypothesis Methodology §5/§9.1).
# Carried to the LLM via the ``causal_nodes_to_add`` / ``causal_edges_to_add`` /
# ``node_evidence_links`` fields on the investigation ``state_updates`` and
# consumed by ``causal_graph.ingest_emitted_chain``. Both the prompt block that
# teaches them and the engine ingestion are gated on the
# ``enable_hypothesis_chain_emission`` flag (off by default).
class CausalNodeToAdd(BaseModel):
    """A causal node emitted during lazy backward expansion (methodology §5/S3).

    The lazy primitive is "this state directly produces <a node closer to the
    problem>", so a node carries the single downstream edge it creates via
    ``produces``. The PROBLEM node ``D`` is engine-seeded, never emitted here.
    """

    statement: str
    node_type: NodeType = Field(
        description="'root' (a candidate root cause) or 'intermediate' (a state on the way to D)."
    )
    produces: Optional[str] = Field(
        default=None,
        description=(
            "The node this directly causes (one step closer to the problem): an "
            "existing node id (cn_...), 'D' for the problem node, or 'new_index_N' "
            "referencing another node in this same causal_nodes_to_add list."
        ),
    )
    and_group: Optional[str] = Field(
        default=None,
        description=(
            "If this node is co-necessary with other causes of the same `produces` "
            "(an AND-set, M7), a shared key marking the group; null/distinct = an "
            "independent (OR) alternative cause."
        ),
    )


class CausalEdgeToAdd(BaseModel):
    """An explicit cause→effect edge, for convergence (S2) or links beyond a
    node's own ``produces``."""

    cause: str = Field(
        description="Cause node ref: existing id, 'D', or 'new_index_N'."
    )
    effect: str = Field(
        description="Effect node ref: existing id, 'D', or 'new_index_N'."
    )
    and_group: Optional[str] = Field(default=None, description="AND-set key (M7).")
    reasoning: Optional[str] = Field(default=None)


class NodeEvidenceLinkToAdd(BaseModel):
    """Link evidence to a specific causal NODE (rung-level), not the whole chain
    — what makes step-by-step descent and AND-validation computable (§9.1)."""

    node_ref: str = Field(description="Node ref: existing id, 'D', or 'new_index_N'.")
    evidence_id_ref: str = Field(
        description="Evidence ID (ev_...) or 'new_index_N' if created this turn."
    )
    stance: EvidenceStance
    reasoning: str
    stance_confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class EvidenceNeedUpdate(BaseModel):
    """LLM-emitted: create a new evidence need OR update an existing one.

    Evidence needs are the demand-side counterpart to evidence rows —
    a persistent registry of "what data would advance this case." See
    docs/architecture/investigation-engine/evidence-needs-design.md.

    On create (``need_id`` is None): ``purpose`` is required, ``status``
    must be ``None`` or ``"pending"``, and the engine assigns a fresh
    ``eneed_<uuid>`` ID.

    On update (``need_id`` is set): the referenced need is updated.
    ``purpose`` is immutable on the update path; lists are merged
    (append-only) by the engine.

    ``new_index_N`` references: ``need_id`` (same-turn re-update),
    ``motivating_hypothesis_ids`` (refs to ``hypotheses_to_add``), and
    ``fulfilling_evidence_ids`` (refs to ``evidence_to_add``) all accept
    ``new_index_N`` placeholders that the engine resolves via
    ``_resolve_id_ref`` at apply time. Bare integers are coerced at
    schema-validation time, mirroring ``HypothesisEvidenceLinkToAdd``.
    """

    need_id: Optional[str] = Field(
        default=None,
        description=(
            "Set to update an existing need; omit (or set None) to "
            "create a new one. Accepts a real ``eneed_xxxxxxxxxxxx`` "
            "ID, a ``new_index_N`` placeholder referencing a need "
            "created earlier in this same evidence_need_updates list, "
            "or a bare integer (coerced)."
        ),
    )
    purpose: Optional[NeedPurpose] = Field(
        default=None,
        description=(
            "Why this need exists: ``symptom_verification`` (motivated "
            "by the problem statement) or ``causal_verification`` "
            "(motivated by one or more hypotheses). REQUIRED on create "
            "(``need_id`` is None); OMIT on update — it is immutable on "
            "the update path, so resending it is unnecessary."
        ),
    )
    request_text: Optional[str] = Field(
        default=None,
        max_length=500,
        description=(
            "What data would fulfill this need. REQUIRED on create; "
            "OMIT on update unless revising the text (None leaves the "
            "existing text unchanged)."
        ),
    )
    rationale: Optional[str] = Field(
        default=None,
        max_length=500,
        description=(
            "Why this data would advance the investigation. REQUIRED on "
            "create; OMIT on update unless revising (None leaves the "
            "existing rationale unchanged)."
        ),
    )
    priority: Optional[NeedPriority] = Field(
        default=None,
        description=(
            "Surfacing-order hint for EVIDENCE-type suggestions. "
            "Defaults to MEDIUM on create; OMIT on update to preserve "
            "the existing priority (None leaves it unchanged — sending "
            "a value here would otherwise clobber the stored priority)."
        ),
    )
    motivating_hypothesis_ids: Optional[List[str]] = Field(
        default_factory=list,
        description=(
            "Hypotheses that motivate this need. Each entry accepts a "
            "real ``hyp_xxxxxxxxxxxx`` ID, a ``new_index_N`` "
            "placeholder referencing an entry in ``hypotheses_to_add`` "
            "this turn, or a bare integer (coerced). Empty list means "
            "motivated by the problem statement (symptom needs)."
        ),
    )
    state: Optional[NeedState] = Field(
        default=None,
        description=(
            "Set when updating; omit for create (engine defaults to "
            "PENDING). FULFILLED requires non-empty "
            "``fulfilling_evidence_ids``; SUPERSEDED requires "
            "non-empty ``superseded_reason``."
        ),
    )
    fulfilling_evidence_ids: Optional[List[str]] = Field(
        default_factory=list,
        description=(
            "Evidence rows that fulfill this need. Each entry accepts a "
            "real ``ev_xxxxxxxxxxxx`` ID, a ``new_index_N`` placeholder "
            "referencing an entry in ``evidence_to_add`` this turn, or "
            "a bare integer (coerced). Appended on update."
        ),
    )
    superseded_reason: Optional[str] = Field(
        default=None,
        max_length=500,
        description=("Required when ``state=SUPERSEDED``; must be None " "otherwise."),
    )

    @field_validator(
        "need_id",
        "motivating_hypothesis_ids",
        "fulfilling_evidence_ids",
        mode="before",
    )
    @classmethod
    def _coerce_bare_int_to_new_index(cls, v):
        """Coerce bare integers to ``new_index_N`` form. Same shape as
        ``HypothesisEvidenceLinkToAdd._coerce_bare_int_to_new_index``
        (PR #354). Handles both scalar and list inputs."""

        def _coerce_one(item):
            if isinstance(item, int) and not isinstance(item, bool):
                return f"new_index_{item}"
            return item

        if isinstance(v, list):
            return [_coerce_one(item) for item in v]
        return _coerce_one(v)

    @model_validator(mode="after")
    def _validate_create_vs_update_semantics(self) -> "EvidenceNeedUpdate":
        """Cross-field invariants.

        Create path (``need_id is None``):
        - ``purpose``, ``request_text``, and ``rationale`` are required
          (they are Optional on the field for the update path, where
          they are omitted; on create they must be present).
        - ``status`` must be None or ``PENDING``.

        Both paths:
        - ``state=SUPERSEDED`` requires non-empty ``superseded_reason``;
          non-SUPERSEDED forbids ``superseded_reason``.
        - ``state=FULFILLED`` requires at least one
          ``fulfilling_evidence_id`` on this emission.
        """
        if self.need_id is None:
            missing = [
                name
                for name in ("purpose", "request_text", "rationale")
                if getattr(self, name) in (None, "")
            ]
            if missing:
                raise ValueError(
                    "create path (need_id=None) requires: "
                    f"{', '.join(missing)}. These are create-only on "
                    "the schema (Optional for the update path); supply "
                    "them when creating a new need."
                )
            if self.state not in (None, NeedState.PENDING):
                raise ValueError(
                    f"Cannot create a need with state={self.state.value!r}; "
                    "use state=None or 'pending' on create, and emit a "
                    "follow-up update to transition to other statuses."
                )

        if self.state == NeedState.SUPERSEDED:
            if not (self.superseded_reason and self.superseded_reason.strip()):
                raise ValueError(
                    "state=SUPERSEDED requires a non-empty superseded_reason"
                )
        else:
            if self.superseded_reason is not None:
                raise ValueError(
                    "superseded_reason must be None unless state=SUPERSEDED"
                )

        if self.state == NeedState.FULFILLED and not self.fulfilling_evidence_ids:
            raise ValueError(
                "state=FULFILLED requires at least one fulfilling_evidence_id "
                "in the same emission"
            )

        return self


class JournalEntryOutput(BaseModel):
    """A journal entry produced by the LLM for the investigation journal."""

    entry_type: Literal[
        "finding", "decision", "user_context", "ruled_out", "blocker", "milestone"
    ] = Field(description="Type of journal entry")
    content: str = Field(
        description="The distilled insight (max 200 chars)",
    )

    @field_validator("content")
    @classmethod
    def truncate_content(cls, v: str) -> str:
        """Truncate instead of rejecting — LLMs often exceed the 200 char guideline."""
        if len(v) > 200:
            return v[:197] + "..."
        return v

    evidence_id: Optional[str] = Field(
        default=None,
        description="Evidence ID this entry relates to, if any",
    )
    hypothesis_id: Optional[str] = Field(
        default=None,
        description="Hypothesis ID this entry relates to, if any",
    )


class WorkingConclusionUpdate(BaseModel):
    """Current working theory of the case.

    All fields are optional to allow empty working_conclusion during early INQUIRY phase
    when the agent doesn't yet have enough information to form a theory.
    """

    summary: Optional[str] = None
    likelihood: Optional[float] = Field(None, ge=0.0, le=1.0)
    next_steps: Optional[List[str]] = Field(default_factory=list)
    blockers: Optional[List[str]] = Field(default_factory=list)


class BlockerType(str, Enum):
    """Type of blocker preventing investigation progress."""

    DATA_CORRUPTED = "data_corrupted"
    DATA_MISSING = "data_missing"
    DATA_INCOMPLETE = "data_incomplete"
    DATA_ACCESS_DENIED = "data_access_denied"
    TOOL_UNAVAILABLE = "tool_unavailable"
    EXTERNAL_DEPENDENCY = "external_dependency"


class EvidenceQualityIssue(BaseModel):
    """
    Quality issue with evidence that may block or limit investigation.

    Reference: Prompt Engineering Guide Section 14 (lines 3352-3376)
    """

    evidence_id: str = Field(description="Evidence ID with quality issue")
    issue_type: str = Field(
        description="corrupted | incomplete | missing_metadata | ambiguous | etc."
    )
    severity: Literal["blocking", "limiting", "minor"] = Field(
        description="blocking=cannot proceed, limiting=reduced confidence, minor=note only"
    )
    description: str = Field(description="What's wrong with this evidence")
    workaround: Optional[str] = Field(
        None, description="How to work around this issue if possible"
    )


class MissingCriticalData(BaseModel):
    """
    Proactive detection of critical data blockers.
    Flags data quality issues to the agent via system feedback so it can
    communicate limitations and suggest alternatives in its response.

    Reference: Prompt Engineering Guide Section 14 (lines 3316-3351)
    """

    blocker_type: BlockerType = Field(description="Type of data blocker")
    description: str = Field(description="Specific description of what's missing/wrong")
    what_was_expected: str = Field(description="What data was expected")
    what_was_found: str = Field(description="What was actually found")
    impact: str = Field(description="How this blocks investigation progress")
    suggested_alternatives: Optional[List[str]] = Field(
        default_factory=list,
        description="Alternative data sources or approaches user could try",
    )


class ProposedTransition(BaseModel):
    """
    Agent signals a state transition. Engine handles everything else.

    Terminal transitions (→ RESOLVED or → CLOSED) require user confirmation;
    this signal sets pending_transition. Engine derives closure_reason from
    case state programmatically — the LLM does not author it. The engine
    builds the confirmation prompt summary from existing helpers.
    """

    to_state: str = Field(description="Target state: 'resolved' or 'closed'")
    evidence_ids: Optional[List[str]] = Field(
        default_factory=list,
        description="Evidence IDs supporting this transition proposal",
    )

    @field_validator("to_state", mode="before")
    @classmethod
    def _normalize_to_state(cls, v):
        # LLMs are inconsistent with case ('RESOLVED' vs 'resolved'). The engine
        # compares to_state against the lowercase CaseState values, so a raw
        # 'RESOLVED' is spuriously rejected as 'not a valid edge'. Normalize at
        # parse time so the edge check is case-tolerant.
        return v.strip().lower() if isinstance(v, str) else v


class RootCauseConclusionUpdate(BaseModel):
    """Final root cause conclusion."""

    root_cause: str
    mechanism: str
    evidence_ids: List[str] = Field(default_factory=list)
    likelihood: float = Field(default=0.7, ge=0.0, le=1.0)


class SolutionToAdd(BaseModel):
    """Proposed solution."""

    description: str
    solution_type: SolutionType
    estimated_impact: str
    risks: str
    commands: Optional[List[str]] = Field(
        default_factory=list,
        description="Specific commands for the user to execute",
    )


# =============================================================================
# Template Schemas
# =============================================================================


class SuggestedFollowUp(BaseModel):
    """A follow-up suggestion for the user, classified by intended user action."""

    label: str = Field(
        description=(
            "The user's next move, phrased in the user's own voice (3-8 words) "
            "— what they would say or do next, e.g. 'Search KB for incidents', "
            "'Share what I'm seeing'. This is the only text shown to the user. "
            "Never phrase it as a question you ask the user (that belongs in "
            "agent_response); a question the user asks you is fine."
        )
    )
    action_type: Literal["DECIDE", "RUN", "EVIDENCE", "FREE_SPEECH"] = Field(
        # FREE_SPEECH is the safe default: a suggestion wrongly treated as
        # clickable submits a broken message in the user's name; one wrongly
        # treated as informational merely makes the user type. An omitted
        # action_type therefore degrades to non-clickable.
        default="FREE_SPEECH",
        description=(
            "DECIDE = you expect the user's decision/answer and pre-compose "
            "it as payload; a click sends it as their message. "
            "RUN = you composed an exact command; a click copies it for the "
            "user to run externally. "
            "EVIDENCE = GET data from the user's environment "
            "(informational). "
            "FREE_SPEECH = GET the user's own words (informational). "
            "If the user would have to supply content — data or words — the "
            "type is never DECIDE or RUN. When unsure, use FREE_SPEECH."
        ),
    )
    payload: Optional[str] = Field(
        default=None,
        description=(
            "DECIDE/RUN only. DECIDE: the exact message a click sends — "
            "verbatim, the user's complete message; never a placeholder or "
            "preamble the user would need to edit, and never a question only "
            "the user can answer. RUN: the exact command a click copies "
            "(<placeholders> allowed — the user edits them externally). "
            "Required for DECIDE and RUN; omit for EVIDENCE and FREE_SPEECH "
            "— those carry everything in label + body/hints."
        ),
    )
    body: Optional[str] = Field(
        default=None,
        description="Reasoning text shown on card (why the user should take this action)",
    )

    # Shell command prefixes — encoding safety net: a DECIDE whose payload is
    # actually a shell command would SUBMIT the command as a chat message on
    # click; coerce it to RUN so the click copies instead.
    _COMMAND_PREFIXES: ClassVar[tuple] = (
        "kubectl",
        "redis-cli",
        "docker",
        "curl",
        "wget",
        "ssh",
        "scp",
        "rsync",
        "grep",
        "awk",
        "sed",
        "cat",
        "tail",
        "head",
        "journalctl",
        "systemctl",
        "service",
        "sudo",
        "apt",
        "yum",
        "dnf",
        "pip",
        "npm",
        "yarn",
        "helm",
        "terraform",
        "aws",
        "gcloud",
        "az",
        "psql",
        "mysql",
        "mongo",
        "etcdctl",
        "crictl",
        "ctr",
        "nslookup",
        "dig",
        "ping",
        "traceroute",
        "netstat",
        "ss",
        "iptables",
        "top",
        "htop",
        "free",
        "df",
        "du",
        "lsof",
        "strace",
        "tcpdump",
    )

    # Outcome-data safety net — the opposite (and more user-harmful) miscast to
    # the command→RUN one: a DECIDE payload that hands the agent results/output
    # the user has NOT actually submitted. On click it submits a false "here are
    # my results" claim in the user's name and wastes the turn. That outcome data
    # is content only the user can supply, so the suggestion is EVIDENCE. The
    # prompt already forbids this shape (_FOLLOW_UP_SUGGESTIONS_BLOCK, "I ran it
    # — here's the result" BAD case); this makes the prohibition deterministic.
    #
    # PRECISION IS THE WHOLE GAME HERE. Two valid DECIDE shapes must NOT be
    # caught, or the net does more harm than the bug:
    #   1. Steers the agent can act on from case evidence:
    #      "Let's review the error logs", "Let's verify the results".
    #   2. Compliance acknowledgements that advance a gate:
    #      "I've applied the fix" (the user reporting they did the mitigation).
    # So coercion requires the payload to actually be a HANDOFF of outcome data:
    #   (A) the user presents results inline — "here are the logs",
    #       "attached are the results"; OR
    #   (B) the user CLAIMS a completed action AND asks the agent to inspect the
    #       outcome — "I re-ran it, check the output". A completed-action claim
    #       on its own (no inspect request) stays a DECIDE (case 2 above); an
    #       inspect verb on its own stays a DECIDE (case 1 above).
    # Sub-patterns are clause-bounded ([^.?!\n]) so a result noun in a later
    # sentence can't bind to a presenter in an earlier one.

    # (A) "here's / here are / attached / sharing … results/output/logs"
    _RESULTS_PRESENTED_RE: ClassVar = re.compile(
        r"(?:here'?s|here\s+is|here\s+are|attached\s+(?:is|are)|"
        r"i'?ve\s+attached|i\s+attached|i'?m\s+sharing|sharing|i\s+pasted)"
        r"[^.?!\n]{0,30}?"
        r"\b(?:results?|output|logs?|stdout|stderr|tracebacks?|stack\s?traces?)\b",
        re.IGNORECASE,
    )
    # (B1) first-person CLAIM of a completed action (not an inspect/observe verb)
    _USER_DID_ACTION_RE: ClassVar = re.compile(
        r"\bi(?:'ve| have| just| already)?\s+"
        r"(?:applied|ran|re-?ran|executed|deployed|redeployed|restarted|"
        r"rolled\s+back|updated|patched|implemented|completed|finished|"
        r"made\s+the\s+change)\b",
        re.IGNORECASE,
    )
    # (B2) an imperative asking the agent to inspect the outcome
    _INSPECT_REQUEST_RE: ClassVar = re.compile(
        r"\b(?:check|checking|review|reviewing|see|look\s+at|verify|examine|"
        r"inspect|tell\s+me\s+about)\b",
        re.IGNORECASE,
    )

    @model_validator(mode="after")
    def _coerce_command_payload_to_run(self) -> "SuggestedFollowUp":
        """Encoding safety net: a DECIDE payload that is actually a shell
        command would be SUBMITTED as a chat message on click. When the
        payload's first word is a known CLI tool, coerce the type to RUN so
        the click copies the command instead."""
        if self.action_type == "DECIDE" and self.payload:
            payload_first_word = self.payload.strip().split()[0]
            if payload_first_word in self._COMMAND_PREFIXES:
                self.action_type = "RUN"
        return self

    @model_validator(mode="after")
    def _coerce_result_inspection_payload_to_evidence(self) -> "SuggestedFollowUp":
        """Encoding safety net (EVIDENCE miscast): a DECIDE payload that hands
        the agent results/output the user has NOT actually submitted would, on
        click, send a false "here are my results" message in the user's name.
        That outcome data is content only the user can supply, so the suggestion
        is EVIDENCE, not DECIDE. Coerce the type and drop the payload
        (``_enforce_payload_scope`` would otherwise require it). Mirror of
        ``_coerce_command_payload_to_run`` for the opposite, more user-harmful
        miscast direction.

        Coerce only on an actual data HANDOFF (see the regex block above):
        results presented inline, OR a completed-action claim paired with an
        inspect request. A bare steer ("Let's review the logs") and a bare
        compliance claim ("I've applied the fix") are left as DECIDE.

        Runs only on DECIDE (a command already coerced to RUN above is left
        alone — copying a command is a legitimate request for the user to run
        it and report back separately)."""
        if self.action_type == "DECIDE" and self.payload:
            payload = self.payload
            is_handoff = self._RESULTS_PRESENTED_RE.search(payload) or (
                self._USER_DID_ACTION_RE.search(payload)
                and self._INSPECT_REQUEST_RE.search(payload)
            )
            if is_handoff:
                self.action_type = "EVIDENCE"
                self.payload = None
        return self

    @model_validator(mode="after")
    def _enforce_payload_scope(self) -> "SuggestedFollowUp":
        """``payload`` is meaningful only for the clickable types (DECIDE —
        the message a click sends; RUN — the command a click copies).
        Require it there; drop it for EVIDENCE and FREE_SPEECH so a stray
        agent-voiced question/description the LLM put in ``payload`` never
        lingers on a field the user never sees."""
        if self.action_type in ("DECIDE", "RUN"):
            if not self.payload:
                raise ValueError(
                    f"{self.action_type} suggestion requires payload "
                    "(the text a click sends or copies)"
                )
        else:
            self.payload = None
        return self

    # FREE_SPEECH fields
    hints: Optional[List[str]] = Field(
        default=None,
        description=(
            "FREE_SPEECH only: 2-5 short tags (1-3 words) naming aspects of the "
            "PROBLEM the user should cover (e.g. 'symptoms', 'timeline', "
            "'affected services'). NOT your own intent categories, mode labels, "
            "or yes/no options."
        ),
    )

    # EVIDENCE-suggestion-to-need linkage (Phase 6: engine resolves
    # ``new_index_N`` here against ``metadata["evidence_needs_updated"]``
    # before flattening to the API response dict).
    evidence_need_id: Optional[str] = Field(
        default=None,
        description=(
            "For action_type=EVIDENCE only: the persistent EvidenceNeed "
            "this suggestion derives from. Accepts a real "
            "``eneed_xxxxxxxxxxxx`` ID, a ``new_index_N`` placeholder "
            "referencing an entry in this turn's "
            "``evidence_need_updates`` list, or a bare integer "
            "(coerced). Used by the frontend for visual linkage."
        ),
    )

    @field_validator("evidence_need_id", mode="before")
    @classmethod
    def _coerce_bare_int_evidence_need_id(cls, v):
        """Bare int → ``new_index_N``. Same shape as
        ``HypothesisEvidenceLinkToAdd._coerce_bare_int_to_new_index``."""
        if isinstance(v, int) and not isinstance(v, bool):
            return f"new_index_{v}"
        return v

    @model_validator(mode="after")
    def _validate_evidence_need_id_only_with_evidence_action(
        self,
    ) -> "SuggestedFollowUp":
        """``evidence_need_id`` only makes sense for EVIDENCE-type
        suggestions; reject the combination on other action types."""
        if self.evidence_need_id is not None and self.action_type != "EVIDENCE":
            raise ValueError(
                f"evidence_need_id only permitted with action_type=EVIDENCE "
                f"(got action_type={self.action_type!r})"
            )
        return self

    # NOTE: there is no ``intent`` field on this LLM-facing model. Intent
    # metadata (QueryIntent routing) is owned by the engine, not the LLM —
    # it is attached deterministically by the response builder via
    # ``engine_owned_affordances`` when a state-machine gate is pending
    # (Gate 1 / Gate 2 / Gate 3 / pending_transition). The engine→API
    # serialization adds ``intent`` to ``SuggestedActionResponse`` at the
    # boundary; the LLM never sees it. Any LLM that tries to emit an
    # ``intent`` field will have it surfaced by ``_log_dropped_fields`` as
    # a top-level dropped key.


class BaseInteractionResponse(BaseModel):
    """Base class for all agent responses."""

    agent_response: str = Field(description="Natural language response to the user.")
    suggested_follow_ups: Optional[List[SuggestedFollowUp]] = Field(
        default=None,
        description="2-4 contextual follow-up actions the user can take. Each should be specific to the current investigation state.",
    )

    @field_validator("suggested_follow_ups", mode="before")
    @classmethod
    def coerce_follow_ups_from_string(cls, v: Any) -> Any:
        if isinstance(v, str):
            import json as _json

            try:
                v = _json.loads(v)
            except (ValueError, TypeError):
                return None
        return v


class InquiryResponse(BaseInteractionResponse):
    """Response schema for INQUIRY state."""

    class InquiryStateUpdate(BaseModel):
        # Pydantic policy: extra='ignore' (the v2 default) is deliberate.
        # The INV-07 invariant ("no Evidence creation during INQUIRY") is
        # enforced by field absence + the absence of an evidence-creation
        # branch in _apply_inquiry_updates — NOT by rejecting unknown
        # fields. Switching to extra='forbid' was considered and rejected:
        # it would convert benign LLM drift into user-facing turn
        # failures with no improvement to the invariant. See
        # investigation-lifecycle-logic.md §1.3.1 (INV-07 drift note).
        # If LLM drift becomes a concern, add a passive observability
        # signal at the structured-output parse step instead of
        # tightening the schema.
        problem_confirmation: Optional[ProblemConfirmation] = None
        proposed_problem_statement: Optional[str] = None
        preliminary_urgency: Optional[PreliminaryUrgency] = None
        knowledge_match: Optional[KnowledgeMatch] = None
        knowledge_resolution: Optional[KnowledgeResolution] = None
        user_confirmed_investigation: bool = Field(
            default=False,
            description=(
                "Set to True when the user confirms the problem statement. "
                "Explicit: 'Yes', 'Correct', 'Let's investigate', 'That's right'. "
                "Implicit (after problem statement was presented): user asks diagnostic "
                "questions or expresses urgency about the problem. "
                "Set False when: same turn you first present the problem statement, "
                "user explicitly disagrees, user submits new data with a question "
                "(answer the question first), or message is unrelated."
            ),
        )
        proposed_transition: Optional[ProposedTransition] = Field(
            None,
            description=(
                "Propose INQUIRY → CLOSED for user confirmation when the user asks "
                "to close/cancel without investigating. Use to_state='closed'. "
                "The transition is NOT executed until the user explicitly confirms."
            ),
        )

        # Post-010 (strict evidence model): evidence_to_add is intentionally
        # absent from INQUIRY. Evidence presupposes a confirmed claim; during
        # INQUIRY the claim is still being formed. Uploaded files persist in
        # the ``uploaded_files`` table; the LLM evaluates them and creates
        # evidence rows via ``evidence_to_add`` once the case transitions to
        # INVESTIGATING. See
        # docs/architecture/investigation-engine/evidence-driven-investigation-framework.md
        # §5 for the canonical rule.

    state_updates: InquiryStateUpdate


class TerminalResponse(BaseInteractionResponse):
    """Response schema for RESOLVED/CLOSED state."""

    class TerminalStateUpdate(BaseModel):
        final_summary_update: Optional[str] = None
        documentation_links: Optional[List[str]] = Field(default_factory=list)

    state_updates: TerminalStateUpdate


# =============================================================================
# Investigation Schemas (Dynamic Views)
# =============================================================================


class InvestigationResponse_Diagnosis(BaseInteractionResponse):
    """Schema for DIAGNOSIS stage — covers symptom verification, hypothesis work, and solution proposal."""

    class DiagnosisStateUpdate(BaseModel):
        milestones: Optional[MilestoneUpdates] = None
        verification_updates: Optional[ProblemVerificationUpdate] = None
        evidence_to_add: Optional[List[EvidenceToAdd]] = Field(default_factory=list)
        hypotheses_to_add: Optional[List[HypothesisToAdd]] = Field(default_factory=list)
        hypotheses_to_update: Dict[str, HypothesisUpdate] = Field(default_factory=dict)
        hypothesis_evidence_links: Optional[List[HypothesisEvidenceLinkToAdd]] = Field(
            default_factory=list
        )
        causal_nodes_to_add: Optional[List[CausalNodeToAdd]] = Field(
            default_factory=list
        )
        causal_edges_to_add: Optional[List[CausalEdgeToAdd]] = Field(
            default_factory=list
        )
        node_evidence_links: Optional[List[NodeEvidenceLinkToAdd]] = Field(
            default_factory=list
        )
        evidence_need_updates: Optional[List[EvidenceNeedUpdate]] = Field(
            default_factory=list,
            description=(
                "Demand-side updates: create / update evidence needs. "
                "See docs/architecture/investigation-engine/evidence-needs-design.md."
            ),
        )
        solutions_to_add: Optional[List[SolutionToAdd]] = Field(default_factory=list)
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        root_cause_conclusion: Optional[RootCauseConclusionUpdate] = None
        journal_entries: Optional[List[JournalEntryOutput]] = Field(
            default_factory=list,
            description="Key findings or decisions to record in the investigation journal. "
            "Only include entries for significant insights — not every turn needs one.",
        )
        missing_critical_data: Optional[MissingCriticalData] = Field(
            None,
            description="Proactive blocker detection. Flags data quality issues via system feedback.",
        )
        evidence_quality_issues: Optional[List[EvidenceQualityIssue]] = Field(
            default_factory=list,
            description="Quality issues with evidence that may limit investigation.",
        )
        proposed_transition: Optional[ProposedTransition] = Field(
            None,
            description=(
                "Propose a terminal transition (RESOLVED/CLOSED) for user confirmation. "
                "The transition is NOT executed until the user explicitly confirms."
            ),
        )
        outcome: Optional[TurnOutcome] = Field(
            default=TurnOutcome.CONVERSATION,
            description="Classification of this turn's outcome. Server recomputes from actual state changes; omit if unsure.",
        )

    internal_reasoning: Optional[InternalReasoning] = Field(
        None,
        description="REQUIRED when completing milestones, otherwise optional. Justification BEFORE state changes.",
    )
    state_updates: DiagnosisStateUpdate


class InvestigationResponse_Mitigation(BaseInteractionResponse):
    """Schema for MITIGATION stage — applying and verifying temporary fix."""

    class MitigationStateUpdate(BaseModel):
        milestones: Optional[MilestoneUpdates] = None
        evidence_to_add: Optional[List[EvidenceToAdd]] = Field(default_factory=list)
        evidence_need_updates: Optional[List[EvidenceNeedUpdate]] = Field(
            default_factory=list,
            description=(
                "Demand-side updates: typically used to mark FULFILLED "
                "symptom needs with absence-evidence fulfillments "
                "(SYMPTOM_ABSENCE_EVIDENCE) in MITIGATION."
            ),
        )
        solutions_to_add: Optional[List[SolutionToAdd]] = Field(default_factory=list)
        solution_feedback: Optional[str] = None
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        journal_entries: Optional[List[JournalEntryOutput]] = Field(
            default_factory=list,
            description="Key findings or decisions to record in the investigation journal. "
            "Only include entries for significant insights — not every turn needs one.",
        )
        missing_critical_data: Optional[MissingCriticalData] = Field(
            None,
            description="Proactive blocker detection. Flags data quality issues via system feedback.",
        )
        evidence_quality_issues: Optional[List[EvidenceQualityIssue]] = Field(
            default_factory=list,
            description="Quality issues with evidence that may limit investigation.",
        )
        proposed_transition: Optional[ProposedTransition] = Field(
            None,
            description=(
                "Propose a terminal transition (RESOLVED/CLOSED) for user confirmation. "
                "The transition is NOT executed until the user explicitly confirms."
            ),
        )
        outcome: Optional[TurnOutcome] = Field(
            default=TurnOutcome.CONVERSATION,
            description="Classification of this turn's outcome. Server recomputes from actual state changes; omit if unsure.",
        )

    internal_reasoning: Optional[InternalReasoning] = Field(
        None,
        description="REQUIRED when completing milestones, otherwise optional. Justification BEFORE state changes.",
    )
    state_updates: MitigationStateUpdate


class InvestigationResponse_Treatment(BaseInteractionResponse):
    """Schema for TREATMENT stage — verifying fix, extended diagnosis if fix fails."""

    class TreatmentStateUpdate(BaseModel):
        milestones: Optional[MilestoneUpdates] = None
        evidence_to_add: Optional[List[EvidenceToAdd]] = Field(default_factory=list)
        hypotheses_to_add: Optional[List[HypothesisToAdd]] = Field(default_factory=list)
        hypotheses_to_update: Dict[str, HypothesisUpdate] = Field(default_factory=dict)
        hypothesis_evidence_links: Optional[List[HypothesisEvidenceLinkToAdd]] = Field(
            default_factory=list
        )
        causal_nodes_to_add: Optional[List[CausalNodeToAdd]] = Field(
            default_factory=list
        )
        causal_edges_to_add: Optional[List[CausalEdgeToAdd]] = Field(
            default_factory=list
        )
        node_evidence_links: Optional[List[NodeEvidenceLinkToAdd]] = Field(
            default_factory=list
        )
        evidence_need_updates: Optional[List[EvidenceNeedUpdate]] = Field(
            default_factory=list,
            description=(
                "Demand-side updates: typically used to re-check FULFILLED "
                "needs with absence-evidence fulfillments "
                "(CAUSAL_ABSENCE_EVIDENCE + SYMPTOM_ABSENCE_EVIDENCE)."
            ),
        )
        solutions_to_add: Optional[List[SolutionToAdd]] = Field(default_factory=list)
        solution_feedback: Optional[str] = None
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        root_cause_conclusion: Optional[RootCauseConclusionUpdate] = None
        journal_entries: Optional[List[JournalEntryOutput]] = Field(
            default_factory=list,
            description="Key findings or decisions to record in the investigation journal. "
            "Only include entries for significant insights — not every turn needs one.",
        )
        proposed_transition: Optional[ProposedTransition] = Field(
            None,
            description=(
                "Propose a terminal transition (RESOLVED/CLOSED) for user confirmation. "
                "The transition is NOT executed until the user explicitly confirms."
            ),
        )
        outcome: Optional[TurnOutcome] = Field(
            default=TurnOutcome.CONVERSATION,
            description="Classification of this turn's outcome. Server recomputes from actual state changes; omit if unsure.",
        )

    internal_reasoning: Optional[InternalReasoning] = Field(
        None,
        description="REQUIRED when completing milestones, otherwise optional. Justification BEFORE state changes.",
    )
    state_updates: TreatmentStateUpdate


class InvestigationResponse_General(BaseInteractionResponse):
    """Fallback 'Full' schema if stage is ambiguous or degraded."""

    class GeneralStateUpdate(BaseModel):
        milestones: Optional[MilestoneUpdates] = None
        verification_updates: Optional[ProblemVerificationUpdate] = None
        evidence_to_add: Optional[List[EvidenceToAdd]] = Field(default_factory=list)
        hypotheses_to_add: Optional[List[HypothesisToAdd]] = Field(default_factory=list)
        hypotheses_to_update: Dict[str, HypothesisUpdate] = Field(default_factory=dict)
        hypothesis_evidence_links: Optional[List[HypothesisEvidenceLinkToAdd]] = Field(
            default_factory=list
        )
        causal_nodes_to_add: Optional[List[CausalNodeToAdd]] = Field(
            default_factory=list
        )
        causal_edges_to_add: Optional[List[CausalEdgeToAdd]] = Field(
            default_factory=list
        )
        node_evidence_links: Optional[List[NodeEvidenceLinkToAdd]] = Field(
            default_factory=list
        )
        evidence_need_updates: Optional[List[EvidenceNeedUpdate]] = Field(
            default_factory=list,
            description=(
                "Demand-side updates. Engine's path-conditional emission "
                "backstop gates causal-purpose needs by state at apply-time."
            ),
        )
        solutions_to_add: Optional[List[SolutionToAdd]] = Field(default_factory=list)
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        root_cause_conclusion: Optional[RootCauseConclusionUpdate] = None
        journal_entries: Optional[List[JournalEntryOutput]] = Field(
            default_factory=list,
            description="Key findings or decisions to record in the investigation journal. "
            "Only include entries for significant insights — not every turn needs one.",
        )
        missing_critical_data: Optional[MissingCriticalData] = Field(
            None,
            description="Proactive blocker detection. Flags data quality issues via system feedback.",
        )
        evidence_quality_issues: Optional[List[EvidenceQualityIssue]] = Field(
            default_factory=list,
            description="Quality issues with evidence that may limit investigation.",
        )
        proposed_transition: Optional[ProposedTransition] = Field(
            None,
            description=(
                "Propose a terminal transition (RESOLVED/CLOSED) for user confirmation. "
                "The transition is NOT executed until the user explicitly confirms."
            ),
        )
        outcome: Optional[TurnOutcome] = Field(
            default=TurnOutcome.CONVERSATION,
            description="Classification of this turn's outcome. Server recomputes from actual state changes; omit if unsure.",
        )

    internal_reasoning: Optional[InternalReasoning] = Field(
        None,
        description="REQUIRED when completing milestones, otherwise optional. Justification BEFORE state changes.",
    )
    state_updates: GeneralStateUpdate


def get_schema_for_stage(stage: Optional[InvestigationStage]) -> Any:
    """Factory to get the appropriate Pydantic model for the current stage."""
    if stage == InvestigationStage.DIAGNOSIS:
        return InvestigationResponse_Diagnosis
    elif stage == InvestigationStage.MITIGATION:
        return InvestigationResponse_Mitigation
    elif stage == InvestigationStage.TREATMENT:
        return InvestigationResponse_Treatment
    else:
        return InvestigationResponse_General
