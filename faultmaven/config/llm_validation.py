"""Startup fail-fast for LLM provider credentials.

FaultMaven ships a **recommended** provider (``CHAT_PROVIDER=gemini``, mirrored
in ``.env.example``), but no credential can ship with it — so the recommendation
is never a silent fallback: an operator who supplied, say, an OpenAI key but
never set ``CHAT_PROVIDER`` must not have the default quietly try Gemini and
fail at first use, deep in a turn, with an opaque error.

This module enforces the minimum viable LLM config **at startup** instead:

1. The resolved provider's credential must be present — an API key for cloud
   providers, or ``LOCAL_LLM_URL`` for ``local``. An operator who sets nothing
   resolves to the shipped default and is rejected here for the missing key,
   with a message naming the provider actually in effect.

Either failure raises ``ValueError`` so the lifespan config gate aborts boot
with an actionable message. The gate is skipped under ``SKIP_SERVICE_CHECKS``
(tests / zero-config dev), matching the other startup service gates.
"""

from __future__ import annotations

import logging
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
    """Fail fast unless the active provider has a usable credential.

    Validates the **resolved** provider (``settings.llm.provider``, which honors
    a literal ``CHAT_PROVIDER`` env var AND a preset/container-injected value) —
    not ``os.getenv("CHAT_PROVIDER")``, which would false-positive on
    preset-configured deployments. There is still no usable default: an
    unconfigured deployment resolves to the placeholder provider with no
    credential and is rejected here. Raises ``ValueError`` (caught by the
    lifespan gate, which aborts startup) with an actionable message; no-op on
    success.
    """
    provider = settings.llm.provider.value.lower()

    # Local: needs a URL, not a key.
    if provider == "local":
        if not (settings.llm.local_url or "").strip():
            raise ValueError(
                "CHAT_PROVIDER=local requires LOCAL_LLM_URL to point at your "
                "Ollama/vLLM server (e.g. http://host.docker.internal:11434). "
                "See .env.example section 1."
            )
        logger.info("✅ LLM provider 'local' configured (LOCAL_LLM_URL set)")
        return

    # Cloud providers: need the matching API key.
    mapping = _PROVIDER_API_KEY.get(provider)
    if mapping is None:
        raise ValueError(
            f"CHAT_PROVIDER={provider!r} is not a supported provider. "
            f"Choose one of: {', '.join(_SUPPORTED)}."
        )
    field, env_name = mapping
    if not _secret_present(getattr(settings.llm, field, None)):
        raise ValueError(
            f"No usable LLM credential: the active provider is {provider!r} but "
            f"{env_name} is missing or empty. Set CHAT_PROVIDER to a provider "
            "whose API key you have (or supply this one's key / LOCAL_LLM_URL "
            "for 'local'). A provider is shipped as the default, but no "
            "credential is — one must always be configured. See .env.example "
            "section 1."
        )
    logger.info("✅ LLM provider '%s' configured (%s set)", provider, env_name)
