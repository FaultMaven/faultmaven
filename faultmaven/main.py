"""main.py

Purpose: FastAPI entry point and central application setup

Requirements:
--------------------------------------------------------------------------------
• Initialize the core FastAPI application instance
• Configure CORS middleware for browser extension
• Include API routers from data_ingestion, query_processing, and kb_management
• Set up startup/shutdown event handlers
• Integrate Comet Opik tracing middleware

Key Components:
--------------------------------------------------------------------------------
  app = FastAPI(title='FaultMaven API')
  app.include_router(data_ingestion.router, prefix='/api/v1')
  @app.on_event('startup')

Technology Stack:
--------------------------------------------------------------------------------
FastAPI, Uvicorn, Comet Opik

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Cost-Efficiency: Use semantic caching
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

# Load environment variables FIRST - before any other imports
from dotenv import load_dotenv

load_dotenv()

# Now import everything else
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from starlette.formparsers import MultiPartParser

from faultmaven.utils.serialization import to_json_compatible

# Configure enhanced logging system first
from .infrastructure.logging.config import get_logger

logger = get_logger(__name__)

# Module-level settings cache (set during lifespan startup)
_app_settings = None


def _is_test_environment(settings=None) -> bool:
    """Detect if we're running in a test environment (pytest or skip_service_checks)."""
    # Check for pytest in command line arguments
    if "pytest" in " ".join(sys.argv) or any("test" in arg.lower() for arg in sys.argv):
        return True

    # Get settings (from parameter, module cache, or environment)
    if settings is None:
        settings = _app_settings
        if settings is None:
            # Fallback: try to get settings (for early calls before lifespan)
            try:
                from .config.settings import get_settings

                settings = get_settings()
            except Exception:
                # If settings not available, fall back to os.getenv for backward compatibility
                if os.getenv("SKIP_SERVICE_CHECKS", "").lower() == "true":
                    return True
                if os.getenv("PYTEST_CURRENT_TEST"):
                    return True
                return False

    # Use settings (deployment-agnostic)
    if settings.server.skip_service_checks:
        return True

    if settings.server.pytest_current_test:
        return True

    # Check if we're being imported by pytest
    if "pytest" in sys.modules:
        return True

    return False


def _check_llm_configuration(llm_provider, settings=None) -> None:
    """Check if any LLM provider is configured and print warning if not."""
    # Get settings if not provided
    if settings is None:
        settings = _app_settings
        if settings is None:
            try:
                from .config.settings import get_settings

                settings = get_settings()
            except Exception:
                # If settings unavailable, skip check
                return

    # Skip check in test environments
    if _is_test_environment(settings=settings):
        return

    # Check if we have any configured providers
    has_provider = False
    provider_name = None

    # Check common API key environment variables from settings (deployment-agnostic)
    llm_settings = settings.llm
    llm_keys = {
        "OpenAI": (
            llm_settings.openai_api_key.get_secret_value()
            if llm_settings.openai_api_key
            else ""
        ),
        "Anthropic": (
            llm_settings.anthropic_api_key.get_secret_value()
            if llm_settings.anthropic_api_key
            else ""
        ),
        "Fireworks": (
            llm_settings.fireworks_api_key.get_secret_value()
            if llm_settings.fireworks_api_key
            else ""
        ),
        "Groq": (
            llm_settings.groq_api_key.get_secret_value()
            if llm_settings.groq_api_key
            else ""
        ),
        "Gemini": (
            llm_settings.gemini_api_key.get_secret_value()
            if llm_settings.gemini_api_key
            else ""
        ),
    }

    for name, key in llm_keys.items():
        if key and not key.startswith("your_") and key != "":
            has_provider = True
            provider_name = name
            break

    # Check for local LLM configuration from settings
    chat_provider = llm_settings.provider.value.lower() if llm_settings.provider else ""
    if chat_provider == "local":
        # Note: LOCAL_LLM_URL would need to be added to LLMSettings if used
        # For now, checking provider setting is sufficient
        has_provider = True
        provider_name = "Local (Ollama)"

    if has_provider:
        logger.info(f"✅ LLM Provider configured: {provider_name}")

        # Check if effective DA provider supports tool calling
        try:
            from .infrastructure.llm.providers.registry import get_registry

            registry = get_registry()
            llm_settings = settings.llm

            effective_da_provider = llm_settings.get_da_provider()
            effective_da_model = llm_settings.get_da_model()
            da_provider_name = (
                effective_da_provider.value
                if hasattr(effective_da_provider, "value")
                else str(effective_da_provider)
            )

            da_provider_instance = registry.get_provider(da_provider_name)
            if da_provider_instance and not da_provider_instance.supports_tool_calling(
                effective_da_model
            ):
                source = (
                    "DA_PROVIDER"
                    if llm_settings.da_provider is not None
                    else "CHAT_PROVIDER (no DA_PROVIDER override set)"
                )
                logger.warning(
                    f"{da_provider_name}/{effective_da_model} does not support tool calling "
                    f"(resolved from {source}). "
                    "Directed Analysis features (search_file, deep_analysis) will be unavailable. "
                    "Investigation responses will be limited to structural index summaries. "
                    "To enable full investigation, set DA_PROVIDER to a tool-capable provider "
                    "(anthropic, openai, gemini)."
                )
        except Exception as e:
            logger.debug(f"Could not check DA provider tool calling capability: {e}")
    else:
        # Print prominent warning banner
        banner = """
╔═══════════════════════════════════════════════════════════════════════════════╗
║                        ⚠️  NO LLM PROVIDER CONFIGURED                         ║
╠═══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║  FaultMaven requires an LLM provider to function. Please configure one:       ║
║                                                                               ║
║  Option 1: Cloud Provider (in your .env file)                                 ║
║    OPENAI_API_KEY=sk-...                                                      ║
║    ANTHROPIC_API_KEY=sk-ant-...                                               ║
║                                                                               ║
║  Option 2: Local LLM (Ollama)                                                 ║
║    CHAT_PROVIDER=local                                                        ║
║    LOCAL_LLM_URL=http://localhost:11434                                       ║
║    LOCAL_LLM_MODEL=llama3.1                                                   ║
║                                                                               ║
║  See: https://github.com/FaultMaven/faultmaven#quick-start                    ║
║                                                                               ║
╚═══════════════════════════════════════════════════════════════════════════════╝
"""
        logger.warning(banner)


from .api.middleware.logging import LoggingMiddleware
from .api.routes.sessions import router as investigation_sessions_router
from .infrastructure.observability.tracing import init_opik_tracing

# Import API routes from modules
# All routes now in modules following vertical slice architecture
from .modules.agent.api.routes import router as agent_router
from .modules.auth.api.auth import router as auth_router
from .modules.auth.api.oauth import router as oauth_router
from .modules.auth.api.organizations import router as organizations_router
from .modules.auth.api.session import router as session_router
from .modules.auth.api.teams import router as teams_router
from .modules.case.api.routes import router as case_router
from .modules.evidence.api.routes import router as evidence_router
from .modules.knowledge.api.routes import router as knowledge_router
from .modules.report.api.routes import router as report_router

# Admin routes
from .api.routes.admin import router as admin_users_router
from .api.routes.admin_config import router as admin_config_router

# SessionManager now handled via DI container - services.session.SessionService

# Optional Opik middleware import
try:
    import opik

    OPIK_AVAILABLE = True

    try:
        from opik.integrations.fastapi import OpikMiddleware

        OPIK_MIDDLEWARE_AVAILABLE = True
    except ImportError:
        OPIK_MIDDLEWARE_AVAILABLE = False
        logger.debug(
            "Opik middleware class not available, tracing will work without middleware"
        )
except ImportError:
    OPIK_AVAILABLE = False
    OPIK_MIDDLEWARE_AVAILABLE = False
    logger.info("Opik not available, running without tracing")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup and shutdown events."""
    # Startup
    logger.info("Starting FaultMaven API server...")

    # Initialize and validate configuration first
    logger.info("Validating configuration...")
    try:
        from .config.settings import get_settings

        settings = get_settings()

        # Validate workers configuration for in-memory storage
        workers = settings.server.workers
        storage_type = (settings.database.session_storage_type or "inmemory").lower()

        if workers > 1 and storage_type == "inmemory":
            logger.error(
                f"❌ Invalid configuration: WORKERS={workers} with in-memory storage"
            )
            logger.error(
                "   In-memory storage only works with WORKERS=1 (each worker has separate memory)."
            )
            logger.error("   Solutions:")
            logger.error(
                "   1. Set WORKERS=1 in your .env file (recommended for local deployment)"
            )
            logger.error(
                "   2. Use Redis storage: Set SESSION_STORAGE_TYPE=redis in your .env file"
            )
            raise ValueError(
                f"WORKERS={workers} is incompatible with in-memory storage. "
                "Set WORKERS=1 or use SESSION_STORAGE_TYPE=redis."
            )
        elif workers > 1:
            logger.info(
                f"✅ Multi-worker configuration (WORKERS={workers}) with {storage_type} storage"
            )
        else:
            logger.debug(f"Using single worker (WORKERS={workers})")
        logger.info("Configuration validated successfully")

        # Make configuration available to app
        app.extra["settings"] = settings
    except Exception as e:
        logger.error(f"Configuration initialization failed: {e}")
        raise

    # Store settings in module scope for helper functions
    global _app_settings
    _app_settings = settings

    # Initialize the DI container first (before any services that depend on it)
    # Container initialization includes service registration internally
    logger.info("Initializing DI container...")
    try:
        from .container import container

        await container.initialize()

        # Verify critical services are available IMMEDIATELY after initialization
        user_store = container.get_user_store()
        token_manager = container.get_token_manager()

        logger.info(
            f"Container initialization complete. Checking authentication services..."
        )
        logger.info(
            f"   - user_store: {type(user_store).__name__ if user_store else 'None'}"
        )
        logger.info(
            f"   - token_manager: {type(token_manager).__name__ if token_manager else 'None'}"
        )

        if user_store is None or token_manager is None:
            logger.error(
                f"❌ Critical authentication services missing after container initialization:"
            )
            logger.error(f"   - user_store: {user_store}")
            logger.error(f"   - token_manager: {token_manager}")
            logger.error(f"   - Container initialized: {container.is_initialized}")
            logger.error(
                f"   - Container has user_store attr: {hasattr(container, 'user_store')}"
            )
            if hasattr(container, "user_store"):
                logger.error(f"   - Container.user_store value: {container.user_store}")
            raise RuntimeError(
                "Container initialization incomplete: authentication services not available. "
                "Check container initialization logs for errors during register_infrastructure()."
            )

        logger.info(
            "✅ DI container initialized successfully with authentication services"
        )

        # Make container available to app for access by other components
        app.extra["di_container"] = container

        # ============================================================
        # Bootstrap Application (deployment-agnostic architecture)
        # ============================================================
        # Ensures default organization exists for single-tenant mode
        # Must run after container initialization (requires tenant_provider)
        # Must run after container initialization (requires tenant_provider)
        try:
            from .bootstrap.startup import bootstrap_application

            await bootstrap_application(container)
            logger.debug("✅ Application bootstrap complete")

            # Apply LLM config overrides from database (if any)
            try:
                from .config.llm_config_overrides import apply_overrides_to_settings

                await apply_overrides_to_settings(settings)
                logger.debug("✅ LLM config overrides applied")
            except Exception as e:
                logger.debug(f"LLM config overrides skipped: {e}")
        except Exception as e:
            logger.critical(
                f"🔥 BLOCKING STARTUP FAILURE: Application bootstrap failed: {e}"
            )
            # FAIL FAST: Re-raise to stop startup.
            # A broken bootstrap means DB or critical directories are missing.
            raise RuntimeError(f"Critical bootstrap failure: {e}") from e

        # ============================================================
        # Composition Root: Attach all services to app.state
        # ============================================================
        # This follows the Composition Root principle (P5):
        # - Services are wired here at startup
        # - FastAPI dependencies access via request.app.state
        # - Services do NOT call container.get_*() themselves
        # ============================================================

        # CRITICAL: Set authentication services FIRST - they're required for the API to work
        # These were already verified above, so they must be available
        app.state.token_manager = token_manager
        app.state.user_store = user_store
        app.state.user_service = container.get_user_service()
        app.state.auth_service = container.get_auth_service()
        app.state.oauth_service = (
            container.get_oauth_service()
        )  # OAuth service (optional)

        # Other services (may fail gracefully)
        try:
            app.state.session_service = container.get_session_service()
            app.state.case_service = container.get_case_service()
            app.state.investigation_service = container.get_investigation_service()
            app.state.investigation_orchestrator = (
                container.get_investigation_orchestrator()
            )
            app.state.knowledge_service = container.get_knowledge_service()
            app.state.evidence_service = container.get_evidence_service()
            app.state.preprocessing_service = container.get_preprocessing_service()
            app.state.enhanced_agent_service = container.get_enhanced_agent_service()
            app.state.memory_service = container.get_memory_service()
            app.state.planning_service = container.get_planning_service()
            app.state.orchestration_service = container.get_orchestration_service()
            app.state.data_service = container.get_data_service()
            app.state.tenant_provider = container.get_tenant_provider()
            app.state.report_generation_service = (
                container.get_report_generation_service()
            )
            app.state.report_recommendation_service = (
                container.get_report_recommendation_service()
            )
            app.state.organization_service = container.get_organization_service()
            app.state.team_service = container.get_team_service()
            app.state.job_service = container.get_job_service()
            # Query classification engine (optional - may not be available)
            try:
                app.state.query_classification_engine = (
                    container.get_query_classification_engine()
                )
            except AttributeError:
                logger.warning("Query classification engine not available - skipping")
                app.state.query_classification_engine = None
            app.state.tracer = container.get_tracer()
            app.state.llm_provider = container.get_llm_provider()
        except AttributeError as e:
            logger.warning(
                f"Some optional services not available: {e} - continuing with available services"
            )
        except Exception as e:
            logger.warning(
                f"Error setting some services: {e} - continuing with available services"
            )
        app.state.tracer = container.get_tracer()
        app.state.llm_provider = container.get_llm_provider()
        logger.info("✅ Services attached to app.state (Composition Root)")

        # Check LLM provider configuration and warn if none configured
        _check_llm_configuration(app.state.llm_provider, settings=settings)
    except RuntimeError as e:
        # Container already logged the error and raised if it's a production fail-fast
        # Re-raise to let FastAPI handle startup failure
        raise
    except Exception as e:
        logger.error(f"DI container initialization failed: {e}")
        # Only allow graceful degradation in non-production environments
        # Use settings (deployment-agnostic) instead of os.getenv()
        from .config.settings import Environment

        is_production = settings.server.environment == Environment.PRODUCTION
        if is_production:
            logger.critical(
                "Production startup failed - cannot continue with incomplete container"
            )
            raise RuntimeError(
                f"Critical container initialization failed in production: {e}"
            ) from e
        logger.warning(
            "Continuing with fallback service implementations (non-production mode)"
        )

    # Initialize core services with K8s support
    # SessionManager replaced by services.session.SessionService via DI container
    # Access via: container.get_session_service()

    # ML Model Loading Strategy (configurable lazy vs eager loading)
    # Default: lazy loading for faster startup, models load on first use
    try:
        lazy_load = settings.embedding.lazy_load_ml_models
        preload_models = settings.embedding.preload_models or []

        if lazy_load and not preload_models:
            logger.info(
                "🚀 Lazy ML model loading enabled - models will load on first use"
            )
            logger.info("   (Set LAZY_LOAD_ML_MODELS=false for eager loading)")
        else:
            # Eager loading or specific models requested
            logger.info("Pre-loading ML models during startup...")
            from .infrastructure.model_cache import model_cache

            # Determine which models to load
            models_to_load = []
            if not lazy_load:
                # Eager mode: load all default models
                models_to_load = ["BAAI/bge-m3"]
            elif preload_models:
                # Lazy mode with specific preload list
                models_to_load = preload_models

            for model_name in models_to_load:
                try:
                    triggered_by = "startup" if not lazy_load else "preload"
                    if model_name == "BAAI/bge-m3":
                        bge_model = model_cache.get_bge_m3_model(
                            triggered_by=triggered_by
                        )
                        if bge_model:
                            load_info = model_cache.get_model_load_info(model_name)
                            load_time = (
                                load_info.load_time_seconds if load_info else "?"
                            )
                            logger.info(f"✅ {model_name} pre-loaded in {load_time}s")
                        else:
                            logger.warning(f"⚠️ {model_name} not available")
                    else:
                        logger.warning(f"Unknown model for preloading: {model_name}")
                except Exception as e:
                    logger.warning(f"Failed to pre-load {model_name}: {e}")
    except Exception as e:
        logger.warning(f"ML model loading configuration error: {e}")

    # Setup tracing
    init_opik_tracing()

    # Check and start local LLM services if needed
    try:
        from .infrastructure.llm.local_llm_manager import (
            check_and_start_local_llm_service,
        )

        # Check if we're configured to use local LLM providers
        # Use settings (deployment-agnostic) instead of os.getenv()
        llm_settings = settings.llm
        chat_provider = (
            llm_settings.provider.value.lower() if llm_settings.provider else ""
        )
        classifier_provider = (
            llm_settings.classifier_provider.value.lower()
            if llm_settings.classifier_provider
            else ""
        )

        # Note: LOCAL_LLM_MODEL and LOCAL_LLM_URL would need to be added to LLMSettings
        # For now, using defaults (these are rarely used in local deployment)
        local_llm_model = "llama2-7b"  # Default fallback
        local_llm_base_url = "http://localhost:8080"  # Default fallback

        if chat_provider == "local":
            logger.info("Chat provider set to 'local', checking local LLM service...")
            success = await check_and_start_local_llm_service(
                "local", local_llm_base_url, local_llm_model
            )
            if success:
                logger.info("✅ Local LLM service ready for chat provider")
            else:
                logger.warning("⚠️ Failed to start local LLM service for chat provider")

        if classifier_provider == "local":
            logger.info(
                "Classifier provider set to 'local', checking local LLM service..."
            )
            success = await check_and_start_local_llm_service(
                "local", local_llm_base_url, local_llm_model
            )
            if success:
                logger.info("✅ Local LLM service ready for classifier provider")
            else:
                logger.warning(
                    "⚠️ Failed to start local LLM service for classifier provider"
                )

        if chat_provider != "local" and classifier_provider != "local":
            logger.info(
                "No local LLM providers configured, skipping local service check"
            )

    except Exception as e:
        logger.warning(f"Local LLM service check failed (non-critical): {e}")

    # Initialize Phase 2 monitoring components
    try:
        from .infrastructure.monitoring.alerting import (
            alert_manager,
            setup_default_alert_rules,
        )
        from .infrastructure.monitoring.apm_integration import apm_integration

        # Start APM integration background export
        apm_integration.start_background_export()
        logger.info("✅ APM integration started")

        # Set up default alert rules
        setup_default_alert_rules()
        logger.info("✅ Default alert rules configured")

        logger.info("✅ Phase 2 monitoring components initialized")

    except Exception as e:
        logger.warning(f"Phase 2 monitoring initialization failed (non-critical): {e}")

    # In-process scheduler (opt-in via RUN_SCHEDULER=true)
    # Default: disabled for operational neutrality - use CLI jobs or external schedulers instead
    # See: python -m faultmaven.jobs.run --list
    case_cleanup_scheduler = None
    if settings.server.run_scheduler:
        try:
            from .infrastructure.tasks import start_case_cleanup_scheduler

            # Only start if both case_vector_store and case_store are available
            case_vector_store = getattr(container, "case_vector_store", None)
            case_store = getattr(container, "case_store", None)
            if case_vector_store and case_store:
                case_cleanup_scheduler = start_case_cleanup_scheduler(
                    case_vector_store=case_vector_store,
                    case_store=case_store,
                    interval_hours=6,  # Run cleanup every 6 hours
                )
                logger.info(
                    "✅ Case cleanup scheduler started (RUN_SCHEDULER=true, single-process mode)"
                )
                app.extra["case_cleanup_scheduler"] = case_cleanup_scheduler
            else:
                logger.debug(
                    "Case cleanup scheduler skipped (missing case_vector_store or case_store)"
                )
        except Exception as e:
            logger.warning(
                f"Case cleanup scheduler initialization failed (non-critical): {e}"
            )
    else:
        logger.info(
            "ℹ️ In-process scheduler disabled (RUN_SCHEDULER=false). Use 'python -m faultmaven.jobs.run' for jobs."
        )

    # Middleware must be added before the app starts. It is configured at import time.
    logger.info("✅ Middleware already configured")

    logger.info(
        "🚀 FaultMaven API server startup COMPLETE - ready to serve fast requests!"
    )

    yield

    # Shutdown
    logger.info("Shutting down FaultMaven API server...")

    # Stop case cleanup scheduler
    if case_cleanup_scheduler:
        try:
            from .infrastructure.tasks import stop_case_cleanup_scheduler

            stop_case_cleanup_scheduler(case_cleanup_scheduler)
        except Exception as e:
            logger.warning(f"Error stopping case cleanup scheduler: {e}")

    # Cleanup resources
    if "session_manager" in app.extra:
        # Cleanup any active sessions
        session_manager = app.extra["session_manager"]
        try:
            cleaned_count = await session_manager.cleanup_inactive_sessions()
            logger.info(f"Cleaned up {cleaned_count} expired sessions during shutdown")

            # Close session manager (stops scheduler and connections)
            await session_manager.close()
        except Exception as e:
            logger.error(f"Error during session cleanup: {e}")

    # Cleanup Phase 2 monitoring components
    try:
        from .infrastructure.monitoring.apm_integration import apm_integration

        # Stop APM background export
        apm_integration.stop_background_export()

        # Flush any remaining metrics
        await apm_integration.flush_metrics()

        logger.info("✅ Phase 2 monitoring components cleaned up")

    except Exception as e:
        logger.warning(f"Phase 2 monitoring cleanup failed (non-critical): {e}")

    logger.info("FaultMaven API server shutdown complete")


# Create FastAPI application with disabled automatic redirects
app = FastAPI(
    title="FaultMaven API",
    description="AI-powered troubleshooting assistant for Engineers, "
    "SREs, and DevOps professionals",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
    redirect_slashes=False,  # Disable automatic trailing slash redirects
)

# Override Starlette's default multipart form parser size limits.
# Starlette defaults to 1MB per form part, but our upload limit is MAX_UPLOAD_SIZE_MB (default 10MB).
# All three data submission paths (file upload, page injection, pasted text) go through
# the same unified /turns endpoint as multipart form data and must respect the same limit.
# See: docs/architecture/data-processing/data-preprocessing-design-specification.md Appendix A
_upload_max_bytes = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "10")) * 1024 * 1024
MultiPartParser.max_file_size = _upload_max_bytes
MultiPartParser.max_part_size = _upload_max_bytes


# Add middleware in optimized order to prevent duplicates
def setup_middleware():
    """Setup middleware - only log when not in test mode"""
    import sys

    from faultmaven.config.settings import get_settings

    settings = get_settings()

    # Skip verbose logging during test collection
    if settings.server.pytest_current_test or "pytest" in sys.modules:
        logging_enabled = False
    else:
        logging_enabled = True

    if logging_enabled:
        logger.info("Starting middleware registration...")
        logger.info(
            f"Initial middleware stack: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 1. CORS middleware (first - handles preflight requests)
    # Use configurable origins from settings - production should specify
    # specific extension IDs instead of wildcards (e.g., chrome-extension://abc123)
    cors_origins = list(settings.security.cors_allow_origins)

    # SECURITY: Fail-fast validation - no wildcards allowed in production
    from faultmaven.config.settings import Environment

    if settings.server.environment == Environment.PRODUCTION:
        wildcard_origins = [o for o in cors_origins if "://*" in o]
        if wildcard_origins:
            raise RuntimeError(
                f"SECURITY ERROR: Wildcard CORS origins are not allowed in production: {wildcard_origins}. "
                "Configure CORS_ALLOW_ORIGINS with specific extension IDs "
                "(e.g., chrome-extension://abc123def456)."
            )

    # Add production domain if not already present
    if "https://faultmaven.ai" not in cors_origins:
        cors_origins.append("https://faultmaven.ai")

    # In non-production, support dynamic CORS for local network access
    if settings.server.environment != Environment.PRODUCTION:
        # Add common development origins if not already present
        for dev_origin in [
            "http://localhost:3333",
            "http://localhost:8090",
            "http://localhost:5173",
        ]:
            if dev_origin not in cors_origins:
                cors_origins.append(dev_origin)

        # Add regex pattern for local network IPs (RFC 1918 private networks)
        # This allows dashboard access from phones/tablets on local network
        local_network_regex = (
            r"^https?://"
            r"("
            r"localhost|127\.0\.0\.1|"  # Localhost
            r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"  # Class A: 10.0.0.0/8
            r"172\.(1[6-9]|2[0-9]|3[0-1])\.\d{1,3}\.\d{1,3}|"  # Class B: 172.16.0.0/12
            r"192\.168\.\d{1,3}\.\d{1,3}"  # Class C: 192.168.0.0/16
            r")"
            r"(:\d+)?$"
        )

        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_origin_regex=local_network_regex,
            allow_credentials=settings.security.cors_allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=list(settings.security.cors_expose_headers),
        )

        if logging_enabled:
            logger.info(
                f"✅ CORS configured for development with local network support"
            )
            logger.info(f"   Allowed origins: {cors_origins}")
            logger.info(f"   Local network pattern: {local_network_regex}")
    else:
        # Production: strict origin checking only (no regex patterns)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=settings.security.cors_allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=list(settings.security.cors_expose_headers),
        )
    if logging_enabled:
        logger.info(
            f"After CORS middleware: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 2. Trailing slash middleware (after CORS, prevents 307 redirects)
    try:
        from .api.middleware.trailing_slash import TrailingSlashMiddleware

        app.add_middleware(TrailingSlashMiddleware)
        if logging_enabled:
            logger.info("✅ Trailing slash middleware added")
    except Exception as e:
        logger.warning(f"Failed to add trailing slash middleware: {e}")

    if logging_enabled:
        logger.info(
            f"After trailing slash middleware: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 3. Idempotency middleware (after CORS, before protection) — skip when SKIP_SERVICE_CHECKS
    try:
        if not settings.server.skip_service_checks:
            from .api.middleware.idempotency import IdempotencyMiddleware
            from .container import container

            # Get Redis client from container for idempotency
            redis_client = None
            try:
                redis_client = container.get_redis_client()
            except Exception as e:
                logger.warning(f"Redis not available for idempotency: {e}")

            # Create middleware instance with dependencies
            app.add_middleware(IdempotencyMiddleware, redis_client=redis_client)
            if logging_enabled:
                logger.info("✅ Idempotency middleware added")
        else:
            if logging_enabled:
                logger.info(
                    "Skipping Idempotency middleware (SKIP_SERVICE_CHECKS=True)"
                )
    except Exception as e:
        logger.warning(f"Failed to add idempotency middleware: {e}")

    if logging_enabled:
        logger.info(
            f"After Idempotency middleware: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 4. Request ID and Rate Limiting Headers middleware - skip in test environments
    try:
        if not settings.server.skip_service_checks and not _is_test_environment():
            from .api.middleware.request_id import (
                RateLimitHeaderMiddleware,
                RequestIdMiddleware,
            )

            # Add Request ID middleware
            app.add_middleware(RequestIdMiddleware)

            # Add Rate Limiting Headers middleware
            app.add_middleware(
                RateLimitHeaderMiddleware, default_limit=1000, window_seconds=3600
            )

            if logging_enabled:
                logger.info("✅ Request ID and Rate Limiting Headers middleware added")
        else:
            if logging_enabled:
                logger.info(
                    "Skipping Request ID middleware (test environment or SKIP_SERVICE_CHECKS=True)"
                )

    except Exception as e:
        logger.warning(f"Failed to add request ID middleware: {e}")

    if logging_enabled:
        logger.info(
            f"After Request ID middleware: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 5. Protection middleware (early in stack for security)
    try:
        from .api.protection import setup_protection_middleware

        if not settings.server.skip_service_checks:
            protection_info = setup_protection_middleware(
                app,
                environment=settings.server.environment,
            )
            if logging_enabled:
                if protection_info.get("protection_enabled"):
                    middleware_names = protection_info.get("middleware_added", [])
                    logger.info(f"✅ Protection middleware enabled: {middleware_names}")
                    if protection_info.get("warnings"):
                        logger.warning(
                            f"Protection warnings: {protection_info['warnings']}"
                        )
                else:
                    logger.info("ℹ️ Protection middleware disabled")
            app.extra["protection_info"] = protection_info
        else:
            if logging_enabled:
                logger.info("Skipping Protection middleware (SKIP_SERVICE_CHECKS=True)")
    except Exception as e:
        if logging_enabled:
            logger.warning(f"Failed to setup protection middleware: {e}")
        if settings.server.environment != "development":
            raise

    if logging_enabled:
        logger.info(
            f"After Protection middleware: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 3. GZip middleware
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    if logging_enabled:
        logger.info(
            f"After GZip middleware: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 4. New unified logging middleware (integrates with Phase 1 & 2 infrastructure)
    if logging_enabled:
        logger.info("Adding LoggingMiddleware to FastAPI app")
    app.add_middleware(LoggingMiddleware)
    if logging_enabled:
        logger.info(
            f"After LoggingMiddleware: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 5. Performance tracking middleware (Phase 2 enhancement)
    from .api.middleware.performance import PerformanceTrackingMiddleware

    if not settings.server.skip_service_checks:
        if logging_enabled:
            logger.info("Adding PerformanceTrackingMiddleware to FastAPI app")
        app.add_middleware(PerformanceTrackingMiddleware, service_name="faultmaven_api")
        if logging_enabled:
            logger.info(
                f"After PerformanceTrackingMiddleware: {[type(m).__name__ for m in app.user_middleware]}"
            )
    else:
        if logging_enabled:
            logger.info(
                "Skipping PerformanceTrackingMiddleware (SKIP_SERVICE_CHECKS=True)"
            )

    # 6. System-wide optimization middleware (Phase 2 optimization) - skip in test environments
    if not settings.server.skip_service_checks and not _is_test_environment():
        from .api.middleware.system_optimization import SystemOptimizationMiddleware

        if logging_enabled:
            logger.info("Adding SystemOptimizationMiddleware to FastAPI app")
        app.add_middleware(
            SystemOptimizationMiddleware,
            enable_compression=True,
            enable_caching=True,
            enable_background_optimization=True,
            enable_resource_cleanup=True,
            cache_ttl_seconds=300,
            compression_threshold=1024,
        )
    else:
        if logging_enabled:
            logger.info(
                "Skipping SystemOptimizationMiddleware (test environment or SKIP_SERVICE_CHECKS=True)"
            )
    if logging_enabled:
        logger.info(
            f"After SystemOptimizationMiddleware: {[type(m).__name__ for m in app.user_middleware]}"
        )

    # 7. Opik tracing middleware (if available) - skip in test environments
    if (
        OPIK_AVAILABLE
        and OPIK_MIDDLEWARE_AVAILABLE
        and not settings.server.skip_service_checks
        and not _is_test_environment()
    ):
        if logging_enabled:
            if settings.observability.opik_use_local:
                logger.info("Adding OpikMiddleware for local Opik instance")
            else:
                logger.info("Adding OpikMiddleware for cloud instance")
        app.add_middleware(OpikMiddleware)
        if logging_enabled:
            logger.info(
                f"After Opik middleware: {[type(m).__name__ for m in app.user_middleware]}"
            )
    elif OPIK_AVAILABLE and logging_enabled:
        if settings.server.skip_service_checks or _is_test_environment():
            logger.info(
                "Skipping OpikMiddleware (test environment or SKIP_SERVICE_CHECKS=True)"
            )
        else:
            logger.info(
                "Opik SDK available but middleware not found - tracing will work at function level"
            )

    # 8. Contract Probe middleware (for API compliance monitoring)
    if not settings.server.skip_service_checks and not _is_test_environment():
        try:
            from .api.middleware.contract_probe import ContractProbeMiddleware

            app.add_middleware(
                ContractProbeMiddleware,
                probe_enabled=True,
                log_all_requests=False,  # Only log violations, not all requests
                failure_sample_rate=1.0,
            )
            if logging_enabled:
                logger.info(
                    "✅ Contract Probe middleware added for API compliance monitoring"
                )
        except Exception as e:
            logger.warning(f"Failed to add contract probe middleware: {e}")

    if logging_enabled:
        logger.info(
            f"Final middleware stack: {[type(m).__name__ for m in app.user_middleware]}"
        )


# Configure middleware at import time (must run before app startup).
setup_middleware()

# Include API routers (only those in locked spec)
# REMOVED: data.router - moved to modules/case (data ingestion)
# REMOVED: knowledge.router - moved to modules/knowledge/api/routes.py
# REMOVED: session.router - moved to modules/auth (session management)
# REMOVED: auth.router - moved to faultmaven/api/routes/auth.py
# REMOVED: agent.router - replaced by case routes with real AgentService integration

# Register all module routers
app.include_router(agent_router, prefix="/api/v1")
logger.info("✅ Agent endpoints added")

app.include_router(auth_router, prefix="/api/v1")
logger.info("✅ Auth endpoints added")

app.include_router(case_router, prefix="/api/v1")
logger.info("✅ Case endpoints added")

app.include_router(
    investigation_sessions_router
)  # No prefix - router already has /api/v1/cases/{case_id}/sessions
logger.info("✅ Investigation session endpoints added")

app.include_router(evidence_router, prefix="/api/v1")
logger.info("✅ Evidence endpoints added")

app.include_router(knowledge_router, prefix="/api/v1")
logger.info("✅ Knowledge endpoints added")

app.include_router(organizations_router, prefix="/api/v1")
logger.info("✅ Organization endpoints added")

app.include_router(report_router, prefix="/api/v1")
logger.info("✅ Report endpoints added")

app.include_router(session_router, prefix="/api/v1")
logger.info("✅ Session endpoints added")

app.include_router(teams_router, prefix="/api/v1")
logger.info("✅ Team endpoints added")

# Admin routes (user management + configuration)
app.include_router(admin_users_router)  # prefix already set on router: /api/v1/admin
logger.info("✅ Admin user management endpoints added")

app.include_router(admin_config_router)  # prefix already set on router: /api/v1/admin
logger.info("✅ Admin configuration endpoints added")

# OAuth router (only if enabled)
try:
    from .config.settings import get_settings

    _oauth_settings = get_settings()
    if _oauth_settings.auth.oauth_enabled:
        app.include_router(oauth_router, prefix="/api/v1")
        logger.info("✅ OAuth endpoints added")
    else:
        logger.info("ℹ️ OAuth endpoints disabled (using dev-login mode)")
except Exception as e:
    logger.warning(f"OAuth router initialization failed (non-critical): {e}")

# Prometheus metrics endpoint (PR #5 - observability neutrality)
# Only mounted when METRICS_EXPORTER=prometheus_http
try:
    from .config.settings import MetricsExporter, get_settings

    _metrics_settings = get_settings()
    if _metrics_settings.providers.metrics_exporter == MetricsExporter.PROMETHEUS_HTTP:
        from .infrastructure.observability.metrics_exporters import (
            create_prometheus_metrics_endpoint,
        )

        app.include_router(create_prometheus_metrics_endpoint(), tags=["metrics"])
        logger.info(
            "✅ Prometheus /metrics endpoint mounted (METRICS_EXPORTER=prometheus_http)"
        )
    else:
        logger.info(
            "ℹ️ Prometheus /metrics not mounted (METRICS_EXPORTER=none). Set METRICS_EXPORTER=prometheus_http to enable."
        )
except Exception as e:
    logger.warning(
        f"Prometheus metrics endpoint initialization failed (non-critical): {e}"
    )


# Debug endpoints - ONLY available in development/testing environments
# These endpoints expose internal state and should never be enabled in production
def _is_debug_enabled(settings=None) -> bool:
    """Check if debug endpoints should be enabled based on environment."""
    # Get settings if not provided
    if settings is None:
        settings = _app_settings
        if settings is None:
            try:
                from .config.settings import Environment, get_settings

                settings = get_settings()
            except Exception:
                # Fallback to environment check if settings unavailable
                env = os.getenv("ENVIRONMENT", "development").lower()
                return (
                    env in ("development", "testing", "test")
                    or os.getenv("ENABLE_DEBUG_ENDPOINTS", "").lower() == "true"
                )

    # Use settings (deployment-agnostic)
    env = settings.server.environment.value.lower()
    return (
        env in ("development", "testing", "test")
        or settings.server.enable_debug_endpoints
    )


# Get settings for debug check (may not be available at module level)
try:
    from .config.settings import get_settings

    _debug_settings = get_settings()
except Exception:
    _debug_settings = None

if _is_debug_enabled(settings=_debug_settings):
    logger.info(
        "🔧 Debug endpoints enabled (ENVIRONMENT=%s)",
        _debug_settings.server.environment.value if _debug_settings else "unknown",
    )

    @app.get("/debug/routes")
    async def debug_routes():
        """List all registered routes (path + methods)."""
        routes_info = []
        for route in app.routes:
            path = getattr(route, "path", None)
            methods = list(getattr(route, "methods", []) or [])
            if path:
                routes_info.append({"path": path, "methods": methods})
        return {
            "routes": routes_info,
            "count": len(routes_info),
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
        }

    @app.get("/debug/health")
    async def debug_health():
        """Minimal debug health endpoint."""
        return {
            "status": "ok",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
        }

    @app.get("/debug/config")
    async def debug_config():
        """Get current configuration summary including active preset.

        Returns information about:
        - Active configuration preset (if any)
        - Environment settings
        - Storage backend types
        - LLM provider configuration
        - Protection settings

        Useful for debugging configuration issues and verifying preset application.
        """
        try:
            from .config.presets import get_preset_info, list_available_presets
            from .config.settings import get_settings

            settings = get_settings()

            return {
                "timestamp": to_json_compatible(datetime.now(timezone.utc)),
                "configuration": settings.get_configuration_summary(),
                "available_presets": list_available_presets(),
            }
        except Exception as e:
            logger.error(f"Failed to get configuration info: {e}")
            return {
                "error": f"Failed to get configuration: {e}",
                "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            }

    @app.get("/debug/llm-providers")
    async def debug_llm_providers():
        """Get current LLM provider status and fallback chain."""
        try:
            from .container import container

            # Get the LLM provider (router) from the container
            llm_provider = container.get_llm_provider()

            # Get provider status
            provider_status = llm_provider.get_provider_status()

            # Get fallback chain
            fallback_chain = llm_provider.registry.get_fallback_chain()

            # Get available providers
            available_providers = llm_provider.registry.get_available_providers()

            # Check if strict mode is enabled (from settings, deployment-agnostic)
            from .config.settings import get_settings

            settings_debug = get_settings()
            strict_mode = settings_debug.llm.strict_provider_mode

            return {
                "timestamp": to_json_compatible(datetime.now(timezone.utc)),
                "primary_provider": fallback_chain[0] if fallback_chain else "none",
                "strict_mode": strict_mode,
                "fallback_chain": fallback_chain,
                "available_providers": available_providers,
                "provider_details": provider_status,
            }

        except Exception as e:
            logger.error(f"Failed to get LLM provider status: {e}")
            return {
                "error": f"Failed to get LLM provider status: {e}",
                "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            }

else:
    logger.info(
        "🔒 Debug endpoints disabled in production (ENVIRONMENT=%s)",
        _debug_settings.server.environment.value if _debug_settings else "unknown",
    )

# Modular monolith pivot: keep only core endpoints; advanced routes disabled
# Protection monitoring is now handled by middleware and health endpoints


# Register domain exception handlers (TASK-027)
from faultmaven.api.exception_handlers import get_exception_handlers

for exc_type, handler in get_exception_handlers().items():
    app.add_exception_handler(exc_type, handler)
logger.info("✅ Domain exception handlers registered")


# Custom exception handlers
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Custom handler for request/response validation errors (422)"""
    # Convert validation errors to JSON-serializable format
    errors = []
    for error in exc.errors():
        # Create a copy of the error dict
        clean_error = dict(error)

        # Convert any ValueError objects in ctx to strings
        if "ctx" in clean_error and isinstance(clean_error["ctx"], dict):
            clean_ctx = {}
            for key, value in clean_error["ctx"].items():
                if isinstance(value, ValueError):
                    clean_ctx[key] = str(value)
                else:
                    clean_ctx[key] = value
            clean_error["ctx"] = clean_ctx

        errors.append(clean_error)

    logger.error(
        f"Validation error on {request.method} {request.url}: {errors}",
        extra={
            "validation_errors": errors,
            "body": exc.body if hasattr(exc, "body") else None,
        },
    )

    return JSONResponse(
        status_code=422, content={"detail": "Validation error", "errors": errors}
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Custom HTTPException handler to ensure consistent error response format"""
    detail = exc.detail

    # If detail is a structured error response dict, extract the message
    if isinstance(detail, dict) and "error" in detail and "message" in detail["error"]:
        error_message = detail["error"]["message"]
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": error_message},
            headers=getattr(exc, "headers", None),
        )
    # If detail is a dict but not our standard format, try to extract meaningful text
    elif isinstance(detail, dict):
        # Try to find message in various places
        message = detail.get("message") or detail.get("detail") or str(detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": message},
            headers=getattr(exc, "headers", None),
        )
    # If detail is a string, return it as expected by tests
    else:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(detail)},
            headers=getattr(exc, "headers", None),
        )


@app.exception_handler(500)
async def internal_server_error_handler(request: Request, exc):
    """Custom 500 handler for internal server errors with Request ID for correlation"""
    # Extract Request ID from middleware (stored in request.state by RequestIdMiddleware)
    request_id = getattr(request.state, "request_id", None)

    logger.error(
        f"Internal server error on {request.method} {request.url}: {exc}",
        extra={"request_id": request_id} if request_id else {},
    )

    error_response = {"detail": "Internal server error"}
    if request_id:
        error_response["request_id"] = request_id

    return JSONResponse(status_code=500, content=error_response)


@app.get("/")
async def root():
    """Root endpoint with API information."""
    return {
        "message": "FaultMaven API",
        "version": "1.0.0",
        "description": "AI-powered troubleshooting assistant",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/v1/meta/capabilities")
async def get_capabilities():
    """
    Return backend capabilities for browser extension configuration.

    This endpoint is called by the FaultMaven Copilot browser extension
    to detect the deployment mode and configure itself accordingly.

    Returns:
        Backend capabilities including deployment mode, dashboard URL, and feature flags
    """
    from .config.settings import get_settings

    settings = get_settings()

    # Determine deployment mode based on dashboard URL
    # Cloud: https://app.faultmaven.ai (managed SaaS)
    # Self-hosted: localhost or custom domain (customer-managed)
    is_cloud = settings.auth.dashboard_url == "https://app.faultmaven.ai"
    deployment_mode = "cloud" if is_cloud else "self-hosted"

    return {
        "deploymentMode": deployment_mode,
        "kbManagement": "dashboard",
        "dashboardUrl": settings.auth.dashboard_url,
        "features": {
            "extensionKB": False,  # Always false - extension KB removed
            "adminKB": deployment_mode == "cloud",
            "teamWorkspaces": deployment_mode == "cloud",
            "caseHistory": deployment_mode == "cloud",
            "sso": deployment_mode == "cloud",
        },
        "limits": {
            "maxFileBytes": 10485760,  # 10MB
            "allowedExtensions": [
                ".md",
                ".txt",
                ".log",
                ".json",
                ".csv",
                ".yaml",
                ".yml",
            ],
        },
        "branding": {
            "name": "FaultMaven",
            "supportUrl": "https://github.com/FaultMaven/faultmaven/issues",
        },
    }


@app.get("/health")
async def health_check():
    """Enhanced health check endpoint with component-specific metrics and SLA monitoring."""
    from .infrastructure.health.component_monitor import component_monitor
    from .infrastructure.health.sla_tracker import sla_tracker

    # Get component health status
    try:
        component_health_results = await component_monitor.check_all_components()
        overall_status, overall_summary = component_monitor.get_overall_health_status()
        sla_summary = sla_tracker.get_sla_summary()

        # Enhanced health status with component details
        health_status = {
            "status": overall_status.value,
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "overall_sla": sla_summary["overall_sla"],
            "components": {},
            "services": {"session_manager": "active", "api": "running"},
            "summary": overall_summary,
            "sla_status": {
                "active_breaches": sla_summary["active_breaches"],
                "total_breaches_24h": sla_summary["total_breaches_24h"],
                "worst_performing": sla_summary["worst_performing_component"],
                "best_performing": sla_summary["best_performing_component"],
            },
        }

        # Add detailed component information
        for component_name, component_health in component_health_results.items():
            health_status["components"][component_name] = {
                "status": component_health.status.value,
                "response_time_ms": component_health.response_time_ms,
                "last_error": component_health.last_error,
                "uptime_seconds": component_health.uptime_seconds,
                "sla_current": component_health.sla_current,
                "error_count_24h": component_health.error_count_24h,
                "success_count_24h": component_health.success_count_24h,
                "dependencies": component_health.dependencies,
                "metadata": component_health.metadata,
            }

    except Exception as e:
        logger.error(f"Enhanced health check failed: {e}")
        # Fallback to basic health status
        health_status = {
            "status": "degraded",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "error": "Enhanced health monitoring unavailable",
            "services": {"session_manager": "unknown", "api": "running"},
        }

    # Add session manager health and metrics
    try:
        if "session_manager" in app.extra:
            session_manager = app.extra["session_manager"]
            session_metrics = session_manager.get_session_metrics()

            # Determine session manager health status
            session_status = "healthy"
            if session_metrics["active_sessions"] > 1000:
                session_status = "degraded"
            elif session_metrics["memory_usage_mb"] > session_manager.max_memory_mb:
                session_status = "degraded"

            health_status["services"]["session_manager"] = {
                "status": session_status,
                "metrics": session_metrics,
            }
    except Exception as e:
        logger.warning(f"Failed to get session manager health: {e}")
        health_status["services"]["session_manager"] = "unknown"

    # Add DI container health if available
    try:
        if "di_container" in app.extra:
            container_instance = app.extra["di_container"]
            if hasattr(container_instance, "health_check"):
                container_health = container_instance.health_check()
                health_status["services"]["di_container"] = container_health["status"]
                health_status["container_components"] = container_health.get(
                    "components", {}
                )

                # Add container initialization status for debugging
                health_status["container_initialized"] = getattr(
                    container_instance, "_initialized", False
                )
                health_status["container_initializing"] = getattr(
                    container_instance, "_initializing", False
                )
    except Exception as e:
        logger.warning(f"Failed to get DI container health: {e}")
        health_status["services"]["di_container"] = "unknown"

    return health_status


@app.get("/health/dependencies")
async def health_check_dependencies():
    """Enhanced detailed health check for all dependencies with SLA metrics"""
    try:
        from .container import container
        from .infrastructure.health.component_monitor import component_monitor
        from .infrastructure.health.sla_tracker import sla_tracker

        health = container.health_check()

        # Add detailed timing information
        import time

        start_time = time.time()

        # Test each service getter for performance
        service_tests = {}
        services = [
            "agent",
            "data",
            "knowledge",
            "session",
            "llm_provider",
            "sanitizer",
            "tracer",
        ]

        for service_name in services:
            service_start = time.time()
            try:
                service_method = getattr(
                    container,
                    (
                        f"get_{service_name}_service"
                        if service_name in ["agent", "data", "knowledge", "session"]
                        else f"get_{service_name}"
                    ),
                )
                service_instance = service_method()
                service_tests[service_name] = {
                    "available": service_instance is not None,
                    "response_time_ms": round((time.time() - service_start) * 1000, 2),
                }
            except Exception as e:
                service_tests[service_name] = {
                    "available": False,
                    "error": str(e),
                    "response_time_ms": round((time.time() - service_start) * 1000, 2),
                }

        total_time_ms = round((time.time() - start_time) * 1000, 2)

        # Get enhanced component health data
        component_health_results = await component_monitor.check_all_components()
        dependency_map = component_monitor.get_dependency_map()
        critical_dependencies = component_monitor.get_critical_path_dependencies()

        # Get SLA details for each component
        sla_details = {}
        for component_name in component_health_results.keys():
            try:
                sla_details[component_name] = sla_tracker.get_component_sla_details(
                    component_name
                )
            except Exception as e:
                logger.warning(f"Failed to get SLA details for {component_name}: {e}")
                sla_details[component_name] = {"error": str(e)}

        return {
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "container_health": health,
            "service_tests": service_tests,
            "component_health": {
                component_name: {
                    "status": health.status.value,
                    "response_time_ms": health.response_time_ms,
                    "sla_current": health.sla_current,
                    "last_error": health.last_error,
                    "dependencies": health.dependencies,
                    "metadata": health.metadata,
                }
                for component_name, health in component_health_results.items()
            },
            "dependency_mapping": {
                "all_dependencies": dependency_map,
                "critical_dependencies": critical_dependencies,
            },
            "sla_metrics": sla_details,
            "performance": {
                "total_response_time_ms": total_time_ms,
                "container_initialized": getattr(container, "_initialized", False),
                "container_initializing": getattr(container, "_initializing", False),
                "health_check_overhead_ms": round((time.time() - start_time) * 1000, 2),
            },
        }
    except Exception as e:
        logger.error(f"Enhanced dependency health check failed: {e}")
        return {
            "error": f"Enhanced dependency health check failed: {e}",
            "container_available": False,
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
        }


@app.get("/readiness")
async def readiness():
    """Readiness probe: return unready if Redis or ChromaDB are unavailable."""
    try:
        from .container import container

        await container.initialize()
        if getattr(container, "session_store", None) is None:
            return {"status": "unready", "reason": "redis_unavailable"}
        if getattr(container, "vector_store", None) is None:
            return {"status": "unready", "reason": "chromadb_unavailable"}
        return {"status": "ready"}
    except Exception as e:
        return {"status": "unready", "reason": str(e)}


@app.get("/health/logging")
async def logging_health_check():
    """Get logging system health status."""
    try:
        from faultmaven.infrastructure.logging.coordinator import LoggingCoordinator

        coordinator = LoggingCoordinator()
        health_status = coordinator.get_health_status()

        # Add timestamp and additional metadata
        health_status["timestamp"] = to_json_compatible(datetime.now(timezone.utc))
        health_status["service"] = "logging"

        return health_status
    except Exception as e:
        logger.error(f"Logging health check failed: {e}")
        return {
            "status": "error",
            "error": f"Logging health check failed: {e}",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "service": "logging",
        }


@app.get("/health/sla")
async def health_check_sla():
    """Get SLA status and metrics for all components."""
    try:
        from .infrastructure.health.sla_tracker import sla_tracker

        sla_summary = sla_tracker.get_sla_summary()

        # Get detailed SLA information for each component
        detailed_sla = {}
        for component_name in sla_tracker.component_thresholds.keys():
            try:
                detailed_sla[component_name] = sla_tracker.get_component_sla_details(
                    component_name
                )
            except Exception as e:
                logger.warning(f"Failed to get SLA details for {component_name}: {e}")
                detailed_sla[component_name] = {"error": str(e)}

        return {
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "summary": sla_summary,
            "components": detailed_sla,
        }

    except Exception as e:
        logger.error(f"SLA health check failed: {e}")
        return {
            "error": f"SLA health check failed: {e}",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
        }


@app.get("/health/components/{component_name}")
async def health_check_component(component_name: str):
    """Get detailed health information for a specific component."""
    try:
        from .infrastructure.health.component_monitor import component_monitor
        from .infrastructure.health.sla_tracker import sla_tracker

        # Get component health
        component_health = await component_monitor.check_component_health(
            component_name
        )

        # Get component metrics
        component_metrics = component_monitor.get_component_metrics(component_name)

        # Get SLA details
        try:
            sla_details = sla_tracker.get_component_sla_details(component_name)
        except Exception as e:
            logger.warning(f"Failed to get SLA details for {component_name}: {e}")
            sla_details = {"error": str(e)}

        return {
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "component_name": component_name,
            "health": {
                "status": component_health.status.value,
                "response_time_ms": component_health.response_time_ms,
                "last_error": component_health.last_error,
                "uptime_seconds": component_health.uptime_seconds,
                "sla_current": component_health.sla_current,
                "dependencies": component_health.dependencies,
                "metadata": component_health.metadata,
            },
            "metrics": component_metrics,
            "sla": sla_details,
        }

    except Exception as e:
        logger.error(f"Component health check failed for {component_name}: {e}")
        return {
            "error": f"Component health check failed: {e}",
            "component_name": component_name,
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
        }


@app.get("/health/patterns")
async def health_check_error_patterns():
    """Get error patterns and recovery information from enhanced error context."""
    try:
        from .infrastructure.logging.coordinator import LoggingCoordinator

        coordinator = LoggingCoordinator()
        context = coordinator.get_context()

        if context and context.error_context:
            error_context = context.error_context

            return {
                "timestamp": to_json_compatible(datetime.now(timezone.utc)),
                "escalation_level": error_context.escalation_level.value,
                "detected_patterns": error_context.get_pattern_summary(),
                "recovery_summary": error_context.get_recovery_summary(),
                "layer_errors": {
                    layer: {
                        "error_count": info.get("error_count", 0),
                        "severity_score": info.get("severity_score", 0.0),
                        "last_error_time": info.get("last_error_time"),
                    }
                    for layer, info in error_context.layer_errors.items()
                },
            }
        else:
            return {
                "timestamp": to_json_compatible(datetime.now(timezone.utc)),
                "message": "No active error context",
                "patterns": [],
                "recovery_attempts": [],
            }

    except Exception as e:
        logger.error(f"Error patterns health check failed: {e}")
        return {
            "error": f"Error patterns health check failed: {e}",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
        }


@app.get("/metrics/performance")
async def get_performance_metrics():
    """Get comprehensive performance metrics."""
    try:
        from .api.middleware.performance import PerformanceMetricsEndpoint
        from .infrastructure.monitoring.alerting import alert_manager
        from .infrastructure.monitoring.apm_integration import apm_integration
        from .infrastructure.monitoring.metrics_collector import metrics_collector

        # Find the performance middleware instance
        performance_middleware = None
        for middleware in app.user_middleware:
            if (
                hasattr(middleware, "cls")
                and middleware.cls.__name__ == "PerformanceTrackingMiddleware"
            ):
                performance_middleware = middleware
                break

        if performance_middleware:
            metrics_endpoint = PerformanceMetricsEndpoint(performance_middleware)
            return await metrics_endpoint.get_performance_metrics()
        else:
            # Return basic metrics if middleware not found
            return {
                "timestamp": to_json_compatible(datetime.now(timezone.utc)),
                "error": "Performance middleware not found",
                "metrics_collector": metrics_collector.get_metrics_summary(),
                "apm_integration": apm_integration.get_export_statistics(),
                "alerting": alert_manager.get_alert_statistics(),
            }

    except Exception as e:
        logger.error(f"Performance metrics endpoint failed: {e}")
        return {
            "error": f"Performance metrics failed: {e}",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
        }


@app.get("/metrics/realtime")
async def get_realtime_metrics(time_window_minutes: int = 5):
    """Get real-time performance metrics."""
    try:
        from .infrastructure.monitoring.alerting import alert_manager
        from .infrastructure.monitoring.metrics_collector import metrics_collector

        # Validate time window
        if time_window_minutes < 1 or time_window_minutes > 60:
            time_window_minutes = 5

        dashboard_data = metrics_collector.get_dashboard_data(time_window_minutes)
        active_alerts = alert_manager.get_active_alerts()

        return {
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "time_window_minutes": time_window_minutes,
            "dashboard": dashboard_data,
            "active_alerts": [
                {
                    "rule_name": alert.rule_name,
                    "severity": alert.severity.value,
                    "metric_name": alert.metric_name,
                    "metric_value": alert.metric_value,
                    "threshold_value": alert.threshold_value,
                    "triggered_at": to_json_compatible(alert.triggered_at),
                    "message": alert.message,
                }
                for alert in active_alerts[:10]  # Last 10 alerts
            ],
        }

    except Exception as e:
        logger.error(f"Real-time metrics endpoint failed: {e}")
        return {
            "error": f"Real-time metrics failed: {e}",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
        }


@app.get("/metrics/alerts")
async def get_alert_status():
    """Get current alert status and statistics."""
    try:
        from .infrastructure.monitoring.alerting import alert_manager

        active_alerts = alert_manager.get_active_alerts()
        alert_stats = alert_manager.get_alert_statistics()

        return {
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "statistics": alert_stats,
            "active_alerts": [
                {
                    "alert_id": alert.alert_id,
                    "rule_name": alert.rule_name,
                    "severity": alert.severity.value,
                    "status": alert.status.value,
                    "metric_name": alert.metric_name,
                    "metric_value": alert.metric_value,
                    "threshold_value": alert.threshold_value,
                    "triggered_at": to_json_compatible(alert.triggered_at),
                    "resolved_at": (
                        to_json_compatible(alert.resolved_at)
                        if alert.resolved_at
                        else None
                    ),
                    "message": alert.message,
                    "notification_count": alert.notification_count,
                }
                for alert in active_alerts
            ],
        }

    except Exception as e:
        logger.error(f"Alert status endpoint failed: {e}")
        return {
            "error": f"Alert status failed: {e}",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
        }


@app.get("/metrics/optimization")
async def get_system_optimization_metrics():
    """Get comprehensive system optimization metrics."""
    try:
        # Find system optimization middleware
        system_opt_middleware = None
        for middleware in app.user_middleware:
            if (
                hasattr(middleware, "cls")
                and middleware.cls.__name__ == "SystemOptimizationMiddleware"
            ):
                system_opt_middleware = middleware.cls
                break

        optimization_metrics = {}
        if system_opt_middleware and hasattr(
            system_opt_middleware, "get_optimization_metrics"
        ):
            optimization_metrics = system_opt_middleware.get_optimization_metrics()

        # Get resource optimization metrics if available
        resource_metrics = {}
        try:
            from .container import container

            if hasattr(container, "_resource_optimization_service"):
                resource_service = container._resource_optimization_service
                if resource_service and hasattr(
                    resource_service, "get_resource_usage_stats"
                ):
                    resource_metrics = await resource_service.get_resource_usage_stats()
        except Exception as e:
            logger.warning(f"Failed to get resource optimization metrics: {e}")

        # Get LLM router optimization metrics if available
        llm_optimization_metrics = {}
        try:
            from .container import container

            llm_provider = container.get_llm_provider()
            if hasattr(llm_provider, "get_optimization_metrics"):
                llm_optimization_metrics = llm_provider.get_optimization_metrics()
        except Exception as e:
            logger.warning(f"Failed to get LLM optimization metrics: {e}")

        return {
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "system_optimization": optimization_metrics,
            "resource_optimization": resource_metrics,
            "llm_optimization": llm_optimization_metrics,
            "optimization_summary": {
                "total_optimizations_applied": sum(
                    [
                        optimization_metrics.get("requests_processed", 0),
                        resource_metrics.get("optimization_metrics", {}).get(
                            "memory_pools_created", 0
                        ),
                        llm_optimization_metrics.get("requests_batched", 0),
                    ]
                ),
                "performance_improvements": {
                    "response_compression": optimization_metrics.get(
                        "compression_ratio", 0.0
                    ),
                    "cache_hit_rate": optimization_metrics.get("cache_hit_rate", 0.0),
                    "memory_pool_efficiency": resource_metrics.get(
                        "memory_pools", {}
                    ).get("efficiency", 0.0),
                    "llm_batching_efficiency": llm_optimization_metrics.get(
                        "optimization_status", {}
                    ).get("batching_enabled", False),
                },
            },
        }

    except Exception as e:
        logger.error(f"System optimization metrics endpoint failed: {e}")
        return {
            "error": f"System optimization metrics failed: {e}",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
        }


@app.get("/admin/optimization/trigger-cleanup")
async def trigger_system_cleanup():
    """Trigger comprehensive system cleanup and optimization."""
    try:
        cleanup_results = {}

        # Trigger resource optimization cleanup if available
        try:
            from .container import container

            if hasattr(container, "_resource_optimization_service"):
                resource_service = container._resource_optimization_service
                if resource_service:
                    cleanup_results["resource_cleanup"] = (
                        await resource_service.trigger_resource_cleanup(aggressive=True)
                    )
        except Exception as e:
            cleanup_results["resource_cleanup"] = {"error": str(e)}

        # Trigger manual garbage collection
        import gc

        collected_objects = gc.collect()
        cleanup_results["garbage_collection"] = {
            "objects_collected": collected_objects,
            "memory_freed": True,
        }

        # Clear system optimization middleware caches if available
        for middleware in app.user_middleware:
            if (
                hasattr(middleware, "cls")
                and middleware.cls.__name__ == "SystemOptimizationMiddleware"
            ):
                try:
                    if hasattr(middleware.cls, "_response_cache"):
                        cache_size = len(middleware.cls._response_cache)
                        middleware.cls._response_cache.clear()
                        middleware.cls._cache_access_times.clear()
                        middleware.cls._cache_hit_counts.clear()
                        cleanup_results["cache_cleanup"] = {
                            "entries_cleared": cache_size,
                            "cache_reset": True,
                        }
                except Exception as e:
                    cleanup_results["cache_cleanup"] = {"error": str(e)}
                break

        cleanup_results["timestamp"] = to_json_compatible(datetime.now(timezone.utc))
        cleanup_results["cleanup_triggered"] = True

        return cleanup_results

    except Exception as e:
        logger.error(f"System cleanup trigger failed: {e}")
        return {
            "error": f"System cleanup failed: {e}",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "cleanup_triggered": False,
        }


if __name__ == "__main__":
    import uvicorn

    # Configuration from unified settings
    from faultmaven.config.settings import get_settings

    settings = get_settings()
    host = settings.server.host
    port = settings.server.port
    reload = settings.server.reload
    workers = settings.server.workers

    # Start server
    # Note: workers parameter is only used if > 1 (uvicorn defaults to 1 worker if not specified)
    # Validation happens in lifespan startup, which will catch invalid configurations
    if workers > 1:
        uvicorn.run(
            "faultmaven.main:app",
            host=host,
            port=port,
            reload=reload,
            workers=workers,
            log_level="info",
        )
    else:
        uvicorn.run(
            "faultmaven.main:app", host=host, port=port, reload=reload, log_level="info"
        )
