"""End-to-end: `cache_prompt` survives the full router → registry → provider chain.

The provider-level tests (test_token_extraction_and_caching.py) prove each
provider *consumes* `cache_prompt` correctly. These tests close the loop by
driving a real `LLMRouter` with a real `ProviderRegistry` and a real provider,
with only the HTTP layer mocked, and asserting on the request body the provider
actually sends. This is the exact link where #602 silently broke — the router
dropped the flag before it reached a provider — so it is worth an integration
test rather than trusting the wiring.

The second half of the file covers the router's *other* cache — the in-process
`LLMResponseCache` — where the same class of wiring bug lives: read and write
gated on different conditions, so entries are written that no lookup can reach.
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


def _mock_registry(confidence: float = 0.9, model: str = "gpt-4o"):
    from faultmaven.infrastructure.llm.providers import LLMResponse

    registry = MagicMock()
    registry.get_available_providers.return_value = ["openai"]
    registry.get_fallback_chain.return_value = ["openai"]
    registry.route_request = AsyncMock(
        return_value=LLMResponse(
            content="check the kubelet",
            confidence=confidence,
            provider="openai",
            model=model,
            tokens_used=10,
            response_time_ms=10,
        )
    )
    return registry


@pytest.mark.unit
@pytest.mark.asyncio
class TestResponseCacheStoreGate:
    """The router writes the response cache exactly where it reads it.

    ``check`` can only answer when the caller named a model (the model is part
    of the key) and sent no ``messages``. A store gated any wider is not a
    cache — it is memory the process pays for and no lookup can reach: the
    router used to key entries on ``model or response.model``, so every
    no-model caller (title generation, suggestions) left an entry behind that
    nothing could ever hit. Swept over the shapes ``route`` is actually called
    with rather than asserting one example, so a gate that only re-widens for
    ``messages`` still fails here.
    """

    STORING_CALLS = {
        # (kwargs, expected entries) — stored iff the same call could be served.
        "named_model_prompt_only": ({"model": "gpt-4o"}, 1),
        "no_model": ({"model": None}, 0),
        "named_model_with_messages": ({"model": "gpt-4o", "messages": _MESSAGES}, 0),
        "no_model_with_messages": ({"model": None, "messages": _MESSAGES}, 0),
    }

    @pytest.mark.parametrize("label", sorted(STORING_CALLS))
    async def test_the_cache_is_written_only_where_it_can_be_read(self, label):
        kwargs, expected = self.STORING_CALLS[label]
        registry = _mock_registry()

        with patch(
            "faultmaven.infrastructure.llm.router.get_registry", return_value=registry
        ):
            from faultmaven.infrastructure.llm.router import LLMRouter

            router = LLMRouter()
            response = await router.route(prompt="why is node-3 NotReady?", **kwargs)

        # Confidence is above the store threshold in every case — the gate under
        # test is the model/messages shape, not the quality bar.
        assert response.confidence >= router.confidence_threshold
        assert len(router.cache.cache) == expected

    async def test_a_stored_entry_is_reachable_by_the_identical_call(self):
        """The other half: what the narrowed gate does store must still be
        served, without a second provider call."""
        registry = _mock_registry()

        with patch(
            "faultmaven.infrastructure.llm.router.get_registry", return_value=registry
        ):
            from faultmaven.infrastructure.llm.router import LLMRouter

            router = LLMRouter()
            await router.route(prompt="why is node-3 NotReady?", model="gpt-4o")
            second = await router.route(
                prompt="why is node-3 NotReady?", model="gpt-4o"
            )

        assert second.cached is True
        assert registry.route_request.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
class TestBypassCache:
    """``bypass_cache`` suppresses the lookup and *keeps* the write (#513).

    The engine sets it when retrying a structured call whose cached answer it
    has already found unusable — a body cut off at the generation cap. That
    makes the write the load-bearing half, which is the opposite of the usual
    reading of "bypass": ``max_tokens`` is not part of the cache key, so the
    retry lands on the same key as the truncated entry, and ``LLMResponseCache``
    has no eviction API. If the retry also declined to store, the poisoned entry
    would survive every later identical call, each one paying a wasted attempt
    to rediscover that it will not parse.
    """

    async def test_a_bypass_call_reaches_the_provider_despite_a_warm_entry(self):
        registry = _mock_registry()

        with patch(
            "faultmaven.infrastructure.llm.router.get_registry", return_value=registry
        ):
            from faultmaven.infrastructure.llm.router import LLMRouter

            router = LLMRouter()
            await router.route(prompt="why is node-3 NotReady?", model="gpt-4o")
            second = await router.route(
                prompt="why is node-3 NotReady?", model="gpt-4o", bypass_cache=True
            )

        assert second.cached is not True
        assert registry.route_request.await_count == 2

    async def test_a_bypass_call_overwrites_the_entry_it_bypassed(self):
        """The half that makes the bypass terminal rather than a per-call
        opt-out. After the retry, the *next* ordinary call must be served the
        good response — not the one the bypass was needed to escape."""
        from faultmaven.infrastructure.llm.providers import LLMResponse

        registry = _mock_registry()

        with patch(
            "faultmaven.infrastructure.llm.router.get_registry", return_value=registry
        ):
            from faultmaven.infrastructure.llm.router import LLMRouter

            router = LLMRouter()
            # The poisoned entry: what the first attempt stored before the
            # caller discovered it would not parse.
            await router.route(prompt="why is node-3 NotReady?", model="gpt-4o")
            registry.route_request.return_value = LLMResponse(
                content="the kubelet on node-3 is out of disk",
                confidence=0.9,
                provider="openai",
                model="gpt-4o",
                tokens_used=10,
                response_time_ms=10,
            )
            await router.route(
                prompt="why is node-3 NotReady?", model="gpt-4o", bypass_cache=True
            )
            third = await router.route(prompt="why is node-3 NotReady?", model="gpt-4o")

        assert third.cached is True
        assert third.content == "the kubelet on node-3 is out of disk"
        # One key, replaced — not a second entry alongside the bad one.
        assert len(router.cache.cache) == 1
        # Exactly the two calls that were meant to reach the provider.
        assert registry.route_request.await_count == 2

    async def test_generate_forwards_bypass_cache(self):
        """``generate()`` is the ILLMProvider entrypoint the engine calls; a
        flag dropped there would be silently ignored at the only site that
        sets it."""
        registry = _mock_registry()

        with patch(
            "faultmaven.infrastructure.llm.router.get_registry", return_value=registry
        ):
            from faultmaven.infrastructure.llm.router import LLMRouter

            router = LLMRouter()
            await router.generate(prompt="why is node-3 NotReady?", model="gpt-4o")
            second = await router.generate(
                prompt="why is node-3 NotReady?", model="gpt-4o", bypass_cache=True
            )

        assert second.cached is not True
        assert registry.route_request.await_count == 2
