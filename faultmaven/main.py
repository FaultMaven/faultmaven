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
from typing import Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from opik import OpikMiddleware

from .api import data_ingestion, query_processing, kb_management
from .session_management import SessionManager
from .observability.tracing import setup_tracing
from .models import SessionContext


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global application state
app_state: Dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup and shutdown events."""
    # Startup
    logger.info("Starting FaultMaven API server...")
    
    # Initialize core services
    app_state["session_manager"] = SessionManager()
    
    # Setup tracing
    setup_tracing()
    
    logger.info("FaultMaven API server started successfully")
    
    yield
    
    # Shutdown
    logger.info("Shutting down FaultMaven API server...")
    
    # Cleanup resources
    if "session_manager" in app_state:
        # Cleanup any active sessions
        session_manager = app_state["session_manager"]
        cleaned_count = session_manager.cleanup_inactive_sessions()
        logger.info(f"Cleaned up {cleaned_count} expired sessions")
    
    logger.info("FaultMaven API server shutdown complete")


# Create FastAPI application
app = FastAPI(
    title="FaultMaven API",
    description="AI-powered troubleshooting assistant for Engineers, SREs, and DevOps professionals",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
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

# Add Opik tracing middleware
app.add_middleware(OpikMiddleware)

# Include API routers
app.include_router(
    data_ingestion.router,
    prefix="/api/v1",
    tags=["data_ingestion"]
)

app.include_router(
    query_processing.router,
    prefix="/api/v1",
    tags=["query_processing"]
)

app.include_router(
    kb_management.router,
    prefix="/api/v1",
    tags=["knowledge_base"]
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
    """Health check endpoint."""
    return {
        "status": "healthy",
        "services": {
            "session_manager": "active",
            "api": "running"
        }
    }


@app.get("/api/v1/sessions")
async def list_sessions():
    """List all active sessions (for debugging)."""
    session_manager = app_state.get("session_manager")
    if not session_manager:
        raise HTTPException(status_code=503, detail="Session manager not available")
    
    sessions = session_manager.list_sessions()
    return {
        "sessions": [
            {
                "session_id": session.session_id,
                "user_id": session.user_id,
                "created_at": session.created_at.isoformat(),
                "last_activity": session.last_activity.isoformat(),
                "data_uploads_count": len(session.data_uploads)
            }
            for session in sessions
        ],
        "total": len(sessions)
    }


@app.post("/api/v1/sessions")
async def create_session(user_id: str = None):
    """Create a new troubleshooting session."""
    session_manager = app_state.get("session_manager")
    if not session_manager:
        raise HTTPException(status_code=503, detail="Session manager not available")
    
    session = session_manager.create_session(user_id)
    return {
        "session_id": session.session_id,
        "user_id": session.user_id,
        "created_at": session.created_at.isoformat(),
        "message": "Session created successfully"
    }


if __name__ == "__main__":
    import uvicorn
    
    # Get configuration from environment
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("RELOAD", "false").lower() == "true"
    
    logger.info(f"Starting FaultMaven API on {host}:{port}")
    
    uvicorn.run(
        "faultmaven.main:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info"
    )

