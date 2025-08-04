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
from typing import Any, Dict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

# Load environment variables from .env file
load_dotenv()
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from .api.v1.routes import agent, data, knowledge, session
from .infrastructure.observability.tracing import init_opik_tracing
from .session_management import SessionManager

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

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

# Global application state
app_state: Dict[str, Any] = {}


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
    
    app_state["session_manager"] = SessionManager(
        redis_url=redis_url,
        redis_host=redis_host,
        redis_port=redis_port,
        redis_password=redis_password
    )

    # Setup tracing
    init_opik_tracing()

    logger.info("FaultMaven API server started successfully")

    yield

    # Shutdown
    logger.info("Shutting down FaultMaven API server...")

    # Cleanup resources
    if "session_manager" in app_state:
        # Cleanup any active sessions
        session_manager = app_state["session_manager"]
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

# Add middleware
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

app.add_middleware(GZipMiddleware, minimum_size=1000)

# Add Opik tracing middleware (if available)
if OPIK_AVAILABLE and OPIK_MIDDLEWARE_AVAILABLE:
    if os.getenv("OPIK_USE_LOCAL", "true").lower() == "true":
        logger.info("Adding OpikMiddleware for local Opik instance")
    else:
        logger.info("Adding OpikMiddleware for cloud instance")
    app.add_middleware(OpikMiddleware)
elif OPIK_AVAILABLE:
    logger.info("Opik SDK available but middleware not found - tracing will work at function level")

# Include API routers
app.include_router(data.router, prefix="/api/v1", tags=["data_ingestion"])

app.include_router(agent.router, prefix="/api/v1", tags=["query_processing"])

app.include_router(knowledge.router, prefix="/api/v1", tags=["knowledge_base"])

app.include_router(session.router, prefix="/api/v1", tags=["session_management"])


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


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "services": {"session_manager": "active", "api": "running"},
    }


@app.get("/api/v1/sessions")
async def list_sessions():
    """List all active sessions (for debugging)."""
    session_manager = app_state.get("session_manager")
    if not session_manager:
        raise HTTPException(status_code=503, detail="Session manager not available")

    sessions = await session_manager.list_sessions()
    return {
        "sessions": [
            {
                "session_id": session.session_id,
                "user_id": session.user_id,
                "created_at": session.created_at.isoformat(),
                "last_activity": session.last_activity.isoformat(),
                "data_uploads_count": len(session.data_uploads),
            }
            for session in sessions
        ],
        "total": len(sessions),
    }


@app.post("/api/v1/sessions")
async def create_session(user_id: str = None):
    """Create a new troubleshooting session."""
    session_manager = app_state.get("session_manager")
    if not session_manager:
        raise HTTPException(status_code=503, detail="Session manager not available")

    session = await session_manager.create_session(user_id)
    return {
        "session_id": session.session_id,
        "user_id": session.user_id,
        "created_at": session.created_at.isoformat(),
        "message": "Session created successfully",
    }


if __name__ == "__main__":
    import uvicorn

    # Get configuration from environment
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("RELOAD", "false").lower() == "true"

    logger.info(f"Starting FaultMaven API on {host}:{port}")

    uvicorn.run(
        "faultmaven.main:app", host=host, port=port, reload=reload, log_level="info"
    )
