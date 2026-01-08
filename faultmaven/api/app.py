"""FastAPI Application Setup (TASK-014, TASK-017)

DEPRECATED: This module is deprecated in favor of faultmaven.main.
Use `faultmaven.main:app` as the canonical entrypoint for the application.

Example:
    # Use the canonical entrypoint
    uvicorn.run("faultmaven.main:app", host="0.0.0.0", port=8000)

This legacy module is maintained for backward compatibility with existing
code that imports from faultmaven.api.app. New code should use faultmaven.main
which provides:
- Proper DI container initialization via lifespan
- Enhanced middleware (observability, rate limiting, protection)
- Unified configuration via settings system
- Production-ready security configuration

Legacy module provides (DO NOT USE FOR NEW CODE):
- Application factory with CORS and exception handlers
- Router registration for cases, sessions, evidence, agent, and auth
- JWT authentication support (TASK-017)
- Health check endpoint
- OpenAPI documentation configuration

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

import warnings

warnings.warn(
    "faultmaven.api.app is deprecated. Use 'faultmaven.main:app' as the canonical entrypoint. "
    "This module will be removed in a future version.",
    DeprecationWarning,
    stacklevel=2
)

from typing import Any, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.routes import cases, sessions, evidence, agent, auth, admin, users
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceError,
    ValidationException,
)


def create_app() -> FastAPI:
    """Create FastAPI application.

    Creates and configures the FastAPI application with:
    - CORS middleware
    - Exception handlers
    - API routers
    - Health check endpoint
    - OpenAPI documentation

    Returns:
        Configured FastAPI application

    Example:
        app = create_app()
        uvicorn.run(app, host="0.0.0.0", port=8000)
    """
    app = FastAPI(
        title="FaultMaven API",
        description=(
            "Evidence-centric troubleshooting platform API.\n\n"
            "FaultMaven provides intelligent troubleshooting assistance through:\n"
            "- Case management for tracking issues\n"
            "- Investigation sessions for systematic debugging\n"
            "- Evidence artifacts for storing relevant files\n"
            "- AI-powered analysis and recommendations"
        ),
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        openapi_tags=[
            {
                "name": "Authentication",
                "description": "JWT authentication and authorization operations",
            },
            {
                "name": "Cases",
                "description": "Case management operations",
            },
            {
                "name": "Sessions",
                "description": "Investigation session operations",
            },
            {
                "name": "Evidence",
                "description": "Evidence artifact operations",
            },
            {
                "name": "Users",
                "description": "User profile and management operations",
            },
            {
                "name": "Agent Execution",
                "description": "Execute AI agents for troubleshooting investigations with streaming support",
            },
            {
                "name": "Admin - User Management",
                "description": "Admin-only user management operations (TASK-019)",
            },
            {
                "name": "Health",
                "description": "Health check and status endpoints",
            },
        ],
    )

    # CORS middleware
    # Note: Configure per environment in production
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # TODO: Configure per environment
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register exception handlers
    exception_handlers = get_exception_handlers()
    for exc_class, handler in exception_handlers.items():
        app.add_exception_handler(exc_class, handler)

    # Register routers
    app.include_router(auth.router)  # Auth endpoints first (login, refresh, etc.)
    app.include_router(users.router)  # User management (TASK-018)
    app.include_router(admin.router)  # Admin user management (TASK-019)
    app.include_router(cases.router)
    app.include_router(sessions.router)
    app.include_router(evidence.router)
    app.include_router(agent.router)

    # Health check endpoint
    @app.get("/health", tags=["Health"])
    async def health_check() -> Dict[str, Any]:
        """Health check endpoint.

        Returns the health status of the API server.
        Use this endpoint for load balancer health checks
        and monitoring.

        Returns:
            Health status dictionary with:
            - status: "healthy"
            - version: API version
        """
        return {
            "status": "healthy",
            "version": "1.0.0",
        }

    @app.get("/", include_in_schema=False)
    async def root() -> Dict[str, Any]:
        """Root endpoint redirect to documentation.

        Returns:
            Redirect information to API documentation
        """
        return {
            "message": "FaultMaven API",
            "docs_url": "/api/docs",
            "redoc_url": "/api/redoc",
            "health_url": "/health",
        }

    return app


# Create default application instance
app = create_app()
