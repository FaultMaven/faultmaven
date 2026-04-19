"""Tests for VectorizeFileTool — on-demand vectorization for semantic search.

Storage redesign 2026-04 phase 2: VectorizeFileTool reads evidence from
`case.evidence` (via `context.in_memory_case` / `context.case_repository`)
instead of the deleted standalone evidence service.
"""

from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.vectorize_file_tool import (
    VECTORIZATION_MAX_SIZE_BYTES,
    VectorizeFileTool,
)


def _make_evidence(
    evidence_id: str = "ev_abc",
    case_id: str = "case_123",
    content_size_bytes: int = 100_000,
    preprocessed_content: Optional[str] = "Structural index content here...",
    data_type: str = "logs",
):
    ev = MagicMock()
    ev.evidence_id = evidence_id
    ev.case_id = case_id
    ev.content_size_bytes = content_size_bytes
    ev.preprocessed_content = preprocessed_content
    ev.data_type = data_type
    return ev


def _make_context(case_id: str = "case_123", evidence_items=None):
    case = MagicMock()
    case.case_id = case_id
    case.evidence = evidence_items if evidence_items is not None else [_make_evidence()]
    return ToolContext(
        session_id="sess_1",
        case_id=case_id,
        organization_id="org_1",
        user_id="user_1",
        in_memory_case=case,
    )


@pytest.fixture
def mock_settings():
    """Patch get_settings to return controllable vectorization threshold."""
    settings = MagicMock()
    settings.agent.vectorization_min_size_bytes = 50_000  # 50KB default
    with patch(
        "faultmaven.modules.agent.tools.vectorize_file_tool.get_settings",
        return_value=settings,
    ):
        yield settings


@pytest.fixture
def tool():
    return VectorizeFileTool(
        case_vector_store=MagicMock(),
        storage_service=MagicMock(),
    )


@pytest.fixture
def context():
    return _make_context()


class TestSizeGates:
    @pytest.mark.asyncio
    async def test_rejects_file_below_minimum(self, tool, mock_settings):
        ev = _make_evidence(content_size_bytes=10_000)
        context = _make_context(evidence_items=[ev])

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc"},
            context=context,
        )

        assert result.success is False
        assert "too small" in result.error
        assert str(mock_settings.agent.vectorization_min_size_bytes) in result.error

    @pytest.mark.asyncio
    async def test_rejects_file_above_maximum(self, tool, mock_settings):
        ev = _make_evidence(content_size_bytes=60_000_000)
        context = _make_context(evidence_items=[ev])

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc"},
            context=context,
        )

        assert result.success is False
        assert "too large" in result.error
        assert str(VECTORIZATION_MAX_SIZE_BYTES) in result.error

    @pytest.mark.asyncio
    async def test_accepts_file_within_range(self, tool, context, mock_settings):
        with patch(
            "faultmaven.core.preprocessing.vector_storage.store_in_vector_db_background",
            new_callable=AsyncMock,
        ) as mock_store:
            result = await tool.execute_with_context(
                params={"evidence_id": "ev_abc"},
                context=context,
            )

        assert result.success is True
        assert "vectorized" in result.data["message"]
        mock_store.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_custom_min_size_threshold(self, tool, mock_settings):
        mock_settings.agent.vectorization_min_size_bytes = 200_000
        ev = _make_evidence(content_size_bytes=100_000)
        context = _make_context(evidence_items=[ev])

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc"},
            context=context,
        )

        assert result.success is False
        assert "too small" in result.error
        assert "200000" in result.error


class TestVectorization:
    @pytest.mark.asyncio
    async def test_calls_store_function(self, tool, context, mock_settings):
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
    async def test_no_preprocessed_content(self, tool, mock_settings):
        ev = _make_evidence(preprocessed_content=None)
        context = _make_context(evidence_items=[ev])

        result = await tool.execute_with_context(
            params={"evidence_id": "ev_abc"},
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
    async def test_evidence_not_found(self, tool, mock_settings):
        # No evidence on the case at all.
        context = _make_context(evidence_items=[])
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
