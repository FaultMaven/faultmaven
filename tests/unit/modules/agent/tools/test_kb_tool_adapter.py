"""Tests for KB tool adapters — KBToolAdapter, CaseEvidenceQAAdapter."""

from unittest.mock import AsyncMock

import pytest

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.kb_configs.case_evidence_config import (
    CaseEvidenceConfig,
)
from faultmaven.modules.agent.tools.kb_tool_adapter import (
    CaseEvidenceQAAdapter,
    KBToolAdapter,
)
from tests.model_content import disabled_redaction
from tests.unit.modules.agent.tools.leaky_failure import (
    assert_reported_by_class,
    leaky_error,
)
from tests.utils import admit_every_parent

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
        redaction_ctx=disabled_redaction(),
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
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool, admit=admit_every_parent)

        result = await adapter.execute_with_context(
            {"question": "How to diagnose memory leaks?"}, context
        )

        assert result.success is True
        mock_kb_tool._arun.assert_called_once_with(
            question="How to diagnose memory leaks?",
            scope_filter=CASE_SCOPE,
            k=5,
            redaction_ctx=context.redaction_ctx,
            admit=admit_every_parent,
        )

    @pytest.mark.asyncio
    async def test_the_scope_is_the_contexts_not_the_users(self, mock_kb_tool, context):
        """The adapter forwards the case scope; the turn's user is not an input."""
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool, admit=admit_every_parent)

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
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool, admit=admit_every_parent)

        result = await adapter.execute_with_context({"question": "test"}, context)

        assert result.success is False
        assert "no retrieval scope" in result.error
        assert "draw no conclusion" in result.error
        mock_kb_tool._arun.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_question_returns_error(self, mock_kb_tool, context):
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool, admit=admit_every_parent)

        result = await adapter.execute_with_context({"question": ""}, context)

        assert result.success is False
        assert "No question" in result.error

    def test_adapter_name(self, mock_kb_tool):
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool, admit=admit_every_parent)
        assert adapter.name == "kb_qa"

    def test_adapter_has_parameters_schema(self, mock_kb_tool):
        adapter = KBToolAdapter(wrapped_tool=mock_kb_tool, admit=admit_every_parent)
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
            redaction_ctx=context.redaction_ctx,
            # A context that gives no tools gets the text that names none (#1977).
            empty_result=CaseEvidenceConfig().empty_result_message,
        )

    def test_adapter_name(self, mock_case_evidence_tool):
        adapter = CaseEvidenceQAAdapter(wrapped_tool=mock_case_evidence_tool)
        assert adapter.name == "case_evidence_search"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_cls, guidance",
    [
        (KBToolAdapter, "draw no conclusion about what it holds"),
        (CaseEvidenceQAAdapter, "draw no conclusion about the case's evidence"),
    ],
)
async def test_a_failed_search_names_its_class_and_keeps_its_guidance(
    adapter_cls, guidance, context, caplog
):
    """#2332: the class and the #943 guidance reach the model, the text the log."""
    wrapped = AsyncMock()
    wrapped._arun = AsyncMock(side_effect=leaky_error())

    admission = {"admit": admit_every_parent} if adapter_cls is KBToolAdapter else {}
    result = await adapter_cls(wrapped_tool=wrapped, **admission).execute_with_context(
        {"question": "test"}, context
    )

    assert result.success is False
    assert guidance in result.error
    assert_reported_by_class(result.error, caplog.text)
