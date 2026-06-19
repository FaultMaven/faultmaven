"""Startup fail-fast for LLM provider credentials.

FaultMaven has **no default LLM provider**: every model needs a credential, so a
silent default (historically ``fireworks``) is meaningless — if the operator
supplied, say, an OpenAI key but never set ``CHAT_PROVIDER``, the silent default
would try Fireworks and fail at first use, deep in a turn, with an opaque error.

This module enforces the minimum viable LLM config **at startup** instead:

1. ``CHAT_PROVIDER`` must be **explicitly set** (env or preset) — there is no
   implicit provider.
2. The chosen provider's credential must be present — an API key for cloud
   providers, or ``LOCAL_LLM_URL`` for ``local``.

Either failure raises ``ValueError`` so the lifespan config gate aborts boot
with an actionable message. The gate is skipped under ``SKIP_SERVICE_CHECKS``
(tests / zero-config dev), matching the other startup service gates.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from faultmaven.config.settings import Settings

logger = logging.getLogger(__name__)


# Cloud provider → (settings field holding the SecretStr key, env var name).
# ``local`` is handled separately (it needs a URL, not a key).
_PROVIDER_API_KEY: dict[str, tuple[str, str]] = {
    "openai": ("openai_api_key", "OPENAI_API_KEY"),
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    "gemini": ("gemini_api_key", "GEMINI_API_KEY"),
    "fireworks": ("fireworks_api_key", "FIREWORKS_API_KEY"),
    "groq": ("groq_api_key", "GROQ_API_KEY"),
    "huggingface": ("huggingface_api_key", "HUGGINGFACE_API_KEY"),
    "cohere": ("cohere_api_key", "COHERE_API_KEY"),
    "openrouter": ("openrouter_api_key", "OPENROUTER_API_KEY"),
}

_SUPPORTED = sorted(list(_PROVIDER_API_KEY) + ["local"])


def _secret_present(value) -> bool:
    """True if a SecretStr / str credential is set and non-empty."""
    if value is None:
        return False
    getter = getattr(value, "get_secret_value", None)
    raw = getter() if callable(getter) else value
    return bool(raw and str(raw).strip())


def validate_llm_provider_credentials(settings: "Settings") -> None:
    """Fail fast unless a provider is explicitly chosen and its credential is set.

    Raises ``ValueError`` (caught by the lifespan config gate, which aborts
    startup) with an actionable message. No-op return on success.
    """
    # 1. CHAT_PROVIDER must be explicitly provided — no implicit default.
    if not (os.getenv("CHAT_PROVIDER") or "").strip():
        raise ValueError(
            "CHAT_PROVIDER is not set. FaultMaven has no default LLM provider — "
            "you must choose one and supply its credential. Set CHAT_PROVIDER to "
            f"one of: {', '.join(_SUPPORTED)} (and the matching API key, or "
            "LOCAL_LLM_URL for 'local'). See .env.example section 1."
        )

    provider = settings.llm.provider.value.lower()

    # 2a. Local: needs a URL, not a key.
    if provider == "local":
        if not (settings.llm.local_url or "").strip():
            raise ValueError(
                "CHAT_PROVIDER=local requires LOCAL_LLM_URL to point at your "
                "Ollama/vLLM server (e.g. http://host.docker.internal:11434). "
                "See .env.example section 1."
            )
        logger.info("✅ LLM provider 'local' configured (LOCAL_LLM_URL set)")
        return

    # 2b. Cloud providers: need the matching API key.
    mapping = _PROVIDER_API_KEY.get(provider)
    if mapping is None:
        raise ValueError(
            f"CHAT_PROVIDER={provider!r} is not a supported provider. "
            f"Choose one of: {', '.join(_SUPPORTED)}."
        )
    field, env_name = mapping
    if not _secret_present(getattr(settings.llm, field, None)):
        raise ValueError(
            f"CHAT_PROVIDER={provider} requires {env_name} to be set, but it is "
            "missing or empty. Supply the API key for your chosen provider (or "
            "switch CHAT_PROVIDER to a provider whose key you have). See "
            ".env.example section 1."
        )
    logger.info("✅ LLM provider '%s' configured (%s set)", provider, env_name)
