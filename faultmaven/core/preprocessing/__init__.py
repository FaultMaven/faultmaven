"""Data Preprocessing Package

Four-tier data preprocessing pipeline:
- Tier 0: Classification (DataClassifier in faultmaven.modules.preprocessing.classifier)
- Tier 1: Mechanical extraction (extractors in faultmaven.modules.preprocessing.extractors)
- Tier 2: Mechanical search (search_file agent tool + BasicTier2Service)
- Tier 3: Interpreted search / deep analysis (backends in ``tier2/`` — the
  ``Tier2`` token refers to the agent-tools tier system: Tier 2 = search,
  Tier 3 = interpreted analysis)

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md
"""

from .models import (
    AnalysisContext,
    Chunk,
    DataExcerpt,
    DeepAnalysisResult,
    ExtractionResult,
    FileInfo,
    PreprocessingResult,
    UnifiedDataType,
    generate_concise_summary,
    to_unified_data_type,
)
from .vector_storage import (
    VectorIndexOutcome,
    chunk_structural_index,
    store_in_vector_db_background,
)

__all__ = [
    "UnifiedDataType",
    "PreprocessingResult",
    "ExtractionResult",
    "DeepAnalysisResult",
    "AnalysisContext",
    "DataExcerpt",
    "Chunk",
    "FileInfo",
    "to_unified_data_type",
    "generate_concise_summary",
    "chunk_structural_index",
    "store_in_vector_db_background",
    "VectorIndexOutcome",
]
