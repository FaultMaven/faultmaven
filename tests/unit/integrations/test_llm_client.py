"""Unit Tests for LLM Client (TASK-015)

This module tests the LLMClient wrapper which provides a unified
interface for Anthropic and OpenAI LLM providers.

Design Reference: docs/architecture/TASK-015-agent-orchestration-design.md
"""

import os
from typing import Any, AsyncGenerator, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from faultmaven.integrations.llm_client import (
    LLMClient,
    LLMProvider,
    AnthropicClient,
    OpenAIClient,
    BaseLLMClient,
    create_llm_client,
)
from faultmaven.domain.events import (
    LLMEvent,
    LLMEventType,
    Message,
    MessageRole,
    Tool,
    ToolCall,
)
from faultmaven.exceptions import LLMException


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_messages():
    """Create sample conversation messages."""
    return [
        Message.user("What is causing the error?"),
        Message.assistant("I'll analyze the logs."),
        Message.user("Please check the error.log file."),
    ]


@pytest.fixture
def sample_tools():
    """Create sample tools for function calling."""
    return [
        Tool(
            name="read_file",
            description="Read a file",
            parameters={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                },
                "required": ["file_id"],
            },
        ),
        Tool(
            name="list_evidence",
            description="List evidence",
            parameters={"type": "object", "properties": {}},
        ),
    ]


# =============================================================================
# Test: LLMProvider Enum
# =============================================================================


class TestLLMProvider:
    """Tests for LLMProvider enum."""

    def test_anthropic_provider(self):
        """Test Anthropic provider value."""
        assert LLMProvider.ANTHROPIC.value == "anthropic"

    def test_openai_provider(self):
        """Test OpenAI provider value."""
        assert LLMProvider.OPENAI.value == "openai"

    def test_provider_from_string(self):
        """Test creating provider from string."""
        assert LLMProvider("anthropic") == LLMProvider.ANTHROPIC
        assert LLMProvider("openai") == LLMProvider.OPENAI


# =============================================================================
# Test: BaseLLMClient
# =============================================================================


class TestBaseLLMClient:
    """Tests for BaseLLMClient abstract class."""

    def test_base_client_is_abstract(self):
        """Test that BaseLLMClient cannot be instantiated."""
        with pytest.raises(TypeError):
            BaseLLMClient()


# =============================================================================
# Test: AnthropicClient
# =============================================================================


class TestAnthropicClient:
    """Tests for AnthropicClient."""

    def test_initialization(self):
        """Test client initialization."""
        client = AnthropicClient(
            api_key="test-key",
            model="claude-3-5-sonnet-20241022",
            timeout=60,
        )

        assert client.api_key == "test-key"
        assert client.model == "claude-3-5-sonnet-20241022"
        assert client.timeout == 60

    def test_initialization_with_defaults(self):
        """Test client initialization with defaults."""
        client = AnthropicClient(api_key="test-key")

        assert client.model == "claude-sonnet-4-20250514"
        assert client.timeout == 120

    def test_get_model_info(self):
        """Test get_model_info returns correct info."""
        client = AnthropicClient(api_key="test-key", model="claude-3-opus")

        info = client.get_model_info()

        assert info["provider"] == "anthropic"
        assert info["model"] == "claude-3-opus"
        assert info["timeout"] == 120

    @pytest.mark.asyncio
    async def test_count_tokens_estimate(self):
        """Test token count estimation."""
        client = AnthropicClient(api_key="test-key")

        messages = [
            Message.user("Hello, how are you?"),
            Message.assistant("I'm doing well, thank you for asking!"),
        ]

        count = await client.count_tokens(messages)

        # Should return a reasonable estimate (chars / 4)
        assert count > 0
        assert count < 100

    def test_convert_messages_user(self):
        """Test converting user messages."""
        client = AnthropicClient(api_key="test-key")

        messages = [Message.user("Hello")]
        converted = client._convert_messages(messages)

        assert len(converted) == 1
        assert converted[0]["role"] == "user"
        assert converted[0]["content"] == "Hello"

    def test_convert_messages_assistant(self):
        """Test converting assistant messages."""
        client = AnthropicClient(api_key="test-key")

        messages = [Message.assistant("Hi there!")]
        converted = client._convert_messages(messages)

        assert len(converted) == 1
        assert converted[0]["role"] == "assistant"
        assert converted[0]["content"] == "Hi there!"

    def test_convert_messages_skips_system(self):
        """Test that system messages are skipped (handled separately)."""
        client = AnthropicClient(api_key="test-key")

        messages = [Message.system("You are helpful.")]
        converted = client._convert_messages(messages)

        assert len(converted) == 0

    def test_convert_messages_tool_result(self):
        """Test converting tool result messages."""
        client = AnthropicClient(api_key="test-key")

        messages = [Message.tool("File contents here", "tc_123", "read_file")]
        converted = client._convert_messages(messages)

        assert len(converted) == 1
        assert converted[0]["role"] == "user"
        assert converted[0]["content"][0]["type"] == "tool_result"
        assert converted[0]["content"][0]["tool_use_id"] == "tc_123"

    def test_convert_tool(self, sample_tools):
        """Test converting tool to Anthropic format."""
        client = AnthropicClient(api_key="test-key")

        converted = client._convert_tool(sample_tools[0])

        assert converted["name"] == "read_file"
        assert converted["description"] == "Read a file"
        assert "input_schema" in converted


# =============================================================================
# Test: OpenAIClient
# =============================================================================


class TestOpenAIClient:
    """Tests for OpenAIClient."""

    def test_initialization(self):
        """Test client initialization."""
        client = OpenAIClient(
            api_key="test-key",
            model="gpt-4-turbo",
            timeout=90,
        )

        assert client.api_key == "test-key"
        assert client.model == "gpt-4-turbo"
        assert client.timeout == 90

    def test_initialization_with_defaults(self):
        """Test client initialization with defaults."""
        client = OpenAIClient(api_key="test-key")

        assert client.model == "gpt-4o"
        assert client.timeout == 120

    def test_get_model_info(self):
        """Test get_model_info returns correct info."""
        client = OpenAIClient(api_key="test-key", model="gpt-4-turbo")

        info = client.get_model_info()

        assert info["provider"] == "openai"
        assert info["model"] == "gpt-4-turbo"
        assert info["timeout"] == 120

    @pytest.mark.asyncio
    async def test_count_tokens_fallback(self):
        """Test token count with fallback heuristic."""
        client = OpenAIClient(api_key="test-key")

        messages = [
            Message.user("Hello, how are you?"),
        ]

        # This uses fallback since tiktoken may not be installed
        count = await client.count_tokens(messages)

        assert count > 0

    def test_convert_messages_with_system(self, sample_messages):
        """Test converting messages with system prompt."""
        client = OpenAIClient(api_key="test-key")

        converted = client._convert_messages(sample_messages, system="You are helpful.")

        assert converted[0]["role"] == "system"
        assert converted[0]["content"] == "You are helpful."
        assert len(converted) == 4  # 1 system + 3 messages

    def test_convert_messages_tool_calls(self):
        """Test converting assistant message with tool calls."""
        client = OpenAIClient(api_key="test-key")

        tool_call = ToolCall(id="tc_1", name="read_file", arguments={"file_id": "f1"})
        messages = [Message.assistant("Let me read that.", tool_calls=[tool_call])]

        converted = client._convert_messages(messages)

        assert len(converted) == 1
        assert "tool_calls" in converted[0]
        assert converted[0]["tool_calls"][0]["id"] == "tc_1"
        assert converted[0]["tool_calls"][0]["function"]["name"] == "read_file"

    def test_convert_tool(self, sample_tools):
        """Test converting tool to OpenAI format."""
        client = OpenAIClient(api_key="test-key")

        converted = client._convert_tool(sample_tools[0])

        assert converted["type"] == "function"
        assert converted["function"]["name"] == "read_file"
        assert "parameters" in converted["function"]


# =============================================================================
# Test: LLMClient Unified Interface
# =============================================================================


class TestLLMClient:
    """Tests for LLMClient unified interface."""

    def test_initialization_anthropic(self):
        """Test initialization with Anthropic provider."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            client = LLMClient(provider=LLMProvider.ANTHROPIC)

            assert client.provider == LLMProvider.ANTHROPIC
            assert "claude" in client.model.lower()

    def test_initialization_openai(self):
        """Test initialization with OpenAI provider."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            client = LLMClient(provider=LLMProvider.OPENAI)

            assert client.provider == LLMProvider.OPENAI
            assert "gpt" in client.model.lower()

    def test_initialization_from_string(self):
        """Test initialization with string provider."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            client = LLMClient(provider="anthropic")

            assert client.provider == LLMProvider.ANTHROPIC

    def test_initialization_custom_model(self):
        """Test initialization with custom model."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            client = LLMClient(
                provider=LLMProvider.ANTHROPIC,
                model="claude-3-opus-20240229",
            )

            assert client.model == "claude-3-opus-20240229"

    def test_initialization_custom_api_key(self):
        """Test initialization with custom API key."""
        client = LLMClient(
            provider=LLMProvider.ANTHROPIC,
            api_key="custom-key",
        )

        assert client._client.api_key == "custom-key"

    def test_initialization_unsupported_provider_raises(self):
        """Test that unsupported provider raises ValueError."""
        with pytest.raises(ValueError):
            LLMClient(provider="unsupported")

    def test_initialization_missing_api_key_raises(self):
        """Test that missing API key raises LLMException."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(LLMException):
                LLMClient(provider=LLMProvider.ANTHROPIC)

    def test_get_model_info(self):
        """Test get_model_info delegates to client."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            client = LLMClient(provider=LLMProvider.ANTHROPIC)

            info = client.get_model_info()

            assert "provider" in info
            assert "model" in info

    @pytest.mark.asyncio
    async def test_count_tokens(self):
        """Test count_tokens delegates to client."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            client = LLMClient(provider=LLMProvider.ANTHROPIC)

            messages = [Message.user("Test")]
            count = await client.count_tokens(messages)

            assert count >= 0

    @pytest.mark.asyncio
    async def test_complete_aggregates_stream(self, sample_messages):
        """Test complete method aggregates streaming response."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            client = LLMClient(provider=LLMProvider.ANTHROPIC)

            # Mock the stream_completion method
            async def mock_stream(*args, **kwargs):
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Hello ")
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="World!")
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="",
                    metadata={"input_tokens": 10, "output_tokens": 5},
                )

            client._client.stream_completion = mock_stream

            result = await client.complete(sample_messages)

            assert result["content"] == "Hello World!"
            assert result["usage"]["input_tokens"] == 10
            assert result["usage"]["output_tokens"] == 5

    @pytest.mark.asyncio
    async def test_complete_collects_tool_calls(self, sample_messages):
        """Test complete method collects tool calls."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            client = LLMClient(provider=LLMProvider.ANTHROPIC)

            tool_call = ToolCall(id="tc_1", name="read_file", arguments={"file_id": "f1"})

            async def mock_stream(*args, **kwargs):
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Let me read that.")
                yield LLMEvent(event_type=LLMEventType.TOOL_USE, content=tool_call)
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="",
                    metadata={"input_tokens": 10, "output_tokens": 5},
                )

            client._client.stream_completion = mock_stream

            result = await client.complete(sample_messages)

            assert len(result["tool_calls"]) == 1
            assert result["tool_calls"][0].name == "read_file"

    @pytest.mark.asyncio
    async def test_complete_raises_on_error(self, sample_messages):
        """Test complete method raises on LLM error."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            client = LLMClient(provider=LLMProvider.ANTHROPIC)

            async def mock_stream(*args, **kwargs):
                yield LLMEvent(
                    event_type=LLMEventType.ERROR,
                    content="API Error: Rate limit exceeded",
                )

            client._client.stream_completion = mock_stream

            with pytest.raises(LLMException):
                await client.complete(sample_messages)


# =============================================================================
# Test: Factory Function
# =============================================================================


class TestCreateLLMClient:
    """Tests for create_llm_client factory function."""

    def test_create_with_defaults(self):
        """Test creating client with environment defaults."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            client = create_llm_client()

            assert client.provider == LLMProvider.ANTHROPIC

    def test_create_with_provider_env(self):
        """Test creating client with LLM_PROVIDER env var."""
        with patch.dict(os.environ, {
            "LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "test-key",
        }):
            client = create_llm_client()

            assert client.provider == LLMProvider.OPENAI

    def test_create_with_explicit_provider(self):
        """Test creating client with explicit provider."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            client = create_llm_client(provider="openai")

            assert client.provider == LLMProvider.OPENAI

    def test_create_with_custom_model(self):
        """Test creating client with custom model."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            client = create_llm_client(model="claude-3-opus")

            assert client.model == "claude-3-opus"

    def test_create_with_custom_timeout(self):
        """Test creating client with custom timeout."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            client = create_llm_client(timeout=60)

            assert client._client.timeout == 60


# =============================================================================
# Test: Event Types
# =============================================================================


class TestLLMEvents:
    """Tests for LLM event handling."""

    @pytest.mark.asyncio
    async def test_stream_yields_text_chunks(self):
        """Test that streaming yields TEXT_CHUNK events."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            client = LLMClient(provider=LLMProvider.ANTHROPIC)

            async def mock_stream(*args, **kwargs):
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Hello")
                yield LLMEvent(event_type=LLMEventType.COMPLETION, content="", metadata={})

            client._client.stream_completion = mock_stream

            events = []
            async for event in client.stream_completion([Message.user("Hi")]):
                events.append(event)

            text_chunks = [e for e in events if e.event_type == LLMEventType.TEXT_CHUNK]
            assert len(text_chunks) >= 1

    @pytest.mark.asyncio
    async def test_stream_yields_tool_use_events(self):
        """Test that streaming yields TOOL_USE events."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            client = LLMClient(provider=LLMProvider.ANTHROPIC)

            tool_call = ToolCall(id="tc_1", name="read_file", arguments={})

            async def mock_stream(*args, **kwargs):
                yield LLMEvent(event_type=LLMEventType.TOOL_USE, content=tool_call)
                yield LLMEvent(event_type=LLMEventType.COMPLETION, content="", metadata={})

            client._client.stream_completion = mock_stream

            events = []
            async for event in client.stream_completion([Message.user("Hi")]):
                events.append(event)

            tool_events = [e for e in events if e.event_type == LLMEventType.TOOL_USE]
            assert len(tool_events) == 1

    @pytest.mark.asyncio
    async def test_stream_yields_completion_event(self):
        """Test that streaming yields COMPLETION event at end."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            client = LLMClient(provider=LLMProvider.ANTHROPIC)

            async def mock_stream(*args, **kwargs):
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Done")
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="Done",
                    metadata={"input_tokens": 5, "output_tokens": 1},
                )

            client._client.stream_completion = mock_stream

            events = []
            async for event in client.stream_completion([Message.user("Hi")]):
                events.append(event)

            # Last event should be completion
            completion_events = [e for e in events if e.event_type == LLMEventType.COMPLETION]
            assert len(completion_events) == 1
            assert "input_tokens" in completion_events[0].metadata
