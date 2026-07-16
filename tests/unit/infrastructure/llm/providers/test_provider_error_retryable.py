"""Regression tests: provider generate() raises LLMException with correct
retryability on HTTP errors.

Audit 2026-06: Groq/Anthropic passed ``retryable=response.status == 429``,
which forced 5xx to non-retryable (the resilient client then failed fast on a
transient server error). The fix removed the override and made 429 retryable in
the central derivation. These tests exercise the real generate() error paths.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.llm.providers.anthropic import AnthropicProvider
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.cohere_provider import CohereProvider
from faultmaven.infrastructure.llm.providers.fireworks_provider import FireworksProvider
from faultmaven.infrastructure.llm.providers.groq_provider import GroqProvider
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider


def _mock_error_session(status: int):
    """aiohttp.ClientSession mock whose post() returns a non-200 response."""
    mock_response = AsyncMock()
    mock_response.status = status
    mock_response.json = AsyncMock(return_value={})
    mock_response.text = AsyncMock(return_value="boom")
    mock_response.headers = {}
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def _mock_timeout_session():
    """aiohttp.ClientSession mock whose post() context raises TimeoutError.

    Simulates the aiohttp.ClientTimeout firing inside ``async with
    session.post(...) as response:`` — the path each provider guards with
    ``except asyncio.TimeoutError``.
    """
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_ctx)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def _config(name, base_url, model):
    return ProviderConfig(
        name=name,
        api_key="test-key",
        base_url=base_url,
        models=[model],
        default_model=model,
        timeout=30,
    )


# (provider_class, base_url, model)
_PROVIDERS = [
    (OpenAIProvider, "https://api.openai.com/v1", "gpt-4o"),
    (
        FireworksProvider,
        "https://api.fireworks.ai/inference/v1",
        "accounts/fireworks/models/deepseek-v3",
    ),
    (CohereProvider, "https://api.cohere.ai/v1", "command-r-plus"),
    (GroqProvider, "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    (AnthropicProvider, "https://api.anthropic.com/v1", "claude-sonnet-4-6"),
]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("provider_class,base_url,model", _PROVIDERS)
async def test_5xx_is_retryable(provider_class, base_url, model):
    provider = provider_class(_config(provider_class.__name__, base_url, model))
    with patch("aiohttp.ClientSession", return_value=_mock_error_session(503)):
        with pytest.raises(LLMException) as exc_info:
            await provider.generate("hello")
    assert exc_info.value.status_code == 503
    assert exc_info.value.retryable is True


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("provider_class,base_url,model", _PROVIDERS)
async def test_4xx_is_non_retryable(provider_class, base_url, model):
    provider = provider_class(_config(provider_class.__name__, base_url, model))
    with patch("aiohttp.ClientSession", return_value=_mock_error_session(400)):
        with pytest.raises(LLMException) as exc_info:
            await provider.generate("hello")
    assert exc_info.value.status_code == 400
    assert exc_info.value.retryable is False


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_class,base_url,model",
    # Providers without an internal 429 retry loop — they raise immediately,
    # so the central derivation must classify 429 as retryable.
    [p for p in _PROVIDERS if p[0] not in (GroqProvider, AnthropicProvider)],
)
async def test_429_is_retryable(provider_class, base_url, model):
    provider = provider_class(_config(provider_class.__name__, base_url, model))
    with patch("aiohttp.ClientSession", return_value=_mock_error_session(429)):
        with pytest.raises(LLMException) as exc_info:
            await provider.generate("hello")
    assert exc_info.value.status_code == 429
    assert exc_info.value.retryable is True


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("provider_class,base_url,model", _PROVIDERS)
async def test_timeout_carries_504_and_is_retryable(provider_class, base_url, model):
    """A client/read timeout must raise LLMException(status_code=504).

    Regression: providers used to raise a bare ``LLMException("…timed out…")``
    with no status_code, so ``retryable`` defaulted to False and the message
    ("timed out") did not contain the ``"timeout"`` substring the API's turn
    handler string-matched — the failure fell through to a naked 500 instead of a
    504. Stamping 504 lets both the engine retry loop and the API translate a
    timeout off typed metadata, not the message.
    """
    provider = provider_class(_config(provider_class.__name__, base_url, model))
    with patch("aiohttp.ClientSession", return_value=_mock_timeout_session()):
        with pytest.raises(LLMException) as exc_info:
            await provider.generate("hello")
    assert exc_info.value.status_code == 504
    assert exc_info.value.retryable is True
    assert "timed out" in str(exc_info.value).lower()
