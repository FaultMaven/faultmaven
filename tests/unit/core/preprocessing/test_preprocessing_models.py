"""Tests for core preprocessing models, enums, errors, and utility functions."""

import pytest

from faultmaven.core.preprocessing.models import (
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
from faultmaven.models.api import DataType as DetailedDataType

# =============================================================================
# UnifiedDataType Enum
# =============================================================================


class TestUnifiedDataType:
    def test_all_six_types_exist(self):
        expected = {"logs", "metrics", "configuration", "code", "text", "image"}
        actual = {t.value for t in UnifiedDataType}
        assert actual == expected

    def test_is_string_enum(self):
        assert isinstance(UnifiedDataType.LOGS, str)
        assert UnifiedDataType.LOGS == "logs"


# =============================================================================
# to_unified_data_type Mapping
# =============================================================================


class TestToUnifiedDataType:
    @pytest.mark.parametrize(
        "detailed, expected_unified",
        [
            (DetailedDataType.LOGS_AND_ERRORS, UnifiedDataType.LOGS),
            (DetailedDataType.ERROR_REPORT, UnifiedDataType.LOGS),
            (DetailedDataType.TRACE_DATA, UnifiedDataType.LOGS),
            (DetailedDataType.COMMAND_OUTPUT, UnifiedDataType.LOGS),
            (DetailedDataType.METRICS_AND_PERFORMANCE, UnifiedDataType.METRICS),
            (DetailedDataType.PROFILING_DATA, UnifiedDataType.METRICS),
            (DetailedDataType.STRUCTURED_CONFIG, UnifiedDataType.CONFIGURATION),
            (DetailedDataType.SOURCE_CODE, UnifiedDataType.CODE),
            (DetailedDataType.UNSTRUCTURED_TEXT, UnifiedDataType.TEXT),
            (DetailedDataType.DOCUMENTATION, UnifiedDataType.TEXT),
            (DetailedDataType.VISUAL_EVIDENCE, UnifiedDataType.IMAGE),
            (DetailedDataType.UNANALYZABLE, UnifiedDataType.TEXT),
        ],
    )
    def test_all_detailed_types_map_correctly(self, detailed, expected_unified):
        assert to_unified_data_type(detailed) == expected_unified

    def test_all_detailed_types_are_covered(self):
        """Every DetailedDataType should map to something."""
        for dt in DetailedDataType:
            result = to_unified_data_type(dt)
            assert isinstance(result, UnifiedDataType)


# =============================================================================
# ExtractionResult
# =============================================================================


class TestExtractionResult:
    def test_basic_construction(self):
        result = ExtractionResult(method="crime_scene", content="parsed data")
        assert result.method == "crime_scene"
        assert result.content == "parsed data"
        assert result.metadata == {}

    def test_with_metadata(self):
        meta = {"error_count": 5, "line_count": 100}
        result = ExtractionResult(method="statistical", content="stats", metadata=meta)
        assert result.metadata == meta


# =============================================================================
# PreprocessingResult
# =============================================================================


class TestPreprocessingResult:
    def test_basic_construction(self):
        result = PreprocessingResult(
            data_type=UnifiedDataType.LOGS,
            detailed_data_type=DetailedDataType.LOGS_AND_ERRORS,
            summary="Log analysis summary",
            structural_index="=== Errors ===\nNullPointer at line 5",
            content_size_bytes=1024,
            content_type="text/plain",
            extraction_method="crime_scene",
            compression_ratio=0.15,
            content_hash="abc123def456",
        )
        assert result.data_type == UnifiedDataType.LOGS
        assert result.content_size_bytes == 1024
        assert result.processing_time_ms == 0

    def test_temp_id_auto_generated(self):
        r1 = PreprocessingResult(
            data_type=UnifiedDataType.TEXT,
            detailed_data_type=DetailedDataType.UNSTRUCTURED_TEXT,
            summary="test",
            structural_index="test",
            content_size_bytes=10,
            content_type="text/plain",
            extraction_method="structure_extraction",
            compression_ratio=1.0,
            content_hash="hash1",
        )
        r2 = PreprocessingResult(
            data_type=UnifiedDataType.TEXT,
            detailed_data_type=DetailedDataType.UNSTRUCTURED_TEXT,
            summary="test",
            structural_index="test",
            content_size_bytes=10,
            content_type="text/plain",
            extraction_method="structure_extraction",
            compression_ratio=1.0,
            content_hash="hash2",
        )
        assert r1.temp_id.startswith("tmp_")
        assert r2.temp_id.startswith("tmp_")
        assert r1.temp_id != r2.temp_id

    def test_content_ref_optional(self):
        result = PreprocessingResult(
            data_type=UnifiedDataType.CODE,
            detailed_data_type=DetailedDataType.SOURCE_CODE,
            summary="code summary",
            structural_index="def foo(): pass",
            content_size_bytes=50,
            content_type="text/x-python",
            extraction_method="ast_parse",
            compression_ratio=0.5,
            content_hash="hash",
        )
        assert result.content_ref is None


# =============================================================================
# AnalysisContext / DataExcerpt / DeepAnalysisResult
# =============================================================================


class TestTier2Models:
    def test_analysis_context_minimal(self):
        ctx = AnalysisContext(case_id="case_abc123")
        assert ctx.case_id == "case_abc123"
        assert ctx.case_summary is None
        assert ctx.active_hypotheses is None
        assert ctx.investigation_stage is None

    def test_analysis_context_full(self):
        ctx = AnalysisContext(
            case_id="case_abc123",
            case_summary="Server is returning 500",
            active_hypotheses=["memory leak", "deadlock"],
            investigation_stage="HYPOTHESIS_VALIDATION",
        )
        assert len(ctx.active_hypotheses) == 2

    def test_data_excerpt(self):
        excerpt = DataExcerpt(
            content="ERROR: connection refused",
            line_start=10,
            line_end=15,
            relevance=0.85,
        )
        assert excerpt.relevance == 0.85

    def test_deep_analysis_result_defaults(self):
        result = DeepAnalysisResult(answer="No issues found")
        assert result.excerpts == []
        assert result.confidence == 0.0
        assert result.tokens_used == 0
        assert result.processing_time_ms == 0
        assert result.backend_used == ""


# =============================================================================
# Chunk / FileInfo
# =============================================================================


class TestDataclasses:
    def test_chunk_defaults(self):
        chunk = Chunk(text="some text")
        assert chunk.text == "some text"
        assert chunk.metadata == {}

    def test_chunk_with_metadata(self):
        chunk = Chunk(text="content", metadata={"section": "ERRORS"})
        assert chunk.metadata["section"] == "ERRORS"

    def test_file_info(self):
        fi = FileInfo(
            filename="app.log",
            mime_type="text/plain",
            raw_content=b"log data here",
        )
        assert fi.filename == "app.log"
        assert fi.extension == ""
        assert fi.processing_time_ms == 0.0

    def test_file_info_with_extension(self):
        fi = FileInfo(
            filename="config.yaml",
            mime_type="application/yaml",
            raw_content=b"key: value",
            extension=".yaml",
        )
        assert fi.extension == ".yaml"


# =============================================================================
# Utility Functions
# =============================================================================


class TestGenerateConciseSummary:
    def test_short_text_unchanged(self):
        text = "Short summary"
        assert generate_concise_summary(text, max_length=500) == text

    def test_exact_length_unchanged(self):
        text = "x" * 500
        assert generate_concise_summary(text, max_length=500) == text

    def test_long_text_truncated(self):
        text = "A" * 1000
        result = generate_concise_summary(text, max_length=100)
        assert "... [truncated] ..." in result
        assert len(result) <= 100
        assert result.startswith("A")
        assert result.endswith("A")

    def test_custom_max_length(self):
        text = "B" * 200
        result = generate_concise_summary(text, max_length=50)
        assert "... [truncated] ..." in result
        assert len(result) <= 50

    def test_default_max_length_respected(self):
        """Regression: truncated output must not exceed max_length (was 521 chars for 500 limit)."""
        text = "X" * 2000
        result = generate_concise_summary(text)
        assert len(result) <= 500
