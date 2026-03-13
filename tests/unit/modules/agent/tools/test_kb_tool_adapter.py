"""Tests for KB tool adapters — GlobalKBToolAdapter, UserKBToolAdapter."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.kb_tool_adapter import (
    GlobalKBToolAdapter,
    UserKBToolAdapter,
)


@pytest.fixture
def context():
    return ToolContext(
        session_id="sess_1",
        case_id="case_123",
        organization_id="org_1",
        user_id="user_42",
    )


@pytest.fixture
def mock_global_kb():
    tool = AsyncMock()
    tool._arun = AsyncMock(
        return_value="**Answer**: Standard approach is to check heap dumps.\n\n"
        "Sources: [KB-001] Memory Troubleshooting Guide"
    )
    return tool


@pytest.fixture
def mock_user_kb():
    tool = AsyncMock()
    tool._arun = AsyncMock(
        return_value="**Answer**: Your runbook says to restart the pod first.\n\n"
        "Sources: [RB-005] My OOM Runbook"
    )
    return tool


@pytest.fixture
def global_adapter(mock_global_kb):
    return GlobalKBToolAdapter(wrapped_tool=mock_global_kb)


@pytest.fixture
def user_adapter(mock_user_kb):
    return UserKBToolAdapter(wrapped_tool=mock_user_kb)


class TestGlobalKBToolAdapterInterface:
    """Verify AgentTool interface compliance."""

    def test_name(self, global_adapter):
        assert global_adapter.name == "global_kb_qa"

    def test_description_is_string(self, global_adapter):
        assert isinstance(global_adapter.description, str)
        assert len(global_adapter.description) > 20

    def test_parameters_schema(self, global_adapter):
        schema = global_adapter.parameters_schema
        assert schema["type"] == "object"
        assert "question" in schema["properties"]
        assert schema["required"] == ["question"]

    def test_get_schema(self, global_adapter):
        schema = global_adapter.get_schema()
        assert schema["name"] == "global_kb_qa"
        assert "parameters" in schema

    def test_validate_params_missing_question(self, global_adapter):
        error = global_adapter.validate_params({})
        assert error is not None
        assert "question" in error

    def test_validate_params_valid(self, global_adapter):
        error = global_adapter.validate_params({"question": "How to debug?"})
        assert error is None


class TestGlobalKBToolAdapterExecution:
    """Test global KB adapter delegation."""

    @pytest.mark.asyncio
    async def test_delegates_to_wrapped_tool(
        self, global_adapter, context, mock_global_kb
    ):
        result = await global_adapter.execute_with_context(
            params={"question": "standard approach for memory leaks?"},
            context=context,
        )

        assert result.success is True
        assert "heap dumps" in result.data
        mock_global_kb._arun.assert_awaited_once_with(
            question="standard approach for memory leaks?", k=5
        )

    @pytest.mark.asyncio
    async def test_empty_question_returns_error(self, global_adapter, context):
        result = await global_adapter.execute_with_context(
            params={"question": ""},
            context=context,
        )

        assert result.success is False
        assert "No question" in result.error

    @pytest.mark.asyncio
    async def test_wrapped_tool_error_returns_error(
        self, global_adapter, context, mock_global_kb
    ):
        mock_global_kb._arun.side_effect = Exception("ChromaDB connection failed")

        result = await global_adapter.execute_with_context(
            params={"question": "test question"},
            context=context,
        )

        assert result.success is False
        assert "failed" in result.error.lower()

    @pytest.mark.asyncio
    async def test_does_not_pass_scope_id(
        self, global_adapter, context, mock_global_kb
    ):
        """Global KB should NOT receive user scope."""
        await global_adapter.execute_with_context(
            params={"question": "memory leak best practices"},
            context=context,
        )

        call_kwargs = mock_global_kb._arun.call_args
        # Global KB _arun signature: (question, k) — no user_id
        assert "user_id" not in (call_kwargs.kwargs if call_kwargs.kwargs else {})


class TestUserKBToolAdapterInterface:
    """Verify AgentTool interface compliance."""

    def test_name(self, user_adapter):
        assert user_adapter.name == "user_kb_qa"

    def test_description_is_string(self, user_adapter):
        assert isinstance(user_adapter.description, str)
        assert (
            "personal" in user_adapter.description.lower()
            or "runbook" in user_adapter.description.lower()
        )

    def test_parameters_schema(self, user_adapter):
        schema = user_adapter.parameters_schema
        assert schema["type"] == "object"
        assert "question" in schema["properties"]
        assert schema["required"] == ["question"]


class TestUserKBToolAdapterExecution:
    """Test user KB adapter delegation with user scoping."""

    @pytest.mark.asyncio
    async def test_delegates_with_user_id(self, user_adapter, context, mock_user_kb):
        result = await user_adapter.execute_with_context(
            params={"question": "my OOM runbook"},
            context=context,
        )

        assert result.success is True
        assert "runbook" in result.data.lower()
        mock_user_kb._arun.assert_awaited_once_with(
            user_id="user_42",
            question="my OOM runbook",
            k=5,
        )

    @pytest.mark.asyncio
    async def test_uses_context_user_id(self, user_adapter, mock_user_kb):
        """Verify the adapter extracts user_id from ToolContext."""
        ctx = ToolContext(
            session_id="s",
            case_id="c",
            organization_id="o",
            user_id="different_user",
        )
        await user_adapter.execute_with_context(
            params={"question": "my procedure"},
            context=ctx,
        )

        call_kwargs = mock_user_kb._arun.call_args.kwargs
        assert call_kwargs["user_id"] == "different_user"

    @pytest.mark.asyncio
    async def test_empty_question_returns_error(self, user_adapter, context):
        result = await user_adapter.execute_with_context(
            params={"question": "   "},
            context=context,
        )

        assert result.success is False

    @pytest.mark.asyncio
    async def test_wrapped_tool_error_returns_error(
        self, user_adapter, context, mock_user_kb
    ):
        mock_user_kb._arun.side_effect = Exception("Vector store unavailable")

        result = await user_adapter.execute_with_context(
            params={"question": "my runbook"},
            context=context,
        )

        assert result.success is False
        assert "failed" in result.error.lower()
