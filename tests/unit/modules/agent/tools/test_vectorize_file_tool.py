"""Tests for VectorizeFileTool — on-demand vectorization for semantic search."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.vectorize_file_tool import (
    VectorizeFileTool,
    VECTORIZATION_MAX_SIZE_BYTES,
)


@pytest.fixture
def mock_settings():
    """Patch get_settings to return controllable vectorization threshold."""
    settings = MagicMock()
    settings.agent.vectorization_min_size_bytes = 50_000  # 50KB default
    with patch(
        "faultmaven.modules.agent.tools.vectorize_file_tool.get_settings",
        return_value=settings,
    ) as mock:
        yield settings


@pytest.fixture
def tool():
    return VectorizeFileTool(
        case_vector_store=MagicMock(),
        storage_service=MagicMock(),
    )


@pytest.fixture
def context():
    evidence = MagicMock()
    evidence.case_id = "case_123"
    evidence.content_size_bytes = 100_000  # 100KB — above minimum
    evidence.preprocessed_content = "Structural index content here..."
    evidence.data_type = "logs"

    evidence_service = AsyncMock()
    evidence_service.get_evidence.return_value = evidence

    return ToolContext(
        session_id="sess_1",
        case_id="case_123",
        organization_id="org_1",
        user_id="user_1",
        evidence_service=evidence_service,
    )


class TestSizeGates:
    @pytest.mark.asyncio
    async def test_rejects_file_below_minimum(self, tool, context, mock_settings):
        """Files below the configured minimum should be rejected."""
        evidence = context.evidence_service.get_evidence.return_value
        evidence.content_size_bytes = 10_000  # 10KB

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_small"},
            context=context,
        )

        assert result.success is False
        assert "too small" in result.error
        assert str(mock_settings.agent.vectorization_min_size_bytes) in result.error

    @pytest.mark.asyncio
    async def test_rejects_file_above_maximum(self, tool, context, mock_settings):
        """Files above 50MB should be rejected."""
        evidence = context.evidence_service.get_evidence.return_value
        evidence.content_size_bytes = 60_000_000  # 60MB

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_huge"},
            context=context,
        )

        assert result.success is False
        assert "too large" in result.error
        assert str(VECTORIZATION_MAX_SIZE_BYTES) in result.error

    @pytest.mark.asyncio
    async def test_accepts_file_within_range(self, tool, context, mock_settings):
        """Files between configured minimum and 50MB should be accepted."""
        with patch(
            "faultmaven.core.preprocessing.vector_storage.store_in_vector_db_background",
            new_callable=AsyncMock,
        ) as mock_store:
            result = await tool.execute_with_context(
                params={"evidence_id": "ev_good"},
                context=context,
            )

        assert result.success is True
        assert "vectorized" in result.data["message"]
        mock_store.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_custom_min_size_threshold(self, tool, context, mock_settings):
        """Minimum size threshold should be configurable via settings."""
        mock_settings.agent.vectorization_min_size_bytes = 200_000  # 200KB

        evidence = context.evidence_service.get_evidence.return_value
        evidence.content_size_bytes = 100_000  # 100KB — below new threshold

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_under_custom"},
            context=context,
        )

        assert result.success is False
        assert "too small" in result.error
        assert "200000" in result.error


class TestVectorization:
    @pytest.mark.asyncio
    async def test_calls_store_function(self, tool, context, mock_settings):
        """Should call store_in_vector_db_background with correct params."""
        with patch(
            "faultmaven.core.preprocessing.vector_storage.store_in_vector_db_background",
            new_callable=AsyncMock,
        ) as mock_store:
            await tool.execute_with_context(
                params={"evidence_id": "ev_abc"},
                context=context,
            )

        mock_store.assert_awaited_once()
        call_kwargs = mock_store.call_args[1]
        assert call_kwargs["case_id"] == "case_123"
        assert call_kwargs["evidence_id"] == "ev_abc"
        assert "Structural index" in call_kwargs["structural_index"]

    @pytest.mark.asyncio
    async def test_no_preprocessed_content(self, tool, context, mock_settings):
        """Should fail if evidence has no preprocessed content."""
        evidence = context.evidence_service.get_evidence.return_value
        evidence.preprocessed_content = None

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_empty"},
            context=context,
        )

        assert result.success is False
        assert "no preprocessed content" in result.error


class TestValidation:
    @pytest.mark.asyncio
    async def test_missing_evidence_id(self, tool, context):
        result = await tool.execute_with_context(
            params={},
            context=context,
        )
        assert result.success is False
        assert "evidence_id" in result.error

    @pytest.mark.asyncio
    async def test_no_vector_store(self, context):
        tool = VectorizeFileTool(case_vector_store=None)
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc"},
            context=context,
        )
        assert result.success is False
        assert "not available" in result.error

    @pytest.mark.asyncio
    async def test_evidence_not_found(self, tool, context, mock_settings):
        context.evidence_service.get_evidence.return_value = None
        result = await tool.execute_with_context(
            params={"evidence_id": "ev_missing"},
            context=context,
        )
        assert result.success is False
        assert "not found" in result.error


class TestToolProperties:
    def test_name(self, tool):
        assert tool.name == "vectorize_file"

    def test_schema(self, tool):
        schema = tool.parameters_schema
        assert "evidence_id" in schema["properties"]
        assert schema["required"] == ["evidence_id"]
