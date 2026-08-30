"""Report Domain Models - Owned by Case Module.

Per module-organization-design.md:
- Case module owns the reports table (FK to cases)
- Report module is a Domain Service that operates on Case-owned data
- These models are canonical and should be imported from Case contracts

Data models for the case documentation generation feature including:
- Report types (Incident Report, Runbook, Post-Mortem)
- Report metadata and versioning
- Intelligent runbook recommendations with similarity search
- Dual-source runbook support (incident-driven + document-driven)

Version: 2.0 (Updated with intelligent recommendations)
"""

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from faultmaven.utils.serialization import to_json_compatible


# ``ReportType`` is a WIDER vocabulary than ``reports.report_type``, and the
# asymmetry is deliberate rather than drift (#520). Kept as a comment, not as
# the enum's docstring: that docstring is published verbatim as the schema
# description in ``docs/reference/api/openapi.json``, and this is internal
# rationale.
#
# Two members are ``reports`` rows. ``RUNBOOK`` is not, and never was:
# ``reports_type_check`` has admitted exactly ``resolution_summary`` and
# ``closure_summary`` since the clean baseline, and a runbook lives in
# ``conversion_drafts`` / ``knowledge_items`` instead. It is a member because
# the API genuinely uses it as a *projection* vocabulary —
# ``GET /cases/{id}/reports?report_type=runbook`` filters case-linked drafts
# projected into the report shape, and the report recommendation advertises it
# as something the user can go and make (via ConversionService).
#
#   - The STORAGE vocabulary is ``PERSISTED_REPORT_TYPES`` below, and it must
#     equal ``reports_type_check`` exactly. That is what a repository may write
#     and what ``ReportType(row.report_type)`` may have to parse.
#   - The API vocabulary is the enum, and it is a strict superset.
#
# Widening the CHECK to admit ``runbook`` was the other way to close the
# divergence and would have been wrong: it makes half-formed runbook rows
# writable into a table whose whole point is that runbooks are not in it.
# Narrowing the enum would have been wrong too — it is reachable API surface.
# What was missing is that the subset relation was undeclared, so nothing
# stopped a writer reaching for ``RUNBOOK`` and getting a bare
# ``IntegrityError``, and nothing kept the two halves in step.
#
# ``tests/unit/modules/case/test_report_vocabulary.py`` pins the storage half
# against the ORM CheckConstraint AND against the migration that owns it, and
# pins the non-persistable remainder to exactly ``{RUNBOOK}`` so a new member
# cannot be added without deciding which half it belongs to.
class ReportType(str, Enum):
    """Type of case documentation report"""

    # Auto-generated on terminal transition — these ARE ``reports`` rows.
    RESOLUTION_SUMMARY = "resolution_summary"  # RESOLVED cases
    CLOSURE_SUMMARY = "closure_summary"  # CLOSED cases

    # User-requested via ConversionService — NOT a ``reports`` row; the
    # artifact lands in conversion_drafts/knowledge_items.
    RUNBOOK = "runbook"  # From RESOLVED cases (requires root cause)


#: The subset of :class:`ReportType` that ``reports_type_check`` admits, i.e.
#: the only values a repository may write to ``reports.report_type``.
#: Single source of truth for that judgment — the generation service refuses
#: anything outside it up front rather than letting the database do it with a
#: bare ``IntegrityError``, and the vocabulary test pins it to the constraint.
PERSISTED_REPORT_TYPES: frozenset[ReportType] = frozenset(
    {ReportType.RESOLUTION_SUMMARY, ReportType.CLOSURE_SUMMARY}
)


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
        None, description="Case investigation context (incident-driven only)"
    )

    # For document-driven runbooks
    document_title: Optional[str] = Field(
        None, description="Source document title (document-driven only)"
    )
    original_document_id: Optional[str] = Field(
        None, description="Reference to uploaded document (document-driven only)"
    )

    # Common metadata
    domain: str = Field(..., description="Technology domain for filtering")
    tags: List[str] = Field(default_factory=list, description="Classification tags")
    llm_model: Optional[str] = Field(None, description="LLM model used for generation")
    embedding_model: Optional[str] = Field(
        None, description="Embedding model for vector search"
    )


class CaseReport(BaseModel):
    """
    Generated case documentation report (DR-005).
    Supports DUAL runbook sources:
    - Incident-driven: Generated from case resolution
    - Document-driven: Generated from uploaded documentation
    """

    report_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique report identifier (UUID v4)",
    )
    case_id: str = Field(
        ...,
        description="Foreign key to parent case (or 'doc-derived' for document-driven)",
    )
    report_type: ReportType = Field(..., description="Type of report")
    title: str = Field(
        ..., min_length=10, max_length=200, description="Human-readable title"
    )
    content: str = Field(..., description="Full report content in Markdown format")
    # ``reports_format_check`` admits 'markdown' and 'html'; this Literal
    # admitted only 'markdown'. That is a load-path constraint, not a write-path
    # one: a stored 'html' row is hydrated straight into this model by
    # ``_row_to_report``, so the narrow Literal turned a row the database
    # accepts into a ValidationError — a 500 on read of an already-persisted
    # report (#520). The database is the wider half of this pair, so the code
    # side is what widens; the alternative, dropping 'html' from the CHECK, is a
    # tightening that could reject a row valid today. Nothing writes 'html'
    # today, which bounds the blast radius but does not remove the hazard.
    format: Literal["markdown", "html"] = Field(
        default="markdown", description="Report format"
    )
    generation_status: ReportStatus = Field(..., description="Generation status")
    generated_at: str = Field(
        default_factory=lambda: to_json_compatible(datetime.now(timezone.utc)),
        description="ISO 8601 timestamp when report was first generated",
    )
    updated_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 timestamp when report was last updated (None for new reports, set on update)",
    )
    generation_time_ms: int = Field(
        ..., ge=0, le=120000, description="Generation time (ms)"
    )
    is_current: bool = Field(
        default=True, description="Latest version for this report_type"
    )
    version: int = Field(default=1, ge=1, le=5, description="Version number")
    linked_to_closure: bool = Field(default=False, description="Linked to case closure")
    auto_generated: bool = Field(
        default=False,
        description="True for auto-generated terminal summaries, False for user-requested reports",
    )
    metadata: Optional[RunbookMetadata] = Field(
        None, description="Runbook-specific metadata"
    )


class RunbookRef(BaseModel):
    """Reference to an existing published runbook in the knowledge base.

    An honest reference to a live KB item (``knowledge_items`` row), not a
    reconstructed report. The previous shape minted a ``CaseReport`` with an
    invented ``generation_status``/``version`` and a defaulted ``generated_at``
    — a report row that never existed.
    """

    item_id: str = Field(..., description="KB item id (knowledge_items.item_id)")
    title: str = Field(..., description="Runbook title")
    scope: str = Field(
        ..., description="Visibility floor of the KB item ('personal' or 'global')"
    )


class RunbookRecommendation(BaseModel):
    """Runbook-specific recommendation with similarity analysis"""

    action: Literal["review_or_generate", "generate"] = Field(
        ...,
        description=(
            "Recommended action:\n"
            "- review_or_generate: A similar runbook exists (>=70% best-chunk "
            "similarity); review it or generate a new one\n"
            "- generate: Low/no similarity (<70%), recommend generating new runbook"
        ),
    )
    existing_runbook: Optional[RunbookRef] = Field(
        None, description="Existing similar runbook in the KB (if found)"
    )
    similarity_score: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="Semantic similarity score (0.0-1.0)"
    )
    reason: str = Field(
        ..., max_length=500, description="Human-readable explanation of recommendation"
    )


class ReportRecommendation(BaseModel):
    """Intelligent recommendations for report generation"""

    case_id: str = Field(..., description="Case identifier")
    available_for_generation: List[ReportType] = Field(
        ...,
        description=(
            "Report types available.\n"
            "- Auto-generated: resolution_summary (resolved), closure_summary (closed)\n"
            "- User-requested: runbook (via ConversionService)"
        ),
    )
    runbook_recommendation: RunbookRecommendation = Field(
        ..., description="Runbook-specific recommendation"
    )


class ReportGenerationRequest(BaseModel):
    """Request to generate case documentation reports"""

    report_types: List[ReportType] = Field(
        ..., min_length=1, max_length=3, description="Types of reports to generate"
    )


class ReportGenerationResponse(BaseModel):
    """Response after generating reports"""

    case_id: str = Field(..., description="Case identifier")
    reports: List[CaseReport] = Field(..., description="Generated reports")
    remaining_regenerations: int = Field(
        ...,
        ge=0,
        le=5,
        description="Number of regenerations remaining (max 5 per report type)",
    )


class CaseClosureRequest(BaseModel):
    """Request to close a case"""

    closure_note: Optional[str] = Field(
        None, max_length=500, description="Optional closure note"
    )


class CaseClosureResponse(BaseModel):
    """Response after closing a case"""

    case_id: str = Field(..., description="Case identifier")
    closed_at: str = Field(..., description="Closure timestamp (ISO 8601)")
    archived_reports: List[CaseReport] = Field(
        ..., description="Reports linked to closure"
    )
    download_available_until: str = Field(
        ..., description="Reports download expiry (ISO 8601, 90 days from closure)"
    )


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Enumerations
    "ReportType",
    "PERSISTED_REPORT_TYPES",
    "ReportStatus",
    "RunbookSource",
    # Models
    "RunbookMetadata",
    "CaseReport",
    "RunbookRef",
    "RunbookRecommendation",
    "ReportRecommendation",
    # Request/Response DTOs
    "ReportGenerationRequest",
    "ReportGenerationResponse",
    "CaseClosureRequest",
    "CaseClosureResponse",
]
