"""Tests for LocalTier2Service (local LLM-based analysis)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.preprocessing.models import AnalysisContext, UnifiedDataType
from faultmaven.core.preprocessing.tier2.interface import ITier2AnalysisService
from faultmaven.core.preprocessing.tier2.local_service import LocalTier2Service


@pytest.fixture
def mock_storage():
    storage = AsyncMock()
    storage.retrieve = AsyncMock(
        return_value=b"line1\nline2 error occurred\nline3\nline4 error again\nline5"
    )
    return storage


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    response = MagicMock()
    response.content = "Analysis: Found 2 errors related to connection timeout."
    response.tokens_used = 150
    llm.generate = AsyncMock(return_value=response)
    return llm


@pytest.fixture
def context():
    return AnalysisContext(
        case_id="case_abc",
        case_summary="Server returning 500 errors",
        active_hypotheses=["memory leak", "connection pool exhausted"],
    )


@pytest.fixture
def service(mock_llm, mock_storage):
    return LocalTier2Service(
        llm_client=mock_llm,
        storage_service=mock_storage,
        context_lines=2,
        max_excerpts=3,
    )


class TestLocalTier2Interface:
    def test_implements_interface(self):
        assert issubclass(LocalTier2Service, ITier2AnalysisService)

    def test_default_params(self):
        s = LocalTier2Service(llm_client=AsyncMock(), storage_service=AsyncMock())
        assert s.context_lines == 20
        assert s.max_excerpts == 5
        assert s.max_tokens == 2000


class TestLocalTier2Analyze:
    @pytest.mark.asyncio
    async def test_successful_analysis(self, service, context):
        result = await service.analyze(
            file_ref="ref_1",
            query="what errors occurred",
            context=context,
            data_type=UnifiedDataType.LOGS,
        )
        assert result.backend_used == "local_llm"
        assert result.confidence == 0.6
        assert "Analysis:" in result.answer
        assert result.tokens_used == 150
        assert len(result.excerpts) > 0

    @pytest.mark.asyncio
    async def test_no_excerpts_low_confidence(self, service, context):
        service.storage_service.retrieve.return_value = b"all good\nno issues"
        result = await service.analyze(
            file_ref="ref_1",
            query="critical database failure",
            context=context,
            data_type=UnifiedDataType.LOGS,
        )
        assert result.confidence == 0.2
        assert len(result.excerpts) == 0

    @pytest.mark.asyncio
    async def test_llm_failure_fallback(self, service, context):
        service.llm_client.generate.side_effect = RuntimeError("LLM offline")
        result = await service.analyze(
            file_ref="ref_1",
            query="error",
            context=context,
            data_type=UnifiedDataType.LOGS,
        )
        assert "LLM analysis failed" in result.answer
        assert result.backend_used == "local_llm"

    @pytest.mark.asyncio
    async def test_bytes_decoded(self, service, context):
        service.storage_service.retrieve.return_value = b"error log data"
        result = await service.analyze(
            file_ref="ref_1",
            query="error",
            context=context,
            data_type=UnifiedDataType.LOGS,
        )
        assert result.backend_used == "local_llm"

    @pytest.mark.asyncio
    async def test_string_content_handled(self, service, context):
        service.storage_service.retrieve.return_value = "string error content"
        result = await service.analyze(
            file_ref="ref_1",
            query="error",
            context=context,
            data_type=UnifiedDataType.LOGS,
        )
        assert result.backend_used == "local_llm"

    @pytest.mark.asyncio
    async def test_response_without_content_attr(self, service, context):
        """Test when LLM response doesn't have .content attribute."""
        service.llm_client.generate.return_value = "plain string response"
        result = await service.analyze(
            file_ref="ref_1",
            query="error",
            context=context,
            data_type=UnifiedDataType.LOGS,
        )
        assert result.answer == "plain string response"


class TestLocalTier2IsAvailable:
    @pytest.mark.asyncio
    async def test_available_with_is_available_method(self):
        llm = AsyncMock()
        llm.is_available = AsyncMock(return_value=True)
        service = LocalTier2Service(llm_client=llm, storage_service=AsyncMock())
        assert await service.is_available() is True

    @pytest.mark.asyncio
    async def test_not_available(self):
        llm = AsyncMock()
        llm.is_available = AsyncMock(return_value=False)
        service = LocalTier2Service(llm_client=llm, storage_service=AsyncMock())
        assert await service.is_available() is False

    @pytest.mark.asyncio
    async def test_default_available_without_method(self):
        llm = MagicMock(spec=[])  # No is_available method
        service = LocalTier2Service(llm_client=llm, storage_service=AsyncMock())
        assert await service.is_available() is True

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        llm = AsyncMock()
        llm.is_available = AsyncMock(side_effect=RuntimeError("boom"))
        service = LocalTier2Service(llm_client=llm, storage_service=AsyncMock())
        assert await service.is_available() is False


class TestBuildAnalysisPrompt:
    def test_includes_data_type(self):
        prompt = LocalTier2Service._build_analysis_prompt(
            query="what happened",
            sections=[],
            context=AnalysisContext(case_id="c"),
            data_type=UnifiedDataType.LOGS,
        )
        assert "logs" in prompt

    def test_includes_case_summary(self):
        prompt = LocalTier2Service._build_analysis_prompt(
            query="what happened",
            sections=[],
            context=AnalysisContext(case_id="c", case_summary="Server crashed"),
            data_type=UnifiedDataType.LOGS,
        )
        assert "Server crashed" in prompt

    def test_includes_hypotheses(self):
        prompt = LocalTier2Service._build_analysis_prompt(
            query="what happened",
            sections=[],
            context=AnalysisContext(
                case_id="c", active_hypotheses=["memory leak", "deadlock"]
            ),
            data_type=UnifiedDataType.LOGS,
        )
        assert "memory leak" in prompt
        assert "deadlock" in prompt

    def test_includes_sections(self):
        sections = [
            {"start": 1, "end": 5, "text": "error log content here"},
        ]
        prompt = LocalTier2Service._build_analysis_prompt(
            query="what errors",
            sections=sections,
            context=AnalysisContext(case_id="c"),
            data_type=UnifiedDataType.LOGS,
        )
        assert "error log content here" in prompt
        assert "Section 1" in prompt

    def test_includes_query(self):
        prompt = LocalTier2Service._build_analysis_prompt(
            query="find the root cause",
            sections=[],
            context=AnalysisContext(case_id="c"),
            data_type=UnifiedDataType.METRICS,
        )
        assert "find the root cause" in prompt
