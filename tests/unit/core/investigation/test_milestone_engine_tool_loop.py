"""
Tests for MilestoneEngine tool-calling loop (_tool_augmented_generate).

Validates the bounded tool-calling loop that allows the LLM to search
evidence files via investigation tools before producing structured output
via the schema tool (termination signal).
"""

import json
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from pydantic import BaseModel, Field

from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    MilestoneEngineError,
)
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall
from faultmaven.models.interfaces import ToolResult

# =========================================================================
# Test schema model
# =========================================================================


class SampleResponse(BaseModel):
    """Minimal schema model for testing."""

    agent_response: str = "test response"
    next_action: str = "continue"


class SampleResponseWithEnum(BaseModel):
    """Schema with an enum field for testing _fix_enum_violations."""

    agent_response: str = "test"
    status: str = Field(
        default="active", json_schema_extra={"enum": ["active", "inactive", "pending"]}
    )


# =========================================================================
# Helpers
# =========================================================================


def _make_tool_call_response(
    tool_name: str,
    arguments: dict,
    call_id: str = "call_123",
    content: str = "",
) -> LLMResponse:
    """Create an LLMResponse with a single tool call."""
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=100,
        response_time_ms=500,
        tool_calls=[
            ToolCall(
                id=call_id,
                type="function",
                function={
                    "name": tool_name,
                    "arguments": json.dumps(arguments),
                },
            )
        ],
    )


def _make_schema_response(
    data: dict, schema_name: str = "SampleResponse"
) -> LLMResponse:
    """Create an LLMResponse with a schema tool call (termination signal)."""
    return LLMResponse(
        content="",
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=200,
        response_time_ms=600,
        tool_calls=[
            ToolCall(
                id="call_schema",
                type="function",
                function={
                    "name": schema_name,
                    "arguments": json.dumps(data),
                },
            )
        ],
    )


def _make_no_tool_call_response() -> LLMResponse:
    """Create an LLMResponse with no tool calls."""
    return LLMResponse(
        content="Some text response",
        confidence=0.8,
        provider="test",
        model="test-model",
        tokens_used=50,
        response_time_ms=300,
        tool_calls=None,
    )


def _make_engine(
    mock_provider=None,
    mock_registry=None,
    mock_evidence_service=None,
    da_provider=None,
):
    """Create a MilestoneEngine with mocked dependencies."""
    provider = mock_provider or AsyncMock()
    repo = MagicMock()
    repo.save = AsyncMock()

    engine = MilestoneEngine(
        llm_provider=provider,
        repository=repo,
        investigation_tools=mock_registry,
        evidence_service=mock_evidence_service,
        da_provider=da_provider,
    )
    return engine


def _make_mock_registry():
    """Create a mock AgentToolRegistry."""
    mock_tool = MagicMock()
    mock_tool.name = "search_file"
    mock_tool.get_schema.return_value = {
        "name": "search_file",
        "description": "Search for patterns in evidence files",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }

    registry = MagicMock()
    registry.get_all_tools.return_value = [mock_tool]
    registry.execute_tool = AsyncMock()
    return registry


# =========================================================================
# Tool loop tests
# =========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestToolAugmentedGenerate:
    """Tests for _tool_augmented_generate()."""

    async def test_tool_loop_single_search_then_schema(self):
        """LLM calls search_file once, then schema tool. 2 LLM calls total."""
        # Arrange
        mock_provider = AsyncMock()
        search_response = _make_tool_call_response(
            "search_file", {"query": "ssh failed"}, call_id="call_search"
        )
        schema_response = _make_schema_response(
            {"agent_response": "Found SSH failures", "next_action": "continue"}
        )
        mock_provider.generate = AsyncMock(
            side_effect=[search_response, schema_response]
        )

        mock_registry = _make_mock_registry()
        mock_registry.execute_tool.return_value = ToolResult(
            success=True,
            data="Line 42: SSH authentication failure",
        )

        engine = _make_engine(mock_provider=mock_provider, mock_registry=mock_registry)

        investigation_tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_file",
                    "description": "Search files",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                },
            }
        ]
        tool_context = MagicMock()

        # Act
        result = await engine._tool_augmented_generate(
            prompt="Investigate SSH issues",
            schema_model=SampleResponse,
            investigation_tools=investigation_tools,
            tool_context=tool_context,
        )

        # Assert
        assert isinstance(result, SampleResponse)
        assert result.agent_response == "Found SSH failures"
        assert result.next_action == "continue"
        assert mock_provider.generate.call_count == 2
        mock_registry.execute_tool.assert_called_once_with(
            "search_file",
            {"query": "ssh failed"},
            tool_context,
        )

    async def test_tool_loop_max_iterations_forces_schema(self):
        """LLM always calls search_file. After MAX_TOOL_ITERATIONS, forced to call schema."""
        # Arrange
        mock_provider = AsyncMock()

        # First 4 iterations: search_file calls
        search_responses = [
            _make_tool_call_response(
                "search_file", {"query": f"query_{i}"}, call_id=f"call_{i}"
            )
            for i in range(4)
        ]
        # 5th call (forced final): schema tool
        forced_schema = _make_schema_response(
            {"agent_response": "Forced output", "next_action": "continue"}
        )

        mock_provider.generate = AsyncMock(
            side_effect=search_responses + [forced_schema]
        )

        mock_registry = _make_mock_registry()
        mock_registry.execute_tool.return_value = ToolResult(
            success=True, data="search result"
        )

        engine = _make_engine(mock_provider=mock_provider, mock_registry=mock_registry)
        tool_context = MagicMock()

        investigation_tools = [
            {
                "type": "function",
                "function": {"name": "search_file", "parameters": {}},
            }
        ]

        # Act
        result = await engine._tool_augmented_generate(
            prompt="Investigate",
            schema_model=SampleResponse,
            investigation_tools=investigation_tools,
            tool_context=tool_context,
        )

        # Assert: 4 search iterations + 1 forced final = 5 LLM calls
        assert mock_provider.generate.call_count == 5
        assert result.agent_response == "Forced output"

        # Verify the last call used tool_choice="required"
        last_call_kwargs = mock_provider.generate.call_args_list[-1]
        # keyword args
        assert last_call_kwargs.kwargs.get("tool_choice") == "required"

    async def test_tool_loop_deep_analysis_limited_to_one(self):
        """Second deep_analysis call gets limit message instead of executing."""
        # Arrange
        mock_provider = AsyncMock()

        # First call: deep_analysis
        da_response_1 = _make_tool_call_response(
            "deep_analysis",
            {"file_id": "ev_1", "question": "What failed?"},
            call_id="call_da1",
        )
        # Second call: deep_analysis (should be rate-limited)
        da_response_2 = _make_tool_call_response(
            "deep_analysis",
            {"file_id": "ev_2", "question": "What else?"},
            call_id="call_da2",
        )
        # Third call: schema
        schema_response = _make_schema_response(
            {"agent_response": "Analysis complete", "next_action": "continue"}
        )

        mock_provider.generate = AsyncMock(
            side_effect=[da_response_1, da_response_2, schema_response]
        )

        mock_registry = _make_mock_registry()
        mock_registry.execute_tool.return_value = ToolResult(
            success=True, data="Deep analysis result"
        )

        engine = _make_engine(mock_provider=mock_provider, mock_registry=mock_registry)
        tool_context = MagicMock()

        investigation_tools = [
            {
                "type": "function",
                "function": {"name": "deep_analysis", "parameters": {}},
            }
        ]

        # Act
        result = await engine._tool_augmented_generate(
            prompt="Analyze",
            schema_model=SampleResponse,
            investigation_tools=investigation_tools,
            tool_context=tool_context,
        )

        # Assert
        assert result.agent_response == "Analysis complete"
        # execute_tool called only once (first deep_analysis)
        assert mock_registry.execute_tool.call_count == 1

    async def test_tool_result_truncated_at_max_chars(self):
        """Tool results exceeding TOOL_RESULT_MAX_CHARS should be truncated."""
        # Arrange
        mock_provider = AsyncMock()
        search_response = _make_tool_call_response(
            "search_file", {"query": "error"}, call_id="call_long"
        )
        schema_response = _make_schema_response(
            {"agent_response": "done", "next_action": "continue"}
        )
        mock_provider.generate = AsyncMock(
            side_effect=[search_response, schema_response]
        )

        # Return a tool result exceeding TOOL_RESULT_MAX_CHARS
        long_result = "x" * (MilestoneEngine.TOOL_RESULT_MAX_CHARS + 1000)
        mock_registry = _make_mock_registry()
        mock_registry.execute_tool.return_value = ToolResult(
            success=True, data=long_result
        )

        engine = _make_engine(mock_provider=mock_provider, mock_registry=mock_registry)
        tool_context = MagicMock()

        investigation_tools = [
            {"type": "function", "function": {"name": "search_file", "parameters": {}}},
        ]

        # Act
        result = await engine._tool_augmented_generate(
            prompt="Search",
            schema_model=SampleResponse,
            investigation_tools=investigation_tools,
            tool_context=tool_context,
        )

        # Assert: Check that the tool result message was truncated
        # The second generate call should have the truncated tool result
        second_call = mock_provider.generate.call_args_list[1]
        messages = second_call.kwargs.get("messages") or second_call[1].get("messages")
        tool_msg = [m for m in messages if m.get("role") == "tool"][0]
        assert len(tool_msg["content"]) <= MilestoneEngine.TOOL_RESULT_MAX_CHARS + len(
            "\n[truncated]"
        )
        assert tool_msg["content"].endswith("[truncated]")

    async def test_tool_execution_error_returns_error_string(self):
        """When tool execution fails, error message is passed to LLM as tool result."""
        # Arrange
        mock_provider = AsyncMock()
        search_response = _make_tool_call_response(
            "search_file", {"query": "error"}, call_id="call_err"
        )
        schema_response = _make_schema_response(
            {"agent_response": "Handled error", "next_action": "continue"}
        )
        mock_provider.generate = AsyncMock(
            side_effect=[search_response, schema_response]
        )

        mock_registry = _make_mock_registry()
        mock_registry.execute_tool.return_value = ToolResult(
            success=False, data=None, error="File not found"
        )

        engine = _make_engine(mock_provider=mock_provider, mock_registry=mock_registry)
        tool_context = MagicMock()

        investigation_tools = [
            {"type": "function", "function": {"name": "search_file", "parameters": {}}},
        ]

        # Act
        result = await engine._tool_augmented_generate(
            prompt="Search",
            schema_model=SampleResponse,
            investigation_tools=investigation_tools,
            tool_context=tool_context,
        )

        # Assert: Error message passed as tool result, loop continues
        assert result.agent_response == "Handled error"
        second_call = mock_provider.generate.call_args_list[1]
        messages = second_call.kwargs.get("messages") or second_call[1].get("messages")
        tool_msg = [m for m in messages if m.get("role") == "tool"][0]
        assert "Error: File not found" in tool_msg["content"]

    async def test_no_tool_calls_from_llm_continues_loop(self):
        """When LLM returns no tool calls (non-final), loop continues."""
        # Arrange
        mock_provider = AsyncMock()
        no_tool_response = _make_no_tool_call_response()
        schema_response = _make_schema_response(
            {"agent_response": "done", "next_action": "continue"}
        )
        # First call: no tool calls. Second: still loops, returns schema
        mock_provider.generate = AsyncMock(
            side_effect=[no_tool_response, schema_response]
        )

        engine = _make_engine(mock_provider=mock_provider)
        tool_context = MagicMock()

        investigation_tools = [
            {"type": "function", "function": {"name": "search_file", "parameters": {}}},
        ]

        # Act
        result = await engine._tool_augmented_generate(
            prompt="Search",
            schema_model=SampleResponse,
            investigation_tools=investigation_tools,
            tool_context=tool_context,
        )

        # Assert
        assert result.agent_response == "done"
        assert mock_provider.generate.call_count == 2

    async def test_no_tool_calls_on_final_iteration_raises(self):
        """When LLM returns no tool calls on forced final iteration, raises error."""
        # Arrange
        mock_provider = AsyncMock()
        # All iterations return no tool calls
        no_tool_responses = [_make_no_tool_call_response() for _ in range(5)]
        mock_provider.generate = AsyncMock(side_effect=no_tool_responses)

        engine = _make_engine(mock_provider=mock_provider)
        tool_context = MagicMock()

        investigation_tools = [
            {"type": "function", "function": {"name": "search_file", "parameters": {}}},
        ]

        # Act & Assert
        with pytest.raises(
            MilestoneEngineError, match="no tool calls on forced final iteration"
        ):
            await engine._tool_augmented_generate(
                prompt="Search",
                schema_model=SampleResponse,
                investigation_tools=investigation_tools,
                tool_context=tool_context,
            )


# =========================================================================
# _generate_structured_output routing tests
# =========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestGenerateStructuredOutputRouting:
    """Tests for _generate_structured_output routing to tool loop vs single-shot."""

    async def test_no_tools_uses_single_shot_path(self):
        """When no investigation_tools, _tool_augmented_generate is NOT called.

        This is a regression test ensuring the routing logic in
        _generate_structured_output correctly takes the single-shot path
        (not the tool loop) when investigation_tools are absent.
        """
        # Arrange
        engine = _make_engine()

        # Patch _tool_augmented_generate to detect if it's called
        tool_augmented_called = False
        original_tag = engine._tool_augmented_generate

        async def track_tool_augmented(*args, **kwargs):
            nonlocal tool_augmented_called
            tool_augmented_called = True
            return await original_tag(*args, **kwargs)

        engine._tool_augmented_generate = track_tool_augmented

        # Also patch the single-shot LLM call path to return valid data
        from faultmaven.infrastructure.llm.structured_output_capability import (
            StructuredOutputCapability,
            StructuredOutputMode,
            StructuredOutputStrategy,
        )

        mock_strategy = StructuredOutputStrategy(
            capability=StructuredOutputCapability.FUNCTION_CALLING,
            mode=StructuredOutputMode.FUNCTION_CALLING,
            include_schema_in_prompt=False,
            response_format=None,
            extra_config={},
        )
        engine.llm_provider.get_structured_output_strategy = MagicMock(
            return_value=mock_strategy
        )

        schema_response = LLMResponse(
            content="",
            confidence=0.9,
            provider="test",
            model="test-model",
            tokens_used=100,
            response_time_ms=500,
            tool_calls=[
                ToolCall(
                    id="call_fc",
                    type="function",
                    function={
                        "name": "SampleResponse",
                        "arguments": json.dumps(
                            {"agent_response": "single shot", "next_action": "done"}
                        ),
                    },
                )
            ],
        )
        engine.llm_provider.generate = AsyncMock(return_value=schema_response)

        # Act - no investigation_tools or tool_context
        result = await engine._generate_structured_output(
            prompt="Test prompt",
            schema_model=SampleResponse,
        )

        # Assert
        assert (
            not tool_augmented_called
        ), "_tool_augmented_generate should not be called without tools"
        assert isinstance(result, SampleResponse)
        assert result.agent_response == "single shot"

    async def test_with_tools_routes_to_tool_augmented(self):
        """When investigation_tools and tool_context provided, routes to tool loop."""
        # Arrange
        mock_provider = AsyncMock()
        schema_response = _make_schema_response(
            {"agent_response": "tool augmented", "next_action": "continue"}
        )
        mock_provider.generate = AsyncMock(return_value=schema_response)
        # supports_tool_calling is a sync method on real providers
        mock_provider.supports_tool_calling = Mock(return_value=True)

        engine = _make_engine(mock_provider=mock_provider)

        investigation_tools = [
            {"type": "function", "function": {"name": "search_file", "parameters": {}}},
        ]
        tool_context = MagicMock()

        # Act
        result = await engine._generate_structured_output(
            prompt="Test prompt",
            schema_model=SampleResponse,
            investigation_tools=investigation_tools,
            tool_context=tool_context,
        )

        # Assert
        assert isinstance(result, SampleResponse)
        assert result.agent_response == "tool augmented"


# =========================================================================
# Helper method tests
# =========================================================================


@pytest.mark.unit
class TestBuildDaToolSchemas:
    """Tests for _build_da_tool_schemas()."""

    def test_returns_openai_format_tool_defs(self):
        """Verifies tool schemas wrapped in OpenAI format."""
        mock_tool = MagicMock()
        mock_tool.name = "search_file"
        mock_tool.get_schema.return_value = {
            "name": "search_file",
            "description": "Search files",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        }

        registry = MagicMock()
        registry.get_all_tools.return_value = [mock_tool]

        engine = _make_engine(mock_registry=registry)
        result = engine._build_da_tool_schemas()

        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "search_file"

    def test_no_investigation_tools_returns_empty(self):
        """Without investigation_tools, returns empty list."""
        engine = _make_engine()
        engine.investigation_tools = None
        result = engine._build_da_tool_schemas()
        assert result == []


@pytest.mark.unit
class TestBuildToolContext:
    """Tests for _build_tool_context()."""

    def test_creates_tool_context_with_case_fields(self):
        """Verifies ToolContext created with correct case fields.

        Storage redesign 2026-04 phase 2: ToolContext carries `case_repository`
        instead of `evidence_service` — tools now read evidence from
        `case.evidence` directly.
        """
        engine = _make_engine(mock_evidence_service=MagicMock())

        mock_case = MagicMock()
        mock_case.case_id = "case_001"
        mock_case.organization_id = "org_123"

        intent_data = {"user_id": "user_abc"}

        result = engine._build_tool_context(mock_case, intent_data)

        assert result.session_id == "case_001"
        assert result.case_id == "case_001"
        assert result.organization_id == "org_123"
        assert result.user_id == "user_abc"
        assert result.case_repository is engine.repository

    def test_default_user_id_when_not_in_intent(self):
        """Uses 'system' as default user_id when not in intent_data."""
        engine = _make_engine()

        mock_case = MagicMock()
        mock_case.case_id = "case_002"
        mock_case.organization_id = "org_456"

        result = engine._build_tool_context(mock_case, None)

        assert result.user_id == "system"


@pytest.mark.unit
class TestBuildAssistantMessage:
    """Tests for _build_assistant_message()."""

    def test_builds_openai_format_assistant_message(self):
        """Verifies OpenAI-format assistant message from LLMResponse."""
        response = LLMResponse(
            content="Let me search.",
            confidence=0.9,
            provider="test",
            model="test-model",
            tokens_used=50,
            response_time_ms=300,
            tool_calls=[
                ToolCall(
                    id="call_1",
                    type="function",
                    function={"name": "search_file", "arguments": '{"query": "error"}'},
                ),
            ],
        )

        result = MilestoneEngine._build_assistant_message(response)

        assert result["role"] == "assistant"
        assert result["content"] == "Let me search."
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["id"] == "call_1"
        assert result["tool_calls"][0]["function"]["name"] == "search_file"

    def test_no_tool_calls_omits_key(self):
        """Without tool_calls, the key should not be in the message."""
        response = LLMResponse(
            content="Just text",
            confidence=0.9,
            provider="test",
            model="test-model",
            tokens_used=50,
            response_time_ms=300,
            tool_calls=None,
        )

        result = MilestoneEngine._build_assistant_message(response)

        assert result["role"] == "assistant"
        assert result["content"] == "Just text"
        assert "tool_calls" not in result

    def test_empty_content_passes_empty_string(self):
        """Empty content should be represented as empty string."""
        response = LLMResponse(
            content=None,
            confidence=0.9,
            provider="test",
            model="test-model",
            tokens_used=50,
            response_time_ms=300,
            tool_calls=[
                ToolCall(
                    id="call_2",
                    type="function",
                    function={"name": "test", "arguments": "{}"},
                ),
            ],
        )

        result = MilestoneEngine._build_assistant_message(response)

        assert result["content"] == ""


@pytest.mark.unit
class TestFormatToolResult:
    """Tests for _format_tool_result()."""

    def test_success_with_string_data(self):
        """Success result with string data returns data directly."""
        result = ToolResult(success=True, data="Found 3 errors")
        formatted = MilestoneEngine._format_tool_result(result)
        assert formatted == "Found 3 errors"

    def test_success_with_dict_data(self):
        """Success result with dict data returns JSON string."""
        result = ToolResult(success=True, data={"matches": 3, "file": "syslog"})
        formatted = MilestoneEngine._format_tool_result(result)
        assert json.loads(formatted) == {"matches": 3, "file": "syslog"}

    def test_success_with_none_data(self):
        """Success result with None data returns default message."""
        result = ToolResult(success=True, data=None)
        formatted = MilestoneEngine._format_tool_result(result)
        assert formatted == "Success (no data returned)"

    def test_error_result_formatted_with_prefix(self):
        """Error result formatted with 'Error: ' prefix."""
        result = ToolResult(success=False, data=None, error="File not found")
        formatted = MilestoneEngine._format_tool_result(result)
        assert formatted == "Error: File not found"

    def test_error_result_no_error_message(self):
        """Error result without error message uses default."""
        result = ToolResult(success=False, data=None, error=None)
        formatted = MilestoneEngine._format_tool_result(result)
        assert formatted == "Error: Unknown error"


@pytest.mark.unit
class TestParseNestedJson:
    """Tests for _parse_nested_json() static method."""

    def test_string_json_parsed_recursively(self):
        """Nested JSON strings are parsed recursively."""
        obj = {
            "outer": '{"inner_key": "inner_value"}',
            "plain": "not json",
        }

        result = MilestoneEngine._parse_nested_json(obj)

        assert result["outer"] == {"inner_key": "inner_value"}
        assert result["plain"] == "not json"

    def test_deeply_nested_json(self):
        """Multiple levels of nested JSON strings are parsed."""
        obj = {
            "level1": '{"level2": "{\\"level3\\": \\"deep\\"}"}',
        }

        result = MilestoneEngine._parse_nested_json(obj)

        assert result["level1"]["level2"] == {"level3": "deep"}

    def test_list_items_parsed(self):
        """JSON strings in lists are parsed."""
        obj = ['{"key": "value"}', "plain text", 42]

        result = MilestoneEngine._parse_nested_json(obj)

        assert result[0] == {"key": "value"}
        assert result[1] == "plain text"
        assert result[2] == 42

    def test_non_json_string_preserved(self):
        """Non-JSON strings are left as-is."""
        obj = {"key": "just a normal string"}
        result = MilestoneEngine._parse_nested_json(obj)
        assert result["key"] == "just a normal string"

    def test_non_string_types_preserved(self):
        """Ints, floats, bools, None are preserved."""
        obj = {"int": 42, "float": 3.14, "bool": True, "none": None}
        result = MilestoneEngine._parse_nested_json(obj)
        assert result == obj


@pytest.mark.unit
class TestFixEnumViolations:
    """Tests for _fix_enum_violations() static method."""

    def test_valid_enum_value_preserved(self):
        """Valid enum values are not modified."""
        schema = {
            "properties": {
                "status": {"enum": ["active", "inactive", "pending"]},
            },
        }
        obj = {"status": "active"}
        result = MilestoneEngine._fix_enum_violations(obj, schema)
        assert result["status"] == "active"

    def test_invalid_enum_corrected_to_close_match(self):
        """Invalid enum values are corrected to closest match."""
        schema = {
            "properties": {
                "status": {"enum": ["active", "inactive", "pending"]},
            },
        }
        obj = {"status": "actve"}  # typo
        result = MilestoneEngine._fix_enum_violations(obj, schema)
        assert result["status"] == "active"

    def test_no_close_match_uses_fallback(self):
        """When no close match exists, falls back to first valid value."""
        schema = {
            "properties": {
                "status": {"enum": ["active", "inactive", "pending"]},
            },
        }
        obj = {"status": "completely_wrong_value_xyz"}
        result = MilestoneEngine._fix_enum_violations(obj, schema)
        assert result["status"] == "active"  # first enum value

    def test_non_enum_fields_preserved(self):
        """Non-enum fields are not modified."""
        schema = {
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
            },
        }
        obj = {"name": "test", "count": 42}
        result = MilestoneEngine._fix_enum_violations(obj, schema)
        assert result == {"name": "test", "count": 42}

    def test_unknown_fields_preserved(self):
        """Fields not in schema properties are preserved as-is."""
        schema = {
            "properties": {
                "known": {"type": "string"},
            },
        }
        obj = {"known": "value", "unknown_field": "extra"}
        result = MilestoneEngine._fix_enum_violations(obj, schema)
        assert result["unknown_field"] == "extra"

    def test_nested_object_enums_fixed(self):
        """Enum violations in nested objects (via $ref) are corrected."""
        schema = {
            "properties": {
                "nested": {"$ref": "#/$defs/Inner"},
            },
            "$defs": {
                "Inner": {
                    "properties": {
                        "level": {"enum": ["low", "medium", "high"]},
                    },
                },
            },
        }
        obj = {"nested": {"level": "hi"}}  # should match "high"
        result = MilestoneEngine._fix_enum_violations(obj, schema)
        assert result["nested"]["level"] == "high"

    def test_list_items_enums_fixed(self):
        """Enum violations in list items are corrected."""
        schema = {
            "properties": {
                "items": {
                    "items": {"$ref": "#/$defs/Item"},
                },
            },
            "$defs": {
                "Item": {
                    "properties": {
                        "priority": {"enum": ["low", "medium", "high"]},
                    },
                },
            },
        }
        obj = {"items": [{"priority": "lo"}, {"priority": "medium"}]}
        result = MilestoneEngine._fix_enum_violations(obj, schema)
        assert result["items"][0]["priority"] == "low"
        assert result["items"][1]["priority"] == "medium"


# =========================================================================
# Constants tests
# =========================================================================


@pytest.mark.unit
class TestToolLoopConstants:
    """Tests for tool loop constants."""

    def test_max_tool_iterations_is_4(self):
        assert MilestoneEngine.MAX_TOOL_ITERATIONS == 4

    def test_tool_result_max_chars_is_8000(self):
        assert MilestoneEngine.TOOL_RESULT_MAX_CHARS == 8000

    def test_max_deep_analysis_is_1(self):
        assert MilestoneEngine.MAX_DEEP_ANALYSIS == 1


# =========================================================================
# DA provider routing tests
# =========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestDaProviderRouting:
    """Tests that _tool_augmented_generate uses da_provider when set."""

    async def test_tool_loop_uses_da_provider_when_set(self):
        """When da_provider is set, it should be used instead of llm_provider."""
        default_provider = AsyncMock()
        da_prov = AsyncMock()

        # DA provider returns schema tool call immediately
        schema_response = _make_schema_response(
            {"agent_response": "Found it", "next_action": "done"}
        )
        da_prov.generate = AsyncMock(return_value=schema_response)

        registry = _make_mock_registry()
        engine = _make_engine(
            mock_provider=default_provider,
            mock_registry=registry,
            mock_evidence_service=MagicMock(),
            da_provider=da_prov,
        )

        tool_defs = engine._build_da_tool_schemas()
        tool_ctx = MagicMock()

        await engine._tool_augmented_generate(
            prompt="test",
            schema_model=SampleResponse,
            investigation_tools=tool_defs,
            tool_context=tool_ctx,
        )

        # da_provider should have been called
        da_prov.generate.assert_called()
        # default llm_provider should NOT have been called
        default_provider.generate.assert_not_called()

    async def test_tool_loop_falls_back_to_llm_provider(self):
        """When da_provider is None, llm_provider is used."""
        default_provider = AsyncMock()

        schema_response = _make_schema_response(
            {"agent_response": "Found it", "next_action": "done"}
        )
        default_provider.generate = AsyncMock(return_value=schema_response)

        registry = _make_mock_registry()
        engine = _make_engine(
            mock_provider=default_provider,
            mock_registry=registry,
            mock_evidence_service=MagicMock(),
            da_provider=None,
        )

        tool_defs = engine._build_da_tool_schemas()
        tool_ctx = MagicMock()

        await engine._tool_augmented_generate(
            prompt="test",
            schema_model=SampleResponse,
            investigation_tools=tool_defs,
            tool_context=tool_ctx,
        )

        # llm_provider should have been called (fallback)
        default_provider.generate.assert_called()


@pytest.mark.unit
@pytest.mark.asyncio
class TestProactiveVectorizationGate:
    """Proactive vectorization must only fire for Directed Analysis turns.

    Triage and Knowledge Query turns don't consult case evidence via
    semantic search, so eagerly embedding attachments would be wasted
    work — and on a cold-cached BGE-M3 model it can dominate the turn
    budget. The gate condition is `force_tool_use=True`, which DA turns
    set (tool_choice="required") and other modes don't.
    """

    async def _setup_engine_with_case(self):
        mock_provider = AsyncMock()
        schema_response = _make_schema_response(
            {"agent_response": "ok", "next_action": "continue"}
        )
        mock_provider.generate = AsyncMock(return_value=schema_response)

        engine = _make_engine(
            mock_provider=mock_provider,
            mock_registry=_make_mock_registry(),
        )
        # Spy on the proactive entrypoint — return empty dict like the real one
        engine._start_proactive_vectorization = AsyncMock(return_value={})

        case = MagicMock()
        case.evidence = []
        return engine, case

    async def test_da_mode_triggers_proactive_vectorization(self):
        engine, case = await self._setup_engine_with_case()

        await engine._tool_augmented_generate(
            prompt="test",
            schema_model=SampleResponse,
            investigation_tools=[],
            tool_context=MagicMock(),
            case=case,
            force_tool_use=True,
        )

        engine._start_proactive_vectorization.assert_awaited_once()

    async def test_non_da_mode_skips_proactive_vectorization(self):
        engine, case = await self._setup_engine_with_case()

        await engine._tool_augmented_generate(
            prompt="test",
            schema_model=SampleResponse,
            investigation_tools=[],
            tool_context=MagicMock(),
            case=case,
            force_tool_use=False,
        )

        engine._start_proactive_vectorization.assert_not_called()
