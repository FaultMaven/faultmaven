"""
Preprocessing Models — Design Specification v3.0

Defines the core types for the three-tier preprocessing pipeline:
- Tier 0: Classification (DataType, ClassificationResult)
- Tier 1: Mechanical Extraction (ExtractionResult, PreprocessingResult)
- Tier 2: Deep Analysis (DeepAnalysisResult, AnalysisContext, DataExcerpt)
- Vector DB: Chunk model for structural index storage
- Errors: FileTooLargeError, DuplicateFileError

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from faultmaven.models.api import DataType as DetailedDataType

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

    Each extractor (logs, metrics, config, code, text, image) returns this.
    The preprocessing orchestrator wraps it into a full PreprocessingResult.
    """

    method: str = Field(
        description=(
            "Extraction method: structural_index, statistical_profile, "
            "parse_and_sanitize, ast_extraction, structure_extraction, "
            "metadata_extraction"
        )
    )
    content: str = Field(description="Extracted structural index / processed content")
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Type-specific extraction metadata",
    )


class SanitizationResult(BaseModel):
    """Output from the sanitization step."""

    content: str = Field(description="Content with PII/secrets redacted")
    redactions_made: int = Field(default=0, description="Number of redactions applied")
    redactions: List[tuple] = Field(
        default_factory=list,
        description="List of (type, count) redaction details",
    )
    skipped: bool = Field(
        default=False,
        description="True if sanitization was skipped (e.g., local LLM provider)",
    )


class ErrorSummary(BaseModel):
    """Structured insights from log structural index."""

    total_errors: int
    severity_distribution: Dict[str, int]
    first_error_line: int
    last_error_line: int
    error_burst_detected: bool
    unique_error_types: List[str]


class AnomalySummary(BaseModel):
    """Structured insights from metrics statistical profile."""

    total_anomalies: int
    metrics_analyzed: List[str]
    anomaly_types: Dict[str, int]
    most_anomalous_metric: str
    time_range: str


class ConfigSummary(BaseModel):
    """Structured insights from config parsing."""

    format: str
    total_keys: int
    secrets_found: int
    secrets_redacted: bool
    validation_status: str


class PreprocessingResult(BaseModel):
    """
    Output from Tier 0 + Tier 1 preprocessing.

    This is the bridge between raw uploaded data and the Evidence Architecture.
    Contains the structural index (for agent analysis and vector DB) and
    a concise summary (for Evidence.summary).

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
    extraction_method: str = Field(
        description=(
            "Method: structural_index, statistical_profile, "
            "parse_and_sanitize, ast_extraction, structure_extraction, "
            "metadata_extraction"
        )
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

    # Sanitization
    sanitization_applied: bool = Field(default=False)
    redactions_count: int = Field(default=0)

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


# =============================================================================
# Errors
# =============================================================================


class FileTooLargeError(Exception):
    """Raised when an uploaded file exceeds the size limit."""

    def __init__(self, file_size: int, max_size: int):
        self.file_size = file_size
        self.max_size = max_size
        super().__init__(
            f"File size {file_size} bytes exceeds limit of {max_size} bytes"
        )


class DuplicateFileError(Exception):
    """Raised when the same file content already exists for a case."""

    def __init__(self, existing_evidence_id: str, content_hash: str):
        self.existing_evidence_id = existing_evidence_id
        self.content_hash = content_hash
        super().__init__(
            f"Duplicate file (hash={content_hash[:16]}...) "
            f"already exists as {existing_evidence_id}"
        )


# =============================================================================
# Utility Functions
# =============================================================================


def compute_content_hash(raw_content: bytes) -> str:
    """Compute SHA-256 hash of raw file content for deduplication."""
    return hashlib.sha256(raw_content).hexdigest()


def generate_concise_summary(text: str, max_length: int = 500) -> str:
    """
    Generate concise summary without LLM — take beginning and end.

    Design Reference:
        data-preprocessing-design-specification.md Section 4.10
    """
    if len(text) <= max_length:
        return text
    half = max_length // 2
    return f"{text[:half]}... [truncated] ...{text[-half:]}"
