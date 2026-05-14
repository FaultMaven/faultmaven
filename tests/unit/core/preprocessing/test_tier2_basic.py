"""Tests for BasicTier2Service (keyword search, no LLM)."""

from unittest.mock import AsyncMock

import pytest

from faultmaven.core.preprocessing.models import AnalysisContext, UnifiedDataType
from faultmaven.core.preprocessing.tier2.basic import BasicTier2Service
from faultmaven.core.preprocessing.tier2.interface import ITier2SearchService


@pytest.fixture
def mock_storage():
    storage = AsyncMock()
    storage.retrieve_file = AsyncMock(
        return_value=b"line1\nline2 error\nline3\nline4 error warning\nline5"
    )
    return storage


@pytest.fixture
def context():
    return AnalysisContext(case_id="case_abc")


@pytest.fixture
def service(mock_storage):
    return BasicTier2Service(
        storage_service=mock_storage, context_lines=2, max_excerpts=3
    )


class TestBasicTier2ServiceInterface:
    def test_implements_interface(self):
        assert issubclass(BasicTier2Service, ITier2SearchService)

    def test_default_params(self):
        s = BasicTier2Service(storage_service=AsyncMock())
        assert s.context_lines == 20
        assert s.max_excerpts == 5


class TestBasicTier2Analyze:
    @pytest.mark.asyncio
    async def test_returns_matching_excerpts(self, service, context):
        result = await service.analyze(
            file_ref="ref_1",
            query="error warning",
            context=context,
            data_type=UnifiedDataType.LOGS,
        )
        assert result.backend_used == "basic_search"
        assert result.confidence == 0.3
        assert len(result.excerpts) > 0

    @pytest.mark.asyncio
    async def test_no_matches_returns_zero_confidence(self, service, context):
        service.storage_service.retrieve_file.return_value = (
            b"all good\nno problems here"
        )
        result = await service.analyze(
            file_ref="ref_1",
            query="critical failure",
            context=context,
            data_type=UnifiedDataType.LOGS,
        )
        assert result.confidence == 0.0
        assert "No matching sections" in result.answer
        assert len(result.excerpts) == 0

    @pytest.mark.asyncio
    async def test_bytes_decoded(self, service, context):
        service.storage_service.retrieve_file.return_value = b"test error content"
        result = await service.analyze(
            file_ref="ref_1",
            query="error",
            context=context,
            data_type=UnifiedDataType.LOGS,
        )
        assert result.confidence == 0.3

    @pytest.mark.asyncio
    async def test_string_content(self, service, context):
        service.storage_service.retrieve_file.return_value = "string error content"
        result = await service.analyze(
            file_ref="ref_1",
            query="error",
            context=context,
            data_type=UnifiedDataType.LOGS,
        )
        assert result.confidence == 0.3

    @pytest.mark.asyncio
    async def test_processing_time_tracked(self, service, context):
        result = await service.analyze(
            file_ref="ref_1",
            query="error",
            context=context,
            data_type=UnifiedDataType.LOGS,
        )
        assert result.processing_time_ms >= 0

    @pytest.mark.asyncio
    async def test_is_available_always_true(self, service):
        assert await service.is_available() is True


class TestKeywordSearch:
    def test_empty_query(self, service):
        result = service._keyword_search("some content", "")
        assert result == []

    def test_short_keywords_skipped(self, service):
        # Words <= 2 chars are skipped
        result = service._keyword_search("an error in the code", "an in")
        assert result == []

    def test_matches_found(self, service):
        content = "line0\nline1\nerror here\nline3\nline4"
        result = service._keyword_search(content, "error here")
        assert len(result) > 0
        assert "error here" in result[0]["text"]

    def test_relevance_scoring(self, service):
        content = "error warning message\nnormal line"
        result = service._keyword_search(content, "error warning extra")
        assert len(result) > 0
        # 2 of 3 keywords matched (extra > 2 chars but not in content... "extra" is > 2 chars)
        # "error" matches, "warning" matches, "extra" doesn't
        assert result[0]["relevance"] == pytest.approx(2 / 3, abs=0.01)

    def test_max_excerpts_limit(self):
        s = BasicTier2Service(
            storage_service=AsyncMock(), context_lines=0, max_excerpts=2
        )
        content = "\n".join(f"error line {i}" for i in range(100))
        result = s._keyword_search(content, "error line")
        assert len(result) <= 2


class TestMergeOverlapping:
    def test_empty_list(self):
        assert BasicTier2Service._merge_overlapping([]) == []

    def test_no_overlap(self):
        matches = [
            {"start": 1, "end": 5, "text": "a", "relevance": 0.5},
            {"start": 10, "end": 15, "text": "b", "relevance": 0.8},
        ]
        result = BasicTier2Service._merge_overlapping(matches)
        assert len(result) == 2

    def test_overlapping_merged(self):
        matches = [
            {"start": 1, "end": 10, "text": "a" * 5, "relevance": 0.5},
            {"start": 5, "end": 15, "text": "b" * 10, "relevance": 0.9},
        ]
        result = BasicTier2Service._merge_overlapping(matches)
        assert len(result) == 1
        assert result[0]["end"] == 15
        assert result[0]["relevance"] == 0.9

    def test_relevance_max_preserved(self):
        matches = [
            {"start": 1, "end": 10, "text": "long text here", "relevance": 0.3},
            {"start": 5, "end": 12, "text": "longer text here!!", "relevance": 0.8},
        ]
        result = BasicTier2Service._merge_overlapping(matches)
        assert result[0]["relevance"] == 0.8
