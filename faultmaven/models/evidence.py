"""
Evidence-Centric Troubleshooting Data Models

This module defines the data structures for evidence-based diagnostic investigation,
replacing the simple suggested_actions pattern with structured evidence requests
that include acquisition guidance.

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


# =============================================================================
# Enums
# =============================================================================


class EvidenceCategory(str, Enum):
    """Categories of diagnostic evidence"""
    SYMPTOMS = "symptoms"           # Error messages, failures, logs
    TIMELINE = "timeline"           # When did it start, what changed
    CHANGES = "changes"             # Deployments, config changes, code updates
    CONFIGURATION = "configuration"  # Settings, environment variables
    SCOPE = "scope"                 # How many users/systems affected
    METRICS = "metrics"             # Performance data, resource usage
    ENVIRONMENT = "environment"     # Infrastructure state, versions


class EvidenceStatus(str, Enum):
    """Status of evidence request fulfillment"""
    PENDING = "pending"      # User hasn't provided yet
    PARTIAL = "partial"      # Some information provided (0.3-0.7)
    COMPLETE = "complete"    # Fully answered (≥0.8)
    BLOCKED = "blocked"      # User cannot provide (no access, doesn't exist)
    OBSOLETE = "obsolete"    # No longer needed (hypothesis changed)


class EvidenceForm(str, Enum):
    """How evidence was submitted"""
    USER_INPUT = "user_input"  # Text via query/ endpoint
    DOCUMENT = "document"      # File via data/ endpoint


class EvidenceType(str, Enum):
    """How evidence relates to current hypotheses"""
    SUPPORTIVE = "supportive"  # Confirms/supports investigation direction
    REFUTING = "refuting"      # Contradicts hypothesis or expectation
    NEUTRAL = "neutral"        # Doesn't clearly support or contradict
    ABSENCE = "absence"        # User checked but evidence doesn't exist


class CompletenessLevel(str, Enum):
    """Describes how well evidence answers specific request(s)"""
    PARTIAL = "partial"              # 0.3-0.7: Some info, need more
    COMPLETE = "complete"            # 0.8-1.0: Fully answers request
    OVER_COMPLETE = "over_complete"  # Satisfies >1 request (len(matched_request_ids) > 1)


class UserIntent(str, Enum):
    """User's intent when submitting input"""
    PROVIDING_EVIDENCE = "providing_evidence"      # Answering evidence request
    ASKING_QUESTION = "asking_question"            # Asking for clarification/info
    REPORTING_UNAVAILABLE = "reporting_unavailable"  # Cannot provide evidence
    REPORTING_STATUS = "reporting_status"          # Update on progress ("working on it")
    CLARIFYING = "clarifying"                      # Asking what we mean
    OFF_TOPIC = "off_topic"                        # Unrelated to investigation


class CaseStatus(str, Enum):
    """Case lifecycle status - matches faultmaven.models.case.CaseStatus

    NOTE: This is kept for backwards compatibility with diagnostic state tracking.
    The canonical definition is in faultmaven.models.case.CaseStatus.
    These MUST match exactly to avoid validation errors.
    """
    # Active states
    CONSULTING = "consulting"      # Q&A, exploring (Phase: INTAKE)
    INVESTIGATING = "investigating" # Active troubleshooting (Phases 1-5)

    # Terminal states (cannot be changed)
    RESOLVED = "resolved"          # Closed with root cause found + solution
    CLOSED = "closed"              # Closed without resolution


# =============================================================================
# Data Models
# =============================================================================


class AcquisitionGuidance(BaseModel):
    """Instructions for obtaining diagnostic evidence"""

    commands: List[str] = Field(
        default_factory=list,
        max_length=3,
        description="Shell commands to run (max 3)"
    )
    file_locations: List[str] = Field(
        default_factory=list,
        max_length=3,
        description="File paths to check (max 3)"
    )
    ui_locations: List[str] = Field(
        default_factory=list,
        max_length=3,
        description="UI navigation paths (max 3)"
    )
    alternatives: List[str] = Field(
        default_factory=list,
        max_length=3,
        description="Alternative methods (max 3)"
    )
    prerequisites: List[str] = Field(
        default_factory=list,
        max_length=2,
        description="Requirements to obtain evidence (max 2)"
    )
    expected_output: Optional[str] = Field(
        None,
        max_length=200,
        description="What user should expect to see"
    )

    @field_validator('commands', 'file_locations', 'ui_locations', 'alternatives')
    @classmethod
    def validate_max_items(cls, v: List[str]) -> List[str]:
        """Ensure lists don't exceed max items"""
        if len(v) > 3:
            raise ValueError("Maximum 3 items allowed")
        return v

    @field_validator('prerequisites')
    @classmethod
    def validate_prerequisites(cls, v: List[str]) -> List[str]:
        """Ensure prerequisites don't exceed max items"""
        if len(v) > 2:
            raise ValueError("Maximum 2 prerequisites allowed")
        return v


class EvidenceRequest(BaseModel):
    """Structured request for diagnostic evidence with acquisition guidance

    OODA Integration: Generated during OODA Observe step, linked to hypothesis testing
    """

    request_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique identifier for this evidence request"
    )
    label: str = Field(
        ...,
        max_length=100,
        description="Brief title for the request"
    )
    description: str = Field(
        ...,
        max_length=500,
        description="What evidence is needed and why"
    )
    category: EvidenceCategory = Field(
        ...,
        description="Category of diagnostic evidence"
    )
    guidance: AcquisitionGuidance = Field(
        ...,
        description="Instructions for obtaining evidence"
    )
    status: EvidenceStatus = Field(
        default=EvidenceStatus.PENDING,
        description="Fulfillment status"
    )
    created_at_turn: int = Field(
        ...,
        ge=0,
        description="Turn number when request was created"
    )
    updated_at_turn: Optional[int] = Field(
        None,
        ge=0,
        description="Turn number when last updated"
    )
    completeness: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Fulfillment completeness score"
    )

    # OODA Integration (v3.2.0)
    requested_by_ooda_step: Optional[str] = Field(
        None,
        description="Which OODA step generated this (observe, orient, decide, act)"
    )
    for_hypothesis_id: Optional[str] = Field(
        None,
        description="Hypothesis ID this evidence tests (Phase 4 validation)"
    )
    priority: int = Field(
        default=2,
        ge=1,
        le=3,
        description="1=critical, 2=important, 3=nice-to-have"
    )

    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "request_id": "er-001",
                "label": "Error rate metrics",
                "description": "Current error rate vs baseline to quantify severity",
                "category": "metrics",
                "guidance": {
                    "commands": ["kubectl logs -l app=api --since=2h | grep '500' | wc -l"],
                    "file_locations": [],
                    "ui_locations": ["Datadog > API Errors Dashboard"],
                    "alternatives": ["Check New Relic error rate graph"],
                    "prerequisites": ["kubectl access"],
                    "expected_output": "Error count (baseline: 2-3/hour)"
                },
                "status": "pending",
                "created_at_turn": 1,
                "completeness": 0.0
            }
        }


class FileMetadata(BaseModel):
    """Metadata for uploaded files (documents, log excerpts)"""

    filename: str = Field(..., description="Original filename")
    content_type: str = Field(..., description="MIME type (e.g., text/plain, application/json)")
    size_bytes: int = Field(..., ge=0, description="File size in bytes")
    upload_timestamp: datetime = Field(..., description="When file was uploaded (ISO 8601)")
    file_id: str = Field(..., description="Storage reference ID")


class EvidenceProvided(BaseModel):
    """Record of evidence user provided"""

    evidence_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique identifier"
    )
    turn_number: int = Field(..., ge=1, description="Turn when evidence was provided")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="When evidence was provided")

    # Content
    form: EvidenceForm = Field(..., description="How evidence was submitted")
    content: str = Field(..., description="Text or file reference/path")
    file_metadata: Optional[FileMetadata] = Field(
        None,
        description="Populated when form == DOCUMENT"
    )

    # Classification
    addresses_requests: List[str] = Field(
        default_factory=list,
        description="Evidence request IDs this satisfies"
    )
    completeness: CompletenessLevel = Field(..., description="Completeness level")
    evidence_type: EvidenceType = Field(..., description="Evidential value")
    user_intent: UserIntent = Field(..., description="User's intent")

    # Analysis
    key_findings: List[str] = Field(
        default_factory=list,
        description="Key insights extracted from evidence"
    )
    confidence_impact: Optional[float] = Field(
        None,
        ge=-1.0,
        le=1.0,
        description="Impact on confidence score (-1 to 1)"
    )


class EvidenceClassification(BaseModel):
    """Multi-dimensional classification of user input"""

    # Dimension 1: Request matching
    matched_request_ids: List[str] = Field(
        default_factory=list,
        description="Which evidence request IDs this addresses (0 to N)"
    )

    # Dimension 2: Completeness
    completeness: CompletenessLevel = Field(..., description="Completeness level")
    completeness_score: float = Field(..., ge=0.0, le=1.0, description="Numeric score")

    # Dimension 3: Form
    form: EvidenceForm = Field(..., description="user_input or document")

    # Dimension 4: Evidence type
    evidence_type: EvidenceType = Field(..., description="Evidential value")

    # Dimension 5: User intent
    user_intent: UserIntent = Field(..., description="What user is trying to do")

    # Context
    rationale: Optional[str] = Field(None, description="Why this classification was made")
    follow_up_needed: Optional[str] = Field(None, description="What follow-up is needed")


class ImmediateAnalysis(BaseModel):
    """Immediate feedback after file upload"""

    matched_requests: List[str] = Field(
        default_factory=list,
        description="Evidence request IDs this data satisfies"
    )
    completeness_scores: Dict[str, float] = Field(
        default_factory=dict,
        description="Map of request_id to completeness score (0-1)"
    )
    key_findings: List[str] = Field(
        default_factory=list,
        max_length=5,
        description="Top findings from the uploaded data"
    )
    evidence_type: EvidenceType = Field(..., description="How evidence relates to hypotheses")
    next_steps: str = Field(..., description="What the agent will do next")


class ConflictDetection(BaseModel):
    """Detection of refuting evidence"""

    contradicted_hypothesis: str = Field(..., description="Which hypothesis is contradicted")
    reason: str = Field(..., description="Why this is a conflict")
    confirmation_required: bool = Field(True, description="User must confirm refutation")


class DataUploadResponse(BaseModel):
    """Response from data upload with immediate analysis"""

    data_id: str = Field(..., description="Unique identifier for uploaded data")
    filename: str = Field(..., description="Original filename")
    file_metadata: FileMetadata = Field(..., description="File metadata")
    immediate_analysis: ImmediateAnalysis = Field(..., description="Immediate classification and feedback")
    conflict_detected: Optional[ConflictDetection] = Field(
        None,
        description="Present only if refuting evidence detected"
    )
