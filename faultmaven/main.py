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
from faultmaven.utils.serialization import to_json_compatible

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

# Configure enhanced logging system first
from .infrastructure.logging.config import get_logger
logger = get_logger(__name__)

def _is_test_environment() -> bool:
    """Detect if we're running in a test environment (pytest or skip_service_checks)."""
    # Check for pytest in command line arguments
    if 'pytest' in ' '.join(sys.argv) or any('test' in arg.lower() for arg in sys.argv):
        return True
    
    # Check for common test environment variables
    if os.getenv('SKIP_SERVICE_CHECKS', '').lower() == 'true':
        return True
        
    if os.getenv('PYTEST_CURRENT_TEST'):
        return True
        
    # Check if we're being imported by pytest
    if 'pytest' in sys.modules:
        return True
        
    return False

# Import API routes
from .api.v1.routes import data, knowledge, session, auth

# Import case routes (always available in production)
from .api.v1.routes import case
CASE_ROUTES_AVAILABLE = True

# Import user KB routes
from .api.v1.routes import user_kb

# Import jobs routes
from .api.v1.routes import jobs

from .infrastructure.observability.tracing import init_opik_tracing
from .api.middleware.logging import LoggingMiddleware
# SessionManager now handled via DI container - services.session.SessionService

# Optional opik middleware import
try:
    import opik
    # Try different middleware import patterns for different Opik versions
    try:
        from opik.integrations.fastapi import OpikMiddleware
        OPIK_MIDDLEWARE_AVAILABLE = True
    except ImportError:
        try:
            from opik import OpikMiddleware
            OPIK_MIDDLEWARE_AVAILABLE = True
        except ImportError:
            OPIK_MIDDLEWARE_AVAILABLE = False
            logger.info("Opik middleware class not available, tracing will work without middleware")
    
    OPIK_AVAILABLE = True
    logger.info("Opik SDK loaded successfully")
except ImportError:
    logger.warning("Opik not available, running without tracing")
    OPIK_AVAILABLE = False
    OPIK_MIDDLEWARE_AVAILABLE = False

# Note: For local Opik, we'll rely on environment variable configuration
# The Opik SDK should pick up the custom URL and headers automatically

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
        logger.info("Configuration validated successfully")
        
        # Make configuration available to app
        app.extra["settings"] = settings
    except Exception as e:
        logger.error(f"Configuration initialization failed: {e}")
        raise

    # Initialize the DI container first (before any services that depend on it)
    logger.info("Initializing DI container...")
    try:
        from .container import container
        await container.initialize()
        logger.info("✅ DI container initialized successfully")

        # Make container available to app for access by other components
        app.extra["di_container"] = container
    except Exception as e:
        logger.error(f"DI container initialization failed: {e}")
        # Don't fail startup - let services use fallback implementations
        logger.warning("Continuing with fallback service implementations")

    # Initialize core services with K8s support
    # SessionManager replaced by services.session.SessionService via DI container
    # Access via: container.get_session_service()

    # Pre-load expensive ML models during startup (not per-request)
    logger.info("Pre-loading ML models...")
    try:
        from .infrastructure.model_cache import model_cache
        bge_model = model_cache.get_bge_m3_model()
        if bge_model:
            logger.info("✅ BGE-M3 model pre-loaded successfully")
        else:
            logger.warning("⚠️ BGE-M3 model not available")
    except Exception as e:
        logger.warning(f"Failed to pre-load ML models: {e}")

    # Setup tracing
    init_opik_tracing()

    # Check and start local LLM services if needed
    try:
        from .infrastructure.llm.local_llm_manager import check_and_start_local_llm_service

        # Check if we're configured to use local LLM providers
        chat_provider = os.getenv("CHAT_PROVIDER", "").lower()
        classifier_provider = os.getenv("CLASSIFIER_PROVIDER", "").lower()

        local_llm_model = os.getenv("LOCAL_LLM_MODEL", "llama2-7b")
        local_llm_base_url = os.getenv("LOCAL_LLM_URL", "http://localhost:8080")

        if chat_provider == "local":
            logger.info("Chat provider set to 'local', checking local LLM service...")
            success = await check_and_start_local_llm_service("local", local_llm_base_url, local_llm_model)
            if success:
                logger.info("✅ Local LLM service ready for chat provider")
            else:
                logger.warning("⚠️ Failed to start local LLM service for chat provider")

        if classifier_provider == "local":
            logger.info("Classifier provider set to 'local', checking local LLM service...")
            success = await check_and_start_local_llm_service("local", local_llm_base_url, local_llm_model)
            if success:
                logger.info("✅ Local LLM service ready for classifier provider")
            else:
                logger.warning("⚠️ Failed to start local LLM service for classifier provider")

        if chat_provider != "local" and classifier_provider != "local":
            logger.info("No local LLM providers configured, skipping local service check")

    except Exception as e:
        logger.warning(f"Local LLM service check failed (non-critical): {e}")

    # Initialize Phase 2 monitoring components
    try:
        from .infrastructure.monitoring.apm_integration import apm_integration
        from .infrastructure.monitoring.alerting import alert_manager, setup_default_alert_rules

        # Start APM integration background export
        apm_integration.start_background_export()
        logger.info("✅ APM integration started")

        # Set up default alert rules
        setup_default_alert_rules()
        logger.info("✅ Default alert rules configured")

        logger.info("✅ Phase 2 monitoring components initialized")

    except Exception as e:
        logger.warning(f"Phase 2 monitoring initialization failed (non-critical): {e}")

    # Start case collection cleanup scheduler for Working Memory feature
    case_cleanup_scheduler = None
    try:
        from .infrastructure.tasks import start_case_cleanup_scheduler

        # Only start if both case_vector_store and case_store are available
        case_vector_store = getattr(container, 'case_vector_store', None)
        case_store = getattr(container, 'case_store', None)
        if case_vector_store and case_store:
            case_cleanup_scheduler = start_case_cleanup_scheduler(
                case_vector_store=case_vector_store,
                case_store=case_store,
                interval_hours=6  # Run cleanup every 6 hours
            )
            logger.info("✅ Case collection cleanup scheduler started (Working Memory lifecycle-based)")
            app.extra["case_cleanup_scheduler"] = case_cleanup_scheduler
        else:
            logger.debug("Case collection cleanup scheduler skipped (missing case_vector_store or case_store)")
    except Exception as e:
        logger.warning(f"Case cleanup scheduler initialization failed (non-critical): {e}")

    logger.info("🚀 FaultMaven API server startup COMPLETE - ready to serve fast requests!")

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
        logger.info(f"Initial middleware stack: {[type(m).__name__ for m in app.user_middleware]}")

    # 1. CORS middleware (first - handles preflight requests)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "chrome-extension://*",  # Browser extension
            "http://localhost:3000",  # Local development
            "http://localhost:8000",  # Local API
            "https://faultmaven.ai",  # Production domain
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "Location",           # Resource creation endpoints
            "X-Total-Count",      # Pagination
            "Link",               # Pagination/deprecation links
            "Deprecation",        # Deprecation headers
            "Sunset",             # Deprecation sunset date
            "X-Request-ID",       # Request correlation
            "Retry-After",        # Rate limiting
        ],
    )
    if logging_enabled:
        logger.info(f"After CORS middleware: {[type(m).__name__ for m in app.user_middleware]}")

    # 2. Trailing slash middleware (after CORS, prevents 307 redirects)
    try:
        from .api.middleware.trailing_slash import TrailingSlashMiddleware
        app.add_middleware(TrailingSlashMiddleware)
        if logging_enabled:
            logger.info("✅ Trailing slash middleware added")
    except Exception as e:
        logger.warning(f"Failed to add trailing slash middleware: {e}")
    
    if logging_enabled:
        logger.info(f"After trailing slash middleware: {[type(m).__name__ for m in app.user_middleware]}")

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
                logger.info("Skipping Idempotency middleware (SKIP_SERVICE_CHECKS=True)")
    except Exception as e:
        logger.warning(f"Failed to add idempotency middleware: {e}")
    
    if logging_enabled:
        logger.info(f"After Idempotency middleware: {[type(m).__name__ for m in app.user_middleware]}")

    # 4. Request ID and Rate Limiting Headers middleware - skip in test environments
    try:
        if not settings.server.skip_service_checks and not _is_test_environment():
            from .api.middleware.request_id import RequestIdMiddleware, RateLimitHeaderMiddleware
            
            # Add Request ID middleware
            app.add_middleware(RequestIdMiddleware)
            
            # Add Rate Limiting Headers middleware  
            app.add_middleware(RateLimitHeaderMiddleware, default_limit=1000, window_seconds=3600)
            
            if logging_enabled:
                logger.info("✅ Request ID and Rate Limiting Headers middleware added")
        else:
            if logging_enabled:
                logger.info("Skipping Request ID middleware (test environment or SKIP_SERVICE_CHECKS=True)")
            
    except Exception as e:
        logger.warning(f"Failed to add request ID middleware: {e}")
    
    if logging_enabled:
        logger.info(f"After Request ID middleware: {[type(m).__name__ for m in app.user_middleware]}")

    # 5. Protection middleware (early in stack for security)
    try:
        from .api.protection import setup_protection_middleware
        if not settings.server.skip_service_checks:
            protection_info = setup_protection_middleware(app, environment=settings.server.environment)
            if logging_enabled:
                if protection_info.get("protection_enabled"):
                    middleware_names = protection_info.get("middleware_added", [])
                    logger.info(f"✅ Protection middleware enabled: {middleware_names}")
                    if protection_info.get("warnings"):
                        logger.warning(f"Protection warnings: {protection_info['warnings']}")
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
        logger.info(f"After Protection middleware: {[type(m).__name__ for m in app.user_middleware]}")

    # 3. GZip middleware
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    if logging_enabled:
        logger.info(f"After GZip middleware: {[type(m).__name__ for m in app.user_middleware]}")

    # 4. New unified logging middleware (integrates with Phase 1 & 2 infrastructure)
    if logging_enabled:
        logger.info("Adding LoggingMiddleware to FastAPI app")
    app.add_middleware(LoggingMiddleware)
    if logging_enabled:
        logger.info(f"After LoggingMiddleware: {[type(m).__name__ for m in app.user_middleware]}")

    # 5. Performance tracking middleware (Phase 2 enhancement)
    from .api.middleware.performance import PerformanceTrackingMiddleware
    if not settings.server.skip_service_checks:
        if logging_enabled:
            logger.info("Adding PerformanceTrackingMiddleware to FastAPI app")
        app.add_middleware(PerformanceTrackingMiddleware, service_name="faultmaven_api")
        if logging_enabled:
            logger.info(f"After PerformanceTrackingMiddleware: {[type(m).__name__ for m in app.user_middleware]}")
    else:
        if logging_enabled:
            logger.info("Skipping PerformanceTrackingMiddleware (SKIP_SERVICE_CHECKS=True)")
    
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
            compression_threshold=1024
        )
    else:
        if logging_enabled:
            logger.info("Skipping SystemOptimizationMiddleware (test environment or SKIP_SERVICE_CHECKS=True)")
    if logging_enabled:
        logger.info(f"After SystemOptimizationMiddleware: {[type(m).__name__ for m in app.user_middleware]}")

    # 7. Opik tracing middleware (if available) - skip in test environments
    if OPIK_AVAILABLE and OPIK_MIDDLEWARE_AVAILABLE and not settings.server.skip_service_checks and not _is_test_environment():
        if logging_enabled:
            if settings.observability.opik_use_local:
                logger.info("Adding OpikMiddleware for local Opik instance")
            else:
                logger.info("Adding OpikMiddleware for cloud instance")
        app.add_middleware(OpikMiddleware)
        if logging_enabled:
            logger.info(f"After Opik middleware: {[type(m).__name__ for m in app.user_middleware]}")
    elif OPIK_AVAILABLE and logging_enabled:
        if settings.server.skip_service_checks or _is_test_environment():
            logger.info("Skipping OpikMiddleware (test environment or SKIP_SERVICE_CHECKS=True)")
        else:
            logger.info("Opik SDK available but middleware not found - tracing will work at function level")

    # 8. Contract Probe middleware (for API compliance monitoring)
    if not settings.server.skip_service_checks and not _is_test_environment():
        try:
            from .api.middleware.contract_probe import ContractProbeMiddleware
            
            app.add_middleware(
                ContractProbeMiddleware,
                probe_enabled=True,
                log_all_requests=False,  # Only log violations, not all requests
                failure_sample_rate=1.0
            )
            if logging_enabled:
                logger.info("✅ Contract Probe middleware added for API compliance monitoring")
        except Exception as e:
            logger.warning(f"Failed to add contract probe middleware: {e}")

    if logging_enabled:
        logger.info(f"Final middleware stack: {[type(m).__name__ for m in app.user_middleware]}")

# Setup middleware
setup_middleware()

# Include API routers (only those in locked spec)
app.include_router(data.router, prefix="/api/v1", tags=["data_ingestion"])

# REMOVED: agent.router - replaced by case routes with real AgentService integration

app.include_router(knowledge.router, prefix="/api/v1", tags=["knowledge_base"])

# Include kb router for backward compatibility with /kb/ prefix
app.include_router(knowledge.kb_router, prefix="/api/v1", tags=["knowledge_base"])

app.include_router(session.router, prefix="/api/v1", tags=["session_management"])

# Authentication routes
app.include_router(auth.router, prefix="/api/v1", tags=["authentication"])

# Case persistence routes (always included in production)
app.include_router(case.router, prefix="/api/v1", tags=["case_persistence"])
logger.info("✅ Case persistence endpoints added")

# User KB routes
app.include_router(user_kb.router, prefix="/api/v1", tags=["user_kb"])
logger.info("✅ User KB endpoints added")

# Jobs management routes
app.include_router(jobs.router, prefix="/api/v1", tags=["job_management"])
logger.info("✅ Job management endpoints added")

# Debug endpoints (present in locked API spec)
@app.get("/debug/routes")
async def debug_routes():
    """List all registered routes (path + methods)."""
    routes_info = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = list(getattr(route, "methods", []) or [])
        if path:
            routes_info.append({"path": path, "methods": methods})
    return {"routes": routes_info, "count": len(routes_info), "timestamp": to_json_compatible(datetime.now(timezone.utc))}


@app.get("/debug/health")
async def debug_health():
    """Minimal debug health endpoint."""
    return {"status": "ok", "timestamp": to_json_compatible(datetime.now(timezone.utc))}


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

        # Check if strict mode is enabled
        strict_mode = os.getenv("STRICT_PROVIDER_MODE", "false").lower() == "true"

        return {
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "primary_provider": fallback_chain[0] if fallback_chain else "none",
            "strict_mode": strict_mode,
            "fallback_chain": fallback_chain,
            "available_providers": available_providers,
            "provider_details": provider_status
        }

    except Exception as e:
        logger.error(f"Failed to get LLM provider status: {e}")
        return {
            "error": f"Failed to get LLM provider status: {e}",
            "timestamp": to_json_compatible(datetime.now(timezone.utc))
        }

# Modular monolith pivot: keep only core endpoints; advanced routes disabled

# Protection system monitoring endpoints
try:
    from .api.v1.routes import protection
    app.include_router(protection.router, prefix="/api/v1", tags=["protection"])
    logger.info("✅ Protection monitoring endpoints added")
except Exception as e:
    logger.warning(f"Failed to add protection monitoring endpoints: {e}")





# Custom exception handlers
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Custom handler for request/response validation errors (422)"""
    logger.error(f"Validation error on {request.method} {request.url}: {exc.errors()}", extra={
        "validation_errors": exc.errors(),
        "body": exc.body if hasattr(exc, 'body') else None,
    })
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Validation error",
            "errors": exc.errors()
        }
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Custom HTTPException handler to ensure consistent error response format"""
    detail = exc.detail

    # If detail is a structured error response dict, extract the message
    if isinstance(detail, dict) and 'error' in detail and 'message' in detail['error']:
        error_message = detail['error']['message']
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": error_message},
            headers=getattr(exc, 'headers', None)
        )
    # If detail is a dict but not our standard format, try to extract meaningful text
    elif isinstance(detail, dict):
        # Try to find message in various places
        message = (
            detail.get('message') or
            detail.get('detail') or
            str(detail)
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": message},
            headers=getattr(exc, 'headers', None)
        )
    # If detail is a string, return it as expected by tests
    else:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(detail)},
            headers=getattr(exc, 'headers', None)
        )

@app.exception_handler(500) 
async def internal_server_error_handler(request: Request, exc):
    """Custom 500 handler for internal server errors"""
    logger.error(f"Internal server error on {request.method} {request.url}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )


@app.get("/")
async def root():
    """Root endpoint with API information."""
    return {
        "message": "FaultMaven API",
        "version": "1.0.0",
        "description": "AI-powered troubleshooting assistant",
        "docs": "/docs",
        "health": "/health"
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
                "best_performing": sla_summary["best_performing_component"]
            }
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
                "metadata": component_health.metadata
            }
        
    except Exception as e:
        logger.error(f"Enhanced health check failed: {e}")
        # Fallback to basic health status
        health_status = {
            "status": "degraded",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "error": "Enhanced health monitoring unavailable",
            "services": {"session_manager": "unknown", "api": "running"}
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
                "metrics": session_metrics
            }
    except Exception as e:
        logger.warning(f"Failed to get session manager health: {e}")
        health_status["services"]["session_manager"] = "unknown"
    
    # Add DI container health if available
    try:
        if "di_container" in app.extra:
            container_instance = app.extra["di_container"]
            if hasattr(container_instance, 'health_check'):
                container_health = container_instance.health_check()
                health_status["services"]["di_container"] = container_health["status"]
                health_status["container_components"] = container_health.get("components", {})
                
                # Add container initialization status for debugging
                health_status["container_initialized"] = getattr(container_instance, '_initialized', False)
                health_status["container_initializing"] = getattr(container_instance, '_initializing', False)
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
        services = ['agent', 'data', 'knowledge', 'session', 'llm_provider', 'sanitizer', 'tracer']
        
        for service_name in services:
            service_start = time.time()
            try:
                service_method = getattr(container, f'get_{service_name}_service' if service_name in ['agent', 'data', 'knowledge', 'session'] else f'get_{service_name}')
                service_instance = service_method()
                service_tests[service_name] = {
                    "available": service_instance is not None,
                    "response_time_ms": round((time.time() - service_start) * 1000, 2)
                }
            except Exception as e:
                service_tests[service_name] = {
                    "available": False,
                    "error": str(e),
                    "response_time_ms": round((time.time() - service_start) * 1000, 2)
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
                sla_details[component_name] = sla_tracker.get_component_sla_details(component_name)
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
                    "metadata": health.metadata
                }
                for component_name, health in component_health_results.items()
            },
            "dependency_mapping": {
                "all_dependencies": dependency_map,
                "critical_dependencies": critical_dependencies
            },
            "sla_metrics": sla_details,
            "performance": {
                "total_response_time_ms": total_time_ms,
                "container_initialized": getattr(container, '_initialized', False),
                "container_initializing": getattr(container, '_initializing', False),
                "health_check_overhead_ms": round((time.time() - start_time) * 1000, 2)
            }
        }
    except Exception as e:
        logger.error(f"Enhanced dependency health check failed: {e}")
        return {
            "error": f"Enhanced dependency health check failed: {e}",
            "container_available": False,
            "timestamp": to_json_compatible(datetime.now(timezone.utc))
        }


@app.get("/readiness")
async def readiness():
    """Readiness probe: return unready if Redis or ChromaDB are unavailable."""
    try:
        from .container import container
        await container.initialize()
        if getattr(container, 'session_store', None) is None:
            return {"status": "unready", "reason": "redis_unavailable"}
        if getattr(container, 'vector_store', None) is None:
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
            "service": "logging"
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
                detailed_sla[component_name] = sla_tracker.get_component_sla_details(component_name)
            except Exception as e:
                logger.warning(f"Failed to get SLA details for {component_name}: {e}")
                detailed_sla[component_name] = {"error": str(e)}
        
        return {
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "summary": sla_summary,
            "components": detailed_sla
        }
        
    except Exception as e:
        logger.error(f"SLA health check failed: {e}")
        return {
            "error": f"SLA health check failed: {e}",
            "timestamp": to_json_compatible(datetime.now(timezone.utc))
        }


@app.get("/health/components/{component_name}")
async def health_check_component(component_name: str):
    """Get detailed health information for a specific component."""
    try:
        from .infrastructure.health.component_monitor import component_monitor
        from .infrastructure.health.sla_tracker import sla_tracker
        
        # Get component health
        component_health = await component_monitor.check_component_health(component_name)
        
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
                "metadata": component_health.metadata
            },
            "metrics": component_metrics,
            "sla": sla_details
        }
        
    except Exception as e:
        logger.error(f"Component health check failed for {component_name}: {e}")
        return {
            "error": f"Component health check failed: {e}",
            "component_name": component_name,
            "timestamp": to_json_compatible(datetime.now(timezone.utc))
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
                        "last_error_time": info.get("last_error_time")
                    }
                    for layer, info in error_context.layer_errors.items()
                }
            }
        else:
            return {
                "timestamp": to_json_compatible(datetime.now(timezone.utc)),
                "message": "No active error context",
                "patterns": [],
                "recovery_attempts": []
            }
            
    except Exception as e:
        logger.error(f"Error patterns health check failed: {e}")
        return {
            "error": f"Error patterns health check failed: {e}",
            "timestamp": to_json_compatible(datetime.now(timezone.utc))
        }


@app.get("/metrics/performance")
async def get_performance_metrics():
    """Get comprehensive performance metrics."""
    try:
        from .api.middleware.performance import PerformanceMetricsEndpoint
        from .infrastructure.monitoring.metrics_collector import metrics_collector
        from .infrastructure.monitoring.apm_integration import apm_integration
        from .infrastructure.monitoring.alerting import alert_manager
        
        # Find the performance middleware instance
        performance_middleware = None
        for middleware in app.user_middleware:
            if hasattr(middleware, 'cls') and middleware.cls.__name__ == 'PerformanceTrackingMiddleware':
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
                "alerting": alert_manager.get_alert_statistics()
            }
            
    except Exception as e:
        logger.error(f"Performance metrics endpoint failed: {e}")
        return {
            "error": f"Performance metrics failed: {e}",
            "timestamp": to_json_compatible(datetime.now(timezone.utc))
        }


@app.get("/metrics/realtime")
async def get_realtime_metrics(time_window_minutes: int = 5):
    """Get real-time performance metrics."""
    try:
        from .infrastructure.monitoring.metrics_collector import metrics_collector
        from .infrastructure.monitoring.alerting import alert_manager
        
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
                    "triggered_at": alert.triggered_at.isoformat(),
                    "message": alert.message
                }
                for alert in active_alerts[:10]  # Last 10 alerts
            ]
        }
        
    except Exception as e:
        logger.error(f"Real-time metrics endpoint failed: {e}")
        return {
            "error": f"Real-time metrics failed: {e}",
            "timestamp": to_json_compatible(datetime.now(timezone.utc))
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
                    "triggered_at": alert.triggered_at.isoformat(),
                    "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
                    "message": alert.message,
                    "notification_count": alert.notification_count
                }
                for alert in active_alerts
            ]
        }
        
    except Exception as e:
        logger.error(f"Alert status endpoint failed: {e}")
        return {
            "error": f"Alert status failed: {e}",
            "timestamp": to_json_compatible(datetime.now(timezone.utc))
        }


@app.get("/metrics/optimization")
async def get_system_optimization_metrics():
    """Get comprehensive system optimization metrics."""
    try:
        # Find system optimization middleware
        system_opt_middleware = None
        for middleware in app.user_middleware:
            if hasattr(middleware, 'cls') and middleware.cls.__name__ == 'SystemOptimizationMiddleware':
                system_opt_middleware = middleware.cls
                break
        
        optimization_metrics = {}
        if system_opt_middleware and hasattr(system_opt_middleware, 'get_optimization_metrics'):
            optimization_metrics = system_opt_middleware.get_optimization_metrics()
        
        # Get resource optimization metrics if available
        resource_metrics = {}
        try:
            from .container import container
            if hasattr(container, '_resource_optimization_service'):
                resource_service = container._resource_optimization_service
                if resource_service and hasattr(resource_service, 'get_resource_usage_stats'):
                    resource_metrics = await resource_service.get_resource_usage_stats()
        except Exception as e:
            logger.warning(f"Failed to get resource optimization metrics: {e}")
        
        # Get LLM router optimization metrics if available
        llm_optimization_metrics = {}
        try:
            from .container import container
            llm_provider = container.get_llm_provider()
            if hasattr(llm_provider, 'get_optimization_metrics'):
                llm_optimization_metrics = llm_provider.get_optimization_metrics()
        except Exception as e:
            logger.warning(f"Failed to get LLM optimization metrics: {e}")
        
        return {
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "system_optimization": optimization_metrics,
            "resource_optimization": resource_metrics,
            "llm_optimization": llm_optimization_metrics,
            "optimization_summary": {
                "total_optimizations_applied": sum([
                    optimization_metrics.get("requests_processed", 0),
                    resource_metrics.get("optimization_metrics", {}).get("memory_pools_created", 0),
                    llm_optimization_metrics.get("requests_batched", 0)
                ]),
                "performance_improvements": {
                    "response_compression": optimization_metrics.get("compression_ratio", 0.0),
                    "cache_hit_rate": optimization_metrics.get("cache_hit_rate", 0.0),
                    "memory_pool_efficiency": resource_metrics.get("memory_pools", {}).get("efficiency", 0.0),
                    "llm_batching_efficiency": llm_optimization_metrics.get("optimization_status", {}).get("batching_enabled", False)
                }
            }
        }
        
    except Exception as e:
        logger.error(f"System optimization metrics endpoint failed: {e}")
        return {
            "error": f"System optimization metrics failed: {e}",
            "timestamp": to_json_compatible(datetime.now(timezone.utc))
        }


@app.get("/admin/optimization/trigger-cleanup")
async def trigger_system_cleanup():
    """Trigger comprehensive system cleanup and optimization."""
    try:
        cleanup_results = {}
        
        # Trigger resource optimization cleanup if available
        try:
            from .container import container
            if hasattr(container, '_resource_optimization_service'):
                resource_service = container._resource_optimization_service
                if resource_service:
                    cleanup_results["resource_cleanup"] = await resource_service.trigger_resource_cleanup(aggressive=True)
        except Exception as e:
            cleanup_results["resource_cleanup"] = {"error": str(e)}
        
        # Trigger manual garbage collection
        import gc
        collected_objects = gc.collect()
        cleanup_results["garbage_collection"] = {
            "objects_collected": collected_objects,
            "memory_freed": True
        }
        
        # Clear system optimization middleware caches if available
        for middleware in app.user_middleware:
            if hasattr(middleware, 'cls') and middleware.cls.__name__ == 'SystemOptimizationMiddleware':
                try:
                    if hasattr(middleware.cls, '_response_cache'):
                        cache_size = len(middleware.cls._response_cache)
                        middleware.cls._response_cache.clear()
                        middleware.cls._cache_access_times.clear()
                        middleware.cls._cache_hit_counts.clear()
                        cleanup_results["cache_cleanup"] = {
                            "entries_cleared": cache_size,
                            "cache_reset": True
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
            "cleanup_triggered": False
        }


if __name__ == "__main__":
    import uvicorn
    
    # Configuration from unified settings
    from faultmaven.config.settings import get_settings
    settings = get_settings()
    host = settings.server.host
    port = settings.server.port
    reload = settings.server.reload
    
    # Start server
    uvicorn.run(
        "faultmaven.main:app", host=host, port=port, reload=reload, log_level="info"
    )
