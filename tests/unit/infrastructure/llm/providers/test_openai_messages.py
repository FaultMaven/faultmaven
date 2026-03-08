"""
Tests for OpenAI provider message handling.

Validates that OpenAI provider correctly handles the messages kwarg for
multi-turn conversations, using messages directly (OpenAI format is canonical).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.providers.base import (
    LLMResponse,
    ProviderConfig,
    ToolCall,
)
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider


@pytest.fixture
def openai_config():
    """Minimal OpenAI provider config for unit tests."""
    return ProviderConfig(
        name="openai",
        api_key="test-key",
        base_url="https://api.openai.com/v1",
        models=["gpt-4o"],
        default_model="gpt-4o",
        timeout=30,
        confidence_score=0.9,
    )


@pytest.fixture
def provider(openai_config):
    """Create an OpenAIProvider instance for testing."""
    return OpenAIProvider(openai_config)


def _mock_openai_response(content="Hello!", tool_calls=None, total_tokens=100):
    """Build a mock OpenAI API JSON response."""
    message = {"content": content, "role": "assistant"}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": {"total_tokens": total_tokens},
    }


def _mock_aiohttp_session(response_data: dict):
    """Create a properly nested mock for aiohttp.ClientSession().post() pattern."""
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=response_data)
    mock_response.text = AsyncMock(return_value="")
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    return mock_session


@pytest.mark.unit
@pytest.mark.asyncio
class TestOpenAIGenerateMessages:
    """Tests for OpenAI provider generate() with messages kwarg."""

    async def test_no_messages_creates_single_user_message(self, provider):
        """Without messages kwarg, prompt should be wrapped as single user message."""
        mock_resp = _mock_openai_response("Generated text")
        mock_session = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await provider.generate("Test prompt")

            call_kwargs = mock_session.post.call_args
            request_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

            assert request_body["messages"] == [
                {"role": "user", "content": "Test prompt"}
            ]

    async def test_messages_passed_through_directly(self, provider):
        """When messages kwarg is provided, it should be used directly (OpenAI canonical format)."""
        messages = [
            {"role": "system", "content": "You are a troubleshooting assistant."},
            {"role": "user", "content": "Check the logs"},
            {"role": "assistant", "content": "I'll search the logs for you."},
            {"role": "user", "content": "Thanks"},
        ]
        mock_resp = _mock_openai_response("Searching logs...")
        mock_session = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await provider.generate("ignored", messages=messages)

            call_kwargs = mock_session.post.call_args
            request_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

            # Messages should be used as-is (OpenAI format is canonical)
            assert request_body["messages"] == messages

    async def test_messages_popped_from_kwargs(self, provider):
        """Messages should be popped from kwargs so it doesn't appear in payload.update(kwargs)."""
        messages = [
            {"role": "user", "content": "Hello"},
        ]
        mock_resp = _mock_openai_response("Hi!")
        mock_session = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            # Pass messages and verify no double-inclusion
            result = await provider.generate(
                "ignored",
                messages=messages,
            )

            call_kwargs = mock_session.post.call_args
            request_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

            # The payload should have messages from the pop, not duplicated
            assert request_body["messages"] == messages
            # Messages should NOT appear as a separate key from kwargs.update
            messages_count = json.dumps(request_body).count('"messages"')
            assert messages_count == 1

    async def test_messages_with_tool_calls_in_conversation(self, provider):
        """Messages containing tool call history should be passed through."""
        messages = [
            {"role": "user", "content": "Search for errors"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "search_file",
                            "arguments": '{"query": "error"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_123",
                "name": "search_file",
                "content": "Found 3 errors",
            },
        ]
        mock_resp = _mock_openai_response("I found 3 errors in the logs.")
        mock_session = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await provider.generate("", messages=messages)

            call_kwargs = mock_session.post.call_args
            request_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

            assert request_body["messages"] == messages

    async def test_generate_returns_llm_response(self, provider):
        """generate() should return a proper LLMResponse object."""
        mock_resp = _mock_openai_response("Test response", total_tokens=50)
        mock_session = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await provider.generate("Hello")

            assert isinstance(result, LLMResponse)
            assert result.content == "Test response"
            assert result.provider == "openai"
            assert result.model == "gpt-4o"
            assert result.tokens_used == 50
