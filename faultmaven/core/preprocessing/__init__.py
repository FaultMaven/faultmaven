"""Data Preprocessing Package

Four-tier data preprocessing pipeline:
- Tier 0: Classification (DataClassifier in faultmaven.modules.preprocessing.classifier)
- Tier 1: Mechanical extraction (extractors in faultmaven.modules.preprocessing.extractors)
- Tier 2: Mechanical search (search_file agent tool + BasicTier2Service)
- Tier 3: Interpreted search / deep analysis (tier2/ backends — legacy package name)

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md
"""

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
    UnifiedDataType,
    compute_content_hash,
    generate_concise_summary,
    to_unified_data_type,
)
from .vector_storage import chunk_structural_index, store_in_vector_db_background

__all__ = [
    "UnifiedDataType",
    "PreprocessingResult",
    "ExtractionResult",
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
    "chunk_structural_index",
    "store_in_vector_db_background",
]
