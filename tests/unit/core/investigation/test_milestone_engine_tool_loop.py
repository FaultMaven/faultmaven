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

    async def test_no_tool_calls_on_forced_schema_escalates_to_fallback(self):
        """When LLM ignores tool_choice=required under forced-schema, raise
        ToolCallingUnsupportedError so _generate_structured_output's fallback
        path retries via the non-tool route (instead of 500-ing the request).
        """
        from faultmaven.exceptions import ToolCallingUnsupportedError

        # Arrange — every iteration returns text only, no tool calls
        mock_provider = AsyncMock()
        no_tool_responses = [_make_no_tool_call_response() for _ in range(5)]
        mock_provider.generate = AsyncMock(side_effect=no_tool_responses)

        engine = _make_engine(mock_provider=mock_provider)
        tool_context = MagicMock()

        investigation_tools = [
            {"type": "function", "function": {"name": "search_file", "parameters": {}}},
        ]

        # Act & Assert
        with pytest.raises(ToolCallingUnsupportedError):
            await engine._tool_augmented_generate(
                prompt="Search",
                schema_model=SampleResponse,
                investigation_tools=investigation_tools,
                tool_context=tool_context,
            )

    async def test_inline_json_text_parsed_as_schema_under_forced_schema(self):
        """When the LLM ignores tool_choice=required but inlines schema JSON
        as text, the loop recovers by parsing the text as the schema."""
        # Arrange — iter 0 returns text (no tool call), iter 1 (forced schema)
        # returns inline JSON wrapped in a ```json fence
        inline_json = (
            "Here you go:\n```json\n"
            '{"agent_response": "recovered", "next_action": "continue"}\n'
            "```"
        )
        first = _make_no_tool_call_response()
        second = LLMResponse(
            content=inline_json,
            confidence=0.7,
            provider="test",
            model="test-model",
            tokens_used=80,
            response_time_ms=400,
            tool_calls=None,
        )
        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(side_effect=[first, second])

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
        assert result.agent_response == "recovered"
        assert mock_provider.generate.call_count == 2

    async def test_recovery_appends_user_nudge_after_text_response(self):
        """When iter N returns text (no tool calls), the loop appends both the
        assistant text AND a user-role nudge directing the schema-tool call.
        Without the user nudge the conversation ends on an assistant message
        and the LLM treats it as 'already answered'."""
        no_tool = _make_no_tool_call_response()
        schema_resp = _make_schema_response(
            {"agent_response": "ok", "next_action": "continue"}
        )
        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(side_effect=[no_tool, schema_resp])

        engine = _make_engine(mock_provider=mock_provider)
        tool_context = MagicMock()
        investigation_tools = [
            {"type": "function", "function": {"name": "search_file", "parameters": {}}},
        ]

        await engine._tool_augmented_generate(
            prompt="Search",
            schema_model=SampleResponse,
            investigation_tools=investigation_tools,
            tool_context=tool_context,
        )

        second_call = mock_provider.generate.call_args_list[1]
        messages = second_call.kwargs.get("messages") or second_call[1].get("messages")
        # Last two messages should be the assistant text, then a user nudge
        assert messages[-2]["role"] == "assistant"
        assert messages[-1]["role"] == "user"
        assert "SampleResponse" in messages[-1]["content"]
        assert "tool" in messages[-1]["content"].lower()

    async def test_iter_1_exception_raises_tool_calling_unsupported(self):
        """When a provider hangs/errors on iteration 1+ (e.g., MiniMax M2P7
        timing out on tool_choice=required during the forced-schema retry),
        the loop must raise ToolCallingUnsupportedError so the non-tool
        fallback fires. Previously iter-1+ exceptions propagated and killed
        the turn, leaving subsequent turns with a hole in conversation
        history. See 2026-05-20 Run 7 post-mortem."""
        from faultmaven.exceptions import ToolCallingUnsupportedError

        # Arrange — iter 0 returns a search tool call (no error). Iter 1
        # raises a TimeoutError, the actual failure shape observed in Run 7.
        search_response = _make_tool_call_response(
            "search_file", {"query": "anything"}, call_id="call_iter0"
        )

        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(
            side_effect=[search_response, TimeoutError("Fireworks 180s timeout")]
        )

        mock_registry = _make_mock_registry()
        mock_registry.execute_tool.return_value = ToolResult(
            success=True, data="result"
        )

        engine = _make_engine(mock_provider=mock_provider, mock_registry=mock_registry)
        tool_context = MagicMock()

        investigation_tools = [
            {"type": "function", "function": {"name": "search_file", "parameters": {}}},
        ]

        # Act & Assert — the TimeoutError on iter 1 must be wrapped, not
        # propagated. The caller's catch handler will then run the
        # non-tool fallback path.
        with pytest.raises(ToolCallingUnsupportedError):
            await engine._tool_augmented_generate(
                prompt="Search",
                schema_model=SampleResponse,
                investigation_tools=investigation_tools,
                tool_context=tool_context,
            )
        # Sanity: provider was called twice (iter 0 search + iter 1 timeout)
        assert mock_provider.generate.call_count == 2

    async def test_inline_json_with_empty_agent_response_escalates(self):
        """Defensive guard: prose embedding a structurally-valid JSON block
        with an empty `agent_response` must NOT be returned as-is. The schema
        validates (Pydantic accepts empty strings) but the response would be
        meaningless to the user. The loop must escalate to ToolCallingUnsupported
        so the non-tool fallback runs."""
        from faultmaven.exceptions import ToolCallingUnsupportedError

        # Both iterations return text. The forced-schema iteration's text is
        # a valid JSON block — but agent_response is empty whitespace.
        prose_with_empty_schema = (
            "Sure, the schema looks like:\n```json\n"
            '{"agent_response": "   ", "next_action": "continue"}\n'
            "```\n(That was just an example.)"
        )
        first = _make_no_tool_call_response()
        second = LLMResponse(
            content=prose_with_empty_schema,
            confidence=0.6,
            provider="test",
            model="test-model",
            tokens_used=70,
            response_time_ms=400,
            tool_calls=None,
        )
        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(side_effect=[first, second])

        engine = _make_engine(mock_provider=mock_provider)
        tool_context = MagicMock()
        investigation_tools = [
            {"type": "function", "function": {"name": "search_file", "parameters": {}}},
        ]

        with pytest.raises(ToolCallingUnsupportedError):
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

    async def test_tool_loop_unsupported_error_falls_back_to_non_tool_path(self):
        """Integration: when the tool loop escalates with ToolCallingUnsupportedError
        (provider ignored tool_choice=required and produced no parseable text),
        _generate_structured_output must catch it and re-run the LLM via the
        non-tool single-shot path. Without this, the user gets a 500."""
        from faultmaven.exceptions import ToolCallingUnsupportedError
        from faultmaven.infrastructure.llm.structured_output_capability import (
            StructuredOutputCapability,
            StructuredOutputMode,
            StructuredOutputStrategy,
        )

        # Arrange — provider passes the supports_tool_calling pre-check, but
        # _tool_augmented_generate raises ToolCallingUnsupportedError. The
        # subsequent single-shot generate returns a valid schema response.
        mock_provider = AsyncMock()
        mock_provider.supports_tool_calling = Mock(return_value=True)

        fallback_response = LLMResponse(
            content="",
            confidence=0.9,
            provider="test",
            model="test-model",
            tokens_used=120,
            response_time_ms=300,
            tool_calls=[
                ToolCall(
                    id="call_fc",
                    type="function",
                    function={
                        "name": "SampleResponse",
                        "arguments": json.dumps(
                            {
                                "agent_response": "fell back ok",
                                "next_action": "done",
                            }
                        ),
                    },
                )
            ],
        )
        mock_provider.generate = AsyncMock(return_value=fallback_response)
        mock_provider.get_structured_output_strategy = MagicMock(
            return_value=StructuredOutputStrategy(
                capability=StructuredOutputCapability.FUNCTION_CALLING,
                mode=StructuredOutputMode.FUNCTION_CALLING,
                include_schema_in_prompt=False,
                response_format=None,
                extra_config={},
            )
        )

        engine = _make_engine(mock_provider=mock_provider)
        engine._tool_augmented_generate = AsyncMock(
            side_effect=ToolCallingUnsupportedError(
                message="provider ignored tool_choice=required",
                provider="test",
                model="test-model",
            )
        )

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

        # Assert — the request did NOT 500; the fallback path produced a
        # valid response from the non-tool single-shot route.
        assert isinstance(result, SampleResponse)
        assert result.agent_response == "fell back ok"
        engine._tool_augmented_generate.assert_awaited_once()
        mock_provider.generate.assert_awaited()


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

    async def test_creates_tool_context_with_case_fields(self):
        """Verifies ToolContext created with correct case fields.

        ToolContext carries ``case_repository``; tools read evidence from
        ``case.evidence`` directly.
        """
        engine = _make_engine()

        mock_case = MagicMock()
        mock_case.case_id = "case_001"
        mock_case.enterprise_id = "ent_123"

        result = await engine._build_tool_context(mock_case, user_id="user_abc")

        assert result.session_id == "case_001"
        assert result.case_id == "case_001"
        assert result.enterprise_id == "ent_123"
        assert result.user_id == "user_abc"
        assert result.case_repository is engine.repository

    async def test_default_user_id_when_no_principal(self):
        """Uses 'system' when the turn carries no authenticated principal."""
        engine = _make_engine()

        mock_case = MagicMock()
        mock_case.case_id = "case_002"
        mock_case.enterprise_id = "ent_456"

        result = await engine._build_tool_context(mock_case, user_id=None)

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
# Per-turn spend bound (#611)
# =========================================================================


def _shipped_default(settings_cls, field: str) -> int:
    """The declared default, not the resolved value — the environment (a dev
    ``.env``) must not decide what the SHIPPED arithmetic is."""
    return settings_cls.model_fields[field].default


# Deliberately not the shipped 150,000 and not a multiple of 3, so a
# hard-coded ceiling and an off-by-one comparison cannot pass by coincidence.
_TEST_CEILING = 100_001


@pytest.mark.unit
@pytest.mark.asyncio
class TestToolLoopSpendBound:
    """#611: the MESSAGE bound on each tool-loop call is structural
    (``prompt_target + tool_observation_max_tokens``; since #614 the model's
    window is a separate hard cap on messages plus the ``tools=`` payload —
    pinned by ``TestToolLoopBoundSplitsSoftAndHardCaps``), and
    ``PROMPT_TURN_TOKEN_CEILING`` is a separate net over METERED, cost-weighted
    spend. Pins what milestone_engine's MAX_TOOL_ITERATIONS comment, the
    settings description and prompt-sizing-optimization.md §4.3 say: the
    formula, the measure the ceiling reads, and that it changes the loop only
    when crossed within the first ``MAX_TOOL_ITERATIONS - 1`` calls."""

    async def test_shipped_defaults_are_the_documented_numbers(self):
        from faultmaven.config.settings import (
            ModelContextSettings,
            PromptBudgetSettings,
        )

        target = _shipped_default(ModelContextSettings, "prompt_target_tokens")
        obs = _shipped_default(PromptBudgetSettings, "tool_observation_max_tokens")
        ceiling = _shipped_default(PromptBudgetSettings, "turn_token_ceiling")
        assert (target, obs, ceiling) == (32_000, 16_000, 150_000)
        assert MilestoneEngine.MAX_TOOL_ITERATIONS == 4

    @pytest.mark.parametrize(
        "prompt_budget, expected",
        [
            # The soft cap is target + observations whatever the window; the
            # window, when known, is the separate hard cap (#614).
            (None, (32_000 + 17_000, None)),  # unknown window: no hard cap
            (1_000_000, (32_000 + 17_000, 1_000_000)),
            (40_000, (32_000 + 17_000, 40_000)),  # window under the soft cap
        ],
    )
    async def test_soft_cap_is_target_plus_observations_window_is_the_hard_cap(
        self, prompt_budget, expected
    ):
        from types import SimpleNamespace

        engine = _make_engine()
        resolved = SimpleNamespace(prompt_target=32_000, prompt_budget=prompt_budget)
        fake_settings = SimpleNamespace(
            prompt_budget=SimpleNamespace(tool_observation_max_tokens=17_000)
        )
        with (
            patch(
                "faultmaven.utils.model_context.resolve_model_budget",
                return_value=resolved,
            ),
            patch(
                "faultmaven.config.settings.get_settings",
                return_value=fake_settings,
            ),
        ):
            assert engine._resolve_tool_loop_budget("openai") == expected

    async def _run_loop(self, monkeypatch, per_call_buckets: list) -> list:
        """Drive the real loop with a model that always wants another search.
        Call *i* meters ``per_call_buckets[i]`` (a dict of token buckets) into
        the turn tracker, standing in for the registry chokepoint, with the
        ceiling set to ``_TEST_CEILING``. Returns the tool names offered per
        call."""
        from faultmaven.config.settings import get_settings
        from faultmaven.infrastructure.llm.metering import (
            TurnTokenTracker,
            active_token_tracker,
        )

        monkeypatch.setattr(
            get_settings().prompt_budget, "turn_token_ceiling", _TEST_CEILING
        )
        tracker = TurnTokenTracker()
        offered: list = []

        async def _generate(**kwargs):
            names = [t["function"]["name"] for t in kwargs["tools"]]
            if names == ["SampleResponse"]:
                resp = _make_schema_response(
                    {"agent_response": "done", "next_action": "continue"}
                )
            else:
                resp = _make_tool_call_response(
                    "search_file", {"query": "q"}, call_id=f"c{len(offered)}"
                )
            for bucket, value in per_call_buckets[len(offered)].items():
                setattr(resp, bucket, value)
            offered.append(names)
            tracker.add(resp)
            return resp

        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(side_effect=_generate)
        mock_registry = _make_mock_registry()
        mock_registry.execute_tool.return_value = ToolResult(success=True, data="r")
        engine = _make_engine(mock_provider=mock_provider, mock_registry=mock_registry)

        token = active_token_tracker.set(tracker)
        try:
            await engine._tool_augmented_generate(
                prompt="Investigate",
                schema_model=SampleResponse,
                investigation_tools=[
                    {"type": "function", "function": {"name": "search_file"}}
                ],
                tool_context=MagicMock(),
            )
        finally:
            active_token_tracker.reset(token)
        return offered

    @staticmethod
    def _split(total: int, parts: int) -> list:
        """``parts`` integers summing exactly to ``total``."""
        base = total // parts
        return [base] * (parts - 1) + [total - base * (parts - 1)]

    async def test_spend_at_the_ceiling_within_the_early_calls_changes_nothing(
        self, monkeypatch
    ):
        """The first n-1 calls meter EXACTLY the ceiling: not over it, so all
        tool rounds run. The n-th call crosses it — and the ceiling does fire
        there — but the iteration it would force schema-only is already final."""
        n = MilestoneEngine.MAX_TOOL_ITERATIONS
        early = self._split(_TEST_CEILING, n - 1)
        buckets = [{"input_tokens": t} for t in early] + [
            {"input_tokens": _TEST_CEILING}
        ] * 2

        offered = await self._run_loop(monkeypatch, buckets)

        assert len(offered) == n + 1
        assert all("search_file" in names for names in offered[:n])
        assert offered[n] == ["SampleResponse"]

    async def test_spend_over_the_ceiling_within_the_early_calls_cuts_a_round(
        self, monkeypatch
    ):
        """One token over across the first n-1 calls and the ceiling takes the
        last tool round away. This is what makes the case above a measurement
        rather than a no-op."""
        n = MilestoneEngine.MAX_TOOL_ITERATIONS
        early = self._split(_TEST_CEILING + 1, n - 1)
        buckets = [{"input_tokens": t} for t in early] + [{"input_tokens": 1}]

        offered = await self._run_loop(monkeypatch, buckets)

        assert len(offered) == n
        assert all("search_file" in names for names in offered[: n - 1])
        assert offered[n - 1] == ["SampleResponse"]

    async def test_ceiling_reads_cost_weighted_spend_not_raw_tokens(self, monkeypatch):
        """Cache reads count at 0.25. The early calls are raw-over (by 4x) but
        cost-weighted exactly at the ceiling, so no round is cut — a ceiling
        compared on ``total_tokens`` would cut one."""
        n = MilestoneEngine.MAX_TOOL_ITERATIONS
        early = self._split(_TEST_CEILING, n - 1)
        buckets = [{"cache_read_tokens": 4 * t} for t in early] + [
            {"input_tokens": _TEST_CEILING}
        ] * 2

        offered = await self._run_loop(monkeypatch, buckets)

        assert len(offered) == n + 1
        assert all("search_file" in names for names in offered[:n])


# =========================================================================
# The per-call bound: a soft cap on messages, the window on what is sent (#614)
# =========================================================================


def _est(text: str, provider: str = "local", model: Optional[str] = None) -> int:
    from faultmaven.utils.token_estimation import estimate_tokens

    return estimate_tokens(text, provider=provider, model=model)


def _msg_tokens(m: dict, provider: str = "local", model=None) -> int:
    """The bound's documented per-message estimate, written out independently
    of the engine's helper so a change to the helper cannot pass by agreeing
    with itself."""
    parts = [str(m.get("content") or "")]
    if m.get("tool_calls"):
        parts.append(str(m.get("tool_calls")))
    if m.get("provider_metadata"):
        parts.append(str(m.get("provider_metadata")))
    return _est(" ".join(parts), provider, model)


def _tools_tokens(tools, provider: str = "local", model=None) -> int:
    return _est(json.dumps(tools, default=str), provider, model) if tools else 0


def _sent_tokens(call: dict, provider: str = "local", model=None) -> int:
    """Estimated size of one recorded ``generate`` call: messages + tools=."""
    return sum(_msg_tokens(m, provider, model) for m in call["messages"]) + (
        _tools_tokens(call["tools"], provider, model)
    )


def _heavy_search_tool(description_chars: int) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "search_file",
            "description": "d" * description_chars,
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
    }


def _recording_provider(result_after: int, provider_name=None):
    """A provider that calls search_file ``result_after`` times (or until
    only the schema tool is offered), then the schema tool. Snapshots each
    call's messages (the loop appends to the list it may have passed) and
    tools."""
    calls: list = []

    async def _generate(**kwargs):
        calls.append({"messages": list(kwargs["messages"]), "tools": kwargs["tools"]})
        names = [t["function"]["name"] for t in kwargs["tools"]]
        if len(calls) > result_after or names == ["SampleResponse"]:
            return _make_schema_response(
                {"agent_response": "done", "next_action": "continue"}
            )
        return _make_tool_call_response(
            "search_file", {"query": "q"}, call_id=f"c{len(calls)}"
        )

    provider = AsyncMock()
    if provider_name is not None:
        provider.provider_name = provider_name
    provider.generate = AsyncMock(side_effect=_generate)
    return provider, calls


def _registry_returning(result_chars: int):
    registry = _make_mock_registry()
    registry.execute_tool.return_value = ToolResult(
        success=True, data="r" * result_chars
    )
    return registry


def _caps(soft: int, hard: Optional[int]):
    """A per-call budget pair, imported lazily so this module still imports
    against a tree without it (the regression pin below is revert-verified
    against one)."""
    from faultmaven.core.investigation.milestone_engine import _ToolLoopBudget

    return _ToolLoopBudget(soft=soft, hard=hard)


def _groups_history(n: int, result_chars: int = 2_000) -> list:
    msgs = [
        {"role": "system", "content": "SYS " + "s" * 400},
        {"role": "user", "content": "BASE " + "b" * 400},
    ]
    for i in range(n):
        msgs.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": f"c{i}", "function": {"name": "x"}}],
            }
        )
        msgs.append(
            {"role": "tool", "tool_call_id": f"c{i}", "content": "r" * result_chars}
        )
    return msgs


_MARKER_TEXT = (
    "[Earlier tool calls and their results were elided to stay within "
    "the context budget. Re-run a search if you need those specifics.]"
)


def _main_bound(messages: list, budget: int, provider: str, model) -> list:
    """The elision rule of origin/main's ``_bound_tool_loop_messages`` before
    #614 (d8ba79c6b), written out as the reference: ``messages`` alone within
    ONE budget, ``min(prompt_target + observations, window)``, tools never
    counted. Returns what main would send, the marker as the string MARKER."""

    def tok(m):
        return _msg_tokens(m, provider, model)

    if sum(tok(m) for m in messages) <= budget:
        return list(messages)
    head, rest = messages[:2], messages[2:]
    groups: list = []
    for m in rest:
        if m.get("role") == "assistant" or not groups:
            groups.append([m])
        else:
            groups[-1].append(m)
    avail = budget - sum(tok(m) for m in head) - _est(_MARKER_TEXT, provider, model)
    kept: list = []
    for g in reversed(groups):
        gt = sum(tok(m) for m in g)
        if gt <= avail:
            kept.insert(0, g)
            avail -= gt
        else:
            break
    if len(kept) == len(groups):
        return list(messages)
    out = list(head) + ["MARKER"]
    for g in kept:
        out.extend(g)
    return out


def _shape(sent: list, history: list) -> list:
    """What a call sent, as positions in the full history (MARKER for the
    elision marker), so two runs compare by decision, not by object."""
    index = {id(m): i for i, m in enumerate(history)}
    return [
        (
            "MARKER"
            if (
                m == "MARKER"
                or (isinstance(m, dict) and m.get("content") == _MARKER_TEXT)
            )
            else index[id(m)]
        )
        for m in sent
    ]


def _real_da_registry(result_chars: int):
    """The six tools the DA registry can hold, with their real schemas; only
    execution is stubbed."""
    from faultmaven.modules.agent.tools.deep_analysis_tool import DeepAnalysisTool
    from faultmaven.modules.agent.tools.kb_tool_adapter import (
        CaseEvidenceQAAdapter,
        KBToolAdapter,
    )
    from faultmaven.modules.agent.tools.search_file_tool import SearchFileTool
    from faultmaven.modules.agent.tools.vectorize_file_tool import VectorizeFileTool
    from faultmaven.modules.agent.tools.web_search import WebSearchTool

    registry = MagicMock()
    registry.get_all_tools.return_value = [
        SearchFileTool(storage_service=MagicMock()),
        DeepAnalysisTool(tier2_service=MagicMock()),
        WebSearchTool(settings=MagicMock(), provider=MagicMock()),
        KBToolAdapter(wrapped_tool=MagicMock()),
        VectorizeFileTool(case_vector_store=MagicMock(), storage_service=MagicMock()),
        CaseEvidenceQAAdapter(wrapped_tool=MagicMock()),
    ]
    registry.execute_tool = AsyncMock(
        return_value=ToolResult(success=True, data="r" * result_chars)
    )
    return registry


def _diagnosis_turn_provider(provider_name: str):
    """Two parallel searches per tool iteration; a valid Diagnosis answer as
    soon as the schema tool is the only one offered."""
    calls: list = []

    async def _generate(**kwargs):
        calls.append({"messages": list(kwargs["messages"]), "tools": kwargs["tools"]})
        names = [t["function"]["name"] for t in kwargs["tools"]]
        if names == ["InvestigationResponse_Diagnosis"]:
            return _make_schema_response(
                {"agent_response": "done", "state_updates": {}},
                schema_name="InvestigationResponse_Diagnosis",
            )
        n = len(calls)
        return LLMResponse(
            content="",
            confidence=0.9,
            provider="test",
            model="test-model",
            tokens_used=0,
            response_time_ms=0,
            tool_calls=[
                ToolCall(
                    id=f"{k}{n}",
                    type="function",
                    function={
                        "name": "search_file",
                        "arguments": json.dumps({"query": f"q{k}{n}"}),
                    },
                )
                for k in ("a", "b")
            ],
        )

    provider = AsyncMock()
    provider.provider_name = provider_name
    provider.generate = AsyncMock(side_effect=_generate)
    return provider, calls


@pytest.mark.unit
@pytest.mark.asyncio
class TestToolLoopBoundSplitsSoftAndHardCaps:
    """#614: every tool-loop call keeps ``messages`` within the SOFT cap
    (``prompt_target + tool_observation_max_tokens``, tools not counted — the
    pre-#614 rule), and, when the model's window is known, ``messages`` PLUS
    that call's ``tools=`` payload within the window (the HARD cap). Counted
    with the estimator the bound uses for messages (``len // 4`` for these
    unnamed providers). The final iteration sends only the schema tool."""

    @pytest.mark.parametrize(
        "da",
        [None, ("gemini", "gemini-3.7-flash")],
        ids=["router-window-unknown", "gemini-da-1m-window"],
    )
    async def test_default_config_elides_exactly_what_main_did(self, monkeypatch, da):
        """The regression pin. On a full-size diagnosis turn — a 32,000-token
        base, the real Diagnosis schema tool and the six investigation tools
        (~12,200 tokens of tools payload) — every call sends exactly what
        origin/main sent, both through the router (window unknown) and to a
        large-window DA model. Counting the tools against the soft cap left
        ~2,150 tokens for observations here instead of ~14,400."""
        from faultmaven.config.settings import get_settings
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )
        from faultmaven.utils.model_context import resolve_model_budget

        settings = get_settings()
        monkeypatch.setattr(settings.model_context, "prompt_target_tokens", 32_000)
        monkeypatch.setattr(
            settings.prompt_budget, "tool_observation_max_tokens", 16_000
        )
        provider_name, da_model = da if da else ("LLMRouter", None)
        provider, calls = _diagnosis_turn_provider(provider_name)
        registry = _real_da_registry(result_chars=7_600)
        repo = MagicMock()
        repo.save = AsyncMock()
        engine = MilestoneEngine(
            llm_provider=provider if da is None else AsyncMock(),
            repository=repo,
            investigation_tools=registry,
            da_provider=provider if da else None,
            da_model=da_model,
        )
        real_bound = engine._bound_tool_loop_messages
        decisions: list = []

        def spy(messages, *args, **kwargs):
            out = real_bound(messages, *args, **kwargs)
            # Snapshot both: a pass-through returns the live history, which
            # the loop keeps appending to.
            decisions.append((list(messages), list(out)))
            return out

        engine._bound_tool_loop_messages = spy

        await engine._tool_augmented_generate(
            prompt="B" * (4 * 32_000),
            schema_model=InvestigationResponse_Diagnosis,
            investigation_tools=engine._build_da_tool_schemas(),
            tool_context=MagicMock(),
        )

        resolved = resolve_model_budget(provider_name, da_model)
        main_budget = resolved.prompt_target + 16_000
        if resolved.prompt_budget is not None:
            main_budget = min(main_budget, resolved.prompt_budget)
        assert main_budget == 48_000
        assert len(decisions) == len(calls) == MilestoneEngine.MAX_TOOL_ITERATIONS + 1
        est_model = da_model
        for (history, sent), call in zip(decisions, calls):
            expected = _main_bound(history, main_budget, provider_name, est_model)
            assert _shape(sent, history) == _shape(expected, history)
            assert _shape(call["messages"], history) == _shape(expected, history)
        # Positive controls: main elided something on this turn, so the pin
        # covers an elision decision and not only pass-throughs; and on every
        # tool call the history main kept plus the tools sent is over the soft
        # cap — the regime where counting the tools against it elides more.
        assert any(
            "MARKER" in _main_bound(h, 48_000, provider_name, est_model)
            for h, _ in decisions
        )
        for (history, _), call in zip(decisions[:-1], calls[:-1]):
            kept = _main_bound(history, 48_000, provider_name, est_model)
            kept_tokens = sum(
                (
                    _est(_MARKER_TEXT, provider_name, est_model)
                    if m == "MARKER"
                    else _msg_tokens(m, provider_name, est_model)
                )
                for m in kept
            )
            if len(history) > 2:
                assert (
                    kept_tokens + _tools_tokens(call["tools"], provider_name, est_model)
                    > 48_000
                )

    async def test_a_tight_window_keeps_messages_plus_tools_within_it(self):
        heavy = _heavy_search_tool(12_000)  # ~3,000 tokens of tools payload
        provider, calls = _recording_provider(result_after=10)
        engine = _make_engine(
            mock_provider=provider, mock_registry=_registry_returning(2_000)
        )
        window = 5_000
        # The soft cap is far away: only the window can bind here.
        engine._resolve_tool_loop_budget = lambda _name: _caps(10**6, window)

        await engine._tool_augmented_generate(
            prompt="BASE " + "b" * 400,
            schema_model=SampleResponse,
            investigation_tools=[heavy],
            tool_context=MagicMock(),
        )

        tool_calls = calls[: MilestoneEngine.MAX_TOOL_ITERATIONS]
        assert all(len(c["tools"]) == 2 for c in tool_calls)
        for c in calls:
            assert _sent_tokens(c) <= window
        # Positive control: the window binds — observations were elided on the
        # tool calls, with the tools taking ~3,000 of the 5,000.
        assert _tools_tokens([heavy]) > 2_500
        assert any(
            "elided to stay within" in str(m["content"])
            for c in tool_calls
            for m in c["messages"]
        )

    async def test_the_soft_cap_does_not_count_the_tools_the_window_does(self):
        """Same history, same number: as the soft cap nothing is elided though
        messages + tools exceed it; as the window the oldest group goes."""
        engine = _make_engine()
        tools = [_heavy_search_tool(4_000)]
        msgs = _groups_history(3)
        messages_only = sum(_msg_tokens(m) for m in msgs)
        cap = messages_only + _tools_tokens(tools) // 2
        assert messages_only + _tools_tokens(tools) > cap

        assert (
            engine._bound_tool_loop_messages(
                msgs, cap, "local", tools=tools, window_tokens=None
            )
            is msgs
        )
        out = engine._bound_tool_loop_messages(
            msgs, 10**6, "local", tools=tools, window_tokens=cap
        )
        assert out is not msgs
        assert sum(_msg_tokens(m) for m in out) + _tools_tokens(tools) <= cap
        assert "elided to stay within" in out[2]["content"]

    async def test_the_final_iteration_counts_only_the_schema_tool(self):
        """The final call offers the schema tool alone, so it has the
        investigation tools' share of the window back for observations: it
        keeps history that would not fit beside ALL the tools."""
        heavy = _heavy_search_tool(12_000)
        provider, calls = _recording_provider(result_after=10)
        engine = _make_engine(
            mock_provider=provider, mock_registry=_registry_returning(2_000)
        )
        window = 5_000
        engine._resolve_tool_loop_budget = lambda _name: _caps(10**6, window)

        await engine._tool_augmented_generate(
            prompt="BASE " + "b" * 400,
            schema_model=SampleResponse,
            investigation_tools=[heavy],
            tool_context=MagicMock(),
        )

        final = calls[-1]
        n = MilestoneEngine.MAX_TOOL_ITERATIONS
        assert len(calls) == n + 1
        schema_only = final["tools"]
        all_tools = calls[0]["tools"]
        assert [t["function"]["name"] for t in schema_only] == ["SampleResponse"]
        final_messages = sum(_msg_tokens(m) for m in final["messages"])
        # Bounded against what it sends ...
        assert final_messages + _tools_tokens(schema_only) <= window
        # ... and NOT against the tools it does not send: this much history
        # beside all the tools would be over.
        assert final_messages + _tools_tokens(all_tools) > window
        # Positive control: the tool iterations before it were held to the
        # all-tools reading, so the final call kept more.
        last_tool_call = calls[n - 1]
        assert (
            sum(_msg_tokens(m) for m in last_tool_call["messages"])
            + _tools_tokens(all_tools)
            <= window
        )
        assert len(final["messages"]) > len(last_tool_call["messages"])

    async def test_every_caller_must_state_the_tools_and_the_window(self):
        """Neither has a default: a new call site that forgets one fails loudly
        instead of bounding its request with the window skipped or the tools
        uncounted."""
        engine = _make_engine()
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "B"}]
        # Positive control: stating "no tools, window unknown" is accepted.
        assert (
            engine._bound_tool_loop_messages(
                msgs, 100, "local", tools=None, window_tokens=None
            )
            is msgs
        )
        with pytest.raises(TypeError):
            engine._bound_tool_loop_messages(msgs, 100, "local", window_tokens=None)
        with pytest.raises(TypeError):
            engine._bound_tool_loop_messages(msgs, 100, "local", tools=None)

    async def test_a_request_past_the_window_is_refused_a_soft_overrun_is_not(self):
        """The window is the hard limit: a head that overflows it beside this
        call's tools raises instead of being sent. The soft cap is a target:
        a head over it is sent as main sent it."""
        from faultmaven.exceptions import ToolCallingUnsupportedError

        engine = _make_engine()
        tools = [_heavy_search_tool(4_000)]
        msgs = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "BASE " + "b" * 4_000},
        ]
        head = sum(_msg_tokens(m) for m in msgs)
        assert (
            engine._bound_tool_loop_messages(
                msgs, head - 100, "local", tools=tools, window_tokens=None
            )
            is msgs
        )
        # Positive control: without the tools the same head fits the window.
        assert (
            engine._bound_tool_loop_messages(
                msgs, 10**6, "local", tools=None, window_tokens=head + 100
            )
            is msgs
        )
        with pytest.raises(ToolCallingUnsupportedError, match=_BOUND_REFUSAL):
            engine._bound_tool_loop_messages(
                msgs, 10**6, "local", tools=tools, window_tokens=head + 100
            )

    async def test_the_token_cache_holds_only_messages_still_alive(self):
        """The cache is keyed by ``id()``. The elision marker lives only in
        the returned view, so a count cached for it outlives it — and a later
        message dict allocated at its address reads the marker's ~30 tokens
        instead of its own (measured on main: 39 of 40 such dicts). Only
        messages the history keeps alive may be cached."""
        engine = _make_engine()
        msgs = _groups_history(3)
        cache: dict = {}
        out = engine._bound_tool_loop_messages(
            msgs, 1_000, "local", cache, tools=None, window_tokens=None
        )
        # Positive control: an elision happened, so a marker was counted.
        assert any(m.get("content") == _MARKER_TEXT for m in out)
        live = {id(m) for m in msgs}
        assert {k for k in cache if isinstance(k, int)} <= live


# =========================================================================
# The base is sized to the model that receives the loop (#614 F2)
# =========================================================================

_DA_PROVIDER = "openai"  # tokenizer-backed: the bound counts in cl100k
_DA_MODEL = "tiny-window-da-model"


def _da_engine(
    window: int,
    reserve: int,
    monkeypatch,
    result_after: int = 1,
    provider_name: str = _DA_PROVIDER,
    result_chars: int = 1_500,
    da_model: str = _DA_MODEL,
):
    """An engine whose dedicated DA model has a small KNOWN window (through
    the operator override the budget resolver reads — for ``_DA_MODEL``; any
    other ``da_model`` resolves through the built-in registry), and whose DA
    provider records every call."""
    from faultmaven.utils import model_context

    monkeypatch.setattr(
        model_context,
        "_get_overrides",
        lambda: {_DA_MODEL: model_context.ModelWindow(window, reserve)},
    )
    provider, calls = _recording_provider(result_after, provider_name=provider_name)
    provider.supports_tool_calling = MagicMock(return_value=True)
    repo = MagicMock()
    repo.save = AsyncMock()
    engine = MilestoneEngine(
        llm_provider=AsyncMock(),
        repository=repo,
        investigation_tools=_registry_returning(result_chars),
        da_provider=provider,
        da_model=da_model,
    )
    return engine, provider, calls


# What each layer says when it refuses — so a test can tell WHICH refused, and
# the bound's own guard downstream cannot stand in for a broken fit upstream.
_FIT_REFUSAL = "not sending the tool loop"
_BOUND_REFUSAL = "Tool-loop head plus tools payload exceeds"


def _chat_sized_base(tokens: int) -> str:
    """A base that is ``tokens``-ish in cl100k — the size the CHAT model's
    target allowed, far more than the DA window takes."""
    line = "2026-09-24T10:00:01Z ERROR pool timeout conn=4411 host=db-7\n"
    return "BASE\n" + line * (tokens // _est(line, _DA_PROVIDER, _DA_MODEL) + 1)


def _sized_text(tag: str, tokens: int) -> str:
    return tag + " " + "word " * max(0, tokens - 8)


@pytest.mark.unit
@pytest.mark.asyncio
class TestToolLoopBaseFitsTheReceivingModel:
    """#614 F2: a dedicated DA model with a smaller window than the chat
    model's target gets a head that fits its per-call budget — the base is
    re-assembled for it — and a head that cannot fit is never sent."""

    async def test_a_smaller_da_window_gets_a_head_that_fits(self, monkeypatch):
        engine, provider, calls = _da_engine(14_000, 2_000, monkeypatch)
        caps = engine._resolve_tool_loop_budget(_DA_PROVIDER)
        assert caps.hard == 12_000 < caps.soft  # the window binds, not the soft cap
        budget = caps.hard
        base = _chat_sized_base(20_000)
        seen: list = []

        def builder(*, target_tokens, provider_name, model_name):
            seen.append((target_tokens, provider_name, model_name))
            return _sized_text("RESIZED", target_tokens - 200)

        await engine._tool_augmented_generate(
            prompt=base,
            schema_model=SampleResponse,
            investigation_tools=engine._build_da_tool_schemas(),
            tool_context=MagicMock(),
            base_prompt_builder=builder,
        )

        # Positive control: the chat-sized base alone overflows the DA budget.
        assert _est(base, _DA_PROVIDER, _DA_MODEL) > budget
        # Re-assembled once, FOR the receiving model, at the room left beside
        # the system instruction, the largest tools payload and the marker.
        [(target, sized_for, model)] = seen
        assert (sized_for, model) == (_DA_PROVIDER, _DA_MODEL)
        all_tools = calls[0]["tools"]
        system = calls[0]["messages"][0]
        assert target <= budget - _msg_tokens(
            system, _DA_PROVIDER, _DA_MODEL
        ) - _tools_tokens(all_tools, _DA_PROVIDER, _DA_MODEL)
        assert len(calls) == 2
        for c in calls:
            assert c["messages"][1]["content"].startswith("RESIZED")
            assert _sent_tokens(c, _DA_PROVIDER, _DA_MODEL) <= budget

    async def test_a_head_that_fits_is_sent_as_assembled(self, monkeypatch):
        """Positive control for the one above: no overflow, no re-assembly."""
        engine, provider, calls = _da_engine(14_000, 2_000, monkeypatch)
        builder = MagicMock()

        await engine._tool_augmented_generate(
            prompt="BASE small",
            schema_model=SampleResponse,
            investigation_tools=engine._build_da_tool_schemas(),
            tool_context=MagicMock(),
            base_prompt_builder=builder,
        )

        builder.assert_not_called()
        assert all(c["messages"][1]["content"] == "BASE small" for c in calls)

    @pytest.mark.parametrize("rebuild", ["none", "still_too_big", "raises"])
    async def test_a_head_that_cannot_fit_is_never_sent(self, monkeypatch, rebuild):
        from faultmaven.exceptions import ToolCallingUnsupportedError

        engine, provider, calls = _da_engine(14_000, 2_000, monkeypatch)

        def too_big(**_kw):
            return _chat_sized_base(20_000)

        def broken(**_kw):
            raise RuntimeError("assembly failed")

        builder = {"none": None, "still_too_big": too_big, "raises": broken}[rebuild]
        with pytest.raises(ToolCallingUnsupportedError, match=_FIT_REFUSAL):
            await engine._tool_augmented_generate(
                prompt=_chat_sized_base(20_000),
                schema_model=SampleResponse,
                investigation_tools=engine._build_da_tool_schemas(),
                tool_context=MagicMock(),
                base_prompt_builder=builder,
            )
        provider.generate.assert_not_called()

    async def test_the_fitted_head_leaves_room_for_the_elision_marker(
        self, monkeypatch
    ):
        """A base re-assembled to exactly the room left must still take the
        marker when observations are elided on a later call — the fit reserves
        it, or the bound would refuse mid-loop."""
        engine, provider, calls = _da_engine(
            14_000,
            2_000,
            monkeypatch,
            result_after=2,
            provider_name="local",  # len // 4: token counts exact by length
            result_chars=6_000,
        )

        def exact(*, target_tokens, provider_name, model_name):
            return "R" * (4 * target_tokens)  # exactly target_tokens tokens

        await engine._tool_augmented_generate(
            prompt="B" * 4 * 20_000,
            schema_model=SampleResponse,
            investigation_tools=engine._build_da_tool_schemas(),
            tool_context=MagicMock(),
            base_prompt_builder=exact,
        )

        budget = engine._resolve_tool_loop_budget("local").hard
        assert len(calls) == 3
        # Positive control: the room was filled, so observations were elided
        # and the marker had to fit in what the fit reserved for it.
        assert any(
            "elided to stay within" in str(m["content"])
            for c in calls[1:]
            for m in c["messages"]
        )
        for c in calls:
            assert _sent_tokens(c, "local", _DA_MODEL) <= budget

    async def test_the_builder_reaches_the_loop_through_the_structured_path(
        self, monkeypatch
    ):
        """The seam the call sites hand the builder to:
        _generate_structured_output → _inner → _tool_augmented_generate."""
        engine, provider, calls = _da_engine(14_000, 2_000, monkeypatch)
        seen: list = []

        def builder(*, target_tokens, provider_name, model_name):
            seen.append(target_tokens)
            return _sized_text("RESIZED", target_tokens - 200)

        result = await engine._generate_structured_output(
            _chat_sized_base(20_000),
            SampleResponse,
            investigation_tools=engine._build_da_tool_schemas(),
            tool_context=MagicMock(),
            base_prompt_builder=builder,
        )

        assert isinstance(result, SampleResponse)
        assert len(seen) == 1
        assert calls and all(
            c["messages"][1]["content"].startswith("RESIZED") for c in calls
        )

    @pytest.mark.parametrize(
        "provider_name, da_model, window",
        [
            # A tokenizer-backed provider always resolves SOME window (an
            # unlisted model falls through to its provider family), so the
            # unknown-window case needs a provider with no family fallback.
            ("openai", "gpt-4o", "large"),
            ("local", "custom-da-model", "unknown"),
        ],
        ids=["large-window-cl100k", "unknown-window-len4"],
    )
    async def test_the_soft_cap_fits_the_head_without_counting_tools(
        self, monkeypatch, provider_name, da_model, window
    ):
        """Where the window does not bind — a large known window, or none known
        — the head is fitted to the SOFT cap on messages alone, the tools not
        counted. On the cl100k receiver it binds on the TOKENIZER: a base
        within the chat target at ``len // 4`` is over the soft cap there."""
        engine, provider, calls = _da_engine(
            14_000,
            2_000,
            monkeypatch,
            provider_name=provider_name,
            da_model=da_model,
        )
        caps = engine._resolve_tool_loop_budget(provider_name)
        if window == "unknown":
            assert caps.hard is None
            base = "B" * (4 * (caps.soft + 5_000))
        else:
            assert caps.hard is not None and caps.hard > caps.soft + 20_000
            base = _chat_sized_base(caps.soft + 5_000)
        seen: list = []

        def builder(*, target_tokens, provider_name, model_name):
            seen.append(target_tokens)
            if window == "unknown":  # len // 4: exactly target_tokens
                return "RESIZED" + "R" * (4 * target_tokens - 7)
            return _sized_text("RESIZED", target_tokens)

        await engine._tool_augmented_generate(
            prompt=base,
            schema_model=SampleResponse,
            investigation_tools=engine._build_da_tool_schemas(),
            tool_context=MagicMock(),
            base_prompt_builder=builder,
        )

        system = calls[0]["messages"][0]
        marker = _est(_MARKER_TEXT, provider_name, da_model)
        # Re-assembled at the soft cap less the system instruction and marker
        # — no tools subtracted.
        assert seen == [
            caps.soft - _msg_tokens(system, provider_name, da_model) - marker
        ]
        for c in calls:
            assert c["messages"][1]["content"].startswith("RESIZED")
            assert (
                sum(_msg_tokens(m, provider_name, da_model) for m in c["messages"])
                <= caps.soft
            )
        # Positive control: the first call's messages plus its tools are over
        # the soft cap — so a fit that counted the tools would have cut deeper.
        assert _sent_tokens(calls[0], provider_name, da_model) > caps.soft

    async def test_the_dropped_base_leaves_the_token_cache(self, monkeypatch):
        """The cache is keyed by ``id()``: once the chat-sized base is replaced,
        its count must go with it, or a later message dict allocated at its
        address reads a ~20,000-token count."""
        engine, _provider, _calls = _da_engine(14_000, 2_000, monkeypatch)
        caps = engine._resolve_tool_loop_budget(_DA_PROVIDER)
        head = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": _chat_sized_base(20_000)},
        ]
        cache: dict = {}
        out = await engine._fit_tool_loop_base(
            head,
            engine._build_da_tool_schemas(),
            caps,
            _DA_PROVIDER,
            base_prompt_builder=lambda **kw: _sized_text("RESIZED", 2_000),
            token_cache=cache,
        )
        # Positive control: the base was replaced.
        assert out[1] is not head[1] and out[1]["content"].startswith("RESIZED")
        live = {id(m) for m in out}
        assert {k for k in cache if isinstance(k, int)} <= live

    async def test_the_reassembled_base_is_redacted_like_the_original(
        self, monkeypatch
    ):
        """The rebuilt text is raw case content going to a provider reached
        without the router's sanitizer, so it passes the case redaction."""
        engine, provider, calls = _da_engine(14_000, 2_000, monkeypatch)
        redaction_ctx = MagicMock()
        redaction_ctx.asanitize = AsyncMock(
            side_effect=lambda text: text.replace("alice@example.com", "[EMAIL_1]")
        )

        def builder(*, target_tokens, provider_name, model_name):
            return "RESIZED alice@example.com " + _sized_text("", target_tokens - 300)

        await engine._tool_augmented_generate(
            prompt=_chat_sized_base(20_000),
            schema_model=SampleResponse,
            investigation_tools=engine._build_da_tool_schemas(),
            tool_context=MagicMock(),
            redaction_ctx=redaction_ctx,
            base_prompt_builder=builder,
        )

        sent_base = calls[0]["messages"][1]["content"]
        assert sent_base.startswith("RESIZED [EMAIL_1]")
        assert "alice@example.com" not in sent_base

    async def test_the_real_allocator_reassembles_a_base_that_fits(self, monkeypatch):
        """Through ``get_prompt_for_case``: a heavy case assembled for the
        chat budget (32K target, ``len // 4``) is re-assembled for a DA model
        whose tokenizer and window both make it too big, and every call then
        fits — with the main template, not the minimal fallback."""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).parent))
        import test_prompt_budget_allocator as allocator_tests

        from faultmaven.core.investigation.prompts.templates import (
            get_prompt_for_case,
        )

        case = allocator_tests._pressure_case()
        chat_base = get_prompt_for_case(case, "why slow?")
        # A 26K per-call budget: under the chat-sized head, over the ~22K the
        # investigation template needs before it falls to the fallback.
        engine, provider, calls = _da_engine(30_000, 4_000, monkeypatch)
        budget = engine._resolve_tool_loop_budget(_DA_PROVIDER).hard

        def builder(**kw):
            return get_prompt_for_case(case, "why slow?", **kw)

        await engine._tool_augmented_generate(
            prompt=chat_base,
            schema_model=SampleResponse,
            investigation_tools=engine._build_da_tool_schemas(),
            tool_context=MagicMock(),
            base_prompt_builder=builder,
        )

        # Positive control: the chat-sized head does not fit beside the tools.
        fixed = _msg_tokens(
            calls[0]["messages"][0], _DA_PROVIDER, _DA_MODEL
        ) + _tools_tokens(calls[0]["tools"], _DA_PROVIDER, _DA_MODEL)
        assert _est(chat_base, _DA_PROVIDER, _DA_MODEL) + fixed > budget
        sent_base = calls[0]["messages"][1]["content"]
        assert sent_base != chat_base
        assert "INVESTIGATING" in sent_base  # main template, sections squeezed
        for c in calls:
            assert _sent_tokens(c, _DA_PROVIDER, _DA_MODEL) <= budget


def _terminal_case():
    from faultmaven.modules.case.domain.models import Case, CaseState

    case = Case(
        case_id="case_614aaaaaaaaa",
        title="t",
        user_id="u",
        enterprise_id="o",
        description="disk full on db-7",
    )
    # Past the RESOLVED validator (resolved_at etc.), as the INV-12 tests do:
    # only the state the Q&A prompt renders from matters here.
    object.__setattr__(case, "state", CaseState.RESOLVED)
    return case


@pytest.mark.unit
@pytest.mark.asyncio
class TestBothCallSitesWireTheBaseBuilder:
    """The re-assembly is only as good as its wiring: both call sites that
    put the base on the tool loop hand it a builder that assembles THIS turn's
    prompt, for the model the loop names, within the target it names."""

    @staticmethod
    def _assert_builder_sizes_for_the_receiver(builder, marker: str):
        from faultmaven.utils.token_estimation import estimate_tokens

        wide = builder(
            target_tokens=10**6,
            provider_name="anthropic",
            model_name="claude-sonnet-4-6",
        )
        # Well under, not 1 under: each render stamps the current time, whose
        # digits move the count by a token or two between renders.
        tight_target = estimate_tokens(wide, "anthropic", "claude-sonnet-4-6") - 500
        tight = builder(
            target_tokens=tight_target,
            provider_name="anthropic",
            model_name="claude-sonnet-4-6",
        )
        assert marker in wide
        # Positive control: the uncapped prompt breaks the tight target ...
        assert estimate_tokens(wide, "anthropic", "claude-sonnet-4-6") > tight_target
        # ... and the builder's result keeps to it.
        assert estimate_tokens(tight, "anthropic", "claude-sonnet-4-6") <= tight_target

    async def test_the_investigation_turn_passes_a_builder_for_its_prompt(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).parent))
        import test_toolless_turn_single_shot_routing as routing

        engine = routing._tool_engine()
        case = routing._investigating_case()
        case.evidence.append(routing._evidence())
        await engine.process_turn(case=case, user_message="df -h shows 100%")

        kwargs = engine._generate_structured_output.call_args.kwargs
        assert kwargs.get("investigation_tools") is not None
        self._assert_builder_sizes_for_the_receiver(
            kwargs["base_prompt_builder"], "df -h shows 100%"
        )

    async def test_the_terminal_qa_turn_passes_a_builder_for_its_prompt(self):
        from types import SimpleNamespace

        from faultmaven.core.investigation.schemas import TerminalResponse

        # The router's shape: no provider_name, no config — the chat names the
        # terminal prompt is assembled for resolve to (None, None).
        engine = _make_engine(
            mock_provider=SimpleNamespace(), mock_registry=_make_mock_registry()
        )
        engine._generate_structured_output = AsyncMock(
            return_value=TerminalResponse(agent_response="ok", state_updates={})
        )
        engine._remaining_regens_for = AsyncMock(return_value=0)
        engine._case_has_runbook_draft = AsyncMock(return_value=False)

        await engine._process_terminal_qa(
            _terminal_case(), "what fixed the disk?", metadata={}
        )

        kwargs = engine._generate_structured_output.call_args.kwargs
        assert kwargs.get("investigation_tools") is not None
        self._assert_builder_sizes_for_the_receiver(
            kwargs["base_prompt_builder"], "what fixed the disk?"
        )


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


@pytest.mark.unit
@pytest.mark.asyncio
class TestVectorizedFlagPersistence:
    """The Evidence.vectorized flag must be set and persisted on
    successful vectorization so proactive + reactive gates skip already
    indexed evidence on later turns. Guards against the 2026-04-21
    incident where the gate's getattr(ev, "vectorized", False) check
    always evaluated False because the field didn't exist, causing
    every turn to re-queue a proactive task for the same evidence and
    stacking concurrent BGE-M3 encodes past the 60s wait_for bound.
    """

    async def _make_engine_with_tool(
        self, tool_success: bool, tool_data={"indexed": True}
    ):
        # Default payload states `indexed`, as production's success arms do.
        # The gate is `is not True` — fail-closed — so a stand-in returning a
        # bare string or a Mock reads as "not indexed" and would silently
        # exercise the refusal path while looking like a success case.
        mock_registry = MagicMock()
        mock_registry.execute_tool = AsyncMock(
            return_value=ToolResult(success=tool_success, data=tool_data)
        )
        provider = AsyncMock()
        repo = MagicMock()
        # Scoped update replaces aggregate save(case) so a stale snapshot
        # can't truncate concurrent writes on sibling tables.
        repo.save = AsyncMock()
        repo.update_evidence_vectorized = AsyncMock(return_value=True)
        engine = MilestoneEngine(
            llm_provider=provider,
            repository=repo,
            investigation_tools=mock_registry,
        )
        return engine, repo

    def _make_ctx_with_evidence(self, evidence_id: str):
        ev = MagicMock()
        ev.evidence_id = evidence_id
        ev.vectorized = False
        case = MagicMock()
        case.evidence = [ev]
        ctx = MagicMock()
        ctx.in_memory_case = case
        ctx.case_id = "case_test"
        return ctx, case, ev

    async def test_success_sets_and_persists_vectorized_flag(self):
        engine, repo = await self._make_engine_with_tool(tool_success=True)
        ctx, case, ev = self._make_ctx_with_evidence("ev_abc")

        result = await engine._vectorize_evidence("ev_abc", ctx)

        assert result is True
        assert ev.vectorized is True, (
            "Successful vectorization must flip Evidence.vectorized on the "
            "in-memory snapshot so the current turn's gate sees the flip."
        )
        # Persistence must go through the scoped single-row UPDATE, NOT the
        # aggregate save(case) path. An aggregate save from this
        # fire-and-forget task would silently truncate messages and other
        # sibling rows that concurrent turns have written while the BGE-M3
        # encode was in flight.
        repo.update_evidence_vectorized.assert_awaited_once_with(
            "case_test", "ev_abc", True
        )
        repo.save.assert_not_called()

    async def test_failure_leaves_flag_false_and_does_not_persist(self):
        engine, repo = await self._make_engine_with_tool(tool_success=False)
        ctx, _, ev = self._make_ctx_with_evidence("ev_xyz")

        result = await engine._vectorize_evidence("ev_xyz", ctx)

        assert result is False
        assert ev.vectorized is False, (
            "A failed vectorize must not mark the evidence vectorized; "
            "the next turn should retry."
        )
        repo.update_evidence_vectorized.assert_not_called()
        repo.save.assert_not_called()

    async def test_a_success_that_indexed_nothing_does_not_count_as_indexed(self):
        """``success=True`` is not "the file is in the collection".

        ``vectorize_file`` reports a file with no chunkable content as a
        success — the operation completed and established a fact about the
        file — but nothing was written. This boolean is the only thing the
        callers read: True both flips the persistent ``vectorized`` flag and
        emits ``_VECTORIZED_SYSTEM_MESSAGE``, which tells the model the file is
        searchable via ``case_evidence_search``. The model then searches, gets
        nothing back, and reads it as "this file does not contain that" — an
        index that was never written laundered into a finding about the
        evidence (#941).

        Asserting on the tool's own ToolResult would not catch this: its
        message already says the file is not searchable, and no caller here
        renders it.
        """
        engine, repo = await self._make_engine_with_tool(
            tool_success=True,
            tool_data={"evidence_id": "ev_empty", "indexed": False},
        )
        ctx, _, ev = self._make_ctx_with_evidence("ev_empty")

        result = await engine._vectorize_evidence("ev_empty", ctx)

        assert result is False, (
            "an empty index was reported to the caller as a completed one, "
            "which is what emits the 'indexed for semantic search' advisory"
        )
        assert ev.vectorized is False, (
            "the vectorized flag asserts membership of the case collection; "
            "a file that indexed nothing is not in it"
        )
        repo.update_evidence_vectorized.assert_not_called()

    @pytest.mark.parametrize(
        "tool_data",
        [{"evidence_id": "ev_x"}, "ok", None, MagicMock()],
        ids=["key_missing", "string_payload", "none_payload", "mock_payload"],
    )
    async def test_a_success_that_does_not_state_indexed_is_refused(self, tool_data):
        """Fail CLOSED.

        The gate is `is not True`, not `is False`. An unstated key and an
        unrecognisable payload both mean "this caller did not tell us the file
        is indexed", and the safe reading of that is that it is not — otherwise
        the guard depends on every future producer remembering to set a key,
        with a false claim to the model as the penalty for forgetting.
        """
        engine, repo = await self._make_engine_with_tool(
            tool_success=True, tool_data=tool_data
        )
        ctx, _, ev = self._make_ctx_with_evidence("ev_x")

        assert await engine._vectorize_evidence("ev_x", ctx) is False
        assert ev.vectorized is False
        repo.update_evidence_vectorized.assert_not_called()

    async def test_an_actual_index_is_still_reported_as_indexed(self):
        """The gate can PASS. Rejecting every success would silently disable
        vectorization instead of making it honest."""
        engine, repo = await self._make_engine_with_tool(
            tool_success=True,
            tool_data={"evidence_id": "ev_real", "indexed": True},
        )
        ctx, _, ev = self._make_ctx_with_evidence("ev_real")

        result = await engine._vectorize_evidence("ev_real", ctx)

        assert result is True
        assert ev.vectorized is True
        repo.update_evidence_vectorized.assert_awaited_once_with(
            "case_test", "ev_real", True
        )

    async def test_vectorized_evidence_skips_proactive(self):
        """Proactive task should not be created for evidence that is
        already marked vectorized (the persistent-flag gate)."""

        # Post-010: vectorization size lives on uploaded_files.size_bytes.
        # Wire each evidence row to a backing UploadedFile of the right size
        # so the engine's FK traversal returns a real file_meta.
        class _Ev:
            def __init__(self, ev_id, vectorized, file_id):
                self.evidence_id = ev_id
                self.source_file_id = file_id
                self.vectorized = vectorized

        class _File:
            def __init__(self, file_id, sz):
                self.file_id = file_id
                self.size_bytes = sz

        engine, _ = await self._make_engine_with_tool(tool_success=True)
        # _vectorize_evidence shouldn't be called at all when the flag is True
        engine._vectorize_evidence = AsyncMock(return_value=True)

        # Size well above the default min threshold so the size gate
        # isn't what's suppressing the task.
        big = 1_000_000
        files = {
            "file_already": _File("file_already", big),
            "file_new": _File("file_new", big),
        }
        case = MagicMock()
        case.evidence = [
            _Ev("ev_already", vectorized=True, file_id="file_already"),
            _Ev("ev_new", vectorized=False, file_id="file_new"),
        ]
        case.find_uploaded_file = MagicMock(side_effect=lambda fid: files.get(fid))

        tasks = await engine._start_proactive_vectorization(case, MagicMock())

        assert (
            "ev_already" not in tasks
        ), "Gate must skip evidence where vectorized=True"
        assert (
            "ev_new" in tasks
        ), "Gate must still enqueue evidence where vectorized=False"


@pytest.mark.unit
@pytest.mark.asyncio
class TestInflightVectorizeDedup:
    """Cross-turn deduplication of proactive vectorization tasks.

    The persistent Evidence.vectorized flag flips only on completion,
    so it doesn't help a turn whose predecessor is still running.
    Without an in-flight registry, turn N+1 sees vectorized=False and
    starts a second concurrent encode that contends for CPU with turn
    N's task — the stacking pattern that drove every task past the
    60s wait_for bound on 2026-04-21.

    MilestoneEngine is a DI singleton, so self._inflight_vectorize
    survives across turns and lets turn N+1 reuse turn N's task.
    """

    def _make_engine(self):
        provider = AsyncMock()
        repo = MagicMock()
        repo.save = AsyncMock()
        mock_registry = MagicMock()
        engine = MilestoneEngine(
            llm_provider=provider,
            repository=repo,
            investigation_tools=mock_registry,
        )
        assert engine._inflight_vectorize == {}
        return engine

    @staticmethod
    def _case_with(evidence_id: str, vectorized: bool, size: int = 1_000_000):
        """Build a case with one evidence row backed by an uploaded file of
        the requested ``size``. Post-010: vectorization size lives on
        ``uploaded_files.size_bytes`` (Evidence carries no size field), so
        the test gives the engine a real file_meta to read from."""

        class _Ev:
            def __init__(self, ev_id, vec, file_id):
                self.evidence_id = ev_id
                self.source_file_id = file_id
                self.vectorized = vec

        class _File:
            def __init__(self, file_id, sz):
                self.file_id = file_id
                self.size_bytes = sz

        file_id = "file_for_" + evidence_id.replace("-", "_")
        file_meta = _File(file_id, size)
        case = MagicMock()
        case.evidence = [_Ev(evidence_id, vectorized, file_id)]
        case.find_uploaded_file = MagicMock(
            side_effect=lambda fid: file_meta if fid == file_id else None
        )
        return case

    async def test_second_call_reuses_inflight_task(self):
        """Second call with the same evidence_id must reuse the task
        from the first call — creating a second concurrent task is the
        stacking bug this guards against."""
        import asyncio

        engine = self._make_engine()
        gate = asyncio.Event()

        async def _never_finishes(_ev_id, _ctx):
            await gate.wait()
            return True

        engine._vectorize_evidence = _never_finishes

        case = self._case_with("ev_pending", vectorized=False)
        tasks_turn_1 = await engine._start_proactive_vectorization(case, MagicMock())
        tasks_turn_2 = await engine._start_proactive_vectorization(case, MagicMock())

        try:
            assert "ev_pending" in tasks_turn_1
            assert "ev_pending" in tasks_turn_2
            assert (
                tasks_turn_2["ev_pending"] is tasks_turn_1["ev_pending"]
            ), "Second turn must reuse the first turn's task"
            assert len(engine._inflight_vectorize) == 1
        finally:
            gate.set()
            await tasks_turn_1["ev_pending"]

    async def test_registry_cleaned_up_on_completion(self):
        """After the task settles, the registry entry must be removed
        so a later turn can retry cleanly if persistence didn't land."""
        import asyncio

        engine = self._make_engine()
        engine._vectorize_evidence = AsyncMock(return_value=True)

        case = self._case_with("ev_x", vectorized=False)
        tasks = await engine._start_proactive_vectorization(case, MagicMock())
        await tasks["ev_x"]
        # done_callback runs on the event loop; yield so it fires.
        await asyncio.sleep(0)

        assert (
            "ev_x" not in engine._inflight_vectorize
        ), "Registry must be cleaned up after task settles"


@pytest.mark.unit
@pytest.mark.asyncio
class TestReactiveVectorizeTimeout:
    """Reactive vectorization must respect
    AgentSettings.vectorization_reactive_timeout_seconds and fall
    through (no advisory appended) on timeout, leaving the agent to
    continue with whatever evidence it already has. Guards the
    proactive-vs-reactive time-bound split: proactive runs unbounded
    as a background task, reactive bounds synchronously inside the
    tool loop where it's blocking the agent.
    """

    async def test_reactive_times_out_without_appending_advisory(self, monkeypatch):
        import asyncio

        from faultmaven.config.settings import get_settings

        provider = AsyncMock()
        repo = MagicMock()
        repo.save = AsyncMock()
        mock_registry = MagicMock()
        engine = MilestoneEngine(
            llm_provider=provider,
            repository=repo,
            investigation_tools=mock_registry,
        )

        # Simulate an encode that runs far longer than the reactive
        # bound. _vectorize_evidence itself no longer wraps wait_for;
        # the bound lives at the _reactive_vectorize caller.
        async def _slow(_ev_id, _ctx):
            await asyncio.sleep(10)
            return True

        engine._vectorize_evidence = _slow

        # Force a very small reactive timeout so the test is fast.
        settings = get_settings()
        monkeypatch.setattr(
            settings.agent,
            "vectorization_reactive_timeout_seconds",
            1,
        )

        # Evidence large enough to pass the size gate. Post-010 the gate reads
        # uploaded_files.size_bytes via the source_file_id FK, so the backing
        # file must exist: with find_uploaded_file returning None the size
        # resolves to 0 and _reactive_vectorize returns before it ever reaches
        # the wait_for — leaving this test asserting "no advisory" against a
        # path that never ran, which it cannot fail.
        ev = MagicMock()
        ev.evidence_id = "ev_slow"
        ev.source_file_id = "file_slow"
        ev.vectorized = False
        f = MagicMock()
        f.file_id = "file_slow"
        f.size_bytes = 1_000_000
        case = MagicMock()
        case.evidence = [ev]
        case.find_uploaded_file = MagicMock(return_value=f)
        ctx = MagicMock()
        ctx.in_memory_case = case
        ctx.case_id = "case_test"
        ctx.case_repository = None

        result_text = await engine._reactive_vectorize(
            "ev_slow", ctx, "before", "low_confidence"
        )

        assert result_text == "before", (
            "On reactive timeout, the [SYSTEM] advisory must NOT be "
            "appended — the agent continues without claiming a "
            "vectorize happened."
        )


@pytest.mark.unit
@pytest.mark.asyncio
class TestAdvisoryIsNotEmittedForAnIndexThatWasNeverWritten:
    """The [SYSTEM] advisory is the surface — assert THERE (#941).

    ``_vectorize_evidence``'s bool is an intermediate value. What reaches the
    investigating model is ``_VECTORIZED_SYSTEM_MESSAGE``: "This file has been
    automatically indexed for semantic search. Use case_evidence_search…". A
    file that indexed nothing is not in the collection, so that advisory sends
    the model to a search that must come back empty — which it then reads as a
    statement about the file's contents.

    These drive the two emission sites through the REAL ``_vectorize_evidence``
    with a tool that reports a completed run which wrote nothing. Tests that
    asserted only on the returned bool passed while all four emission sites
    ignored it entirely.
    """

    def _engine(self, tool_data):
        registry = MagicMock()
        registry.execute_tool = AsyncMock(
            return_value=ToolResult(success=True, data=tool_data)
        )
        repo = MagicMock()
        repo.save = AsyncMock()
        repo.update_evidence_vectorized = AsyncMock(return_value=True)
        return MilestoneEngine(
            llm_provider=AsyncMock(),
            repository=repo,
            investigation_tools=registry,
        )

    def _ctx(self):
        ev = MagicMock()
        ev.evidence_id = "ev_1"
        ev.source_file_id = "file_1"
        ev.vectorized = False
        # Post-010 the size gate reads uploaded_files.size_bytes via the FK.
        # With no backing file the size resolves to 0 and _reactive_vectorize
        # returns BEFORE it ever vectorizes — which would make an
        # "advisory absent" assertion pass without exercising anything.
        f = MagicMock()
        f.file_id = "file_1"
        f.size_bytes = 1_000_000
        case = MagicMock()
        case.evidence = [ev]
        case.find_uploaded_file = MagicMock(return_value=f)
        ctx = MagicMock()
        ctx.in_memory_case = case
        ctx.case_id = "case_test"
        ctx.case_repository = None
        return ctx, case

    @pytest.mark.parametrize(
        "indexed,expect_advisory",
        [(True, True), (False, False)],
        ids=["indexed", "indexed_nothing"],
    )
    async def test_reactive_path(self, indexed, expect_advisory):
        engine = self._engine({"evidence_id": "ev_1", "indexed": indexed})
        ctx, _ = self._ctx()

        result_text = await engine._reactive_vectorize(
            "ev_1", ctx, "before", "low_confidence"
        )

        emitted = engine._VECTORIZED_SYSTEM_MESSAGE in result_text
        assert emitted is expect_advisory, (
            f"indexed={indexed} emitted the 'indexed for semantic search' "
            f"advisory={emitted}"
        )

    @pytest.mark.parametrize(
        "indexed,expect_advisory",
        [(True, True), (False, False)],
        ids=["indexed", "indexed_nothing"],
    )
    async def test_proactive_path(self, indexed, expect_advisory):
        import asyncio

        engine = self._engine({"evidence_id": "ev_1", "indexed": indexed})
        ctx, case = self._ctx()

        task = asyncio.create_task(engine._vectorize_evidence("ev_1", ctx))
        await task

        result_text = await engine._track_da_result(
            func_name="search_file",
            evidence_id="ev_1",
            tool_result=ToolResult(success=True, data="{}"),
            result_text="before",
            case=case,
            tool_context=ctx,
            da_empty_search_counts={},
            proactive_tasks={"ev_1": task},
        )

        emitted = engine._VECTORIZED_SYSTEM_MESSAGE in result_text
        assert (
            emitted is expect_advisory
        ), f"indexed={indexed} emitted the advisory={emitted}"


# =========================================================================
# Truncation on the tool-augmented path (#1094)
# =========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestToolLoopTruncationLadder:
    """The gap #513's ladder left open.

    ``_generate_structured_output_inner`` has raised the cap on a cut body
    since #513. This path never did: ``max_tokens`` was a fixed 8000 and the
    schema-tool arguments went straight into ``_parse_schema_tool_call``, whose
    repair machinery — nested-JSON parsing, the ``state_updates`` string→``{}``
    coercion, validation degradation — is quite capable of turning a truncated
    payload into a structurally valid response. Those state updates are then
    APPLIED to the case. A cut becomes a claim.
    """

    def _tools(self):
        return [
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

    async def test_a_truncated_call_is_retried_at_a_doubled_cap(self):
        from faultmaven.core.investigation.milestone_engine import (
            STRUCTURED_OUTPUT_MAX_TOKENS,
        )
        from faultmaven.infrastructure.llm.providers import StopReason

        cut = _make_schema_response({"agent_response": "partial"})
        cut.stop_reason = StopReason.MAX_TOKENS
        whole = _make_schema_response(
            {"agent_response": "complete", "next_action": "continue"}
        )

        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(side_effect=[cut, whole])
        engine = _make_engine(
            mock_provider=mock_provider, mock_registry=_make_mock_registry()
        )

        result = await engine._tool_augmented_generate(
            prompt="Investigate",
            schema_model=SampleResponse,
            investigation_tools=self._tools(),
            tool_context=MagicMock(),
        )

        assert result.agent_response == "complete"
        caps = [c.kwargs["max_tokens"] for c in mock_provider.generate.await_args_list]
        assert caps == [
            STRUCTURED_OUTPUT_MAX_TOKENS,
            STRUCTURED_OUTPUT_MAX_TOKENS * 2,
        ]

    async def test_a_complete_call_is_not_retried(self):
        """Negative control — the loop must not double every call."""
        whole = _make_schema_response({"agent_response": "complete"})

        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(return_value=whole)
        engine = _make_engine(
            mock_provider=mock_provider, mock_registry=_make_mock_registry()
        )

        await engine._tool_augmented_generate(
            prompt="Investigate",
            schema_model=SampleResponse,
            investigation_tools=self._tools(),
            tool_context=MagicMock(),
        )

        assert mock_provider.generate.await_count == 1

    async def test_the_retry_is_metered_too(self):
        """Both attempts are real billed calls when a dedicated DA provider is used.

        Metering sits inside the retry so DA-turn spend does not under-report
        on exactly the turns that cost the most.
        """
        from faultmaven.infrastructure.llm.providers import StopReason

        cut = _make_schema_response({"agent_response": "partial"})
        cut.stop_reason = StopReason.MAX_TOKENS
        whole = _make_schema_response({"agent_response": "complete"})

        da_provider = AsyncMock()
        da_provider.provider_name = "openai"
        da_provider.generate = AsyncMock(side_effect=[cut, whole])
        engine = _make_engine(
            mock_registry=_make_mock_registry(), da_provider=da_provider
        )

        with patch(
            "faultmaven.core.investigation.milestone_engine.record_provider_call"
        ) as record:
            await engine._tool_augmented_generate(
                prompt="Investigate",
                schema_model=SampleResponse,
                investigation_tools=self._tools(),
                tool_context=MagicMock(),
            )

        assert record.call_count == 2
