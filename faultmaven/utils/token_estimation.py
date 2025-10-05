"""Token Estimation Utility

Provides accurate token counting for different LLM providers using their
official tokenizers instead of rough estimates.

Supported Providers:
- OpenAI (tiktoken for GPT models)
- Anthropic (anthropic tokenizer for Claude models)
- Fireworks (uses tiktoken since many models are OpenAI-compatible)
- Fallback (character-based estimation for unsupported providers)

Usage:
    >>> from faultmaven.utils.token_estimation import estimate_tokens
    >>> tokens = estimate_tokens("Hello world", provider="openai")
    >>> 2
"""

import logging
from typing import Optional
from functools import lru_cache

logger = logging.getLogger(__name__)


# Try importing provider-specific tokenizers
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    logger.warning("tiktoken not installed - falling back to character-based estimation for OpenAI/Fireworks")

try:
    from anthropic import Anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    logger.warning("anthropic not installed - falling back to character-based estimation for Claude")


@lru_cache(maxsize=10)
def _get_tiktoken_encoder(model: str = "gpt-4"):
    """Get cached tiktoken encoder for OpenAI models

    Args:
        model: Model name (e.g., "gpt-4", "gpt-3.5-turbo")

    Returns:
        tiktoken.Encoding instance
    """
    if not TIKTOKEN_AVAILABLE:
        return None

    try:
        # Map model names to encodings
        if "gpt-4" in model.lower():
            encoding_name = "cl100k_base"
        elif "gpt-3.5" in model.lower():
            encoding_name = "cl100k_base"
        else:
            encoding_name = "cl100k_base"  # Default for modern models

        return tiktoken.get_encoding(encoding_name)
    except Exception as e:
        logger.warning(f"Failed to get tiktoken encoder for {model}: {e}")
        return None


@lru_cache(maxsize=10)
def _get_anthropic_client():
    """Get cached Anthropic client for token counting

    Returns:
        Anthropic client instance or None
    """
    if not ANTHROPIC_AVAILABLE:
        return None

    try:
        # Anthropic client doesn't need API key for counting tokens
        return Anthropic(api_key="dummy")
    except Exception as e:
        logger.warning(f"Failed to create Anthropic client: {e}")
        return None


def estimate_tokens_openai(text: str, model: str = "gpt-4") -> int:
    """Estimate tokens for OpenAI models using tiktoken

    Args:
        text: Input text to tokenize
        model: OpenAI model name

    Returns:
        Number of tokens
    """
    encoder = _get_tiktoken_encoder(model)
    if encoder:
        try:
            return len(encoder.encode(text))
        except Exception as e:
            logger.warning(f"tiktoken encoding failed: {e}, falling back to char estimate")

    # Fallback: rough estimate (4 chars per token for English)
    return len(text) // 4


def estimate_tokens_anthropic(text: str, model: str = "claude-3-sonnet-20240229") -> int:
    """Estimate tokens for Anthropic models using official tokenizer

    Args:
        text: Input text to tokenize
        model: Anthropic model name

    Returns:
        Number of tokens
    """
    client = _get_anthropic_client()
    if client:
        try:
            return client.count_tokens(text)
        except Exception as e:
            logger.warning(f"Anthropic token counting failed: {e}, falling back to char estimate")

    # Fallback: rough estimate (4 chars per token)
    return len(text) // 4


def estimate_tokens_fireworks(text: str, model: str = "llama-v3p1-405b-instruct") -> int:
    """Estimate tokens for Fireworks models

    Many Fireworks models use OpenAI-compatible tokenization, so we use tiktoken.

    Args:
        text: Input text to tokenize
        model: Fireworks model name

    Returns:
        Number of tokens
    """
    # Fireworks often uses OpenAI-compatible models
    return estimate_tokens_openai(text, model="gpt-4")


def estimate_tokens_fallback(text: str) -> int:
    """Fallback token estimation for unsupported providers

    Uses simple character-based heuristic: ~4 characters per token for English.

    Args:
        text: Input text to tokenize

    Returns:
        Estimated number of tokens
    """
    return max(1, len(text) // 4)


def estimate_tokens(
    text: str,
    provider: str = "openai",
    model: Optional[str] = None
) -> int:
    """Estimate token count for given text and provider

    Uses provider-specific tokenizers when available, falls back to character-based
    estimation for unsupported providers.

    Args:
        text: Input text to tokenize
        provider: LLM provider name ("openai", "anthropic", "fireworks", "local", etc.)
        model: Optional specific model name for more accurate counting

    Returns:
        Number of tokens

    Examples:
        >>> estimate_tokens("Hello world", provider="openai")
        2
        >>> estimate_tokens("Hello world", provider="anthropic", model="claude-3-sonnet-20240229")
        2
        >>> estimate_tokens("Hello world", provider="fireworks")
        2
    """
    if not text:
        return 0

    provider = provider.lower()

    # Route to provider-specific estimator
    if provider in ("openai", "openrouter"):
        return estimate_tokens_openai(text, model or "gpt-4")
    elif provider == "anthropic":
        return estimate_tokens_anthropic(text, model or "claude-3-sonnet-20240229")
    elif provider == "fireworks":
        return estimate_tokens_fireworks(text, model or "llama-v3p1-405b-instruct")
    else:
        # Fallback for local, cohere, and unknown providers
        return estimate_tokens_fallback(text)


def estimate_prompt_tokens(
    system_prompt: str,
    user_message: str,
    conversation_history: str = "",
    provider: str = "openai",
    model: Optional[str] = None
) -> dict:
    """Estimate total tokens for a complete prompt assembly

    Breaks down token counts by component for monitoring and optimization.

    Args:
        system_prompt: System instructions
        user_message: Current user query
        conversation_history: Previous conversation context
        provider: LLM provider name
        model: Optional specific model name

    Returns:
        Dictionary with token breakdown:
        {
            "system": int,
            "user": int,
            "history": int,
            "total": int
        }

    Examples:
        >>> estimate_prompt_tokens(
        ...     system_prompt="You are a helpful assistant",
        ...     user_message="Hello",
        ...     provider="openai"
        ... )
        {'system': 6, 'user': 1, 'history': 0, 'total': 7}
    """
    system_tokens = estimate_tokens(system_prompt, provider, model)
    user_tokens = estimate_tokens(user_message, provider, model)
    history_tokens = estimate_tokens(conversation_history, provider, model) if conversation_history else 0

    return {
        "system": system_tokens,
        "user": user_tokens,
        "history": history_tokens,
        "total": system_tokens + user_tokens + history_tokens
    }
