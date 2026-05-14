"""Tests for PreprocessingService.classify_and_extract() — v4.0 pasted text processing."""

from unittest.mock import MagicMock

import pytest

from faultmaven.core.preprocessing.models import PreprocessingResult, UnifiedDataType
from faultmaven.models.api import DataType
from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult
from faultmaven.modules.preprocessing.preprocessing_service import PreprocessingService


@pytest.fixture
def mock_classifier():
    classifier = MagicMock()
    result = MagicMock()
    result.data_type = DataType.LOGS_AND_ERRORS
    result.confidence = 0.90
    result.source = "rule_based"
    result.source_type = None
    result.classification_failed = False
    result.suggested_types = None
    classifier.classify.return_value = result
    return classifier


@pytest.fixture
def mock_logs_extractor():
    extractor = MagicMock()
    extractor.strategy_name = "crime_scene"
    extractor.llm_calls_used = 0
    extractor.extract.return_value = ExtractResult(
        file_extract=(
            "=== Crime Scene ===\n"
            "Error cluster at line 42: NullPointerException\n"
            "Context: Connection pool exhausted"
        ),
        search_map="",
        file_meta={},
    )
    return extractor


@pytest.fixture
def service(mock_classifier, mock_logs_extractor):
    return PreprocessingService(
        classifier=mock_classifier,
        logs_extractor=mock_logs_extractor,
    )


def _log_content(n_lines: int = 250) -> str:
    """Build synthetic log content large enough to exceed MIN_EXTRACTION_LINES."""
    return "\n".join(f"2024-01-01 INFO log line {i}" for i in range(n_lines))


class TestClassifyAndExtract:
    @pytest.mark.asyncio
    async def test_basic_success(self, service):
        """classify_and_extract classifies and extracts pasted text."""
        content = _log_content()
        result = await service.classify_and_extract(content=content)

        assert isinstance(result, PreprocessingResult)
        assert result.data_type == UnifiedDataType.LOGS
        assert result.detailed_data_type == DataType.LOGS_AND_ERRORS
        assert result.extraction_method == "crime_scene"
        assert "Crime Scene" in result.structural_index
        assert result.content_ref is None  # No file storage for pasted text
        assert result.content_type == "text/plain"
        assert len(result.content_hash) == 64

    @pytest.mark.asyncio
    async def test_uses_synthetic_filename(self, service, mock_classifier):
        """Default filename is 'pasted_content.txt'."""
        await service.classify_and_extract(content="some data")
        mock_classifier.classify.assert_called_once()
        call_args = mock_classifier.classify.call_args
        assert call_args[0][0] == "pasted_content.txt"

    @pytest.mark.asyncio
    async def test_custom_filename(self, service, mock_classifier):
        """Custom filename is passed to classifier."""
        await service.classify_and_extract(
            content="some data", filename="user_paste.log"
        )
        call_args = mock_classifier.classify.call_args
        assert call_args[0][0] == "user_paste.log"

    @pytest.mark.asyncio
    async def test_content_hash_computed_from_text(self, service):
        """Content hash is SHA-256 of the text input, not summary."""
        content = "specific content for hash test"
        result = await service.classify_and_extract(content=content)

        import hashlib

        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert result.content_hash == expected

    @pytest.mark.asyncio
    async def test_content_size_from_text(self, service):
        """content_size_bytes reflects the original text size."""
        content = "a" * 1000
        result = await service.classify_and_extract(content=content)
        assert result.content_size_bytes == len(content.encode("utf-8"))

    @pytest.mark.asyncio
    async def test_fallback_when_no_extractor(self, service, mock_classifier):
        """Falls back to direct truncation when no extractor matches."""
        # Set classifier to return a type with no registered extractor
        mock_classifier.classify.return_value.data_type = DataType.VISUAL_EVIDENCE
        result = await service.classify_and_extract(content=_log_content())

        assert result.extraction_method == "direct"

    @pytest.mark.asyncio
    async def test_summary_generated(self, service):
        """Summary is generated from structural index."""
        result = await service.classify_and_extract(content="some data")
        assert result.summary is not None
        assert len(result.summary) > 0

    @pytest.mark.asyncio
    async def test_small_file_passthrough_skips_extractor(
        self, service, mock_logs_extractor
    ):
        """Files below MIN_EXTRACTION_LINES skip extraction and use raw content."""
        small_content = "\n".join(f"2024-01-01 INFO line {i}" for i in range(50))
        result = await service.classify_and_extract(content=small_content)

        assert result.extraction_method == "raw_passthrough"
        mock_logs_extractor.extract.assert_not_called()

    @pytest.mark.asyncio
    async def test_file_at_threshold_runs_extractor(self, service, mock_logs_extractor):
        """Files at or above MIN_EXTRACTION_LINES still go through extraction."""
        result = await service.classify_and_extract(content=_log_content(250))

        assert result.extraction_method == "crime_scene"
        mock_logs_extractor.extract.assert_called_once()


class TestEmptyContentGracefulDegradation:
    """0-byte uploads / empty content route to UNANALYZABLE (ISS-024).

    The classifier short-circuits empty content to UNANALYZABLE; the
    preprocessing service must produce a non-crashing placeholder with
    a clear "file is empty" message — not a confusing
    classification_failed modal.
    """

    @pytest.fixture
    def real_classifier_service(self, mock_logs_extractor):
        """Service wired with the real DataClassifier so we exercise the
        empty-content short-circuit end-to-end."""
        from faultmaven.modules.preprocessing.classifier import DataClassifier

        return PreprocessingService(
            classifier=DataClassifier(),
            logs_extractor=mock_logs_extractor,
        )

    @pytest.mark.asyncio
    async def test_empty_content_routes_to_unanalyzable(
        self, real_classifier_service, mock_logs_extractor
    ):
        result = await real_classifier_service.classify_and_extract(
            content="", filename="empty.bin"
        )

        # Pipeline must produce a result, not raise.
        assert isinstance(result, PreprocessingResult)
        assert result.detailed_data_type == DataType.UNANALYZABLE
        # Extractor must not be invoked on empty content — it would either
        # produce a misleading "no errors" tail or waste a Tier 1 budget.
        mock_logs_extractor.extract.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_content_produces_human_readable_placeholder(
        self, real_classifier_service
    ):
        result = await real_classifier_service.classify_and_extract(
            content="", filename="server.log"
        )

        # The structural_index is what surfaces to the agent and (via the
        # case_ui adapter) to the user. It must clearly say "empty" and
        # name the file so the user can act on it.
        assert "empty" in result.structural_index.lower()
        assert "server.log" in result.structural_index
        assert result.extraction_method == "none"

    @pytest.mark.asyncio
    async def test_empty_content_does_not_set_classification_failed(
        self, real_classifier_service
    ):
        """classification_failed=True triggers a frontend clarification
        modal. For an empty file there's nothing to clarify, so the flag
        must stay False."""
        result = await real_classifier_service.classify_and_extract(content="")

        meta = result.extraction_metadata or {}
        evidence_meta = meta.get("evidence_metadata", {})
        classification_meta = evidence_meta.get("classification", {})
        # The classification metadata block records `failed`; must be False
        # so the case UI doesn't show a "needs clarification" badge.
        assert classification_meta.get("failed") is False

    @pytest.mark.asyncio
    async def test_whitespace_only_content_also_unanalyzable(
        self, real_classifier_service
    ):
        result = await real_classifier_service.classify_and_extract(
            content="   \n\t   \n", filename="blank.txt"
        )

        assert result.detailed_data_type == DataType.UNANALYZABLE
