"""Investigation Schemas for Structured Output

This module defines the Pydantic models used for LLM structured output generation.
It implements the "Form-Filler" pattern where the LLM returns a structured state update
along with its natural language response.

Design Reference:
- docs/architecture/investigation-engine/prompt-engineering-guide.md
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

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

from faultmaven.modules.agent.domain.models.agentic import QueryIntent  # noqa: F401
from faultmaven.modules.case.contracts import (
    ConfidenceLevel,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    HypothesisCategory,
    HypothesisStatus,
    InvestigationStage,
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
            "Example: {'scope_assessed': 'All 20 pods affected per ev_abc123', "
            "'timeline_established': 'Started at 17:44 UTC per ev_def456'}. "
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
    mitigation_hint: Optional[str] = None


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
    scope_assessed: Optional[bool] = None
    timeline_established: Optional[bool] = None
    changes_identified: Optional[bool] = None
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


class EvidenceToAdd(BaseModel):
    """Evidence to be added to the case.

    Milestone Attribution (Option 2.5 - Three-Tier Logic):

    Tier 1: MilestoneUpdates drives milestone state (turn-level, LLM specifies)
    Tier 2: System infers advances_milestones from category (automatic, handles 90%)
    Tier 3: LLM overrides via advances_milestones field (optional, handles 10%)

    The advances_milestones field is OPTIONAL. If omitted, the system will automatically
    infer milestone attribution based on the evidence category and milestones completed
    this turn using the CATEGORY_MILESTONE_MAP.

    Only provide advances_milestones explicitly when:
    - The automatic inference would be wrong (rare edge case)
    - You need to specify a subset of eligible milestones
    - The evidence contributed to a milestone outside normal category mapping

    Design Reference:
    - docs/working/MILESTONE-ADVANCEMENT-ANALYSIS.md (Option 2.5)
    - docs/working/DESIGN-DISCUSSION-SUMMARY-2026-02-11.md
    """

    summary: str
    content_ref: Optional[str] = Field(
        default=None,
        description="Content reference or snippet. If file, use 'file:FILENAME'. Optional if summary is self-contained.",
    )
    category: EvidenceCategory
    source_type: EvidenceSourceType
    likelihood: float = Field(0.8, ge=0.0, le=1.0)

    @field_validator("content_ref", mode="before")
    @classmethod
    def stringify_content_ref(cls, v):
        """
        Convert dict objects to JSON strings for content_ref.

        When LLMs use structured output (tool calling), they may populate content_ref
        with dict objects instead of JSON strings. This validator automatically
        converts dict/list objects to JSON strings, ensuring compatibility with
        the string type constraint.

        Examples:
            - {"key": "value"} → '{"key": "value"}'
            - [1, 2, 3] → '[1, 2, 3]'
            - "already a string" → "already a string"
            - None → None
        """
        if v is None:
            return v
        if isinstance(v, (dict, list)):
            import json

            return json.dumps(v, indent=2)
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
        """
        Fallback to CONTEXTUAL_EVIDENCE for unrecognized categories.

        This handles LLM returning invalid category values due to prompt drift
        or schema mismatch. Instead of failing validation, we fallback to a safe
        default and log a warning for monitoring.

        Reference: EVIDENCE-CREATION-FAILURE-MODES.md Scenario 3
        """
        if isinstance(v, str):
            try:
                return EvidenceCategory(v)
            except ValueError:
                import logging

                logger = logging.getLogger(__name__)
                logger.warning(
                    f"LLM returned unrecognized category '{v}', "
                    f"falling back to CONTEXTUAL_EVIDENCE",
                    extra={
                        "category_attempted": v,
                        "alert_team": "llm_integration",
                        "severity": "warning",
                        "metric": "evidence.category_fallback",
                    },
                )
                return EvidenceCategory.CONTEXTUAL_EVIDENCE
        return v

    @field_validator("likelihood")
    @classmethod
    def validate_likelihood(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


class HypothesisToAdd(BaseModel):
    """New hypothesis to track."""

    statement: str
    category: HypothesisCategory
    likelihood: float = Field(ge=0.0, le=1.0)
    rationale: str


class HypothesisUpdate(BaseModel):
    """Update to existing hypothesis."""

    likelihood: Optional[float] = Field(None, ge=0.0, le=1.0)
    status: Optional[HypothesisStatus] = None
    reason: Optional[str] = None

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v):
        """
        Validate hypothesis status against HypothesisStatus enum.

        Maps known LLM aliases (e.g., "TESTING" -> ACTIVE) and rejects
        unrecognized values by returning None (no status update).
        """
        if v is None:
            return None
        if isinstance(v, HypothesisStatus):
            return v
        if isinstance(v, str):
            # Map known LLM aliases to valid enum values
            aliases = {"TESTING": "active", "testing": "active"}
            mapped = aliases.get(v, v.lower())
            try:
                return HypothesisStatus(mapped)
            except ValueError:
                import logging

                logger = logging.getLogger(__name__)
                logger.warning(
                    f"LLM returned unrecognized hypothesis status '{v}', "
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
        description="Hypothesis ID or 'new_index_N' if created this turn"
    )
    evidence_id_ref: str = Field(
        description="Evidence ID or 'new_index_N' if created this turn"
    )
    stance: EvidenceStance
    reasoning: str
    stance_confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence in the stance assessment (0.0-1.0)",
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
    Agent proposes a state transition that requires user confirmation.

    Terminal transitions (INVESTIGATING → RESOLVED, INVESTIGATING → CLOSED,
    INQUIRY → RESOLVED) are never automatic. The agent proposes the transition,
    and the system holds it pending until the user explicitly confirms.

    This implements the User-Agent Handshake pattern: the agent's interpretation
    of user intent (e.g., "it works" → solution_verified) is not sufficient to
    trigger an irreversible state change. The user must confirm the proposal.
    """

    to_status: str = Field(description="Target status: 'resolved' or 'closed'")
    reason: str = Field(
        description="Why the agent believes this transition is appropriate"
    )
    summary: str = Field(
        description="Summary presented to user for confirmation (e.g., problem statement, root cause, solution applied)"
    )
    evidence_ids: Optional[List[str]] = Field(
        default_factory=list,
        description="Evidence IDs supporting this transition proposal",
    )


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
        description="Short card title (3-8 words, e.g., 'Search KB for incidents')"
    )
    action_type: Literal["COOPERATIVE", "EVIDENCE", "FREE_SPEECH"] = Field(
        default="COOPERATIVE",
        description=(
            "COOPERATIVE = click submits query or copies command; "
            "EVIDENCE = informational, tells user what data to provide; "
            "FREE_SPEECH = informational, asks user a question with framework hints"
        ),
    )
    payload: str = Field(
        description=(
            "COOPERATIVE: pre-composed query text or shell command; "
            "EVIDENCE: description of what data to provide and why; "
            "FREE_SPEECH: the question text shown to the user"
        )
    )
    body: Optional[str] = Field(
        default=None,
        description="Reasoning text shown on card (why the user should take this action)",
    )

    # COOPERATIVE fields
    cooperative_action: Optional[Literal["query_submit", "command_copy"]] = Field(
        default=None,
        description="query_submit = auto-submit as user message; command_copy = copy to clipboard",
    )

    # FREE_SPEECH fields
    hints: Optional[List[str]] = Field(
        default=None,
        description="Short framework tags guiding what aspects the user should address (e.g., 'symptoms', 'timeline', 'affected services')",
    )


class BaseInteractionResponse(BaseModel):
    """Base class for all agent responses."""

    agent_response: str = Field(description="Natural language response to the user.")
    suggested_follow_ups: Optional[List[SuggestedFollowUp]] = Field(
        default=None,
        description="2-4 contextual follow-up actions the user can take. Each should be specific to the current investigation state.",
    )


class InquiryResponse(BaseInteractionResponse):
    """Response schema for INQUIRY status."""

    class InquiryStateUpdate(BaseModel):
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
        evidence_to_add: Optional[List[EvidenceToAdd]] = Field(
            default_factory=list,
            description="Evidence to create from agent findings during this turn",
        )

    state_updates: InquiryStateUpdate


class TerminalResponse(BaseInteractionResponse):
    """Response schema for RESOLVED/CLOSED status."""

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
        solutions_to_add: Optional[List[SolutionToAdd]] = Field(default_factory=list)
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        root_cause_conclusion: Optional[RootCauseConclusionUpdate] = None
        missing_critical_data: Optional[MissingCriticalData] = Field(
            None,
            description="Proactive blocker detection. Flags data quality issues via system feedback.",
        )
        evidence_quality_issues: Optional[List[EvidenceQualityIssue]] = Field(
            default_factory=list,
            description="Quality issues with evidence that may limit investigation.",
        )
        outcome: TurnOutcome

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
        solutions_to_add: Optional[List[SolutionToAdd]] = Field(default_factory=list)
        solution_feedback: Optional[str] = None
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        missing_critical_data: Optional[MissingCriticalData] = Field(
            None,
            description="Proactive blocker detection. Flags data quality issues via system feedback.",
        )
        evidence_quality_issues: Optional[List[EvidenceQualityIssue]] = Field(
            default_factory=list,
            description="Quality issues with evidence that may limit investigation.",
        )
        outcome: TurnOutcome

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
        solutions_to_add: Optional[List[SolutionToAdd]] = Field(default_factory=list)
        solution_feedback: Optional[str] = None
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        root_cause_conclusion: Optional[RootCauseConclusionUpdate] = None
        proposed_transition: Optional[ProposedTransition] = Field(
            None,
            description=(
                "Propose a terminal transition (RESOLVED/CLOSED) for user confirmation. "
                "The transition is NOT executed until the user explicitly confirms."
            ),
        )
        outcome: TurnOutcome

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
        solutions_to_add: Optional[List[SolutionToAdd]] = Field(default_factory=list)
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        root_cause_conclusion: Optional[RootCauseConclusionUpdate] = None
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
        outcome: TurnOutcome

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
