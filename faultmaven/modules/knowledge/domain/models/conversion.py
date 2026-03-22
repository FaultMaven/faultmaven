"""Domain models for document-to-runbook conversion.

Defines the data structures for the conversion pipeline:
- ConversionJob: Tracks a conversion request lifecycle
- ConversionDraft: Individual runbook draft generated from a source document
- Analysis models: LLM analysis and failure mode detection results
- Request/Response models: API contract types
"""

import hashlib
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


# =============================================================================
# Enums
# =============================================================================


class ConversionStatus(str, Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class DraftStatus(str, Enum):
    DRAFT = "draft"
    VERIFIED = "verified"
    DELETED = "deleted"


# =============================================================================
# LLM Analysis Models (structured output from analysis call)
# =============================================================================


class FailureModeAnalysis(BaseModel):
    id: str
    title: str
    domain: str
    service: str
    symptom_class: List[str]
    severity: str
    symptoms_summary: str
    resolution_summary: str


class SourceAssessment(BaseModel):
    content_type: str
    actionability_rating: str = Field(description="low, medium, or high")
    missing_information: List[str]


class AnalysisResult(BaseModel):
    is_actionable: bool
    failure_modes: List[FailureModeAnalysis]
    source_assessment: SourceAssessment


# =============================================================================
# Content Triage Model (structured output from classifier call)
# =============================================================================


class TriageResult(BaseModel):
    is_actionable: bool
    confidence: float
    reason: str


# =============================================================================
# Preprocessing Result
# =============================================================================


class RedactionEntry(BaseModel):
    type: str
    count: int
    context: str


class RedactionReport(BaseModel):
    redactions: List[RedactionEntry] = Field(default_factory=list)
    warning: Optional[str] = None
    total_redacted: int = 0


class PreprocessingResult(BaseModel):
    extracted_text: str
    source_metadata: dict
    redaction_report: RedactionReport = Field(default_factory=RedactionReport)
    triage_result: Optional[TriageResult] = None
    warnings: List[str] = Field(default_factory=list)
    is_rejected: bool = False
    rejection_reason: Optional[str] = None
    token_count: int = 0


# =============================================================================
# Validation and Quality Models
# =============================================================================


class ValidationResult(BaseModel):
    passed: bool
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class QualityScore(BaseModel):
    overall: float
    grade: str
    completeness: float
    clarity: float
    actionability: float
    comprehensiveness: float


# =============================================================================
# Source File Info
# =============================================================================


class SourceFileInfo(BaseModel):
    filename: str
    size_bytes: int
    content_type: str
    retained_path: str


# =============================================================================
# Conversion Draft
# =============================================================================


class ConversionDraft(BaseModel):
    draft_id: str
    runbook_id: str
    title: str
    scope: str
    status: DraftStatus = DraftStatus.DRAFT
    validation: ValidationResult
    quality_score: QualityScore
    file_path: str
    content_preview: str = Field(
        max_length=500, description="First 500 chars of generated markdown"
    )
    content: Optional[str] = Field(
        default=None, description="Full markdown content, included on detail requests"
    )
    quality_warning: Optional[str] = Field(
        default=None, description="Warning if quality score < 50"
    )


# =============================================================================
# Conversion Error (for partial failures)
# =============================================================================


class ConversionError(BaseModel):
    failure_mode_id: str
    error: str
    retryable: bool = False


# =============================================================================
# Conversion Response
# =============================================================================


class ConversionResponse(BaseModel):
    conversion_id: str
    status: ConversionStatus
    source_file: SourceFileInfo
    analysis: AnalysisResult
    drafts: List[ConversionDraft]
    warnings: List[str] = Field(default_factory=list)
    created_at: datetime


# =============================================================================
# Draft Management Request/Response
# =============================================================================


class DraftUpdateRequest(BaseModel):
    content: str = Field(
        min_length=100,
        description="Full runbook markdown content including frontmatter",
    )


class VerifyResponse(BaseModel):
    draft_id: str
    runbook_id: str
    status: str = "verified"
    knowledge_item_id: str
    ingested: bool
    ingested_at: Optional[datetime] = None
    collection: str
    chunks_created: int


# =============================================================================
# ID Generation Utilities
# =============================================================================


def generate_conversion_id() -> str:
    return f"conv_{uuid.uuid4().hex[:12]}"


def generate_draft_id() -> str:
    return f"draft_{uuid.uuid4().hex[:12]}"


def generate_runbook_id(failure_mode: FailureModeAnalysis) -> str:
    """Generate kebab-case ID from service and failure description."""
    base = f"{failure_mode.service}-{failure_mode.title}"
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    if len(slug) > 60:
        slug = slug[:55] + "-" + hashlib.md5(slug.encode()).hexdigest()[:4]
    return slug
