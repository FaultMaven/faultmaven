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

    def test_creates_tool_context_with_case_fields(self):
        """Verifies ToolContext created with correct case fields.

        ToolContext carries ``case_repository``; tools read evidence from
        ``case.evidence`` directly.
        """
        engine = _make_engine()

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

    async def _make_engine_with_tool(self, tool_success: bool):
        mock_registry = MagicMock()
        mock_registry.execute_tool = AsyncMock(
            return_value=ToolResult(success=tool_success, data="ok")
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

        # Evidence large enough to pass the size gate. Post-redesign:
        # content_size_bytes was dropped — size now comes from the extract
        # length when source_file_id is unset (inline-evidence path).
        ev = MagicMock()
        ev.evidence_id = "ev_slow"
        ev.extract = "x" * 1_000_000
        ev.source_file_id = None
        ev.vectorized = False
        case = MagicMock()
        case.evidence = [ev]
        case.find_uploaded_file = MagicMock(return_value=None)
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
