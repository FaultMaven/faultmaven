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

from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

from faultmaven.modules.case.contracts import (
    ConfidenceLevel,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    HypothesisCategory,
    InvestigationStage,
    SolutionType,
    TurnOutcome,
)

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
    milestone_justifications: Dict[str, str] = Field(
        default_factory=dict,
        description="For each milestone being completed, explain why based on evidence",
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
    """Milestones LLM can set to True (never False).

    NOTE: solution_verified is NOT settable here. It requires the User-Agent
    Handshake pattern: the agent proposes resolution via ProposedTransition,
    and the user confirms. Only then does the system set solution_verified=True
    and transition to RESOLVED.
    """

    symptom_verified: Optional[bool] = None
    scope_assessed: Optional[bool] = None
    timeline_established: Optional[bool] = None
    changes_identified: Optional[bool] = None
    root_cause_identified: Optional[bool] = None
    root_cause_likelihood: Optional[float] = Field(None, ge=0.0, le=1.0)
    solution_proposed: Optional[bool] = None
    solution_applied: Optional[bool] = None
    # solution_verified is intentionally excluded — requires User-Agent Handshake
    # via ProposedTransition. See terminal_transitions.py.
    mitigation_applied: Optional[bool] = None
    root_cause_method: Optional[str] = Field(
        None,
        description="direct_analysis | hypothesis_validation | correlation | other",
    )


class ProblemVerificationUpdate(BaseModel):
    """Updates to problem verification data."""

    symptom_correction: Optional[str] = None
    scope_impact: Optional[str] = None
    timeline_start: Optional[str] = None
    timeline_duration: Optional[str] = None
    changes_list: Optional[List[str]] = Field(default_factory=list)


class EvidenceToAdd(BaseModel):
    """Evidence to be added to the case."""

    summary: str
    content_ref: Optional[str] = Field(
        default=None,
        description="Content reference or snippet. If file, use 'file:FILENAME'. Optional if summary is self-contained.",
    )
    category: EvidenceCategory
    source_type: EvidenceSourceType
    likelihood: float = Field(0.8, ge=0.0, le=1.0)

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
    status: Optional[str] = None  # ACTIVE, VALIDATED, REFUTED, RETIRED
    reason: Optional[str] = None


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
    """Current working theory of the case."""

    summary: str
    likelihood: float = Field(ge=0.0, le=1.0)
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
    Triggers immediate degraded mode entry instead of waiting 3 turns.

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
    triggers_degraded_mode: bool = Field(
        True, description="Whether this should trigger immediate degraded mode"
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

    to_status: str = Field(
        description="Target status: 'resolved' or 'closed'"
    )
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
    evidence_ids: List[str]
    likelihood: float = Field(ge=0.0, le=1.0)


class SolutionToAdd(BaseModel):
    """Proposed solution."""

    description: str
    solution_type: SolutionType
    estimated_impact: str
    risks: str


# =============================================================================
# Template Schemas
# =============================================================================


class BaseInteractionResponse(BaseModel):
    """Base class for all agent responses."""

    agent_response: str = Field(description="Natural language response to the user.")


class InquiryResponse(BaseInteractionResponse):
    """Response schema for INQUIRY status."""

    class InquiryStateUpdate(BaseModel):
        problem_confirmation: Optional[ProblemConfirmation] = None
        proposed_problem_statement: Optional[str] = None
        preliminary_urgency: Optional[PreliminaryUrgency] = None
        knowledge_match: Optional[KnowledgeMatch] = None
        knowledge_resolution: Optional[KnowledgeResolution] = None
        quick_suggestions: Optional[List[str]] = Field(default_factory=list)

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


class InvestigationResponse_Verification(BaseInteractionResponse):
    """Schema optimized for SYMPTOM_VERIFICATION stage (Focus: Evidence, Verification)."""

    class VerificationStateUpdate(BaseModel):
        milestones: Optional[MilestoneUpdates] = None
        verification_updates: Optional[ProblemVerificationUpdate] = None
        evidence_to_add: Optional[List[EvidenceToAdd]] = Field(default_factory=list)
        # Hypotheses allowed but secondary
        hypotheses_to_add: Optional[List[HypothesisToAdd]] = Field(default_factory=list)
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        missing_critical_data: Optional[MissingCriticalData] = Field(
            None,
            description="Proactive blocker detection. Triggers immediate degraded mode.",
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
    state_updates: VerificationStateUpdate


class InvestigationResponse_Hypothesis(BaseInteractionResponse):
    """Schema optimized for HYPOTHESIS_FORMULATION/VALIDATION (Focus: Hypotheses, Linking)."""

    class HypothesisStateUpdate(BaseModel):
        milestones: Optional[MilestoneUpdates] = None
        evidence_to_add: Optional[List[EvidenceToAdd]] = Field(default_factory=list)
        hypotheses_to_add: Optional[List[HypothesisToAdd]] = Field(default_factory=list)
        hypotheses_to_update: Dict[str, HypothesisUpdate] = Field(default_factory=dict)
        hypothesis_evidence_links: Optional[List[HypothesisEvidenceLinkToAdd]] = Field(
            default_factory=list
        )
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        root_cause_conclusion: Optional[RootCauseConclusionUpdate] = None
        missing_critical_data: Optional[MissingCriticalData] = Field(
            None,
            description="Proactive blocker detection. Triggers immediate degraded mode.",
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
    state_updates: HypothesisStateUpdate


class InvestigationResponse_Resolution(BaseInteractionResponse):
    """Schema optimized for SOLUTION stage (Focus: Solutions, Verification)."""

    class ResolutionStateUpdate(BaseModel):
        milestones: Optional[MilestoneUpdates] = None
        solutions_to_add: Optional[List[SolutionToAdd]] = Field(default_factory=list)
        solution_feedback: Optional[str] = None  # User feedback on applied solution
        evidence_to_add: Optional[List[EvidenceToAdd]] = Field(
            default_factory=list
        )  # For verification evidence
        working_conclusion: Optional[WorkingConclusionUpdate] = None
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
    state_updates: ResolutionStateUpdate


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
            description="Proactive blocker detection. Triggers immediate degraded mode.",
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
    if stage == InvestigationStage.SYMPTOM_VERIFICATION:
        return InvestigationResponse_Verification
    elif stage in [
        InvestigationStage.HYPOTHESIS_FORMULATION,
        InvestigationStage.HYPOTHESIS_VALIDATION,
    ]:
        return InvestigationResponse_Hypothesis
    elif stage == InvestigationStage.SOLUTION:
        return InvestigationResponse_Resolution
    else:
        return InvestigationResponse_General
