"""
Report Generation Models - Case Documentation & Closure (FR-CM-006)

Data models for the case documentation generation feature including:
- Report types (Incident Report, Runbook, Post-Mortem)
- Report metadata and versioning
- Intelligent runbook recommendations with similarity search
- Dual-source runbook support (incident-driven + document-driven)

Version: 2.0 (Updated with intelligent recommendations)
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Literal
from enum import Enum
from datetime import datetime, timezone
from faultmaven.utils.serialization import to_json_compatible
import uuid


class ReportType(str, Enum):
    """Type of case documentation report"""
    INCIDENT_REPORT = "incident_report"
    RUNBOOK = "runbook"
    POST_MORTEM = "post_mortem"


class ReportStatus(str, Enum):
    """Report generation status"""
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


class RunbookSource(str, Enum):
    """Origin of runbook content"""
    INCIDENT_DRIVEN = "incident_driven"  # Generated from resolved incident
    DOCUMENT_DRIVEN = "document_driven"  # Generated from uploaded documentation


class RunbookMetadata(BaseModel):
    """
    Metadata for runbook reports supporting dual sources.
    Tracks origin (incident vs document) for transparency.
    """
    source: RunbookSource = Field(..., description="Origin of runbook")

    # For incident-driven runbooks
    case_context: Optional[Dict[str, Any]] = Field(
        None,
        description="Case investigation context (incident-driven only)"
    )

    # For document-driven runbooks
    document_title: Optional[str] = Field(
        None,
        description="Source document title (document-driven only)"
    )
    original_document_id: Optional[str] = Field(
        None,
        description="Reference to uploaded document (document-driven only)"
    )

    # Common metadata
    domain: str = Field(..., description="Technology domain for filtering")
    tags: List[str] = Field(default_factory=list, description="Classification tags")
    llm_model: Optional[str] = Field(None, description="LLM model used for generation")
    embedding_model: Optional[str] = Field(None, description="Embedding model for vector search")


class CaseReport(BaseModel):
    """
    Generated case documentation report (DR-005).
    Supports DUAL runbook sources:
    - Incident-driven: Generated from case resolution
    - Document-driven: Generated from uploaded documentation
    """
    report_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique report identifier (UUID v4)"
    )
    case_id: str = Field(
        ...,
        description="Foreign key to parent case (or 'doc-derived' for document-driven)"
    )
    report_type: ReportType = Field(..., description="Type of report")
    title: str = Field(..., min_length=10, max_length=200, description="Human-readable title")
    content: str = Field(..., description="Full report content in Markdown format")
    format: Literal["markdown"] = Field(default="markdown", description="Report format")
    generation_status: ReportStatus = Field(..., description="Generation status")
    generated_at: str = Field(
        default_factory=lambda: to_json_compatible(datetime.now(timezone.utc)),
        description="ISO 8601 timestamp"
    )
    generation_time_ms: int = Field(..., ge=0, le=120000, description="Generation time (ms)")
    is_current: bool = Field(default=True, description="Latest version for this report_type")
    version: int = Field(default=1, ge=1, le=5, description="Version number")
    linked_to_closure: bool = Field(default=False, description="Linked to case closure")
    metadata: Optional[RunbookMetadata] = Field(None, description="Runbook-specific metadata")


class SimilarRunbook(BaseModel):
    """Similar runbook search result from knowledge base"""
    runbook: CaseReport = Field(..., description="The similar runbook")
    similarity_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Cosine similarity score from vector search"
    )
    case_title: str = Field(
        ...,
        description="Title of case that generated this runbook (or document title)"
    )
    case_id: str = Field(
        ...,
        description="Case ID (or 'doc-derived' for document-driven runbooks)"
    )


class RunbookRecommendation(BaseModel):
    """Runbook-specific recommendation with similarity analysis"""
    action: Literal["reuse", "review_or_generate", "generate"] = Field(
        ...,
        description=(
            "Recommended action:\n"
            "- reuse: High similarity (≥85%), recommend using existing runbook\n"
            "- review_or_generate: Moderate similarity (70-84%), offer both options\n"
            "- generate: Low/no similarity (<70%), recommend generating new runbook"
        )
    )
    existing_runbook: Optional[CaseReport] = Field(
        None,
        description="Existing similar runbook (if found)"
    )
    similarity_score: Optional[float] = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Semantic similarity score (0.0-1.0)"
    )
    reason: str = Field(
        ...,
        max_length=500,
        description="Human-readable explanation of recommendation"
    )


class ReportRecommendation(BaseModel):
    """Intelligent recommendations for report generation"""
    case_id: str = Field(..., description="Case identifier")
    available_for_generation: List[ReportType] = Field(
        ...,
        description=(
            "Report types available for generation.\n"
            "- Always includes: incident_report, post_mortem\n"
            "- Conditionally includes: runbook (based on similarity search)"
        )
    )
    runbook_recommendation: RunbookRecommendation = Field(
        ...,
        description="Runbook-specific recommendation"
    )


class ReportGenerationRequest(BaseModel):
    """Request to generate case documentation reports"""
    report_types: List[ReportType] = Field(
        ...,
        min_length=1,
        max_length=3,
        description="Types of reports to generate"
    )


class ReportGenerationResponse(BaseModel):
    """Response after generating reports"""
    case_id: str = Field(..., description="Case identifier")
    reports: List[CaseReport] = Field(..., description="Generated reports")
    remaining_regenerations: int = Field(
        ...,
        ge=0,
        le=5,
        description="Number of regenerations remaining (max 5 per report type)"
    )


class CaseClosureRequest(BaseModel):
    """Request to close a case"""
    closure_note: Optional[str] = Field(
        None,
        max_length=500,
        description="Optional closure note"
    )


class CaseClosureResponse(BaseModel):
    """Response after closing a case"""
    case_id: str = Field(..., description="Case identifier")
    closed_at: str = Field(..., description="Closure timestamp (ISO 8601)")
    archived_reports: List[CaseReport] = Field(..., description="Reports linked to closure")
    download_available_until: str = Field(
        ...,
        description="Reports download expiry (ISO 8601, 90 days from closure)"
    )
