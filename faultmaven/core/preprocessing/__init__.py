"""Data Preprocessing Package

Three-tier data preprocessing pipeline:
- Tier 0+1: Classification and mechanical extraction (models.py, data_preprocessor.py)
- Tier 2: On-demand deep analysis (tier2/)
- Vector DB: Structural index storage (vector_storage.py)

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md
"""

from .data_preprocessor import (
    get_preprocessor_for_data_type,
    preprocess_config,
    preprocess_errors,
    preprocess_logs,
    preprocess_metrics,
)
from .models import (
    AnalysisContext,
    Chunk,
    DataExcerpt,
    DeepAnalysisResult,
    DuplicateFileError,
    ExtractionResult,
    FileInfo,
    FileTooLargeError,
    PreprocessingResult,
    SanitizationResult,
    UnifiedDataType,
    compute_content_hash,
    generate_concise_summary,
    to_unified_data_type,
)
from .vector_storage import chunk_structural_index, store_in_vector_db_background

__all__ = [
    # Legacy preprocessors
    "preprocess_logs",
    "preprocess_metrics",
    "preprocess_errors",
    "preprocess_config",
    "get_preprocessor_for_data_type",
    # Design v3.0 models
    "UnifiedDataType",
    "PreprocessingResult",
    "ExtractionResult",
    "SanitizationResult",
    "DeepAnalysisResult",
    "AnalysisContext",
    "DataExcerpt",
    "Chunk",
    "FileInfo",
    "FileTooLargeError",
    "DuplicateFileError",
    "to_unified_data_type",
    "compute_content_hash",
    "generate_concise_summary",
    # Vector DB
    "chunk_structural_index",
    "store_in_vector_db_background",
]
