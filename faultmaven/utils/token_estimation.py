"""Token Estimation Utility

Provides accurate token counting for different LLM providers using their
official tokenizers instead of rough estimates.

Supported Providers:
- OpenAI (tiktoken cl100k_base for GPT models)
- Anthropic (tiktoken cl100k_base as an offline proxy — see
  ``estimate_tokens_anthropic`` for why we do not call the network counter)
- Fireworks (uses tiktoken since many models are OpenAI-compatible)
- Fallback (character-based estimation for unsupported providers)

Usage:
    >>> from faultmaven.utils.token_estimation import estimate_tokens
    >>> tokens = estimate_tokens("Hello world", provider="openai")
    >>> 2
"""

import logging
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)


# Try importing provider-specific tokenizers
try:
    import tiktoken

    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    logger.warning(
        "tiktoken not installed - falling back to character-based estimation "
        "for OpenAI/Fireworks/Anthropic"
    )


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
            logger.warning(
                f"tiktoken encoding failed: {e}, falling back to char estimate"
            )

    # Fallback: rough estimate (4 chars per token for English)
    return len(text) // 4


def estimate_tokens_anthropic(text: str, model: str = "claude-sonnet-4-6") -> int:
    """Estimate tokens for Anthropic (Claude) models.

    Counting strategy (offline-first, by design — see GAP-4):

    1. **tiktoken ``cl100k_base`` as an offline proxy.** The modern Anthropic
       SDK removed the local ``client.count_tokens`` helper; the only exact
       counter is ``messages.count_tokens``, which is a *network* round-trip
       and needs a real API key. Adding a per-item network call to every turn
       is the latency hazard GAP-4 explicitly warns against, so we use the
       cl100k BPE as a fast, local proxy. It tracks the Claude tokenizer within
       a small margin on the prose/log/JSON/code content FaultMaven assembles —
       far closer than the 4-chars heuristic.
    2. **Char fallback** only when tiktoken is unavailable.

    Args:
        text: Input text to tokenize
        model: Anthropic model name (accepted for API symmetry; cl100k is used
            regardless of the specific Claude model)

    Returns:
        Number of tokens
    """
    encoder = _get_tiktoken_encoder("gpt-4")  # cl100k_base — offline proxy
    if encoder:
        try:
            return len(encoder.encode(text))
        except Exception as e:
            logger.warning(
                f"Anthropic (cl100k proxy) token counting failed: {e}, "
                "falling back to char estimate"
            )

    # Fallback: rough estimate (4 chars per token)
    return len(text) // 4


def estimate_tokens_fireworks(
    text: str, model: str = "accounts/fireworks/models/deepseek-v3"
) -> int:
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
    text: str, provider: str = "openai", model: Optional[str] = None
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
        >>> estimate_tokens("Hello world", provider="anthropic", model="claude-sonnet-4-6")
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
        return estimate_tokens_anthropic(text, model or "claude-sonnet-4-6")
    elif provider == "fireworks":
        return estimate_tokens_fireworks(
            text, model or "accounts/fireworks/models/deepseek-v3"
        )
    else:
        # Fallback for local, cohere, and unknown providers
        return estimate_tokens_fallback(text)


def estimate_prompt_tokens(
    system_prompt: str,
    user_message: str,
    conversation_history: str = "",
    provider: str = "openai",
    model: Optional[str] = None,
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
    history_tokens = (
        estimate_tokens(conversation_history, provider, model)
        if conversation_history
        else 0
    )

    return {
        "system": system_tokens,
        "user": user_tokens,
        "history": history_tokens,
        "total": system_tokens + user_tokens + history_tokens,
    }
