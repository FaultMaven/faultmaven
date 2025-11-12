"""LLM Response Schemas for Milestone-Based Investigation.

These Pydantic models define the structured output format that the LLM returns
for each case status. They enable:
1. Type-safe response parsing
2. Automatic validation
3. Clear separation of concerns (response → state updates)
4. Explicit milestone and evidence tracking
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# ============================================================
# Supporting Models for State Updates
# ============================================================

class MilestoneUpdates(BaseModel):
    """
    Milestone flags from LLM indicating what was completed this turn.

    All fields are optional - LLM only sets what changed/completed.
    """

    symptom_verified: Optional[bool] = Field(
        default=None,
        description="Symptom confirmed with concrete evidence"
    )

    scope_assessed: Optional[bool] = Field(
        default=None,
        description="Scope determined (affected users/services/regions)"
    )

    timeline_established: Optional[bool] = Field(
        default=None,
        description="Timeline determined (when started, when noticed)"
    )

    changes_identified: Optional[bool] = Field(
        default=None,
        description="Recent changes identified (deployments, configs)"
    )

    root_cause_identified: Optional[bool] = Field(
        default=None,
        description="Root cause determined"
    )

    solution_proposed: Optional[bool] = Field(
        default=None,
        description="Solution or mitigation proposed"
    )

    solution_applied: Optional[bool] = Field(
        default=None,
        description="Solution applied by user"
    )

    solution_verified: Optional[bool] = Field(
        default=None,
        description="Solution effectiveness verified"
    )


class EvidenceToAdd(BaseModel):
    """
    Evidence the LLM wants to add to the case.

    NOTE: Category is NOT included here - the evidence processor
    infers it based on content and current investigation state.
    """

    raw_content: str = Field(
        description="Raw evidence content (text, JSON, log snippet, etc.)",
        max_length=50000
    )

    source: Literal["user_message", "llm_inference", "tool_output", "attachment"] = Field(
        description="Where this evidence came from"
    )

    form: Literal["text", "json", "log", "code", "metrics", "other"] = Field(
        default="text",
        description="Format of the content"
    )

    summary: Optional[str] = Field(
        default=None,
        description="Brief summary of evidence (500 chars max)",
        max_length=500
    )


class HypothesisToAdd(BaseModel):
    """
    Hypothesis the LLM wants to generate (optional, systematic investigation).
    """

    text: str = Field(
        description="Hypothesis statement",
        max_length=500
    )

    category: Literal["code", "config", "environment", "network", "data", "hardware", "external", "human", "other"] = Field(
        default="other",
        description="Hypothesis category"
    )

    likelihood: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Initial likelihood estimate (0.0-1.0)"
    )

    rationale: str = Field(
        description="Why this hypothesis is plausible",
        max_length=1000
    )


class EvidenceRequestToAdd(BaseModel):
    """
    Evidence request the LLM wants to make to the user.

    Example: "Please upload logs from the API gateway between 10:00-10:30 UTC"
    """

    request_text: str = Field(
        description="What evidence is requested",
        max_length=500
    )

    priority: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="How critical this evidence is"
    )

    purpose: str = Field(
        description="Why this evidence is needed",
        max_length=500
    )


class WorkingConclusionUpdate(BaseModel):
    """
    Agent's current understanding of the problem (updated iteratively).
    """

    summary: str = Field(
        description="Current best understanding of what's happening",
        max_length=1000
    )

    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in this understanding (0.0-1.0)"
    )

    key_uncertainties: List[str] = Field(
        default_factory=list,
        description="What is still unknown or uncertain"
    )


# ============================================================
# CONSULTING Response Schema
# ============================================================

class ConsultingStateUpdate(BaseModel):
    """
    State updates for CONSULTING status.

    Focuses on problem formalization and decision to investigate.
    """

    initial_symptoms: List[str] = Field(
        default_factory=list,
        description="Symptoms identified from conversation",
        max_items=10
    )

    proposed_problem_statement: Optional[str] = Field(
        default=None,
        description="Formalized problem statement for user confirmation",
        max_length=1000
    )

    urgency_level: Optional[Literal["critical", "high", "medium", "low", "unknown"]] = Field(
        default=None,
        description="Detected urgency level"
    )

    problem_type: Optional[str] = Field(
        default=None,
        description="Type of problem (error, performance, availability, etc.)",
        max_length=100
    )

    decided_to_investigate: bool = Field(
        default=False,
        description="Whether agent/user decided to start formal investigation"
    )

    next_clarifying_questions: List[str] = Field(
        default_factory=list,
        description="Questions agent wants answered before deciding to investigate",
        max_items=5
    )


class ConsultingResponse(BaseModel):
    """
    LLM response for CONSULTING status.

    Used when case is in pre-investigation exploration mode.
    Agent is helping user understand if they should investigate.
    """

    agent_response: str = Field(
        description="Natural language response to user",
        max_length=4000
    )

    state_update: ConsultingStateUpdate = Field(
        description="State changes for this turn"
    )

    suggested_action: Optional[str] = Field(
        default=None,
        description="What user should do next",
        max_length=500
    )


# ============================================================
# INVESTIGATING Response Schema
# ============================================================

class InvestigationStateUpdate(BaseModel):
    """
    State updates for INVESTIGATING status.

    Core of milestone-based investigation workflow.
    """

    milestones: MilestoneUpdates = Field(
        default_factory=MilestoneUpdates,
        description="Milestone completion flags"
    )

    evidence_to_add: List[EvidenceToAdd] = Field(
        default_factory=list,
        description="Evidence to add from this turn",
        max_items=10
    )

    hypotheses_to_add: List[HypothesisToAdd] = Field(
        default_factory=list,
        description="Hypotheses to generate (optional, systematic investigation)",
        max_items=5
    )

    evidence_requests: List[EvidenceRequestToAdd] = Field(
        default_factory=list,
        description="Requests for additional evidence from user",
        max_items=5
    )

    mentioned_request_ids: List[str] = Field(
        default_factory=list,
        description="Evidence request IDs agent mentioned this turn (for mention_count tracking)",
        max_items=20
    )

    working_conclusion: Optional[WorkingConclusionUpdate] = Field(
        default=None,
        description="Updated understanding of the problem"
    )

    root_cause_description: Optional[str] = Field(
        default=None,
        description="Root cause explanation (when root_cause_identified=True)",
        max_length=1000
    )

    solution_description: Optional[str] = Field(
        default=None,
        description="Proposed solution (when solution_proposed=True)",
        max_length=2000
    )


class InvestigationResponse(BaseModel):
    """
    LLM response for INVESTIGATING status.

    Used when case is in active investigation mode.
    Agent is working through milestones and gathering evidence.
    """

    agent_response: str = Field(
        description="Natural language response to user",
        max_length=4000
    )

    state_update: InvestigationStateUpdate = Field(
        description="State changes for this turn"
    )

    next_actions: List[str] = Field(
        default_factory=list,
        description="Suggested next steps for investigation",
        max_items=5
    )

    agent_status: Optional[str] = Field(
        default=None,
        description="What agent is currently doing/thinking",
        max_length=500
    )


# ============================================================
# TERMINAL Response Schema (RESOLVED/CLOSED)
# ============================================================

class DocumentToGenerate(BaseModel):
    """
    Documentation the agent can generate post-investigation.
    """

    document_type: Literal["incident_report", "post_mortem", "runbook", "timeline", "lessons_learned"] = Field(
        description="Type of document"
    )

    title: str = Field(
        description="Document title",
        max_length=200
    )

    content: str = Field(
        description="Document content (markdown format)",
        max_length=20000
    )


class TerminalStateUpdate(BaseModel):
    """
    State updates for terminal statuses (RESOLVED, CLOSED).

    Focus on documentation and lessons learned.
    """

    resolution_summary: Optional[str] = Field(
        default=None,
        description="Summary of how case was resolved",
        max_length=2000
    )

    lessons_learned: List[str] = Field(
        default_factory=list,
        description="Key takeaways from this investigation",
        max_items=10
    )

    prevention_measures: List[str] = Field(
        default_factory=list,
        description="How to prevent this in the future",
        max_items=10
    )

    documents_generated: List[DocumentToGenerate] = Field(
        default_factory=list,
        description="Documentation artifacts",
        max_items=5
    )


class TerminalResponse(BaseModel):
    """
    LLM response for terminal statuses (RESOLVED, CLOSED).

    Used when case is closed. Agent provides retrospective and documentation.
    """

    agent_response: str = Field(
        description="Natural language response to user",
        max_length=4000
    )

    state_update: TerminalStateUpdate = Field(
        description="State changes for this turn"
    )

    closure_reason: Optional[Literal["resolved", "abandoned", "escalated", "consulting_only", "duplicate", "other"]] = Field(
        default=None,
        description="Why case was closed"
    )


# ============================================================
# Response Parser Helpers
# ============================================================

def parse_llm_response(
    response_text: str,
    response_schema: type[BaseModel]
) -> BaseModel:
    """
    Parse LLM response text into structured schema.

    Args:
        response_text: JSON response from LLM
        response_schema: Target Pydantic schema

    Returns:
        Parsed response object

    Raises:
        ValidationError: If response doesn't match schema
    """
    import json

    response_dict = json.loads(response_text)
    return response_schema(**response_dict)


def get_response_schema_for_status(status: str) -> type[BaseModel]:
    """
    Get appropriate response schema for case status.

    Args:
        status: Case status (consulting, investigating, resolved, closed)

    Returns:
        Response schema class
    """
    from faultmaven.models.case import CaseStatus

    schema_map = {
        CaseStatus.CONSULTING: ConsultingResponse,
        CaseStatus.INVESTIGATING: InvestigationResponse,
        CaseStatus.RESOLVED: TerminalResponse,
        CaseStatus.CLOSED: TerminalResponse,
    }

    # Handle string status values
    if isinstance(status, str):
        status = CaseStatus(status)

    return schema_map[status]
