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
from faultmaven.infrastructure.llm.providers.openrouter_provider import (
    OpenRouterProvider,
)


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


def _config_for(
    model: str,
    *,
    name: str = "openai",
    base_url: str = "https://api.openai.com/v1",
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        api_key="test-key",
        base_url=base_url,
        models=[model],
        default_model=model,
        timeout=30,
        confidence_score=0.9,
    )


@pytest.mark.unit
@pytest.mark.asyncio
class TestOpenAITokenLimitParam:
    """The GPT-5 / o-series families reject the legacy ``max_tokens`` parameter
    with a 400 ``unsupported_parameter`` and require ``max_completion_tokens``;
    older models keep ``max_tokens``."""

    @pytest.mark.parametrize(
        "model,expected_param,absent_param",
        [
            ("gpt-4o", "max_tokens", "max_completion_tokens"),
            ("gpt-4-turbo", "max_tokens", "max_completion_tokens"),
            ("gpt-3.5-turbo-0125", "max_tokens", "max_completion_tokens"),
            ("gpt-5.4-mini", "max_completion_tokens", "max_tokens"),
            ("gpt-5", "max_completion_tokens", "max_tokens"),
            ("o1", "max_completion_tokens", "max_tokens"),
            ("o3-mini", "max_completion_tokens", "max_tokens"),
            # Non-OpenAI ids must NOT trip the substring classifier (false-positive
            # guard): a Fireworks/DeepSeek model keeps the legacy ``max_tokens``.
            (
                "accounts/fireworks/models/deepseek-v3",
                "max_tokens",
                "max_completion_tokens",
            ),
        ],
    )
    async def test_token_param_selected_by_model(
        self, model, expected_param, absent_param
    ):
        provider = OpenAIProvider(_config_for(model))
        mock_session = _mock_aiohttp_session(_mock_openai_response("ok"))

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await provider.generate("hi", max_tokens=512)

            request_body = mock_session.post.call_args.kwargs["json"]
            assert request_body[expected_param] == 512
            assert absent_param not in request_body

    async def test_stray_token_kwarg_does_not_duplicate_the_param(self):
        """A passthrough caller that forwards a token key via **kwargs must not
        inject a conflicting/duplicate param into the payload (sending both
        400s on the models that reject the legacy name). ``max_tokens`` is a
        named arg, so the only key that can arrive via kwargs is
        ``max_completion_tokens`` — for a gpt-4o request (which uses
        ``max_tokens``) that stray key would otherwise be a second token param."""
        provider = OpenAIProvider(_config_for("gpt-4o"))
        mock_session = _mock_aiohttp_session(_mock_openai_response("ok"))

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await provider.generate("hi", max_tokens=512, max_completion_tokens=999)

            request_body = mock_session.post.call_args.kwargs["json"]
            assert request_body["max_tokens"] == 512
            assert "max_completion_tokens" not in request_body

    async def test_openrouter_always_uses_legacy_max_tokens(self):
        """OpenRouter's unified gateway normalizes the parameter itself, so even
        a routed ``openai/gpt-5`` keeps ``max_tokens`` (the subclass opts out)."""
        provider = OpenRouterProvider(
            _config_for(
                "openai/gpt-5",
                name="openrouter",
                base_url="https://openrouter.ai/api/v1",
            )
        )
        mock_session = _mock_aiohttp_session(_mock_openai_response("ok"))

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await provider.generate("hi", max_tokens=256)

            request_body = mock_session.post.call_args.kwargs["json"]
            assert request_body["max_tokens"] == 256
            assert "max_completion_tokens" not in request_body


@pytest.mark.unit
def test_token_param_classifier_is_pure_and_case_insensitive():
    uses = OpenAIProvider._uses_completion_tokens_param
    # Uppercase operator config (the exact OPENAI_MODEL=GPT-5.4 shape).
    assert uses("GPT-5.4") is True
    assert uses("o4-mini") is True
    assert uses("openai/o3-mini") is True  # vendor-prefixed id still matches
    assert uses("gpt-4o") is False
    # A family token embedded mid-name must NOT match (anchored at id start).
    assert uses("my-gpt-4-o1-test") is False
    assert uses("chatgpt-4o-latest") is False
    # Fireworks/DeepSeek id must not false-positive match.
    assert uses("accounts/fireworks/models/deepseek-v3") is False
    # OpenRouter opts out unconditionally (gateway normalizes the param).
    assert OpenRouterProvider._uses_completion_tokens_param("openai/gpt-5") is False
