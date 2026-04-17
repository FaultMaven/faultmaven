"""Tests for PreprocessingService.classify_and_extract() — v4.0 pasted text processing."""

from unittest.mock import MagicMock

import pytest

from faultmaven.core.preprocessing.models import PreprocessingResult, UnifiedDataType
from faultmaven.models.api import DataType
from faultmaven.modules.preprocessing.preprocessing_service import PreprocessingService


@pytest.fixture
def mock_classifier():
    classifier = MagicMock()
    result = MagicMock()
    result.data_type = DataType.LOGS_AND_ERRORS
    result.confidence = 0.90
    result.source = "rule_based"
    result.classification_failed = False
    result.suggested_types = None
    classifier.classify.return_value = result
    return classifier


@pytest.fixture
def mock_sanitizer():
    sanitizer = MagicMock()
    sanitizer.sanitize.side_effect = lambda x: x
    return sanitizer


@pytest.fixture
def mock_logs_extractor():
    extractor = MagicMock()
    extractor.strategy_name = "crime_scene"
    extractor.llm_calls_used = 0
    extractor.extract.return_value = (
        "=== Crime Scene ===\n"
        "Error cluster at line 42: NullPointerException\n"
        "Context: Connection pool exhausted"
    )
    return extractor


@pytest.fixture
def service(mock_classifier, mock_sanitizer, mock_logs_extractor):
    return PreprocessingService(
        classifier=mock_classifier,
        sanitizer=mock_sanitizer,
        logs_extractor=mock_logs_extractor,
    )


class TestClassifyAndExtract:
    @pytest.mark.asyncio
    async def test_basic_success(self, service):
        """classify_and_extract classifies and extracts pasted text."""
        content = "2024-01-01 ERROR NullPointerException at line 42"
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
    async def test_no_sanitization_at_extraction_layer(self, service, mock_sanitizer):
        """Extraction never sanitizes — PII redaction happens at the LLM boundary."""
        result = await service.classify_and_extract(content="data with password=secret")
        mock_sanitizer.sanitize.assert_not_called()
        assert result.sanitization_applied is False

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
        result = await service.classify_and_extract(content="image data")

        assert result.extraction_method == "direct"

    @pytest.mark.asyncio
    async def test_summary_generated(self, service):
        """Summary is generated from structural index."""
        result = await service.classify_and_extract(content="some data")
        assert result.summary is not None
        assert len(result.summary) > 0
