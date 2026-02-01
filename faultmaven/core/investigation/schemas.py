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
"""

from typing import Dict, List, Optional, Literal, Union, Any
from pydantic import BaseModel, Field, field_validator

from faultmaven.modules.case.contracts import (
    InvestigationStage,
    TurnOutcome,
    EvidenceCategory,
    EvidenceStance,
    EvidenceSourceType,
    HypothesisCategory,
    SolutionType,
    ConfidenceLevel,
)

# =============================================================================
# Shared Components
# =============================================================================

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
    """Milestones LLM can set to True (never False)."""
    symptom_verified: Optional[bool] = None
    scope_assessed: Optional[bool] = None
    timeline_established: Optional[bool] = None
    changes_identified: Optional[bool] = None
    root_cause_identified: Optional[bool] = None
    root_cause_likelihood: Optional[float] = Field(None, ge=0.0, le=1.0)
    solution_proposed: Optional[bool] = None
    solution_applied: Optional[bool] = None
    solution_verified: Optional[bool] = None
    mitigation_applied: Optional[bool] = None
    root_cause_method: Optional[str] = Field(None, description="direct_analysis | hypothesis_validation | correlation | other")

class ProblemVerificationUpdate(BaseModel):
    """Updates to problem verification data."""
    symptom_correction: Optional[str] = None
    scope_impact: Optional[str] = None
    timeline_start: Optional[str] = None
    timeline_duration: Optional[str] = None
    changes_list: List[str] = Field(default_factory=list)

class EvidenceToAdd(BaseModel):
    """Evidence to be added to the case."""
    summary: str
    content_ref: str = Field(description="Content reference or snippet. If file, use 'file:FILENAME'")
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
    status: Optional[str] = None # ACTIVE, VALIDATED, REFUTED, RETIRED
    reason: Optional[str] = None

class HypothesisEvidenceLinkToAdd(BaseModel):
    """Link evidence to a hypothesis."""
    hypothesis_id_ref: str = Field(description="Hypothesis ID or 'new_index_N' if created this turn")
    evidence_id_ref: str = Field(description="Evidence ID or 'new_index_N' if created this turn")
    stance: EvidenceStance
    reasoning: str
    stance_confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Confidence in the stance assessment (0.0-1.0)")

class WorkingConclusionUpdate(BaseModel):
    """Current working theory of the case."""
    summary: str
    likelihood: float = Field(ge=0.0, le=1.0)
    next_steps: List[str]
    blockers: List[str] = Field(default_factory=list)

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
        quick_suggestions: List[str] = Field(default_factory=list)
    
    state_updates: InquiryStateUpdate

class TerminalResponse(BaseInteractionResponse):
    """Response schema for RESOLVED/CLOSED status."""
    class TerminalStateUpdate(BaseModel):
        final_summary_update: Optional[str] = None
        documentation_links: List[str] = Field(default_factory=list)
        
    state_updates: TerminalStateUpdate

# =============================================================================
# Investigation Schemas (Dynamic Views)
# =============================================================================

class InvestigationResponse_Verification(BaseInteractionResponse):
    """Schema optimized for SYMPTOM_VERIFICATION stage (Focus: Evidence, Verification)."""
    class VerificationStateUpdate(BaseModel):
        milestones: Optional[MilestoneUpdates] = None
        verification_updates: Optional[ProblemVerificationUpdate] = None
        evidence_to_add: List[EvidenceToAdd] = Field(default_factory=list)
        # Hypotheses allowed but secondary
        hypotheses_to_add: List[HypothesisToAdd] = Field(default_factory=list) 
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        outcome: TurnOutcome

    state_updates: VerificationStateUpdate

class InvestigationResponse_Hypothesis(BaseInteractionResponse):
    """Schema optimized for HYPOTHESIS_FORMULATION/VALIDATION (Focus: Hypotheses, Linking)."""
    class HypothesisStateUpdate(BaseModel):
        milestones: Optional[MilestoneUpdates] = None
        evidence_to_add: List[EvidenceToAdd] = Field(default_factory=list)
        hypotheses_to_add: List[HypothesisToAdd] = Field(default_factory=list)
        hypotheses_to_update: Dict[str, HypothesisUpdate] = Field(default_factory=dict)
        hypothesis_evidence_links: List[HypothesisEvidenceLinkToAdd] = Field(default_factory=list)
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        root_cause_conclusion: Optional[RootCauseConclusionUpdate] = None
        outcome: TurnOutcome

    state_updates: HypothesisStateUpdate

class InvestigationResponse_Resolution(BaseInteractionResponse):
    """Schema optimized for SOLUTION stage (Focus: Solutions, Verification)."""
    class ResolutionStateUpdate(BaseModel):
        milestones: Optional[MilestoneUpdates] = None
        solutions_to_add: List[SolutionToAdd] = Field(default_factory=list)
        solution_feedback: Optional[str] = None # User feedback on applied solution
        evidence_to_add: List[EvidenceToAdd] = Field(default_factory=list) # For verification evidence
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        outcome: TurnOutcome

    state_updates: ResolutionStateUpdate

class InvestigationResponse_General(BaseInteractionResponse):
    """Fallback 'Full' schema if stage is ambiguous or degraded."""
    class GeneralStateUpdate(BaseModel):
        milestones: Optional[MilestoneUpdates] = None
        verification_updates: Optional[ProblemVerificationUpdate] = None
        evidence_to_add: List[EvidenceToAdd] = Field(default_factory=list)
        hypotheses_to_add: List[HypothesisToAdd] = Field(default_factory=list)
        hypotheses_to_update: Dict[str, HypothesisUpdate] = Field(default_factory=dict)
        hypothesis_evidence_links: List[HypothesisEvidenceLinkToAdd] = Field(default_factory=list)
        solutions_to_add: List[SolutionToAdd] = Field(default_factory=list)
        working_conclusion: Optional[WorkingConclusionUpdate] = None
        root_cause_conclusion: Optional[RootCauseConclusionUpdate] = None
        outcome: TurnOutcome

    state_updates: GeneralStateUpdate

def get_schema_for_stage(stage: Optional[InvestigationStage]) -> Any:
    """Factory to get the appropriate Pydantic model for the current stage."""
    if stage == InvestigationStage.SYMPTOM_VERIFICATION:
        return InvestigationResponse_Verification
    elif stage in [InvestigationStage.HYPOTHESIS_FORMULATION, InvestigationStage.HYPOTHESIS_VALIDATION]:
        return InvestigationResponse_Hypothesis
    elif stage == InvestigationStage.SOLUTION:
        return InvestigationResponse_Resolution
    else:
        return InvestigationResponse_General
