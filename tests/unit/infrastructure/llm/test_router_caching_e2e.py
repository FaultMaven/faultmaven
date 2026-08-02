"""End-to-end: `cache_prompt` survives the full router → registry → provider chain.

The provider-level tests (test_token_extraction_and_caching.py) prove each
provider *consumes* `cache_prompt` correctly. These tests close the loop by
driving a real `LLMRouter` with a real `ProviderRegistry` and a real provider,
with only the HTTP layer mocked, and asserting on the request body the provider
actually sends. This is the exact link where #602 silently broke — the router
dropped the flag before it reached a provider — so it is worth an integration
test rather than trusting the wiring.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.providers.anthropic import AnthropicProvider
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider
from faultmaven.infrastructure.llm.providers.registry import (
    ProviderRegistry,
    ProviderState,
)


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


def _registry_with(provider, name: str) -> ProviderRegistry:
    """A registry pre-loaded with one already-initialized provider."""
    reg = ProviderRegistry(settings=None)
    reg._providers = {name: provider}
    reg._fallback_chain = [name]
    reg._provider_states = {name: ProviderState(name=name)}
    reg._initialized = True  # skip environment-based lazy init
    return reg


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


_MESSAGES = [
    {"role": "system", "content": "Be helpful."},
    {"role": "user", "content": "Hello"},
]

_ANTHROPIC_RESP = {
    "content": [{"type": "text", "text": "hi"}],
    "usage": {"input_tokens": 5, "output_tokens": 5},
}
_OPENAI_RESP = {
    "choices": [{"message": {"content": "hi", "role": "assistant"}}],
    "usage": {"total_tokens": 10},
}


@pytest.mark.unit
@pytest.mark.asyncio
class TestCachePromptEndToEnd:
    async def test_cache_prompt_reaches_anthropic_body(self, anthropic_provider):
        # router.route(cache_prompt=True) -> call_external -> registry.route_request
        # -> AnthropicProvider.generate -> HTTP body carries the cache breakpoint.
        registry = _registry_with(anthropic_provider, "anthropic")
        mock_session = _mock_aiohttp_session(_ANTHROPIC_RESP)

        with patch(
            "faultmaven.infrastructure.llm.router.get_registry",
            return_value=registry,
        ):
            from faultmaven.infrastructure.llm.router import LLMRouter

            router = LLMRouter()
            with patch("aiohttp.ClientSession", return_value=mock_session):
                # prompt=None keeps the LLMResponseCache out of the path;
                # messages route straight through to the provider.
                # tools is left at the router default (None) — the full chain
                # must handle that without crashing.
                await router.route(
                    prompt=None,
                    messages=_MESSAGES,
                    model="claude-sonnet-4-6",
                    cache_prompt=True,
                )

        body = _request_body(mock_session)
        assert body["system"] == [
            {
                "type": "text",
                "text": "Be helpful.",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    async def test_no_cache_prompt_leaves_anthropic_system_plain(
        self, anthropic_provider
    ):
        registry = _registry_with(anthropic_provider, "anthropic")
        mock_session = _mock_aiohttp_session(_ANTHROPIC_RESP)

        with patch(
            "faultmaven.infrastructure.llm.router.get_registry",
            return_value=registry,
        ):
            from faultmaven.infrastructure.llm.router import LLMRouter

            router = LLMRouter()
            with patch("aiohttp.ClientSession", return_value=mock_session):
                await router.route(
                    prompt=None,
                    messages=_MESSAGES,
                    model="claude-sonnet-4-6",
                )

        body = _request_body(mock_session)
        assert body["system"] == "Be helpful."

    async def test_cache_prompt_never_leaks_through_chain_to_openai(
        self, openai_provider
    ):
        # The full chain must not forward cache_prompt into a non-Anthropic body
        # (OpenAI-family 400s on unknown request fields).
        registry = _registry_with(openai_provider, "openai")
        mock_session = _mock_aiohttp_session(_OPENAI_RESP)

        with patch(
            "faultmaven.infrastructure.llm.router.get_registry",
            return_value=registry,
        ):
            from faultmaven.infrastructure.llm.router import LLMRouter

            router = LLMRouter()
            with patch("aiohttp.ClientSession", return_value=mock_session):
                await router.route(
                    prompt=None,
                    messages=_MESSAGES,
                    model="gpt-4o",
                    cache_prompt=True,
                )

        body = _request_body(mock_session)
        assert "cache_prompt" not in body


@pytest.mark.unit
@pytest.mark.asyncio
class TestCachePromptForwarding:
    """Belt-and-suspenders: the router forwards cache_prompt to route_request
    (the precise link #602 dropped)."""

    async def test_route_forwards_cache_prompt_to_registry(self):
        from faultmaven.infrastructure.llm.providers import LLMResponse

        mock_registry = MagicMock()
        mock_registry.get_available_providers.return_value = ["anthropic"]
        mock_registry.get_fallback_chain.return_value = ["anthropic"]
        mock_registry.route_request = AsyncMock(
            return_value=LLMResponse(
                content="ok",
                confidence=0.9,
                provider="anthropic",
                model="claude-sonnet-4-6",
                tokens_used=10,
                response_time_ms=10,
            )
        )
        with patch(
            "faultmaven.infrastructure.llm.router.get_registry",
            return_value=mock_registry,
        ):
            from faultmaven.infrastructure.llm.router import LLMRouter

            router = LLMRouter()
            await router.route(prompt=None, messages=_MESSAGES, cache_prompt=True)

        assert mock_registry.route_request.call_args.kwargs.get("cache_prompt") is True

    async def test_generate_forwards_cache_prompt(self):
        from faultmaven.infrastructure.llm.providers import LLMResponse

        mock_registry = MagicMock()
        mock_registry.get_available_providers.return_value = ["anthropic"]
        mock_registry.get_fallback_chain.return_value = ["anthropic"]
        mock_registry.route_request = AsyncMock(
            return_value=LLMResponse(
                content="ok",
                confidence=0.9,
                provider="anthropic",
                model="claude-sonnet-4-6",
                tokens_used=10,
                response_time_ms=10,
            )
        )
        with patch(
            "faultmaven.infrastructure.llm.router.get_registry",
            return_value=mock_registry,
        ):
            from faultmaven.infrastructure.llm.router import LLMRouter

            router = LLMRouter()
            # generate() is the ILLMProvider entrypoint the engine calls.
            await router.generate(prompt=None, messages=_MESSAGES, cache_prompt=True)

        assert mock_registry.route_request.call_args.kwargs.get("cache_prompt") is True
