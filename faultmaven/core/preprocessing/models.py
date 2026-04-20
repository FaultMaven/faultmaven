"""
Preprocessing Models

Defines the core types for the four-tier preprocessing pipeline:
- Tier 0: Classification (DataType, ClassificationResult)
- Tier 1: Mechanical Extraction (ExtractionResult, PreprocessingResult)
- Tier 2: Mechanical Search (search_file agent tool + BasicTier2Service backends)
- Tier 3: Interpreted Search / Deep Analysis (DeepAnalysisResult, AnalysisContext, DataExcerpt)
- Vector DB: Chunk model for structural index storage

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from faultmaven.models.api import DataType as DetailedDataType

# =============================================================================
# Extraction Method Vocabulary (shared by ExtractionResult + PreprocessingResult)
# =============================================================================
#
# Every Tier 1 extractor sets its `strategy_name` to one of the domain strategy
# values below. The orchestrator may override with a runtime marker when the
# standard path is bypassed (timeout, error, passthrough, UNANALYZABLE, or
# classification_failed).
#
# Adding a new extractor? Add its strategy_name here so Pydantic can validate.

ExtractionMethod = Literal[
    # Domain strategies (from extractors)
    "crime_scene",  # LogsAndErrorsExtractor
    "exception_context",  # ErrorReportExtractor
    "trace_correlation",  # TraceDataExtractor
    "command_parsing",  # CommandOutputExtractor
    "statistical",  # MetricsAndPerformanceExtractor
    "profiling_hotspot",  # ProfilingDataExtractor
    "ast_parse",  # SourceCodeExtractor
    "documentation_structure",  # DocumentationExtractor
    "vision",  # VisualEvidenceExtractor
    "direct",  # StructuredConfigExtractor / UnstructuredTextExtractor
    # Runtime markers (set by PreprocessingService)
    "page_capture_passthrough",  # page captures bypass extraction
    "structure_extraction",  # timeout/error fallback to TEXT preview
    "none",  # UNANALYZABLE short-circuit
    "classification_failed",  # low-confidence short-circuit for user modal
]

# =============================================================================
# Unified DataType (6 types — shared across preprocessing and evidence)
# =============================================================================


class UnifiedDataType(str, Enum):
    """
    Data type classification — shared across preprocessing and evidence.

    These 6 types are the public interface. Internal preprocessing uses
    the fine-grained DetailedDataType (12 types) for extractor dispatch,
    then maps to these 6 unified types for PreprocessingResult and Evidence.

    Design Reference:
        data-preprocessing-design-specification.md Section 2.3
    """

    LOGS = "logs"  # Time-ordered diagnostic output (logs, traces, command output)
    METRICS = "metrics"  # Quantitative measurements (time-series, dashboards, alerts)
    CONFIGURATION = (
        "configuration"  # Structured system/app config (YAML, JSON, TOML, env)
    )
    CODE = "code"  # Source code files
    TEXT = "text"  # Unstructured prose (docs, runbooks, descriptions)
    IMAGE = "image"  # Visual content (screenshots, diagrams)


# Mapping from fine-grained internal types to unified types
_DETAILED_TO_UNIFIED: Dict[DetailedDataType, UnifiedDataType] = {
    DetailedDataType.LOGS_AND_ERRORS: UnifiedDataType.LOGS,
    DetailedDataType.ERROR_REPORT: UnifiedDataType.LOGS,
    DetailedDataType.TRACE_DATA: UnifiedDataType.LOGS,
    DetailedDataType.COMMAND_OUTPUT: UnifiedDataType.LOGS,
    DetailedDataType.METRICS_AND_PERFORMANCE: UnifiedDataType.METRICS,
    DetailedDataType.PROFILING_DATA: UnifiedDataType.METRICS,
    DetailedDataType.STRUCTURED_CONFIG: UnifiedDataType.CONFIGURATION,
    DetailedDataType.SOURCE_CODE: UnifiedDataType.CODE,
    DetailedDataType.UNSTRUCTURED_TEXT: UnifiedDataType.TEXT,
    DetailedDataType.DOCUMENTATION: UnifiedDataType.TEXT,
    DetailedDataType.VISUAL_EVIDENCE: UnifiedDataType.IMAGE,
    DetailedDataType.UNANALYZABLE: UnifiedDataType.TEXT,
}


def to_unified_data_type(detailed: DetailedDataType) -> UnifiedDataType:
    """Map a fine-grained internal DataType to the unified 6-type enum."""
    return _DETAILED_TO_UNIFIED.get(detailed, UnifiedDataType.TEXT)


# =============================================================================
# Tier 0+1 Output: PreprocessingResult
# =============================================================================


class ExtractionResult(BaseModel):
    """
    Output from a single Tier 1 extractor.

    The 11 extractors return this; the preprocessing orchestrator wraps it
    into a full PreprocessingResult. See `ExtractionMethod` for the allowed
    `method` values.
    """

    method: ExtractionMethod = Field(
        description="Extractor strategy_name or runtime marker"
    )
    content: str = Field(description="Extracted structural index / processed content")
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Type-specific extraction metadata",
    )


class PreprocessingResult(BaseModel):
    """
    Output from Tier 0 + Tier 1 preprocessing — the bridge from raw
    uploaded data to the Evidence Architecture.

    Carries the structural index (for agent analysis and vector DB),
    a concise summary (for `Evidence.summary`), and extraction metadata.

    PII redaction is NOT performed here — it runs at the LLM boundary
    (MilestoneEngine). Structural indexes are stored raw.

    Design Reference:
        data-preprocessing-design-specification.md Section 7.1
    """

    # Identity
    temp_id: str = Field(
        default_factory=lambda: f"tmp_{uuid4().hex[:12]}",
        description="Temporary ID before Evidence object is created",
    )

    # Classification (Tier 0)
    data_type: UnifiedDataType = Field(description="Classified data type (unified)")
    detailed_data_type: DetailedDataType = Field(
        description="Fine-grained data type from internal classifier"
    )

    # Structural Index (Tier 1) — Two levels
    summary: str = Field(
        max_length=500,
        description="Concise summary for Evidence.summary (<500 chars)",
    )
    structural_index: str = Field(
        description="Complete structural index (for agent analysis and vector DB)"
    )

    # Raw File Storage
    content_ref: Optional[str] = Field(
        None, description="Reference to stored raw file (for Tier 2 access)"
    )
    content_size_bytes: int = Field(description="Size of raw file in bytes")
    content_type: str = Field(description="MIME type of original file")

    # Extraction metadata
    extraction_method: ExtractionMethod = Field(
        description="Extractor strategy_name or runtime marker (see ExtractionMethod)"
    )
    compression_ratio: float = Field(
        ge=0.0,
        description="Ratio of index size to raw size",
    )
    extraction_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Type-specific metadata (error counts, anomaly details, etc.)",
    )

    # Deduplication
    content_hash: str = Field(description="SHA-256 hash of raw file content")

    # Performance
    processing_time_ms: int = Field(
        default=0, description="Total Tier 0+1 time in milliseconds"
    )


# =============================================================================
# Tier 2: Deep Analysis Models
# =============================================================================


class AnalysisContext(BaseModel):
    """Context passed to Tier 2 for better analysis."""

    case_id: str
    case_summary: Optional[str] = None
    active_hypotheses: Optional[List[str]] = None
    investigation_stage: Optional[str] = None


class DataExcerpt(BaseModel):
    """A relevant section from the raw data supporting the analysis."""

    content: str
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    relevance: float = 0.0


class DeepAnalysisResult(BaseModel):
    """
    Result from Tier 2 deep analysis.

    Design Reference:
        data-preprocessing-design-specification.md Section 7.2
    """

    answer: str = Field(description="LLM-generated analysis answering the query")
    excerpts: List[DataExcerpt] = Field(
        default_factory=list,
        description="Relevant raw data sections with line numbers",
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Analysis confidence"
    )
    tokens_used: int = Field(default=0)
    processing_time_ms: int = Field(default=0)
    backend_used: str = Field(default="", description="Which Tier 2 backend was used")


# =============================================================================
# Vector DB Chunking
# =============================================================================


@dataclass
class Chunk:
    """A chunk of structural index for vector DB storage."""

    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Supporting Types
# =============================================================================


@dataclass
class FileInfo:
    """Information about an uploaded file for preprocessing."""

    filename: str
    mime_type: str
    raw_content: bytes
    extension: str = ""
    processing_time_ms: float = 0.0


def generate_concise_summary(text: str, max_length: int = 500) -> str:
    """
    Generate concise summary without LLM — take beginning and end.

    Design Reference:
        data-preprocessing-design-specification.md Section 4.10
    """
    if len(text) <= max_length:
        return text
    separator = "... [truncated] ..."
    half = (max_length - len(separator)) // 2
    return f"{text[:half]}{separator}{text[-half:]}"
