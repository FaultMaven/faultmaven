"""Tests for LLMRouter messages passthrough and cache bypass.

Verifies that:
- The `messages` kwarg flows from generate() → route() → registry.route_request()
- Cache is bypassed when messages is provided (multi-turn not cacheable)
- Cache works normally when messages is None (single-shot)
- All kwargs (tools, tool_choice, response_format, messages) pass through correctly
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from faultmaven.infrastructure.llm.providers import LLMResponse


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.get_available_providers.return_value = ["openai"]
    registry.get_fallback_chain.return_value = ["openai"]
    registry.route_request = AsyncMock(
        return_value=LLMResponse(
            content="test response",
            confidence=0.9,
            provider="openai",
            model="gpt-4",
            tokens_used=10,
            response_time_ms=100,
            cached=False,
        )
    )
    return registry


@pytest.fixture
def router(mock_registry):
    with patch(
        "faultmaven.infrastructure.llm.router.get_registry",
        return_value=mock_registry,
    ):
        from faultmaven.infrastructure.llm.router import LLMRouter

        r = LLMRouter()
        return r


class TestRoutePassesMessages:
    """Verify route() passes messages kwarg to registry.route_request()."""

    @pytest.mark.asyncio
    async def test_messages_passed_to_registry(self, router, mock_registry):
        """Messages kwarg should flow from route() to registry.route_request()."""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi", "tool_calls": []},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ]

        await router.route(prompt="test", messages=messages)

        mock_registry.route_request.assert_called_once()
        call_kwargs = mock_registry.route_request.call_args
        assert call_kwargs.kwargs.get("messages") == messages

    @pytest.mark.asyncio
    async def test_messages_none_by_default(self, router, mock_registry):
        """When messages not provided, None is passed through."""
        await router.route(prompt="test")

        call_kwargs = mock_registry.route_request.call_args
        assert call_kwargs.kwargs.get("messages") is None

    @pytest.mark.asyncio
    async def test_tools_and_tool_choice_passed(self, router, mock_registry):
        """Tools and tool_choice pass through to registry."""
        tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]

        await router.route(prompt="test", tools=tools, tool_choice="auto")

        call_kwargs = mock_registry.route_request.call_args
        assert call_kwargs.kwargs.get("tools") == tools
        assert call_kwargs.kwargs.get("tool_choice") == "auto"

    @pytest.mark.asyncio
    async def test_response_format_passed(self, router, mock_registry):
        """response_format passes through to registry."""
        rf = {"type": "json_schema", "json_schema": {"name": "Test", "schema": {}}}

        await router.route(prompt="test", response_format=rf)

        call_kwargs = mock_registry.route_request.call_args
        assert call_kwargs.kwargs.get("response_format") == rf


class TestCacheBypassWithMessages:
    """Verify cache is skipped when messages are provided."""

    @pytest.mark.asyncio
    async def test_cache_bypassed_when_messages_provided(self, router, mock_registry):
        """Multi-turn conversations with messages should bypass cache."""
        router.cache = MagicMock()
        router.cache.check = MagicMock(return_value=None)

        messages = [{"role": "user", "content": "hello"}]
        await router.route(prompt="test", model="gpt-4", messages=messages)

        # Cache.check should NOT be called when messages is provided
        router.cache.check.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_checked_without_messages(self, router, mock_registry):
        """Single-shot requests without messages should check cache."""
        router.cache = MagicMock()
        router.cache.check = MagicMock(return_value=None)
        router.cache.store = MagicMock()

        await router.route(prompt="test", model="gpt-4")

        # Cache.check SHOULD be called for single-shot requests
        router.cache.check.assert_called_once()

    @pytest.mark.asyncio
    async def test_cached_response_returned_without_messages(
        self, router, mock_registry
    ):
        """Cached response is returned when available (no messages)."""
        cached = LLMResponse(
            content="cached",
            confidence=0.95,
            provider="openai",
            model="gpt-4",
            tokens_used=5,
            response_time_ms=1,
            cached=True,
        )
        router.cache = MagicMock()
        router.cache.check = MagicMock(return_value=cached)

        result = await router.route(prompt="test", model="gpt-4")

        assert result.content == "cached"
        assert result.cached is True
        # Registry should NOT be called when cache hit
        mock_registry.route_request.assert_not_called()


class TestGenerateForwardsMessages:
    """Verify generate() (ILLMProvider interface) forwards messages to route()."""

    @pytest.mark.asyncio
    async def test_generate_extracts_and_forwards_messages(self, router, mock_registry):
        """generate(**kwargs) should extract messages and pass to route()."""
        messages = [
            {"role": "user", "content": "search for errors"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search_file", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "found 3 errors"},
        ]

        await router.generate(
            prompt="",
            messages=messages,
            tools=[
                {
                    "type": "function",
                    "function": {"name": "search_file", "parameters": {}},
                }
            ],
            tool_choice="auto",
            max_tokens=4000,
        )

        call_kwargs = mock_registry.route_request.call_args
        assert call_kwargs.kwargs.get("messages") == messages
        assert call_kwargs.kwargs.get("tools") is not None
        assert call_kwargs.kwargs.get("tool_choice") == "auto"

    @pytest.mark.asyncio
    async def test_generate_without_messages_wraps_prompt(self, router, mock_registry):
        """generate() without messages passes prompt normally."""
        await router.generate(prompt="What is 2+2?", max_tokens=100)

        call_kwargs = mock_registry.route_request.call_args
        assert call_kwargs.kwargs.get("messages") is None
        # Prompt should be the sanitized version passed through
        assert "prompt" in call_kwargs.kwargs or len(call_kwargs.args) > 0
