"""
Unit tests for CohereProvider.
Tests the Cohere LLM provider implementation in isolation using mocked HTTP responses.
"""

from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.infrastructure.llm.providers.base import LLMResponse, ProviderConfig
from faultmaven.infrastructure.llm.providers.cohere_provider import CohereProvider


class TestCohereProviderBasics:
    """Test basic CohereProvider functionality"""

    @pytest.fixture
    def cohere_config(self):
        return ProviderConfig(
            name="cohere",
            api_key="test-cohere-key",
            base_url="https://api.cohere.ai/v2",
            models=["command-r-plus"],
            max_retries=3,
            timeout=30,
            confidence_score=0.82,
        )

    def test_provider_name(self, cohere_config):
        provider = CohereProvider(cohere_config)
        assert provider.provider_name == "cohere"

    def test_is_available(self, cohere_config):
        provider = CohereProvider(cohere_config)
        assert provider.is_available() is True

        cohere_config.api_key = None
        provider = CohereProvider(cohere_config)
        assert provider.is_available() is False

    def test_get_supported_models(self, cohere_config):
        provider = CohereProvider(cohere_config)
        models = provider.get_supported_models()
        assert "command-r-plus" in models
        assert isinstance(models, list)


class TestCohereProviderGenerate:
    """Test CohereProvider.generate() method"""

    @pytest.fixture
    def cohere_config(self):
        return ProviderConfig(
            name="cohere",
            api_key="test-key",
            base_url="https://api.cohere.ai/v2",
            models=["command-r-plus"],
            max_retries=3,
            timeout=30,
            confidence_score=0.82,
        )

    @pytest.mark.asyncio
    async def test_generate_success(self, cohere_config):
        provider = CohereProvider(cohere_config)

        # Mock successful response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json.return_value = {
            "id": "chat-123",
            "message": {
                "role": "assistant",
                "content": "Cohere test response",
            },
            "usage": {"tokens": {"input_tokens": 10, "output_tokens": 10}},
            "finish_reason": "COMPLETE",
        }
        mock_response.__aenter__.return_value = mock_response
        mock_response.__aexit__.return_value = None

        with patch("aiohttp.ClientSession.post", return_value=mock_response):
            result = await provider.generate("Test prompt")

        assert isinstance(result, LLMResponse)
        assert result.content == "Cohere test response"
        assert result.provider == "cohere"
        assert result.model == "command-r-plus"
        assert result.tokens_used == 20  # 10 + 10
        assert result.confidence == 0.82

    @pytest.mark.asyncio
    async def test_generate_with_tools(self, cohere_config):
        provider = CohereProvider(cohere_config)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json.return_value = {
            "id": "chat-tool",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "London"}',
                        },
                    }
                ],
            },
            "usage": {"tokens": {"input_tokens": 10, "output_tokens": 10}},
        }
        mock_response.__aenter__.return_value = mock_response
        mock_response.__aexit__.return_value = None

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                    },
                },
            }
        ]

        with patch("aiohttp.ClientSession.post", return_value=mock_response):
            result = await provider.generate("Weather?", tools=tools)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].function["name"] == "get_weather"

    @pytest.mark.asyncio
    async def test_generate_api_error(self, cohere_config):
        provider = CohereProvider(cohere_config)

        # Mock API error response
        mock_response = AsyncMock()
        mock_response.status = 401
        mock_response.text.return_value = "Invalid API key"
        mock_response.__aenter__.return_value = mock_response
        mock_response.__aexit__.return_value = None

        with patch("aiohttp.ClientSession.post", return_value=mock_response):
            with pytest.raises(Exception) as exc_info:
                await provider.generate("Test")

        assert "401" in str(exc_info.value)
        assert "Invalid API key" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_generate_with_strict_tools(self, cohere_config):
        """Test that strict_tools is enabled by default"""
        provider = CohereProvider(cohere_config)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json.return_value = {
            "id": "chat-strict",
            "message": {"role": "assistant", "content": "Response"},
            "usage": {"tokens": {"input_tokens": 10, "output_tokens": 10}},
        }
        mock_response.__aenter__.return_value = mock_response
        mock_response.__aexit__.return_value = None

        tools = [{"type": "function", "function": {"name": "test_tool"}}]

        with patch(
            "aiohttp.ClientSession.post", return_value=mock_response
        ) as mock_post:
            await provider.generate("Test", tools=tools)

            # Verify the call was made
            assert mock_post.called
            # The payload should include strict_tools=True by default
            call_kwargs = mock_post.call_args[1]
            payload = call_kwargs["json"]
            assert payload.get("strict_tools") is True


class TestCohereProviderIntegration:
    """Test Cohere provider integration with registry"""

    @pytest.mark.asyncio
    async def test_cohere_in_provider_schema(self):
        """Test Cohere is registered in PROVIDER_SCHEMA"""
        from faultmaven.infrastructure.llm.providers.registry import PROVIDER_SCHEMA

        assert "cohere" in PROVIDER_SCHEMA
        assert PROVIDER_SCHEMA["cohere"]["provider_class"].__name__ == "CohereProvider"
        assert PROVIDER_SCHEMA["cohere"]["default_model"] == "command-r-plus"
        assert (
            PROVIDER_SCHEMA["cohere"]["default_base_url"] == "https://api.cohere.ai/v2"
        )
        assert PROVIDER_SCHEMA["cohere"]["confidence_score"] == 0.82

    @pytest.mark.asyncio
    async def test_cohere_in_valid_providers(self):
        """Test Cohere appears in valid provider names"""
        from faultmaven.infrastructure.llm.providers.registry import (
            get_valid_provider_names,
        )

        valid_providers = get_valid_provider_names()
        assert "cohere" in valid_providers
