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

from fastapi import APIRouter, Depends, HTTPException, Request, status

from faultmaven.api.middleware.auth import require_platform_admin
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
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser

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
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    llm_provider=Depends(get_llm_provider),
) -> LLMConfigResponse:
    """Get LLM provider configuration and status.

    Returns the current primary provider, fallback chain, and per-provider
    status including health, connectivity, and available models. API keys
    are never exposed — only a boolean indicating whether one is configured.

    Available to any authenticated user (standalone deployment) or admin (cloud).
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

        # Deployment mode determines dashboard behavior (canonical DEPLOYMENT_MODE, ADR-004)
        is_cloud = settings.is_cloud
        deployment = "cloud" if is_cloud else "standalone"
        config_readonly = not is_cloud

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
            is_in_chain = name in fallback_chain

            # Derive provider lifecycle state
            if not has_api_key:
                state = "not_configured"
            elif is_in_chain:
                state = "active"
            else:
                state = "configured"

            # Get runtime info if provider is initialized
            if name in provider_status:
                ps = provider_status[name]
                hs = health_summary.get(name, {})
                providers[name] = LLMProviderDetail(
                    name=name,
                    display_name=PROVIDER_DISPLAY_NAMES.get(name, name.title()),
                    enabled=is_in_chain,
                    connected=hs.get("health", "unknown") in ("healthy", "HEALTHY"),
                    has_api_key=has_api_key,
                    state=state,
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
                    state=state,
                    models=[],
                    selected_model=None,
                    available_models=registry.get_available_models_for(name),
                    health="not_initialized",
                )

        primary = fallback_chain[0] if fallback_chain else "none"

        # Per-setting provenance ('admin-override' vs 'env-default') — what lets
        # the admin dashboard show which source is active (the "two silent
        # sources of truth" fix). Computed in the config layer so the API route
        # stays free of infrastructure imports (architecture boundary).
        from faultmaven.config.llm_config_overrides import get_config_source_map

        config_sources = await get_config_source_map(is_cloud)

        return LLMConfigResponse(
            deployment=deployment,
            config_readonly=config_readonly,
            primary_provider=primary,
            strict_mode=strict_mode,
            fallback_chain=fallback_chain,
            providers=providers,
            config_sources=config_sources,
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
    current_user: AuthenticatedUser = Depends(require_platform_admin),
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
        403 Forbidden: Standalone deployment (config is read-only)
        422 Unprocessable Entity: Invalid provider name
        503 Service Unavailable: LLM provider not initialized
    """
    if not llm_provider:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM provider not initialized",
        )

    # Standalone: config is read-only (managed via .env). Cloud manages config in the DB.
    from faultmaven.config.settings import get_settings as _get_settings

    _settings = _get_settings()
    if not _settings.is_cloud:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Configuration is read-only in standalone deployment. Edit the .env file and restart the server.",
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
                    status_code=422,
                    detail=f"Unknown provider: '{request.primary_provider}'. "
                    f"Valid providers: {', '.join(valid_names)}",
                )
            overrides["primary_provider"] = request.primary_provider

        if request.api_key is not None and request.provider_name is not None:
            if request.provider_name not in valid_names:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown provider: '{request.provider_name}'",
                )
            key_field = f"{request.provider_name}_api_key"
            overrides[key_field] = request.api_key

        if request.model is not None and request.provider_name is not None:
            if request.provider_name not in valid_names:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown provider: '{request.provider_name}'",
                )
            model_field = f"{request.provider_name}_model"

            # Refuse a model that cannot serve the investigation engine's response
            # schemas. The startup gate (validate_structured_output_capacity)
            # covers boot, but this endpoint hot-reloads config afterwards, so
            # without the same check here an operator can swap in a model the
            # engine cannot drive and the deployment only discovers it several
            # turns into the next live investigation. 422 at the point of change
            # is the whole difference.
            # Reached through the injected ``llm_provider`` (same ``registry``
            # already used for ``valid_names`` above) rather than importing the
            # provider registry directly — the API layer must not import from
            # infrastructure, and a direct import trips the api-layer boundary
            # test even though it satisfies the import-linter contracts.
            provider_obj = None
            try:
                provider_obj = registry.get_provider(request.provider_name)
            except Exception as exc:  # provider unavailable — cannot judge
                logger.debug(
                    "Skipping schema-capacity check for %s/%s: %s",
                    request.provider_name,
                    request.model,
                    exc,
                )
            probe = getattr(provider_obj, "supports_engine_response_schemas", None)
            # Fails OPEN when capacity is unknown, matching the startup gate: an
            # unmeasured model is never refused on speculation.
            if probe is not None and not probe(request.model):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Model '{request.model}' cannot serve the investigation "
                        f"engine's response schemas: it accepts small schemas and "
                        f"rejects the larger per-stage ones, so investigations "
                        f"would advance a few turns and then fail every remaining "
                        f"turn. Choose a model with a larger constrained-decoding "
                        f"budget."
                    ),
                )

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
    current_user: AuthenticatedUser = Depends(require_platform_admin),
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
                status_code=422,
                detail=f"Unknown provider: '{provider_name}'. "
                f"Valid providers: {', '.join(valid_names)}",
            )

        registry = llm_provider.registry
        provider = registry.get_provider(provider_name)

        # In strict mode the provider may not be in the active set even though
        # an API key exists.  Fall back to creating a temporary instance.
        if provider is None:
            provider = registry.create_provider_for_test(provider_name)

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
            prompt="Say hello",
            max_tokens=50,
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


def _rate_limiting_installed(app) -> bool:
    """Whether this app is actually rate limiting, read off the middleware stack.

    The only honest answer to "is rate limiting on?" is whether
    ``RateLimitMiddleware`` is in the stack, so that is what is asked. Every
    cheaper proxy for it has been wrong in production:

    * ``settings.security.rate_limit_enabled`` — the field this replaces
      (fm#985 item 16) — was read by no enforcement path at all. It reported
      *disabled* under ``CONFIG_PRESET=local``, which used to set
      ``RATE_LIMIT_ENABLED=false`` while the ``development`` *protection* preset
      it selects rate limits regardless; and it reported *enabled* under
      ``SKIP_SERVICE_CHECKS=True``, which at the time installed no protection
      middleware whatsoever — fm#990 has since removed that gate, so the flag
      no longer affects what is installed. Wrong in both directions, and never
      right except by accident.
    * ``settings.protection`` — that is PII redaction, a different subsystem.
    * ``app.extra["protection_info"]`` — a record of what setup *intended*, and
      absent entirely on the paths that skip or fail setup.

    ``user_middleware`` cannot disagree with what is installed, because it is
    what Starlette builds the stack from. ``main.py``'s CORS-outermost guard
    reads it the same way.

    "Installed" is the claim, and it is the whole claim: an installed limiter
    that is degrading on a Redis outage still counts as on. That is a transient
    condition, it is reported per-request in the response headers, and
    production pins fail-closed anyway — none of which a deployment-status field
    should try to average into a boolean.
    """
    from faultmaven.api.middleware import RateLimitMiddleware

    return any(
        middleware.cls is RateLimitMiddleware
        for middleware in getattr(app, "user_middleware", [])
    )


def _suggestion_store_is_durable(app) -> bool:
    """Is the composed knowledge-suggestion store durable and worker-shared?

    Asks the object that is actually running, not the settings. ``WORKERS`` was
    the old proxy for this question, and it stopped being one the moment the
    store could be a database: ``WORKERS=1`` on a dict-backed process is still
    a process that loses every pending review on restart, and ``WORKERS=4`` on
    a database-backed one is fine. What an operator needs to know is which
    store this process holds.

    ``False`` covers both bad answers — a non-durable store is composed (which
    is CORRECT, not a fault, on a deployment with no database configured), or no
    suggestion service is composed at all and the routes answer 503. They have
    the same consequence for scaling out, and the ``config_hint`` names both.
    """
    service = getattr(getattr(app, "state", None), "suggestion_service", None)
    repository = getattr(service, "_repository", None) if service else None
    if repository is None:
        return False
    # The store STATES its own durability (``ISuggestionRepository.is_durable``)
    # rather than being recognised by type. An ``isinstance`` check would be one
    # more proxy of the same kind as ``WORKERS`` — true of a class, not of the
    # deployment — and it would go stale the moment a third implementation
    # appears or the database one is composed over an ephemeral URL. The
    # composition root is what keeps the claim honest: it picks the in-memory
    # repository (``is_durable == False``) whenever
    # ``persistent_database_configured`` says there is no database to write to.
    return bool(getattr(repository, "is_durable", False))


@router.get("/config/status", response_model=EnvConfigStatusResponse)
async def get_env_config_status(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_platform_admin),
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

        deployment = "cloud" if settings.is_cloud else "standalone"

        # Build feature status
        from faultmaven.api.models import FeatureStatus

        # Web search: needs TAVILY_API_KEY (or Google CSE keys)
        tavily_key = getattr(settings.knowledge, "tavily_api_key", None)
        has_tavily = bool(
            tavily_key
            and hasattr(tavily_key, "get_secret_value")
            and tavily_key.get_secret_value()
        )
        web_search_enabled = (
            getattr(settings.knowledge, "enable_web_search", False) and has_tavily
        )

        # Opik tracing
        opik_enabled = getattr(settings.observability, "opik_enabled", False)

        # Only surface features that require user-provided configuration.
        # Core capabilities (interpreted search, semantic search) that work
        # automatically with the existing LLM are not shown.
        features = {
            "web_search": FeatureStatus(
                enabled=web_search_enabled,
                has_api_key=has_tavily,
                description="Search technical websites during investigations",
                config_hint="Set TAVILY_API_KEY and ENABLE_WEB_SEARCH=true",
            ),
            "llm_tracing": FeatureStatus(
                enabled=opik_enabled,
                has_api_key=bool(
                    getattr(settings.observability, "opik_api_key", None)
                    or getattr(settings.observability, "opik_use_local", False)
                ),
                description="Trace LLM calls for observability and debugging",
                config_hint="Set OPIK_ENABLED=true with Opik cloud key or OPIK_USE_LOCAL=true",
            ),
            # Reported HERE, and this is the answer — deliberately not a
            # startup log line. What goes wrong on an unpinned deployment is
            # that nothing appears: the consent screen renders exactly as it
            # always did, so "inactive" is indistinguishable from "working"
            # from the outside. A startup-only line does not close that, since
            # it has rolled out of `kubectl logs` on any pod that has been up a
            # while — a runbook saying "grep for it" then returns empty on a
            # perfectly healthy deployment and teaches the wrong conclusion.
            #
            # Inactive is a CORRECT state for standalone and self-hosted, which
            # have no published extension id, so this reports rather than
            # degrading /health.
            "first_party_consent_skip": FeatureStatus(
                enabled=bool(settings.auth.oauth_first_party_redirect_patterns),
                description=(
                    "Shipped browser extension signs in without a consent "
                    "prompt (requires its published redirect to be pinned)"
                ),
                config_hint=(
                    "Set OAUTH_FIRST_PARTY_REDIRECT_PATTERNS to a JSON list of "
                    "regexes matching your published extension's "
                    "launchWebAuthFlow redirect, e.g. "
                    r'["^https://<id>\.chromiumapp\.org/?$"]'
                ),
            ),
            # The knowledge-suggestion store, reported as the RUNTIME fact it
            # is (#1227) rather than inferred from a setting. Reported here for
            # the same reason as the consent skip above: the only other signal
            # is a startup log line, and startup logs roll out of
            # `kubectl logs` long before anyone investigates an intermittent
            # 404.
            #
            # It reads False when the composed store is the in-memory double
            # (#1214's shape — non-durable and per worker, so with WORKERS>1 or
            # more than one pod an extract and its approve land on different
            # processes and the approve 404s on an id the API just issued), and
            # when no suggestion service was composed at all. It reads True only
            # when the process actually holds the database-backed store, which
            # is the point: the field answers "is what is running right now safe
            # to scale out", and a value derived from WORKERS could not tell
            # a database-backed deployment from a dict-backed one.
            "suggestion_store_worker_safe": FeatureStatus(
                enabled=_suggestion_store_is_durable(request.app),
                description=(
                    "Knowledge suggestions are stored durably and shared across "
                    "API workers and pods (extract → approve cannot land on a "
                    "process that has never seen the suggestion, and a restart "
                    "does not drop the review inbox)."
                ),
                config_hint=(
                    "True when the process holds the database-backed suggestion "
                    "store. False means no persistent DATABASE_URL is configured "
                    "(so the store is in-memory), or no suggestion service is "
                    "composed at all — configure a database, and until then run "
                    "WORKERS=1"
                ),
            ),
        }

        # Report actual runtime state, not raw setting defaults.
        # Bootstrap may create persistent stores even when settings say "inmemory".
        from pathlib import Path

        # Database: check alembic.ini for actual DB URL (bootstrap always uses this)
        db_backend = settings.database.case_storage_type
        alembic_url = ""
        alembic_config = getattr(settings.database, "alembic_config", None)
        ini_candidates = [Path("alembic.ini")]
        if alembic_config:
            ini_candidates.append(Path(alembic_config))
        for ini_path in ini_candidates:
            if ini_path.exists():
                for line in ini_path.read_text().splitlines():
                    if line.strip().startswith("sqlalchemy.url"):
                        alembic_url = line.split("=", 1)[1].strip()
                        break
                break
        if "sqlite" in alembic_url:
            db_backend = "sqlite"
        elif "postgresql" in alembic_url:
            db_backend = "postgresql"

        # Vector storage: check if ChromaDB PersistentClient is active
        vector_storage = settings.database.vector_storage_type
        chroma_kb_dir = Path(
            getattr(settings.database, "chromadb_kb_persist_dir", "./data/chroma-kb")
        )
        chroma_evidence_dir = Path(
            getattr(
                settings.database,
                "chromadb_evidence_persist_dir",
                "./data/chroma-evidence",
            )
        )
        kb_active = (chroma_kb_dir / "chroma.sqlite3").exists()
        evidence_active = (chroma_evidence_dir / "chroma.sqlite3").exists()
        if kb_active and evidence_active:
            vector_storage = "chromadb (persistent, split: kb + evidence)"
        elif kb_active:
            vector_storage = "chromadb (persistent, kb only)"

        # Session storage: FakeRedis = inmemory, real Redis = redis
        session_storage = settings.database.session_storage_type
        redis_url = getattr(settings.database, "redis_url", None)
        if redis_url and "redis://" in str(redis_url):
            session_storage = "redis"
        else:
            session_storage = "fakeredis (inmemory)"

        return EnvConfigStatusResponse(
            auth_mode=settings.auth.auth_mode,
            deployment=deployment,
            db_backend=db_backend,
            session_storage=session_storage,
            vector_storage=vector_storage,
            llm_provider=(
                settings.llm.provider.value if settings.llm.provider else "not_set"
            ),
            pii_redaction_enabled=settings.protection.protection_enabled,
            rate_limit_enabled=_rate_limiting_installed(request.app),
            features=features,
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
