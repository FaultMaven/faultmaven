"""
Tests for Anthropic provider message conversion and multi-turn support.

Validates _convert_messages_to_anthropic() converts OpenAI-format messages
to Anthropic API format, and that generate() correctly uses the messages kwarg.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.providers.anthropic import AnthropicProvider
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ProviderConfig


@pytest.fixture
def anthropic_config():
    """Minimal Anthropic provider config for unit tests."""
    return ProviderConfig(
        name="anthropic",
        api_key="test-key",
        base_url="https://api.anthropic.com/v1",
        models=["claude-3-sonnet-20240229"],
        default_model="claude-3-sonnet-20240229",
        timeout=30,
        confidence_score=0.9,
    )


@pytest.fixture
def provider(anthropic_config):
    """Create an AnthropicProvider instance for testing."""
    return AnthropicProvider(anthropic_config)


# =========================================================================
# _convert_messages_to_anthropic tests
# =========================================================================


@pytest.mark.unit
class TestConvertMessagesToAnthropic:
    """Tests for _convert_messages_to_anthropic()."""

    def test_system_messages_extracted_to_top_level(self, provider):
        """System messages should be extracted to a top-level 'system' field."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]

        result = provider._convert_messages_to_anthropic(messages)

        assert result["system"] == "You are a helpful assistant."
        assert len(result["messages"]) == 1
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][0]["content"] == "Hello"

    def test_multiple_system_messages_joined(self, provider):
        """Multiple system messages should be joined with double newlines."""
        messages = [
            {"role": "system", "content": "Rule 1: Be helpful."},
            {"role": "system", "content": "Rule 2: Be concise."},
            {"role": "user", "content": "Hello"},
        ]

        result = provider._convert_messages_to_anthropic(messages)

        assert result["system"] == "Rule 1: Be helpful.\n\nRule 2: Be concise."

    def test_user_messages_passed_through(self, provider):
        """User messages should be passed through unchanged."""
        messages = [
            {"role": "user", "content": "What is the issue?"},
        ]

        result = provider._convert_messages_to_anthropic(messages)

        assert len(result["messages"]) == 1
        assert result["messages"][0] == {
            "role": "user",
            "content": "What is the issue?",
        }
        assert "system" not in result

    def test_assistant_message_with_tool_calls(self, provider):
        """Assistant messages with tool_calls become content blocks with tool_use."""
        messages = [
            {"role": "user", "content": "Search for SSH errors"},
            {
                "role": "assistant",
                "content": "Let me search for that.",
                "tool_calls": [
                    {
                        "id": "call_abc123",
                        "type": "function",
                        "function": {
                            "name": "search_file",
                            "arguments": '{"query": "SSH error"}',
                        },
                    }
                ],
            },
        ]

        result = provider._convert_messages_to_anthropic(messages)

        assistant_msg = result["messages"][1]
        assert assistant_msg["role"] == "assistant"
        assert isinstance(assistant_msg["content"], list)
        assert len(assistant_msg["content"]) == 2

        # First block: text
        assert assistant_msg["content"][0] == {
            "type": "text",
            "text": "Let me search for that.",
        }

        # Second block: tool_use
        tool_use = assistant_msg["content"][1]
        assert tool_use["type"] == "tool_use"
        assert tool_use["id"] == "call_abc123"
        assert tool_use["name"] == "search_file"
        assert tool_use["input"] == {"query": "SSH error"}

    def test_assistant_tool_call_arguments_parsed_from_json_string(self, provider):
        """JSON string arguments in tool_calls should be parsed to dict for tool_use input."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_001",
                        "type": "function",
                        "function": {
                            "name": "deep_analysis",
                            "arguments": '{"file_id": "ev_123", "question": "What failed?"}',
                        },
                    }
                ],
            },
        ]

        result = provider._convert_messages_to_anthropic(messages)

        tool_use = result["messages"][1]["content"][
            0
        ]  # No text content, tool_use is first
        assert tool_use["type"] == "tool_use"
        assert tool_use["input"] == {"file_id": "ev_123", "question": "What failed?"}

    def test_assistant_tool_call_invalid_json_arguments(self, provider):
        """Invalid JSON in arguments should fall back to empty dict."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_002",
                        "type": "function",
                        "function": {
                            "name": "search_file",
                            "arguments": "not valid json",
                        },
                    }
                ],
            },
        ]

        result = provider._convert_messages_to_anthropic(messages)

        tool_use = result["messages"][1]["content"][0]
        assert tool_use["input"] == {}

    def test_tool_messages_become_user_with_tool_result(self, provider):
        """Tool messages should become user messages with tool_result content blocks."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "tool",
                "tool_call_id": "call_abc123",
                "content": "Found 3 SSH errors in syslog",
            },
        ]

        result = provider._convert_messages_to_anthropic(messages)

        tool_msg = result["messages"][1]
        assert tool_msg["role"] == "user"
        assert isinstance(tool_msg["content"], list)
        assert len(tool_msg["content"]) == 1
        assert tool_msg["content"][0] == {
            "type": "tool_result",
            "tool_use_id": "call_abc123",
            "content": "Found 3 SSH errors in syslog",
        }

    def test_consecutive_tool_results_grouped(self, provider):
        """Consecutive tool messages should be grouped into a single user message."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search_file", "arguments": "{}"},
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "search_file", "arguments": "{}"},
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "Result 1",
            },
            {
                "role": "tool",
                "tool_call_id": "call_2",
                "content": "Result 2",
            },
        ]

        result = provider._convert_messages_to_anthropic(messages)

        # Should have: user, assistant, user (grouped tool results)
        assert len(result["messages"]) == 3

        grouped_msg = result["messages"][2]
        assert grouped_msg["role"] == "user"
        assert isinstance(grouped_msg["content"], list)
        assert len(grouped_msg["content"]) == 2
        assert grouped_msg["content"][0]["tool_use_id"] == "call_1"
        assert grouped_msg["content"][1]["tool_use_id"] == "call_2"

    def test_assistant_message_empty_content_no_tool_calls(self, provider):
        """Assistant message with empty content and no tool_calls should pass through."""
        messages = [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": ""},
        ]

        result = provider._convert_messages_to_anthropic(messages)

        assert result["messages"][1]["role"] == "assistant"
        assert result["messages"][1]["content"] == ""

    def test_no_system_messages_omits_system_key(self, provider):
        """When no system messages present, result should not contain 'system' key."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]

        result = provider._convert_messages_to_anthropic(messages)

        assert "system" not in result
        assert len(result["messages"]) == 2

    def test_full_multi_turn_conversation(self, provider):
        """Full multi-turn conversation with system, user, assistant, and tool messages."""
        messages = [
            {"role": "system", "content": "You are an investigator."},
            {"role": "user", "content": "Check SSH logs"},
            {
                "role": "assistant",
                "content": "Searching...",
                "tool_calls": [
                    {
                        "id": "call_search",
                        "type": "function",
                        "function": {
                            "name": "search_file",
                            "arguments": '{"query": "SSH"}',
                        },
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_search",
                "content": "SSH auth failure at 14:03:21",
            },
            {"role": "user", "content": "What does that mean?"},
        ]

        result = provider._convert_messages_to_anthropic(messages)

        assert result["system"] == "You are an investigator."
        assert len(result["messages"]) == 4
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][1]["role"] == "assistant"
        assert result["messages"][2]["role"] == "user"  # tool result
        assert result["messages"][3]["role"] == "user"

    def test_assistant_message_with_dict_arguments(self, provider):
        """Arguments that are already dicts should be used directly."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_dict",
                        "type": "function",
                        "function": {
                            "name": "search_file",
                            "arguments": {"query": "error", "limit": 10},
                        },
                    }
                ],
            },
        ]

        result = provider._convert_messages_to_anthropic(messages)

        tool_use = result["messages"][1]["content"][0]
        assert tool_use["input"] == {"query": "error", "limit": 10}


# =========================================================================
# generate() message handling tests
# =========================================================================


def _mock_aiohttp_session(response_data: dict):
    """Create a properly nested mock for aiohttp.ClientSession().post() pattern.

    Handles the double async context manager:
        async with aiohttp.ClientSession() as session:
            async with session.post(...) as response:
    """
    # Mock the response object
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=response_data)
    mock_response.text = AsyncMock(return_value="")
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    # Mock the session
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    return mock_session


@pytest.mark.unit
@pytest.mark.asyncio
class TestAnthropicGenerateMessages:
    """Tests for generate() with messages kwarg."""

    async def test_no_messages_wraps_prompt(self, provider):
        """Without messages kwarg, prompt should be wrapped as single user message."""
        response_data = {
            "content": [{"type": "text", "text": "Hello!"}],
            "usage": {"output_tokens": 10},
        }
        mock_session = _mock_aiohttp_session(response_data)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await provider.generate("Test prompt")

            call_kwargs = mock_session.post.call_args
            request_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

            assert request_body["messages"] == [
                {"role": "user", "content": "Test prompt"}
            ]
            assert "system" not in request_body

    async def test_messages_kwarg_used_directly(self, provider):
        """When messages kwarg is provided, it should be converted and used."""
        messages = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "Hello"},
        ]
        response_data = {
            "content": [{"type": "text", "text": "Hi!"}],
            "usage": {"output_tokens": 5},
        }
        mock_session = _mock_aiohttp_session(response_data)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await provider.generate("ignored prompt", messages=messages)

            call_kwargs = mock_session.post.call_args
            request_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

            # System should be extracted
            assert request_body["system"] == "Be helpful."
            # Messages should contain the converted user message
            assert request_body["messages"] == [{"role": "user", "content": "Hello"}]
