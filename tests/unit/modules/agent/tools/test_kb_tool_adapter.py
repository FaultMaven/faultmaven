"""Tests for KB tool adapters — KBToolAdapter, CaseEvidenceQAAdapter."""

from unittest.mock import AsyncMock

import pytest

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.kb_tool_adapter import (
    CaseEvidenceQAAdapter,
    KBToolAdapter,
)


@pytest.fixture
def context():
    return ToolContext(
        session_id="sess_1",
        case_id="case_123",
        organization_id="org_1",
        user_id="user_42",
        team_ids=["team_sre", "team_platform"],
    )


@pytest.fixture
def mock_kb_tool():
    tool = AsyncMock()
    tool._arun = AsyncMock(
        return_value="**Answer**: Standard approach is to check heap dumps.\n\n"
        "Sources: Memory Troubleshooting Guide"
    )
    return tool


@pytest.fixture
def mock_case_evidence_tool():
    tool = AsyncMock()
    tool._arun = AsyncMock(
        return_value="Found 3 OOM errors in the evidence.\n\nSources: app.log"
    )
    return tool


class TestKBToolAdapter:
    """Tests for unified KB tool adapter."""

    @pytest.mark.asyncio
    async def test_passes_question_to_wrapped_tool(self, mock_kb_tool, context):
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool)

        result = await adapter.execute_with_context(
            {"question": "How to diagnose memory leaks?"}, context
        )

        assert result.success is True
        mock_kb_tool._arun.assert_called_once_with(
            question="How to diagnose memory leaks?",
            user_id="user_42",
            team_ids=["team_sre", "team_platform"],
            k=5,
            context_metadata=None,
        )

    @pytest.mark.asyncio
    async def test_forwards_kb_context_metadata(self, mock_kb_tool):
        """Case domain/service context reaches the wrapped KB tool so the
        reranker's metadata signal can boost aligned runbooks."""
        context = ToolContext(
            session_id="sess_1",
            case_id="case_123",
            organization_id="org_1",
            user_id="user_42",
            kb_context_metadata={"service": "payment-api"},
        )
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool)

        await adapter.execute_with_context({"question": "test"}, context)

        call_kwargs = mock_kb_tool._arun.call_args.kwargs
        assert call_kwargs["context_metadata"] == {"service": "payment-api"}

    @pytest.mark.asyncio
    async def test_empty_kb_context_metadata_forwarded_as_none(
        self, mock_kb_tool, context
    ):
        """No verification context yet → forward None (no spurious boost)."""
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool)

        await adapter.execute_with_context({"question": "test"}, context)

        call_kwargs = mock_kb_tool._arun.call_args.kwargs
        assert call_kwargs["context_metadata"] is None

    @pytest.mark.asyncio
    async def test_passes_user_id_from_context(self, mock_kb_tool, context):
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool)

        await adapter.execute_with_context({"question": "test"}, context)

        call_kwargs = mock_kb_tool._arun.call_args.kwargs
        assert call_kwargs["user_id"] == "user_42"

    @pytest.mark.asyncio
    async def test_passes_team_ids_from_context(self, mock_kb_tool, context):
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool)

        await adapter.execute_with_context({"question": "test"}, context)

        call_kwargs = mock_kb_tool._arun.call_args.kwargs
        assert call_kwargs["team_ids"] == ["team_sre", "team_platform"]

    @pytest.mark.asyncio
    async def test_empty_question_returns_error(self, mock_kb_tool, context):
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool)

        result = await adapter.execute_with_context({"question": ""}, context)

        assert result.success is False
        assert "No question" in result.error

    @pytest.mark.asyncio
    async def test_handles_tool_failure(self, mock_kb_tool, context):
        mock_kb_tool._arun.side_effect = Exception("ChromaDB unavailable")
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool)

        result = await adapter.execute_with_context({"question": "test"}, context)

        assert result.success is False
        assert "failed" in result.error.lower()

    def test_adapter_name(self, mock_kb_tool):
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool)
        assert adapter.name == "kb_qa"

    def test_adapter_has_parameters_schema(self, mock_kb_tool):
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool)
        schema = adapter.parameters_schema
        assert "question" in schema["properties"]
        assert "question" in schema["required"]


class TestCaseEvidenceQAAdapter:
    """Tests for case evidence Q&A adapter."""

    @pytest.mark.asyncio
    async def test_passes_case_id_from_context(self, mock_case_evidence_tool, context):
        adapter = CaseEvidenceQAAdapter(wrapped_tool=mock_case_evidence_tool)

        result = await adapter.execute_with_context(
            {"question": "Any OOM errors?"}, context
        )

        assert result.success is True
        mock_case_evidence_tool._arun.assert_called_once_with(
            case_id="case_123",
            question="Any OOM errors?",
            k=5,
        )

    @pytest.mark.asyncio
    async def test_handles_tool_failure(self, mock_case_evidence_tool, context):
        mock_case_evidence_tool._arun.side_effect = Exception("Search failed")
        adapter = CaseEvidenceQAAdapter(wrapped_tool=mock_case_evidence_tool)

        result = await adapter.execute_with_context({"question": "test"}, context)

        assert result.success is False

    def test_adapter_name(self, mock_case_evidence_tool):
        adapter = CaseEvidenceQAAdapter(wrapped_tool=mock_case_evidence_tool)
        assert adapter.name == "case_evidence_search"
