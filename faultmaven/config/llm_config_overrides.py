"""Apply LLM configuration overrides from the database to the settings singleton.

Overrides stored in the llm_config_overrides table take precedence over
environment variables. This module bridges the DB-stored overrides and
the pydantic-settings singleton.

Usage:
    from faultmaven.config.llm_config_overrides import apply_overrides_to_settings

    # After get_settings() returns, apply DB overrides
    settings = get_settings()
    await apply_overrides_to_settings(settings)
"""

import logging

from pydantic import SecretStr

logger = logging.getLogger(__name__)

# Maps override keys to (settings attribute path, type converter)
# Only keys in this map are accepted — prevents arbitrary setting injection.
_ALLOWED_OVERRIDES = {
    "primary_provider": "provider",
    "strict_provider_mode": "strict_provider_mode",
    "anthropic_api_key": "anthropic_api_key",
    "openai_api_key": "openai_api_key",
    "fireworks_api_key": "fireworks_api_key",
    "groq_api_key": "groq_api_key",
    "gemini_api_key": "gemini_api_key",
    "huggingface_api_key": "huggingface_api_key",
    "cohere_api_key": "cohere_api_key",
    "openrouter_api_key": "openrouter_api_key",
    # Model overrides per provider
    "anthropic_model": "anthropic_model",
    "openai_model": "openai_model",
    "fireworks_model": "fireworks_model",
    "groq_model": "groq_model",
    "gemini_model": "gemini_model",
    "huggingface_model": "huggingface_model",
    "cohere_model": "cohere_chat_model",
    "openrouter_model": "openrouter_model",
    "local_model": "local_model",
}

_API_KEY_FIELDS = {
    "anthropic_api_key",
    "openai_api_key",
    "fireworks_api_key",
    "groq_api_key",
    "gemini_api_key",
    "huggingface_api_key",
    "cohere_api_key",
    "openrouter_api_key",
}


async def apply_overrides_to_settings(settings) -> None:
    """Read overrides from DB and apply them to the LLM settings object.

    Uses object.__setattr__ to bypass pydantic frozen model validation.
    Only applies keys listed in _ALLOWED_OVERRIDES.
    """
    try:
        from faultmaven.infrastructure.persistence.llm_config_repository import (
            get_all_overrides,
        )

        overrides = await get_all_overrides()
    except Exception as e:
        # DB may not be ready (first startup, migrations pending)
        logger.debug(f"Could not read LLM config overrides: {e}")
        return

    if not overrides:
        return

    llm = settings.llm
    applied = []

    for key, value in overrides.items():
        if key not in _ALLOWED_OVERRIDES:
            logger.warning(f"Ignoring unknown LLM config override: {key}")
            continue

        attr_name = _ALLOWED_OVERRIDES[key]

        try:
            if key == "primary_provider":
                from faultmaven.config.settings import LLMProvider

                object.__setattr__(llm, attr_name, LLMProvider(value))
            elif key == "strict_provider_mode":
                object.__setattr__(llm, attr_name, value.lower() in ("true", "1"))
            elif key in _API_KEY_FIELDS:
                object.__setattr__(llm, attr_name, SecretStr(value))
            else:
                object.__setattr__(llm, attr_name, value)
            applied.append(key)
        except Exception as e:
            logger.warning(f"Failed to apply LLM config override {key}: {e}")

    if applied:
        logger.info(f"Applied LLM config overrides: {applied}")


async def reload_llm_config() -> None:
    """Hot-reload LLM configuration from DB overrides.

    Call after writing new overrides to the database. This:
    1. Re-reads overrides from DB
    2. Resets the settings singleton (forces env var reload)
    3. Applies overrides on top of fresh env vars
    4. Resets the provider registry (forces re-initialization)

    In-flight LLM requests may fail during the brief reset window.
    The registry will re-initialize lazily on the next request.
    """
    from faultmaven.config.settings import get_settings, reset_settings
    from faultmaven.infrastructure.llm.providers.registry import reset_registry

    # Reset settings to force fresh env var read
    reset_settings()

    # Get fresh settings and apply DB overrides
    settings = get_settings()
    await apply_overrides_to_settings(settings)

    # Reset registry so it picks up the updated settings
    reset_registry()

    logger.info("LLM configuration reloaded from overrides")


async def save_and_reload(
    overrides: dict[str, str], user_id: str | None = None
) -> None:
    """Persist overrides to DB and hot-reload the LLM configuration.

    This is the single entry point for config writes — API routes should
    call this instead of importing the repository directly.
    """
    from faultmaven.infrastructure.persistence.llm_config_repository import (
        set_overrides,
    )

    await set_overrides(overrides, user_id=user_id)
    await reload_llm_config()
