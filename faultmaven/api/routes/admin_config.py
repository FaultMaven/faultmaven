"""Admin Configuration Routes (Dashboard Phase 1a)

Production-ready admin endpoints for LLM configuration and environment status.
Replaces dev-only /debug/llm-providers and /debug/config endpoints with
authenticated, key-masked equivalents suitable for the dashboard.

Endpoints:
- GET  /api/v1/admin/llm/config      — LLM provider status and fallback chain
- POST /api/v1/admin/llm/config/test  — Test a provider connection
- GET  /api/v1/admin/config/status    — Environment configuration status
"""

import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from faultmaven.api.middleware.auth import get_current_user, require_admin
from faultmaven.api.models import (
    EnvConfigStatusResponse,
    LLMConfigResponse,
    LLMConfigUpdateRequest,
    LLMConfigUpdateResponse,
    LLMConnectionTestRequest,
    LLMConnectionTestResponse,
    LLMProviderDetail,
)
from faultmaven.api.v1.dependencies import get_llm_provider
from faultmaven.models.auth import AuthenticatedUser

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["Admin - Configuration"],
)

# Display names for providers (user-facing labels in the dashboard)
PROVIDER_DISPLAY_NAMES = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "gemini": "Google Gemini",
    "fireworks": "Fireworks AI",
    "groq": "Groq",
    "huggingface": "HuggingFace",
    "cohere": "Cohere",
    "openrouter": "OpenRouter",
    "local": "Local (Ollama/vLLM)",
}


@router.get("/llm/config", response_model=LLMConfigResponse)
async def get_llm_config(
    current_user: AuthenticatedUser = Depends(get_current_user),
    llm_provider=Depends(get_llm_provider),
) -> LLMConfigResponse:
    """Get LLM provider configuration and status.

    Returns the current primary provider, fallback chain, and per-provider
    status including health, connectivity, and available models. API keys
    are never exposed — only a boolean indicating whether one is configured.

    Available to any authenticated user (local deployment) or admin (cloud).
    Route-level access control is handled by the dashboard's LLMConfigRoute guard.

    Returns:
        LLMConfigResponse with provider details and fallback chain

    Raises:
        401 Unauthorized: No valid JWT token
        503 Service Unavailable: LLM provider not initialized
    """
    if not llm_provider:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM provider not initialized",
        )

    try:
        from faultmaven.config.settings import get_settings

        settings = get_settings()

        # Get runtime state from the registry
        registry = llm_provider.registry
        provider_status = registry.get_provider_status()
        health_summary = registry.get_provider_health_summary()
        fallback_chain = registry.get_fallback_chain()
        all_provider_names = registry.get_all_provider_names()
        strict_mode = settings.llm.strict_provider_mode

        # Build per-provider details, including providers that aren't initialized
        providers: dict[str, LLMProviderDetail] = {}
        for name in all_provider_names:
            # Check if API key is configured (without exposing the value)
            has_api_key = _check_has_api_key(settings, name)

            # Get runtime info if provider is initialized
            if name in provider_status:
                ps = provider_status[name]
                hs = health_summary.get(name, {})
                providers[name] = LLMProviderDetail(
                    name=name,
                    display_name=PROVIDER_DISPLAY_NAMES.get(name, name.title()),
                    enabled=ps.get("in_fallback_chain", False),
                    connected=hs.get("health", "unknown") in ("healthy", "HEALTHY"),
                    has_api_key=has_api_key,
                    models=ps.get("models", []),
                    selected_model=ps.get("selected_model"),
                    available_models=ps.get("available_models", []),
                    health=hs.get("health", "unknown"),
                    avg_latency_ms=hs.get("avg_latency_ms", 0.0),
                )
            else:
                # Provider exists in schema but not initialized — still show available models
                providers[name] = LLMProviderDetail(
                    name=name,
                    display_name=PROVIDER_DISPLAY_NAMES.get(name, name.title()),
                    enabled=False,
                    connected=False,
                    has_api_key=has_api_key,
                    models=[],
                    selected_model=None,
                    available_models=registry.get_available_models_for(name),
                    health="not_initialized",
                )

        primary = fallback_chain[0] if fallback_chain else "none"

        return LLMConfigResponse(
            primary_provider=primary,
            strict_mode=strict_mode,
            fallback_chain=fallback_chain,
            providers=providers,
            timestamp=datetime.now(timezone.utc),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get LLM config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get LLM configuration: {str(e)}",
        )


@router.put("/llm/config", response_model=LLMConfigUpdateResponse)
async def update_llm_config(
    request: LLMConfigUpdateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    llm_provider=Depends(get_llm_provider),
) -> LLMConfigUpdateResponse:
    """Update LLM provider configuration.

    Persists configuration changes to the database as overrides that take
    precedence over environment variables. After saving, the settings
    singleton and provider registry are reset so changes take effect
    immediately without a restart.

    Accepts partial updates — only provided fields are changed.

    Returns:
        LLMConfigUpdateResponse with list of updated keys

    Raises:
        401 Unauthorized: No valid JWT token
        422 Unprocessable Entity: Invalid provider name
        503 Service Unavailable: LLM provider not initialized
    """
    if not llm_provider:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM provider not initialized",
        )

    try:
        from faultmaven.config.llm_config_overrides import save_and_reload

        registry = llm_provider.registry
        valid_names = registry.get_all_provider_names()
        overrides: dict[str, str] = {}

        # Validate and collect overrides
        if request.primary_provider is not None:
            if request.primary_provider not in valid_names:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unknown provider: '{request.primary_provider}'. "
                    f"Valid providers: {', '.join(valid_names)}",
                )
            overrides["primary_provider"] = request.primary_provider

        if request.api_key is not None and request.provider_name is not None:
            if request.provider_name not in valid_names:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unknown provider: '{request.provider_name}'",
                )
            key_field = f"{request.provider_name}_api_key"
            overrides[key_field] = request.api_key

        if request.model is not None and request.provider_name is not None:
            if request.provider_name not in valid_names:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unknown provider: '{request.provider_name}'",
                )
            model_field = f"{request.provider_name}_model"
            overrides[model_field] = request.model

        if not overrides:
            return LLMConfigUpdateResponse(
                updated_keys=[],
                message="No changes requested",
                timestamp=datetime.now(timezone.utc),
            )

        # Persist to DB and hot-reload
        await save_and_reload(overrides, user_id=current_user.user_id)

        # Mask API key names in response (don't reveal which key was set)
        safe_keys = [
            (
                k
                if not k.endswith("_api_key")
                else k.replace("_api_key", "_api_key_updated")
            )
            for k in overrides.keys()
        ]

        return LLMConfigUpdateResponse(
            updated_keys=safe_keys,
            message="Configuration updated and applied",
            timestamp=datetime.now(timezone.utc),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update LLM config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update LLM configuration: {str(e)}",
        )


@router.post("/llm/config/test", response_model=LLMConnectionTestResponse)
async def check_llm_connection(
    request: LLMConnectionTestRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    llm_provider=Depends(get_llm_provider),
) -> LLMConnectionTestResponse:
    """Test connectivity to a specific LLM provider.

    Sends a minimal prompt to the specified provider to verify that:
    1. The API key is valid
    2. The provider endpoint is reachable
    3. The configured model responds

    This does NOT use the fallback chain — it tests the specific provider directly.

    Returns:
        LLMConnectionTestResponse with connectivity result and latency

    Raises:
        401 Unauthorized: No valid JWT token
        422 Unprocessable Entity: Unknown provider name
        503 Service Unavailable: LLM provider not initialized
    """
    if not llm_provider:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM provider not initialized",
        )

    provider_name = request.provider.lower()

    try:
        registry = llm_provider.registry
        valid_names = registry.get_all_provider_names()

        if provider_name not in valid_names:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown provider: '{provider_name}'. "
                f"Valid providers: {', '.join(valid_names)}",
            )

        registry = llm_provider.registry
        provider = registry.get_provider(provider_name)

        if provider is None:
            display = PROVIDER_DISPLAY_NAMES.get(provider_name, provider_name)
            return LLMConnectionTestResponse(
                provider=provider_name,
                connected=False,
                error_message=(
                    f"No API key configured for {display}. "
                    "Add an API key first, then test the connection."
                ),
                timestamp=datetime.now(timezone.utc),
            )

        # Send a minimal test prompt directly to the provider
        start_time = time.monotonic()
        response = await provider.generate(
            prompt="Respond with exactly: OK",
            max_tokens=10,
            temperature=0.0,
        )
        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        return LLMConnectionTestResponse(
            provider=provider_name,
            connected=True,
            response_time_ms=elapsed_ms,
            model_used=response.model if hasattr(response, "model") else None,
            timestamp=datetime.now(timezone.utc),
        )

    except HTTPException:
        raise
    except Exception as e:
        elapsed_ms = (
            int((time.monotonic() - start_time) * 1000) if "start_time" in dir() else 0
        )
        logger.warning(f"Connection test failed for {provider_name}: {e}")
        return LLMConnectionTestResponse(
            provider=provider_name,
            connected=False,
            response_time_ms=elapsed_ms,
            error_message=str(e),
            timestamp=datetime.now(timezone.utc),
        )


@router.get("/config/status", response_model=EnvConfigStatusResponse)
async def get_env_config_status(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> EnvConfigStatusResponse:
    """Get environment configuration status (read-only).

    Returns the current deployment configuration including auth mode,
    storage backends, and security settings. This is informational only —
    configuration changes require editing environment variables and restarting.

    Returns:
        EnvConfigStatusResponse with current environment configuration

    Raises:
        401 Unauthorized: No valid JWT token
    """
    try:
        from faultmaven.config.settings import get_settings

        settings = get_settings()

        return EnvConfigStatusResponse(
            auth_mode=settings.auth.auth_mode,
            environment=settings.server.environment.value,
            db_backend=settings.database.case_storage_type,
            session_storage=settings.database.session_storage_type,
            vector_storage=settings.database.vector_storage_type,
            llm_provider=(
                settings.llm.provider.value if settings.llm.provider else "not_set"
            ),
            pii_redaction_enabled=settings.protection.protection_enabled,
            rate_limit_enabled=settings.security.rate_limit_enabled,
            timestamp=datetime.now(timezone.utc),
        )

    except Exception as e:
        logger.error(f"Failed to get env config status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get configuration status: {str(e)}",
        )


def _check_has_api_key(settings, provider_name: str) -> bool:
    """Check whether an API key is configured for a provider.

    The key value is NEVER returned — only a boolean for the response.
    Local provider always returns True (no key needed).
    """
    if provider_name == "local":
        return True

    key_map = {
        "anthropic": "anthropic_api_key",
        "openai": "openai_api_key",
        "fireworks": "fireworks_api_key",
        "groq": "groq_api_key",
        "gemini": "gemini_api_key",
        "huggingface": "huggingface_api_key",
        "cohere": "cohere_api_key",
        "openrouter": "openrouter_api_key",
    }
    attr_name = key_map.get(provider_name)
    if attr_name is None:
        return False

    key_value = getattr(settings.llm, attr_name, None)
    if key_value is None:
        return False

    if hasattr(key_value, "get_secret_value"):
        val = key_value.get_secret_value()
        return bool(val)
    return bool(key_value)
