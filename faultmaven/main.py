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

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

# Load environment variables from .env file
load_dotenv()
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

# Configure enhanced logging system first
from .infrastructure.logging_config import setup_logging, get_logger
setup_logging()
logger = get_logger(__name__)

from .config.feature_flags import (
    USE_REFACTORED_API, 
    USE_DI_CONTAINER,
    ENABLE_MIGRATION_LOGGING,
    log_feature_flag_status
)

# Log feature flag status at startup
log_feature_flag_status()

# Conditionally import API routes based on feature flags
if USE_REFACTORED_API:
    if ENABLE_MIGRATION_LOGGING:
        logger.info("Loading refactored API routes")
    from .api.v1.routes import agent_refactored as agent
    from .api.v1.routes import data_refactored as data
    from .api.v1.routes import knowledge  # Knowledge routes not yet refactored
    from .api.v1.routes import session
else:
    if ENABLE_MIGRATION_LOGGING:
        logger.info("Loading original API routes")
    from .api.v1.routes import agent, data, knowledge, session

from .infrastructure.observability.tracing import init_opik_tracing
from .infrastructure.request_coordinator import UnifiedRequestMiddleware
from .session_management import SessionManager

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

    # Initialize core services with K8s support
    # Priority: Individual parameters > REDIS_URL > defaults
    redis_host = os.getenv("REDIS_HOST")
    redis_port = int(os.getenv("REDIS_PORT", "30379")) if os.getenv("REDIS_PORT") else None
    redis_password = os.getenv("REDIS_PASSWORD")
    redis_url = os.getenv("REDIS_URL")
    
    # Store SessionManager in app.extra for centralized access
    app.extra["session_manager"] = SessionManager(
        redis_url=redis_url,
        redis_host=redis_host,
        redis_port=redis_port,
        redis_password=redis_password
    )

    # Initialize DI container if using refactored services
    if USE_DI_CONTAINER:
        if ENABLE_MIGRATION_LOGGING:
            logger.info("Initializing refactored DI container")
        from .container_refactored import container
        container.initialize()
        app.extra["di_container"] = container
        
        # Health check the container
        health = container.health_check()
        logger.info(f"DI container health: {health['status']}")
        if health['status'] != 'healthy':
            logger.warning(f"DI container degraded: {health['components']}")
    else:
        if ENABLE_MIGRATION_LOGGING:
            logger.info("Using original container system")
        from .container import container
        app.extra["di_container"] = container

    # Setup tracing
    init_opik_tracing()

    logger.info("FaultMaven API server started successfully")

    yield

    # Shutdown
    logger.info("Shutting down FaultMaven API server...")

    # Cleanup resources
    if "session_manager" in app.extra:
        # Cleanup any active sessions
        session_manager = app.extra["session_manager"]
        # TODO: Implement cleanup_inactive_sessions method
        # cleaned_count = session_manager.cleanup_inactive_sessions()
        # logger.info(f"Cleaned up {cleaned_count} expired sessions")
        logger.info("Session cleanup skipped - method not implemented")

    logger.info("FaultMaven API server shutdown complete")


# Create FastAPI application
app = FastAPI(
    title="FaultMaven API",
    description="AI-powered troubleshooting assistant for Engineers, "
    "SREs, and DevOps professionals",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Add middleware in optimized order to prevent duplicates
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
)
logger.info(f"After CORS middleware: {[type(m).__name__ for m in app.user_middleware]}")

# 2. GZip middleware
app.add_middleware(GZipMiddleware, minimum_size=1000)
logger.info(f"After GZip middleware: {[type(m).__name__ for m in app.user_middleware]}")

# 3. Unified request logging middleware (replaces old RequestLoggingMiddleware)
logger.info("Adding UnifiedRequestMiddleware to FastAPI app")
app.add_middleware(UnifiedRequestMiddleware)
logger.info(f"After UnifiedRequest middleware: {[type(m).__name__ for m in app.user_middleware]}")

# 4. Opik tracing middleware (if available) - now coordinated with unified logging
if OPIK_AVAILABLE and OPIK_MIDDLEWARE_AVAILABLE:
    if os.getenv("OPIK_USE_LOCAL", "true").lower() == "true":
        logger.info("Adding OpikMiddleware for local Opik instance")
    else:
        logger.info("Adding OpikMiddleware for cloud instance")
    app.add_middleware(OpikMiddleware)
    logger.info(f"After Opik middleware: {[type(m).__name__ for m in app.user_middleware]}")
elif OPIK_AVAILABLE:
    logger.info("Opik SDK available but middleware not found - tracing will work at function level")

logger.info(f"Final middleware stack: {[type(m).__name__ for m in app.user_middleware]}")

# Include API routers
app.include_router(data.router, prefix="/api/v1", tags=["data_ingestion"])

app.include_router(agent.router, prefix="/api/v1", tags=["query_processing"])

app.include_router(knowledge.router, prefix="/api/v1", tags=["knowledge_base"])

app.include_router(session.router, prefix="/api/v1", tags=["session_management"])


@app.get("/")
async def root():
    """Root endpoint with API information."""
    from .config.feature_flags import get_migration_strategy
    
    return {
        "message": "FaultMaven API",
        "version": "1.0.0",
        "description": "AI-powered troubleshooting assistant",
        "docs": "/docs",
        "health": "/health",
        "architecture": {
            "migration_strategy": get_migration_strategy(),
            "using_refactored_api": USE_REFACTORED_API,
            "using_di_container": USE_DI_CONTAINER
        }
    }


@app.get("/health")
async def health_check():
    """Health check endpoint with architecture status."""
    from .config.feature_flags import get_migration_strategy, is_migration_safe
    
    # Basic health status
    health_status = {
        "status": "healthy",
        "services": {"session_manager": "active", "api": "running"},
        "architecture": {
            "migration_strategy": get_migration_strategy(),
            "migration_safe": is_migration_safe(),
            "using_refactored_api": USE_REFACTORED_API,
            "using_di_container": USE_DI_CONTAINER
        }
    }
    
    # Add DI container health if available
    try:
        if "di_container" in app.extra:
            container_instance = app.extra["di_container"]
            if hasattr(container_instance, 'health_check'):
                container_health = container_instance.health_check()
                health_status["services"]["di_container"] = container_health["status"]
                health_status["architecture"]["container_components"] = container_health.get("components", {})
    except Exception as e:
        logger.warning(f"Failed to get DI container health: {e}")
        health_status["services"]["di_container"] = "unknown"
    
    return health_status


# Additional endpoints for session management (legacy support)
@app.get("/api/v1/sessions")
async def list_sessions():
    """List all sessions (legacy endpoint)."""
    # This endpoint is now handled by the session router
    # Keeping for backward compatibility
    return {"message": "Use /api/v1/sessions/ for session management"}


@app.post("/api/v1/sessions")
async def create_session(user_id: str = None):
    """Create a new session (legacy endpoint)."""
    # This endpoint is now handled by the session router
    # Keeping for backward compatibility
    return {"message": "Use /api/v1/sessions/ for session creation"}


if __name__ == "__main__":
    import uvicorn
    
    # Configuration
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("RELOAD", "false").lower() == "true"
    
    # Start server
    uvicorn.run(
        "faultmaven.main:app", host=host, port=port, reload=reload, log_level="info"
    )
