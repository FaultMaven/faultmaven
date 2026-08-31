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
from faultmaven.infrastructure.llm.providers.gemini import GeminiProvider
from faultmaven.infrastructure.llm.providers.groq_provider import GroqProvider
from faultmaven.infrastructure.llm.providers.huggingface import HuggingFaceProvider
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider
from faultmaven.infrastructure.llm.providers.registry import PROVIDER_SCHEMA


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

# fm#1287 — the transport matrix is DERIVED FROM THE REGISTRY, not hand-listed.
#
# "Every provider types its transport failures" is a cross-provider invariant,
# so the set it is checked over has to be the set of providers that exists. The
# first version of this file enumerated them by hand, and that hand-list was
# ALREADY incomplete — Gemini, HuggingFace and OpenRouter had to be bolted on —
# which is exactly the shape that leaves the NEXT provider silently exempt.
# Same convention as ``test_reasoning_intent.py``, which CLAUDE.md documents.
#
# ``local`` is excluded here and covered by ``test_local_transport_errors.py``
# instead: it is the one provider that picks between THREE wire transports at
# call time, so a single ``generate("hello")`` cannot exercise it the way this
# harness assumes.
_SCHEMA_PROVIDERS = [
    pytest.param(
        schema["provider_class"],
        f"https://{name}.test.invalid/v1",
        schema["default_model"],
        id=name,
    )
    for name, schema in sorted(PROVIDER_SCHEMA.items())
    if name != "local"
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


# =============================================================================
# fm#1287 — a TRANSPORT failure is typed too, on every provider
# =============================================================================


def _mock_transport_error_session(error: Exception):
    """aiohttp.ClientSession mock whose post() context raises *error*.

    Models a failure with no HTTP status at all: the connection dropped, was
    refused, or the body stopped mid-stream. aiohttp reports these as
    ``ClientError`` subclasses whose wording ("Server disconnected", "Cannot
    connect to host …") matches nothing in the retry phrase list.
    """
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(side_effect=error)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_ctx)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def _transport_errors():
    """The three real aiohttp transport failures, freshly constructed."""
    import aiohttp

    class _Key:
        host = "api.example.com"
        port = 443
        is_ssl = True
        ssl = True

    return [
        aiohttp.ServerDisconnectedError(),
        aiohttp.ClientConnectorError(
            connection_key=_Key(), os_error=OSError(111, "Connect call failed")
        ),
        aiohttp.ClientPayloadError("Response payload is not completed"),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("provider_class,base_url,model", _SCHEMA_PROVIDERS)
@pytest.mark.parametrize("error_index", [0, 1, 2])
async def test_transport_failure_is_typed_and_retryable(
    provider_class, base_url, model, error_index
):
    """A dropped/refused connection must raise LLMException(retryable=True).

    Seven of the eight providers let the raw aiohttp exception escape, so
    retryability was decided downstream by matching aiohttp's wording — and
    "Server disconnected" / "Cannot connect to host …" match nothing, so the
    engine treated a dropped socket as PERMANENT and abandoned the turn. Cohere
    was the only one that typed it. Same class of defect as the timeout in
    #1287, in seven more places.
    """
    error = _transport_errors()[error_index]
    provider = provider_class(_config(provider_class.__name__, base_url, model))
    with patch(
        "aiohttp.ClientSession", return_value=_mock_transport_error_session(error)
    ):
        with pytest.raises(LLMException) as exc_info:
            await provider.generate("hello")
    assert exc_info.value.retryable is True
    assert "connection error" in str(exc_info.value).lower()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("provider_class,base_url,model", _SCHEMA_PROVIDERS)
async def test_transport_handler_does_not_swallow_http_errors(
    provider_class, base_url, model
):
    """POSITIVE CONTROL for the parametrization above.

    The new ``except aiohttp.ClientError`` clause sits in the same try as the
    status handling. If it were placed so that it caught the provider's own
    ``LLMException``, every 4xx would silently become a retryable transport
    error. A 400 must still arrive as a non-retryable 400.
    """
    provider = provider_class(_config(provider_class.__name__, base_url, model))
    with patch("aiohttp.ClientSession", return_value=_mock_error_session(400)):
        with pytest.raises(LLMException) as exc_info:
            await provider.generate("hello")
    assert exc_info.value.status_code == 400
    assert exc_info.value.retryable is False
