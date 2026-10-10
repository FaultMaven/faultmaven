"""Tests for KB tool adapters — KBToolAdapter, CaseEvidenceQAAdapter."""

from unittest.mock import AsyncMock

import pytest

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.kb_tool_adapter import (
    CaseEvidenceQAAdapter,
    KBToolAdapter,
)

#: A case scope as ``case_retrieval_scope`` builds it for a shared case.
CASE_SCOPE = {
    "$or": [
        {"scope": "global"},
        {"parent_document_id": {"$in": ["kb_shared_1", "kb_shared_2"]}},
    ]
}


@pytest.fixture
def context():
    return ToolContext(
        session_id="sess_1",
        case_id="case_123",
        enterprise_id="org_1",
        user_id="user_42",
        kb_scope_filter=CASE_SCOPE,
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
            scope_filter=CASE_SCOPE,
            k=5,
        )

    @pytest.mark.asyncio
    async def test_the_scope_is_the_contexts_not_the_users(self, mock_kb_tool, context):
        """The adapter forwards the case scope; the turn's user is not an input."""
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool)

        await adapter.execute_with_context({"question": "test"}, context)

        call_kwargs = mock_kb_tool._arun.call_args.kwargs
        assert call_kwargs["scope_filter"] == CASE_SCOPE
        assert "user_id" not in call_kwargs

    @pytest.mark.asyncio
    async def test_a_context_without_a_scope_is_refused_without_searching(
        self, mock_kb_tool, context
    ):
        """No resolved scope: refuse, rather than invent a narrower one."""
        context.kb_scope_filter = None
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool)

        result = await adapter.execute_with_context({"question": "test"}, context)

        assert result.success is False
        assert "no retrieval scope" in result.error
        assert "draw no conclusion" in result.error
        mock_kb_tool._arun.assert_not_called()

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
