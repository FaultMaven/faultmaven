"""Tests for PreprocessingService.process_upload() and _extract_with_timeout()."""

import asyncio
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from faultmaven.core.preprocessing.models import (
    DuplicateFileError,
    ExtractionResult,
    FileTooLargeError,
    PreprocessingResult,
    UnifiedDataType,
)
from faultmaven.models.api import DataType
from faultmaven.services.preprocessing.preprocessing_service import (
    DEFAULT_MAX_UPLOAD_SIZE,
    TIER1_TIMEOUT_SECONDS,
    PreprocessingService,
)


@pytest.fixture
def mock_classifier():
    classifier = MagicMock()
    result = MagicMock()
    result.data_type = DataType.LOGS_AND_ERRORS
    result.confidence = 0.9
    result.source = "content_analysis"
    result.classification_failed = False
    result.suggested_types = None
    classifier.classify.return_value = result
    return classifier


@pytest.fixture
def mock_sanitizer():
    sanitizer = MagicMock()
    sanitizer.sanitize.side_effect = lambda x: x  # No-op sanitization
    return sanitizer


@pytest.fixture
def mock_logs_extractor():
    extractor = MagicMock()
    extractor.strategy_name = "structural_index"
    extractor.llm_calls_used = 0
    extractor.extract.return_value = "=== Errors ===\nNullPointerException at line 42"
    return extractor


@pytest.fixture
def service(mock_classifier, mock_sanitizer, mock_logs_extractor):
    return PreprocessingService(
        classifier=mock_classifier,
        sanitizer=mock_sanitizer,
        logs_extractor=mock_logs_extractor,
    )


class TestProcessUpload:
    @pytest.mark.asyncio
    async def test_basic_success(self, service):
        raw = b"2024-01-01 ERROR NullPointerException"
        result = await service.process_upload(
            raw_content=raw,
            filename="app.log",
            content_type="text/plain",
            case_id="case_abc",
        )

        assert isinstance(result, PreprocessingResult)
        assert result.data_type == UnifiedDataType.LOGS
        assert result.detailed_data_type == DataType.LOGS_AND_ERRORS
        assert result.content_size_bytes == len(raw)
        assert result.content_type == "text/plain"
        assert result.extraction_method == "structural_index"
        assert len(result.content_hash) == 64
        assert result.temp_id.startswith("tmp_")
        assert result.processing_time_ms >= 0

    @pytest.mark.asyncio
    async def test_file_too_large(self, service):
        raw = b"x" * (DEFAULT_MAX_UPLOAD_SIZE + 1)
        with pytest.raises(FileTooLargeError) as exc_info:
            await service.process_upload(
                raw_content=raw,
                filename="huge.log",
                content_type="text/plain",
                case_id="case_abc",
            )
        assert exc_info.value.file_size == len(raw)
        assert exc_info.value.max_size == DEFAULT_MAX_UPLOAD_SIZE

    @pytest.mark.asyncio
    async def test_custom_max_size(self, service):
        raw = b"x" * 1000
        with pytest.raises(FileTooLargeError):
            await service.process_upload(
                raw_content=raw,
                filename="file.log",
                content_type="text/plain",
                case_id="case_abc",
                max_upload_size=500,
            )

    @pytest.mark.asyncio
    async def test_duplicate_detected(self, service):
        mock_repo = AsyncMock()
        existing = MagicMock()
        existing.evidence_id = "ev_existing"
        mock_repo.find_by_content_hash = AsyncMock(return_value=existing)

        with pytest.raises(DuplicateFileError) as exc_info:
            await service.process_upload(
                raw_content=b"duplicate content",
                filename="dup.log",
                content_type="text/plain",
                case_id="case_abc",
                evidence_repo=mock_repo,
            )
        assert exc_info.value.existing_evidence_id == "ev_existing"

    @pytest.mark.asyncio
    async def test_no_duplicate_when_none_found(self, service):
        mock_repo = AsyncMock()
        mock_repo.find_by_content_hash = AsyncMock(return_value=None)

        result = await service.process_upload(
            raw_content=b"unique content",
            filename="new.log",
            content_type="text/plain",
            case_id="case_abc",
            evidence_repo=mock_repo,
        )
        assert isinstance(result, PreprocessingResult)

    @pytest.mark.asyncio
    async def test_repo_without_dedup_method(self, service):
        """Repo that doesn't support find_by_content_hash should not break."""
        mock_repo = AsyncMock(spec=[])  # No find_by_content_hash

        result = await service.process_upload(
            raw_content=b"some content",
            filename="file.log",
            content_type="text/plain",
            case_id="case_abc",
            evidence_repo=mock_repo,
        )
        assert isinstance(result, PreprocessingResult)

    @pytest.mark.asyncio
    async def test_no_sanitization_at_extraction_layer(self, service):
        """Extraction never sanitizes — PII redaction happens at the LLM boundary."""
        result = await service.process_upload(
            raw_content=b"NullPointerException",
            filename="app.log",
            content_type="text/plain",
            case_id="case_abc",
        )
        assert result.sanitization_applied is False
        service.sanitizer.sanitize.assert_not_called()
        assert result.redactions_count == 0

    @pytest.mark.asyncio
    async def test_storage_service_called(self, service):
        mock_storage = AsyncMock()
        mock_storage.store_raw_file = AsyncMock(return_value="storage://ref/123")

        result = await service.process_upload(
            raw_content=b"log data",
            filename="app.log",
            content_type="text/plain",
            case_id="case_abc",
            storage_service=mock_storage,
        )

        mock_storage.store_raw_file.assert_called_once()
        assert result.content_ref == "storage://ref/123"

    @pytest.mark.asyncio
    async def test_storage_failure_non_fatal(self, service):
        mock_storage = AsyncMock()
        mock_storage.store_raw_file = AsyncMock(side_effect=RuntimeError("disk full"))

        result = await service.process_upload(
            raw_content=b"log data",
            filename="app.log",
            content_type="text/plain",
            case_id="case_abc",
            storage_service=mock_storage,
        )
        assert result.content_ref is None
        assert isinstance(result, PreprocessingResult)

    @pytest.mark.asyncio
    async def test_no_extractor_fallback(self, service):
        """When no extractor exists for the classified type, uses fallback."""
        service.classifier.classify.return_value.data_type = DataType.VISUAL_EVIDENCE
        result = await service.process_upload(
            raw_content=b"image data",
            filename="screenshot.png",
            content_type="image/png",
            case_id="case_abc",
        )
        assert result.extraction_method == "direct"

    @pytest.mark.asyncio
    async def test_compression_ratio_calculated(self, service):
        raw = b"a" * 1000
        result = await service.process_upload(
            raw_content=raw,
            filename="app.log",
            content_type="text/plain",
            case_id="case_abc",
        )
        assert result.compression_ratio >= 0.0

    @pytest.mark.asyncio
    async def test_content_hash_deterministic(self, service):
        raw = b"deterministic content"
        r1 = await service.process_upload(
            raw_content=raw, filename="f.log", content_type="text/plain", case_id="c1"
        )
        r2 = await service.process_upload(
            raw_content=raw, filename="f.log", content_type="text/plain", case_id="c2"
        )
        assert r1.content_hash == r2.content_hash


class TestExtractWithTimeout:
    @pytest.mark.asyncio
    async def test_successful_extraction(self, service, mock_logs_extractor):
        result = await service._extract_with_timeout(
            mock_logs_extractor, "log content", "app.log"
        )
        assert isinstance(result, ExtractionResult)
        assert result.method == "structural_index"

    @pytest.mark.asyncio
    async def test_timeout_fallback(self, service):
        slow_extractor = MagicMock()
        slow_extractor.strategy_name = "structural_index"

        def slow_extract(content):
            time.sleep(5)
            return "should not reach here"

        slow_extractor.extract = slow_extract

        result = await service._extract_with_timeout(
            slow_extractor, "content", "slow.log"
        )
        assert result.metadata.get("timeout_fallback") is True
        assert result.method == "structure_extraction"

    @pytest.mark.asyncio
    async def test_error_fallback(self, service):
        bad_extractor = MagicMock()
        bad_extractor.strategy_name = "structural_index"
        bad_extractor.extract.side_effect = ValueError("parsing failed")

        result = await service._extract_with_timeout(
            bad_extractor, "content", "bad.log"
        )
        assert result.metadata.get("error_fallback") is True
        assert "parsing failed" in result.metadata.get("error", "")


class TestConstants:
    def test_tier1_timeout(self):
        assert TIER1_TIMEOUT_SECONDS == 2.0

    def test_default_max_upload_size(self):
        assert DEFAULT_MAX_UPLOAD_SIZE == 10 * 1024 * 1024
