"""Provider token-extraction (disjoint buckets) and prompt-caching behavior.

Two correctness properties, verified against the real provider ``generate()``
with the HTTP layer mocked:

1. Token buckets are DISJOINT: ``input_tokens`` never includes cached prompt
   tokens (those live in ``cache_read_tokens``), so summing them is correct.
   ``prompt_cache_hit`` — not the overloaded ``cached`` flag — signals a
   provider prompt-cache read.
2. ``cache_prompt`` is consumed only by Anthropic (adds a ``cache_control``
   breakpoint) and is popped by OpenAI-family providers so it can never leak
   into the request body and 400 the call.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.providers.anthropic import AnthropicProvider
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider


def _mock_aiohttp_session(response_data: dict):
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


def _request_body(mock_session) -> dict:
    call = mock_session.post.call_args
    return call.kwargs.get("json") or call[1].get("json")


@pytest.fixture
def anthropic_provider():
    return AnthropicProvider(
        ProviderConfig(
            name="anthropic",
            api_key="test-key",
            base_url="https://api.anthropic.com/v1",
            models=["claude-sonnet-4-6"],
            default_model="claude-sonnet-4-6",
            timeout=30,
            confidence_score=0.9,
        )
    )


@pytest.fixture
def openai_provider():
    return OpenAIProvider(
        ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.openai.com/v1",
            models=["gpt-4o"],
            default_model="gpt-4o",
            timeout=30,
            confidence_score=0.9,
        )
    )


@pytest.mark.unit
@pytest.mark.asyncio
class TestAnthropicExtractionAndCaching:
    async def test_disjoint_token_extraction(self, anthropic_provider):
        response_data = {
            "content": [{"type": "text", "text": "hi"}],
            "usage": {
                "input_tokens": 1000,  # Anthropic's input is already UNCACHED
                "output_tokens": 200,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 100,
            },
        }
        mock_session = _mock_aiohttp_session(response_data)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await anthropic_provider.generate("hi")

        assert result.input_tokens == 1000
        assert result.output_tokens == 200
        assert result.cache_read_tokens == 500
        assert result.cache_write_tokens == 100
        # Disjoint buckets sum to the reported total.
        assert result.tokens_used == 1800
        assert result.prompt_cache_hit is True
        # cached (local SemanticCache) must NOT be set by a provider cache read.
        assert result.cached is False

    async def test_cache_prompt_adds_breakpoint_to_system(self, anthropic_provider):
        messages = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "Hello"},
        ]
        response_data = {
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 5, "output_tokens": 5},
        }
        mock_session = _mock_aiohttp_session(response_data)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            await anthropic_provider.generate(
                "ignored", messages=messages, cache_prompt=True
            )

        body = _request_body(mock_session)
        assert body["system"] == [
            {
                "type": "text",
                "text": "Be helpful.",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    async def test_no_cache_prompt_leaves_system_as_string(self, anthropic_provider):
        messages = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "Hello"},
        ]
        response_data = {
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 5, "output_tokens": 5},
        }
        mock_session = _mock_aiohttp_session(response_data)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            await anthropic_provider.generate("ignored", messages=messages)

        body = _request_body(mock_session)
        assert body["system"] == "Be helpful."


@pytest.mark.unit
@pytest.mark.asyncio
class TestOpenAIExtractionAndCaching:
    async def test_cached_tokens_subtracted_from_input(self, openai_provider):
        response_data = {
            "choices": [{"message": {"content": "hi", "role": "assistant"}}],
            "usage": {
                "prompt_tokens": 1000,  # INCLUSIVE of cached on OpenAI
                "completion_tokens": 200,
                "total_tokens": 1200,
                "prompt_tokens_details": {"cached_tokens": 400},
            },
        }
        mock_session = _mock_aiohttp_session(response_data)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await openai_provider.generate("hi")

        # input excludes the cached portion → disjoint from cache_read.
        assert result.input_tokens == 600
        assert result.cache_read_tokens == 400
        assert result.output_tokens == 200
        assert result.tokens_used == 1200
        assert result.prompt_cache_hit is True

    async def test_no_cache_hit(self, openai_provider):
        response_data = {
            "choices": [{"message": {"content": "hi", "role": "assistant"}}],
            "usage": {
                "prompt_tokens": 300,
                "completion_tokens": 50,
                "total_tokens": 350,
            },
        }
        mock_session = _mock_aiohttp_session(response_data)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await openai_provider.generate("hi")

        assert result.input_tokens == 300
        assert result.cache_read_tokens == 0
        assert result.prompt_cache_hit is False

    async def test_cache_prompt_never_leaks_into_body(self, openai_provider):
        response_data = {
            "choices": [{"message": {"content": "hi", "role": "assistant"}}],
            "usage": {"total_tokens": 10},
        }
        mock_session = _mock_aiohttp_session(response_data)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            # cache_prompt=True must be popped, not forwarded — OpenAI rejects
            # unknown body fields with a 400.
            await openai_provider.generate("hi", cache_prompt=True)

        body = _request_body(mock_session)
        assert "cache_prompt" not in body
