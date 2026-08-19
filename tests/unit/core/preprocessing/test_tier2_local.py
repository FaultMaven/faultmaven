"""Tests for LocalTier2Service (local LLM-based analysis)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.preprocessing.models import AnalysisContext, UnifiedDataType
from faultmaven.core.preprocessing.tier2.interface import ITier2SearchService
from faultmaven.core.preprocessing.tier2.local_service import LocalTier2Service
from faultmaven.infrastructure.llm.providers import LLMResponse, StopReason


@pytest.fixture
def mock_storage():
    storage = AsyncMock()
    storage.retrieve_file = AsyncMock(
        return_value=b"line1\nline2 error occurred\nline3\nline4 error again\nline5"
    )
    return storage


def _llm_response(content: str, stop_reason: StopReason = StopReason.STOP):
    """A real ``LLMResponse``, which is what an ILLMProvider returns.

    A ``MagicMock`` answers every attribute with a truthy Mock, so once this
    service started consulting ``is_truncated`` (#1094) the stand-in would have
    claimed every analysis was cut off — and a fake that cannot say "no" makes
    the test agree with whatever the code does.
    """
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="local",
        model="llama3.2",
        tokens_used=150,
        response_time_ms=10,
        stop_reason=stop_reason,
    )


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.generate = AsyncMock(
        return_value=_llm_response(
            "Analysis: Found 2 errors related to connection timeout."
        )
    )
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
        assert issubclass(LocalTier2Service, ITier2SearchService)

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
        service.storage_service.retrieve_file.return_value = b"all good\nno issues"
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
        service.storage_service.retrieve_file.return_value = b"error log data"
        result = await service.analyze(
            file_ref="ref_1",
            query="error",
            context=context,
            data_type=UnifiedDataType.LOGS,
        )
        assert result.backend_used == "local_llm"

    @pytest.mark.asyncio
    async def test_string_content_handled(self, service, context):
        service.storage_service.retrieve_file.return_value = "string error content"
        result = await service.analyze(
            file_ref="ref_1",
            query="error",
            context=context,
            data_type=UnifiedDataType.LOGS,
        )
        assert result.backend_used == "local_llm"

    @pytest.mark.asyncio
    async def test_a_response_that_is_not_an_llm_response_degrades_to_excerpts(
        self, service, context
    ):
        """The client is an ILLMProvider; a bare string is not a shape it returns.

        This used to assert the opposite — that a plain string was accepted and
        used as the answer — via a ``hasattr(response, "content")`` hedge. The
        DI cannot produce such a client (``create_tier2_service`` is handed the
        router), and the hedge stopped being harmless once a response-level
        signal had to be read: ``is_truncated`` cannot be read off a stand-in
        that only pretends to be a response, so tolerating one would mean
        silently treating every such call as complete.

        Something genuinely unexpected still degrades rather than failing the
        turn — the raw excerpts are returned, which is what the caller needs.
        """
        service.llm_client.generate.return_value = "plain string response"

        result = await service.analyze(
            file_ref="ref_1",
            query="error",
            context=context,
            data_type=UnifiedDataType.LOGS,
        )

        assert "LLM analysis failed" in result.answer
        assert "line2 error occurred" in result.answer
        assert result.backend_used == "local_llm"

    @pytest.mark.asyncio
    async def test_a_truncated_analysis_is_annotated_after_one_retry(
        self, service, context
    ):
        """Its consumer is another LLM, which must not read a cut clause as the whole.

        Annotate rather than refuse: partial analysis of a log file is still
        worth something, and the excerpts travel with it regardless.
        """
        from faultmaven.infrastructure.llm.truncation import TRUNCATION_NOTICE

        service.llm_client.generate = AsyncMock(
            return_value=_llm_response("Found 2 errors rel", StopReason.MAX_TOKENS)
        )

        result = await service.analyze(
            file_ref="ref_1",
            query="error",
            context=context,
            data_type=UnifiedDataType.LOGS,
        )

        assert service.llm_client.generate.await_count == 2
        caps = [
            c.kwargs["max_tokens"] for c in service.llm_client.generate.await_args_list
        ]
        assert caps == [service.max_tokens, service.max_tokens * 2]
        assert "Found 2 errors rel" in result.answer
        assert TRUNCATION_NOTICE.strip() in result.answer


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
