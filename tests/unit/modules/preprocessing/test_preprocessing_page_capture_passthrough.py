"""Unit tests for the page_capture passthrough branch in PreprocessingService.

When ``classification.source_type == "page_capture"``, ``classify_and_extract``
skips the type-specific extractor and emits a ``page_capture_passthrough``
ExtractionResult instead. The copilot has already structured the page via
``htmlToStructuredText``; re-parsing with UnstructuredTextExtractor would
discard that structure and corrupt downstream analysis.

The classifier itself still runs — page captures need a data_type for
extractor routing on follow-up turns and for metadata. Only the extractor
step is bypassed.

Run with:
    pytest tests/unit/modules/preprocessing/test_preprocessing_page_capture_passthrough.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from faultmaven.core.preprocessing.models import PreprocessingResult
from faultmaven.models.api import DataType, SourceMetadata
from faultmaven.modules.preprocessing.preprocessing_service import PreprocessingService

# ============================================================
# Fixtures (mirror style of test_classify_and_extract.py)
# ============================================================


def _make_classification_mock(
    *,
    data_type: DataType = DataType.LOGS_AND_ERRORS,
    confidence: float = 0.96,
    source: str = "source_url",
    source_type: str | None = "page_capture",
    classification_failed: bool = False,
):
    """Build a MagicMock standing in for a ClassificationResult.

    The service treats the classification result as a duck-typed object —
    accessing ``.data_type``, ``.confidence``, ``.source``, ``.source_type``,
    ``.classification_failed``, and ``.suggested_types`` — so a MagicMock
    is sufficient (no Pydantic validators to satisfy).
    """
    classification = MagicMock()
    classification.data_type = data_type
    classification.confidence = confidence
    classification.source = source
    classification.source_type = source_type
    classification.classification_failed = classification_failed
    classification.suggested_types = None
    return classification


@pytest.fixture
def mock_classifier():
    """Default classifier mock — returns a page_capture classification.

    Individual tests override the return value via
    ``mock_classifier.classify.return_value = ...`` when they need a
    different classification (e.g. file_upload to verify the inverse path).
    """
    classifier = MagicMock()
    classifier.classify.return_value = _make_classification_mock()
    return classifier


@pytest.fixture
def mock_logs_extractor():
    """Logs extractor mock — must NOT be called on the page_capture path."""
    extractor = MagicMock()
    extractor.strategy_name = "crime_scene"
    extractor.llm_calls_used = 0
    extractor.extract.return_value = "should not be reached"
    return extractor


@pytest.fixture
def service(mock_classifier, mock_logs_extractor):
    return PreprocessingService(
        classifier=mock_classifier,
        logs_extractor=mock_logs_extractor,
    )


# Page-capture content from the copilot's htmlToStructuredText. Long enough
# (>= MIN_EXTRACTION_LINES = 200) to clear the small-file passthrough so we
# isolate the page_capture branch specifically.
def _page_capture_content(n_lines: int = 250) -> str:
    return "\n".join(
        f"## Section {i}\nCaptured DOM line {i}: error rate 5.2%"
        for i in range(n_lines)
    )


def _page_capture_metadata() -> SourceMetadata:
    return SourceMetadata(
        source_type="page_capture", source_url="https://sentry.io/issues/42"
    )


# ============================================================
# Page-capture passthrough behaviour
# ============================================================


class TestPageCapturePassthrough:
    """The passthrough branch is gated on ``classification.source_type ==
    "page_capture"``. When it fires, it produces a result whose
    ``extraction_method == "page_capture_passthrough"`` and whose
    extraction_metadata records the passthrough.
    """

    @pytest.mark.asyncio
    async def test_page_capture_uses_passthrough_method(self, service, mock_classifier):
        """source_type=page_capture → method == 'page_capture_passthrough'."""
        result = await service.classify_and_extract(
            content=_page_capture_content(),
            filename="captured.html",
            source_metadata=_page_capture_metadata(),
        )

        assert isinstance(result, PreprocessingResult)
        assert result.extraction_method == "page_capture_passthrough"

    @pytest.mark.asyncio
    async def test_page_capture_preserves_content_via_direct_extraction(self, service):
        """The passthrough wraps content with ``_fallback_direct_extraction``,
        which truncates only above 10,000 chars. For typical captures (under
        the cap) the original content is preserved verbatim in the structural
        index — no extractor mangling, no parsing artifacts."""
        # Build content that clears MIN_EXTRACTION_LINES (200) but stays
        # under the 10,000-char direct-truncation cap so we can pin
        # verbatim preservation.
        content = "\n".join(f"line {i:03d}" for i in range(220))
        assert len(content) < 10_000

        result = await service.classify_and_extract(
            content=content,
            filename="captured.html",
            source_metadata=_page_capture_metadata(),
        )

        # structural_index falls back to extraction.content (no
        # extract_result_json metadata key on the passthrough path).
        assert result.structural_index == content

    @pytest.mark.asyncio
    async def test_page_capture_metadata_records_passthrough_flags(self, service):
        """``extraction_metadata`` carries ``passthrough: True`` and
        ``source_type: "page_capture"`` so observability and the evidence
        viewer can distinguish passthrough from full extraction."""
        result = await service.classify_and_extract(
            content=_page_capture_content(),
            filename="captured.html",
            source_metadata=_page_capture_metadata(),
        )

        meta = result.extraction_metadata or {}
        assert meta.get("passthrough") is True
        assert meta.get("source_type") == "page_capture"

    @pytest.mark.asyncio
    async def test_page_capture_skips_extractor(self, service, mock_logs_extractor):
        """The whole reason the passthrough exists: extractors must NOT be
        invoked on already-structured page captures. This is the regression
        line — silently invoking an extractor would garble the htmlToStructuredText
        output without changing the method label."""
        await service.classify_and_extract(
            content=_page_capture_content(),
            filename="captured.html",
            source_metadata=_page_capture_metadata(),
        )

        mock_logs_extractor.extract.assert_not_called()

    @pytest.mark.asyncio
    async def test_page_capture_still_invokes_classifier(
        self, service, mock_classifier
    ):
        """Page captures still need a data_type for downstream routing
        (e.g. follow-up agent tools picking the right query strategy).
        Only the *extractor* step is bypassed; classification still runs."""
        await service.classify_and_extract(
            content=_page_capture_content(),
            filename="captured.html",
            source_metadata=_page_capture_metadata(),
        )

        mock_classifier.classify.assert_called_once()

    @pytest.mark.asyncio
    async def test_page_capture_preserves_classified_data_type(
        self, service, mock_classifier
    ):
        """The classified data_type (e.g. LOGS_AND_ERRORS for a Sentry capture)
        flows into the result so the evidence row records what kind of page
        was captured — even though no extractor ran."""
        # Override classification to METRICS so we can verify it propagates.
        mock_classifier.classify.return_value = _make_classification_mock(
            data_type=DataType.METRICS_AND_PERFORMANCE,
            source_type="page_capture",
        )

        result = await service.classify_and_extract(
            content=_page_capture_content(),
            filename="captured.html",
            source_metadata=SourceMetadata(
                source_type="page_capture",
                source_url="https://grafana.example.com/d/abc",
            ),
        )

        assert result.detailed_data_type == DataType.METRICS_AND_PERFORMANCE
        assert result.extraction_method == "page_capture_passthrough"


class TestPageCapturePassthroughDoesNotFireForOtherSources:
    """Inverse: when source_type is NOT page_capture, the extractor pipeline
    runs normally. Pin this so the passthrough branch can't accidentally
    swallow file_upload or text_paste content.
    """

    @pytest.mark.asyncio
    async def test_file_upload_runs_normal_extraction(
        self, service, mock_classifier, mock_logs_extractor
    ):
        """source_type=file_upload → extractor runs; method != passthrough."""
        # Configure classifier to return file_upload origin (no boost path).
        mock_classifier.classify.return_value = _make_classification_mock(
            source_type="file_upload",
        )

        result = await service.classify_and_extract(
            content=_page_capture_content(),
            filename="server.log",
            source_metadata=SourceMetadata(source_type="file_upload"),
        )

        # Extractor was invoked and the result reflects its strategy_name.
        mock_logs_extractor.extract.assert_called_once()
        assert result.extraction_method == "crime_scene"
        assert result.extraction_method != "page_capture_passthrough"

    @pytest.mark.asyncio
    async def test_text_paste_runs_normal_extraction(
        self, service, mock_classifier, mock_logs_extractor
    ):
        """source_type=text_paste → extractor runs; method != passthrough.

        Pasted text is unstructured and benefits from the type-specific
        extractor's parsing — the passthrough optimization is specifically
        for already-structured page captures."""
        mock_classifier.classify.return_value = _make_classification_mock(
            source_type="text_paste",
        )

        result = await service.classify_and_extract(
            content=_page_capture_content(),
            filename="pasted.txt",
            source_metadata=SourceMetadata(source_type="text_paste"),
        )

        mock_logs_extractor.extract.assert_called_once()
        assert result.extraction_method != "page_capture_passthrough"

    @pytest.mark.asyncio
    async def test_no_source_metadata_runs_normal_extraction(
        self, service, mock_classifier, mock_logs_extractor
    ):
        """When source_metadata is omitted entirely, the classifier returns
        ``source_type=None`` and the passthrough branch's equality check
        (``== "page_capture"``) is False — extractor runs normally."""
        mock_classifier.classify.return_value = _make_classification_mock(
            source_type=None,
        )

        result = await service.classify_and_extract(
            content=_page_capture_content(),
            filename="server.log",
        )

        mock_logs_extractor.extract.assert_called_once()
        assert result.extraction_method != "page_capture_passthrough"
