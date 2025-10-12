"""OODA Response Models

This module defines Pydantic models for structured LLM responses in the OODA
Investigation Framework. These models enforce response format specifications
defined in the prompt files, enabling the three-tier fallback parsing strategy.

Design Reference: docs/architecture/RESPONSE_FORMAT_INTEGRATION_SPEC.md
"""

from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime


# =============================================================================
# Supporting Field Models
# =============================================================================


class SuggestedAction(BaseModel):
    """Clickable action suggestion for UI guidance

    Action types:
    - question_template: Pre-filled question user can click
    - command: Diagnostic command to run
    - upload_data: Request file upload
    - transition: Mode/phase transition
    - create_runbook: Offer to create documentation
    """

    action_type: Literal[
        "question_template", "command", "upload_data", "transition", "create_runbook"
    ] = Field(..., description="Type of action determining behavior")

    label: str = Field(
        ..., max_length=100, description="Display text for button"
    )

    description: str = Field(
        ..., max_length=200, description="What this action does"
    )

    data: Dict[str, Any] = Field(
        default_factory=dict, description="Action-specific data payload"
    )

    icon: Optional[str] = Field(
        None, max_length=10, description="UI icon hint (emoji or icon name)"
    )


class CommandSuggestion(BaseModel):
    """Diagnostic command suggestion with safety classification"""

    command: str = Field(
        ..., max_length=500, description="The command to run"
    )

    description: str = Field(
        ..., max_length=200, description="Brief description of what command does"
    )

    safety: Literal["safe", "read_only", "caution"] = Field(
        default="safe", description="Safety classification of command"
    )

    expected_output: Optional[str] = Field(
        None, max_length=300, description="What user should see in output"
    )


class CommandValidation(BaseModel):
    """Response when user asks to validate a command (e.g., 'Can I run X?')"""

    command: str = Field(..., description="The command user wants to validate")

    is_safe: bool = Field(..., description="Overall safety assessment")

    safety_level: Literal["safe", "read_only", "caution", "dangerous"] = Field(
        ..., description="Safety classification"
    )

    explanation: str = Field(
        ..., max_length=500, description="What the command does and its effects"
    )

    concerns: List[str] = Field(
        default_factory=list, max_length=5, description="Potential risks or issues"
    )

    safer_alternative: Optional[str] = Field(
        None, description="Alternative command if risky"
    )

    conditions_for_safety: List[str] = Field(
        default_factory=list,
        max_length=5,
        description="Conditions under which command is safe",
    )

    should_diagnose_first: bool = Field(
        default=False,
        description="Whether user should diagnose root cause before running",
    )


class EvidenceRequest(BaseModel):
    """Structured evidence request for Lead Investigator mode"""

    evidence_type: Literal[
        "scope",
        "timeline",
        "configuration",
        "logs",
        "metrics",
        "test_result",
        "implementation_proof",
    ] = Field(..., description="Type of evidence needed")

    description: str = Field(
        ..., max_length=300, description="What you need and why"
    )

    collection_method: str = Field(
        ...,
        max_length=500,
        description="Specific instructions (command, file path, UI location)",
    )

    expected_result: str = Field(
        ..., max_length=300, description="What user should see"
    )

    urgency: Literal["immediate", "high", "normal", "low"] = Field(
        default="normal", description="Urgency level"
    )


# =============================================================================
# Phase-Specific Field Models
# =============================================================================


class ScopeAssessment(BaseModel):
    """Blast radius assessment (Phase 1)"""

    affected_users: Literal["all", "subset", "specific", "unknown"] = Field(
        ..., description="Who is affected by the problem"
    )

    affected_components: List[str] = Field(
        default_factory=list, description="List of affected services/components"
    )

    impact_severity: Literal["low", "medium", "high", "critical"] = Field(
        ..., description="Problem severity level"
    )

    blast_radius: str = Field(
        ..., max_length=300, description="Textual description of scope"
    )


class TimelineUpdate(BaseModel):
    """Timeline information (Phase 2)"""

    problem_start_time: str = Field(
        ..., description="ISO 8601 timestamp or 'unknown'"
    )

    recent_changes: List[str] = Field(
        default_factory=list, description="Recent changes (deployments, configs)"
    )

    change_correlation: str = Field(
        ..., max_length=300, description="How changes relate to problem"
    )


class Hypothesis(BaseModel):
    """Root cause hypothesis (Phase 3)"""

    id: str = Field(..., max_length=20, description="Hypothesis ID (e.g., 'H1')")

    statement: str = Field(
        ..., max_length=200, description="Clear hypothesis statement"
    )

    likelihood: float = Field(
        ..., ge=0.0, le=1.0, description="Probability/confidence score (0.0-1.0)"
    )

    rationale: str = Field(
        ..., max_length=300, description="Why this is likely"
    )

    testing_approach: str = Field(
        ..., max_length=300, description="How to test this hypothesis"
    )


class TestResult(BaseModel):
    """Result of hypothesis validation test (Phase 4)"""

    outcome: Literal["supported", "refuted", "inconclusive"] = Field(
        ..., description="Test outcome"
    )

    confidence_change: float = Field(
        ...,
        ge=-1.0,
        le=1.0,
        description="Impact on confidence (+increase, -decrease)",
    )

    new_confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Updated confidence score"
    )

    evidence_summary: str = Field(
        ..., max_length=300, description="What the test showed"
    )


class SolutionProposal(BaseModel):
    """Solution proposal (Phase 5)"""

    approach: str = Field(..., max_length=300, description="What to change")

    rationale: str = Field(
        ..., max_length=300, description="Why this fixes the root cause"
    )

    risks: List[str] = Field(
        default_factory=list, max_length=5, description="Potential risks"
    )

    verification_method: str = Field(
        ..., max_length=300, description="How to verify it worked"
    )


class CaseSummary(BaseModel):
    """Case documentation (Phase 6)"""

    root_cause: str = Field(..., max_length=300, description="Final determination")

    solution_applied: str = Field(
        ..., max_length=300, description="What was done"
    )

    lessons_learned: List[str] = Field(
        default_factory=list, description="Key lessons"
    )

    prevention_measures: List[str] = Field(
        default_factory=list, description="How to prevent recurrence"
    )


# =============================================================================
# Base Response Models
# =============================================================================


class OODAResponse(BaseModel):
    """Base OODA framework response structure

    This is the core response model used across all phases and modes.
    Specific modes/phases add additional fields via subclassing.
    """

    # ALWAYS PRESENT
    answer: str = Field(..., description="Natural language response to user")

    # OPTIONAL: GUIDANCE FIELDS
    clarifying_questions: List[str] = Field(
        default_factory=list,
        max_length=3,
        description="Questions to understand user intent",
    )

    suggested_actions: List[SuggestedAction] = Field(
        default_factory=list,
        max_length=6,
        description="Clickable action suggestions",
    )

    suggested_commands: List[CommandSuggestion] = Field(
        default_factory=list,
        max_length=5,
        description="Diagnostic commands to run",
    )

    command_validation: Optional[CommandValidation] = Field(
        None, description="Command safety validation"
    )

    # OPTIONAL: METADATA
    response_metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Additional response metadata"
    )


class ConsultantResponse(OODAResponse):
    """Consultant Mode response (Phase 0: Intake)

    Extends OODAResponse with problem detection fields.
    """

    # PROBLEM DETECTION
    problem_detected: bool = Field(
        default=False, description="Whether problem signals detected"
    )

    problem_summary: Optional[str] = Field(
        None, max_length=200, description="Brief problem summary if detected"
    )

    severity: Optional[Literal["low", "medium", "high", "critical"]] = Field(
        None, description="Problem severity if detected"
    )


class LeadInvestigatorResponse(OODAResponse):
    """Lead Investigator Mode response (Phases 1-6)

    Extends OODAResponse with investigation-specific fields.
    """

    # EVIDENCE REQUEST
    evidence_request: Optional[EvidenceRequest] = Field(
        None, description="Structured evidence request"
    )

    # PHASE CONTROL
    phase_complete: bool = Field(
        default=False, description="Whether phase objectives are met"
    )

    should_advance: bool = Field(
        default=False, description="Whether to advance to next phase"
    )

    advancement_rationale: Optional[str] = Field(
        None, max_length=300, description="Explanation for phase advancement"
    )

    # PHASE-SPECIFIC FIELDS (conditionally populated)
    # Phase 1: Blast Radius
    scope_assessment: Optional[ScopeAssessment] = Field(
        None, description="Blast radius assessment (Phase 1)"
    )

    # Phase 2: Timeline
    timeline_update: Optional[TimelineUpdate] = Field(
        None, description="Timeline information (Phase 2)"
    )

    # Phase 3: Hypothesis
    new_hypotheses: List[Hypothesis] = Field(
        default_factory=list, max_length=4, description="New hypotheses (Phase 3)"
    )

    # Phase 4: Validation
    hypothesis_tested: Optional[str] = Field(
        None, max_length=200, description="Hypothesis being tested (Phase 4)"
    )

    test_result: Optional[TestResult] = Field(
        None, description="Test result (Phase 4)"
    )

    # Phase 5: Solution
    solution_proposal: Optional[SolutionProposal] = Field(
        None, description="Solution proposal (Phase 5)"
    )

    # Phase 6: Document
    case_summary: Optional[CaseSummary] = Field(
        None, description="Case documentation (Phase 6)"
    )


# =============================================================================
# Utility Functions
# =============================================================================


def get_response_model_for_mode(
    engagement_mode: str,
) -> type[OODAResponse]:
    """Get appropriate response model for engagement mode

    Args:
        engagement_mode: 'consultant' or 'lead_investigator'

    Returns:
        Response model class
    """
    if engagement_mode == "consultant":
        return ConsultantResponse
    elif engagement_mode == "lead_investigator":
        return LeadInvestigatorResponse
    else:
        return OODAResponse


def create_minimal_response(answer: str) -> OODAResponse:
    """Create minimal response with just answer field

    Used as fallback when parsing fails completely.

    Args:
        answer: Natural language response text

    Returns:
        Minimal OODAResponse with only answer field
    """
    # Provide default answer if empty
    if not answer or not answer.strip():
        answer = "I'm here to assist you. How can I help with your troubleshooting needs?"

    return OODAResponse(answer=answer)
