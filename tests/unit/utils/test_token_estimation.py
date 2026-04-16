"""Tests for token_estimation — provider routing, fallback behavior, prompt breakdown."""

from unittest.mock import MagicMock, patch

import pytest

from faultmaven.utils.token_estimation import (
    estimate_prompt_tokens,
    estimate_tokens,
    estimate_tokens_fallback,
    estimate_tokens_openai,
)


class TestEstimateTokensFallback:
    """Test character-based fallback: ~4 chars per token, minimum 1."""

    def test_empty_string_returns_zero_via_main(self):
        assert estimate_tokens("", provider="local") == 0

    def test_short_text_minimum_one(self):
        assert estimate_tokens_fallback("hi") == 1  # 2 // 4 = 0 → max(1, 0) = 1

    def test_reasonable_estimate_for_english(self):
        text = "This is a sample sentence with approximately forty characters."
        tokens = estimate_tokens_fallback(text)
        # ~60 chars / 4 = ~15 tokens
        assert 10 <= tokens <= 20


class TestEstimateTokensOpenAI:
    """Test OpenAI token estimation via tiktoken."""

    def test_basic_counting(self):
        # tiktoken should be available in the test environment
        tokens = estimate_tokens_openai("Hello world")
        assert isinstance(tokens, int)
        assert tokens >= 1

    def test_longer_text_scales(self):
        short = estimate_tokens_openai("Hello")
        long = estimate_tokens_openai("Hello " * 100)
        assert long > short

    def test_fallback_when_encoder_unavailable(self):
        with patch(
            "faultmaven.utils.token_estimation._get_tiktoken_encoder",
            return_value=None,
        ):
            tokens = estimate_tokens_openai("Hello world test string")
            # Fallback: len // 4
            assert tokens == len("Hello world test string") // 4


class TestEstimateTokensRouting:
    """Test the main estimate_tokens() router dispatches to correct provider."""

    def test_routes_to_openai(self):
        with patch(
            "faultmaven.utils.token_estimation.estimate_tokens_openai", return_value=5
        ) as mock:
            result = estimate_tokens("hello", provider="openai")
            assert result == 5
            mock.assert_called_once()

    def test_routes_openrouter_to_openai(self):
        with patch(
            "faultmaven.utils.token_estimation.estimate_tokens_openai", return_value=5
        ) as mock:
            result = estimate_tokens("hello", provider="openrouter")
            assert result == 5
            mock.assert_called_once()

    def test_routes_to_anthropic(self):
        with patch(
            "faultmaven.utils.token_estimation.estimate_tokens_anthropic",
            return_value=3,
        ) as mock:
            result = estimate_tokens("hello", provider="anthropic")
            assert result == 3
            mock.assert_called_once()

    def test_routes_to_fireworks(self):
        with patch(
            "faultmaven.utils.token_estimation.estimate_tokens_fireworks",
            return_value=4,
        ) as mock:
            result = estimate_tokens("hello", provider="fireworks")
            assert result == 4
            mock.assert_called_once()

    def test_routes_unknown_to_fallback(self):
        with patch(
            "faultmaven.utils.token_estimation.estimate_tokens_fallback",
            return_value=2,
        ) as mock:
            result = estimate_tokens("hello", provider="cohere")
            assert result == 2
            mock.assert_called_once()

    def test_empty_text_returns_zero(self):
        assert estimate_tokens("", provider="openai") == 0
        assert estimate_tokens("", provider="anthropic") == 0

    def test_provider_case_insensitive(self):
        with patch(
            "faultmaven.utils.token_estimation.estimate_tokens_openai", return_value=5
        ) as mock:
            estimate_tokens("hello", provider="OpenAI")
            mock.assert_called_once()


class TestEstimatePromptTokens:
    """Test prompt token breakdown for monitoring and optimization."""

    def test_basic_breakdown(self):
        result = estimate_prompt_tokens(
            system_prompt="You are helpful",
            user_message="Hello",
            provider="local",  # Use fallback for deterministic results
        )
        assert "system" in result
        assert "user" in result
        assert "history" in result
        assert "total" in result
        assert result["total"] == result["system"] + result["user"] + result["history"]

    def test_empty_history_is_zero(self):
        result = estimate_prompt_tokens(
            system_prompt="You are helpful",
            user_message="Hello",
            conversation_history="",
            provider="local",
        )
        assert result["history"] == 0

    def test_history_included_in_total(self):
        without_history = estimate_prompt_tokens(
            system_prompt="X" * 100,
            user_message="Y" * 100,
            conversation_history="",
            provider="local",
        )
        with_history = estimate_prompt_tokens(
            system_prompt="X" * 100,
            user_message="Y" * 100,
            conversation_history="Z" * 400,
            provider="local",
        )
        assert with_history["total"] > without_history["total"]
        assert with_history["history"] > 0

    def test_passes_model_to_provider(self):
        with patch(
            "faultmaven.utils.token_estimation.estimate_tokens",
            wraps=estimate_tokens,
        ) as mock:
            estimate_prompt_tokens(
                system_prompt="system",
                user_message="user",
                provider="openai",
                model="gpt-4o",
            )
            # Verify model was passed through
            for call in mock.call_args_list:
                assert (
                    call[1].get("model") == "gpt-4o"
                    or call[0][2] == "gpt-4o"
                    or call.kwargs.get("model") == "gpt-4o"
                )
