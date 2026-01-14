"""API Dependencies Module

Purpose: FastAPI dependency injection functions

This module provides dependency injection functions for FastAPI endpoints,
using the Composition Root pattern (Principle 5).

Key Features:
- Request-scoped dependencies via app.state
- Services are wired in main.py at startup (Composition Root)
- Dependencies access services via request.app.state
- NO container.get_*() calls - that's Service Locator anti-pattern

Pattern:
    # In main.py (Composition Root):
    app.state.session_service = container.get_session_service()

    # In dependencies (this file):
    async def get_session_service(request: Request):
        return request.app.state.session_service
"""

from typing import TYPE_CHECKING, Any, Optional

from fastapi import Depends, HTTPException, Request

from ...models import SessionContext

# TD-001: IReportStore removed - reports now stored via CaseRepository
from ...models.interfaces import IJobService
from ...models.interfaces_case import ICaseService
from ...providers.tenancy.base import TenantProvider

# Lazy import to avoid circular dependency - DataService, SessionService, KnowledgeService imported in functions or TYPE_CHECKING
# OLD: from ...services.agentic.orchestration.agent_service import AgentService (ARCHIVED)
from ...services.preprocessing import PreprocessingService

# Type hints for lazy imports
if TYPE_CHECKING:
    from ...modules.knowledge.domain.services.knowledge_service import KnowledgeService
    from ...services import DataService, SessionService


# Service Dependencies (Composition Root pattern - access via app.state)


async def get_session_service(request: Request):
    """Get SessionService instance from app.state (Composition Root)"""
    return request.app.state.session_service


# OLD OODA AgentService - REMOVED (use InvestigationService instead)
# async def get_agent_service() -> AgentService:
#     """Get AgentService instance from container"""
#     return container.get_agent_service()


async def get_preprocessing_service(request: Request) -> PreprocessingService:
    """Get PreprocessingService instance from app.state (Composition Root)"""
    preprocessing_service = request.app.state.preprocessing_service
    if preprocessing_service is None:
        raise HTTPException(
            status_code=503, detail="Preprocessing service not available"
        )
    return preprocessing_service


async def get_enhanced_agent_service(request: Request):
    """Get EnhancedAgentService instance from app.state (Composition Root)"""
    return request.app.state.enhanced_agent_service


async def get_memory_service(request: Request):
    """Get MemoryService instance from app.state (Composition Root)"""
    return request.app.state.memory_service


async def get_planning_service(request: Request):
    """Get PlanningService instance from app.state (Composition Root)"""
    return request.app.state.planning_service


async def get_case_service(request: Request) -> Optional[ICaseService]:
    """Get CaseService instance from app.state (Composition Root)"""
    return getattr(request.app.state, "case_service", None)


async def get_investigation_service(request: Request):
    """Get InvestigationService instance from app.state (v2.0 milestone-based)"""
    service = request.app.state.investigation_service
    if service is None:
        raise HTTPException(
            status_code=503, detail="Investigation service not available"
        )
    return service


async def get_investigation_orchestrator(request: Request):
    """Get InvestigationOrchestrator instance from app.state (TASK-026)"""
    orchestrator = request.app.state.investigation_orchestrator
    if orchestrator is None:
        raise HTTPException(
            status_code=503, detail="Investigation orchestrator not available"
        )
    return orchestrator


# TD-001: get_report_store removed - reports now accessed via get_case_repository()


async def get_case_repository(request: Request) -> Optional[Any]:
    """Get CaseRepository instance from app.extra (TD-001: use for report persistence)"""
    try:
        container = request.app.extra.get("di_container")
        if container:
            return getattr(container, "case_repository", None)
        return None
    except Exception:
        # Case repository is optional - return None if not available
        return None


async def get_tenant_provider(request: Request) -> Optional[TenantProvider]:
    """Get TenantProvider instance from app.state (TASK-023/024).

    Returns TenantProvider for multi-tenant isolation in API endpoints.
    """
    return getattr(request.app.state, "tenant_provider", None)


async def get_report_generation_service(request: Request):
    """Get ReportGenerationService instance from app.state (TASK-024)"""
    return getattr(request.app.state, "report_generation_service", None)


async def get_report_recommendation_service(request: Request):
    """Get ReportRecommendationService instance from app.state (TASK-024)"""
    return getattr(request.app.state, "report_recommendation_service", None)


async def get_case_vector_store(request: Request):
    """Get CaseVectorStore instance from app.extra"""
    try:
        container = request.app.extra.get("di_container")
        if container:
            return getattr(container, "case_vector_store", None)
        return None
    except Exception:
        # Case vector store is optional - return None if not available
        return None


# Authentication Dependencies


async def get_session_id(request: Request) -> Optional[str]:
    """
    Extract session ID from request headers

    Returns the session ID if present in headers or query params.
    Used for session-based operations and permission checks.
    """
    # Check for session ID in headers (primary method)
    session_id = request.headers.get("X-Session-Id")

    # Fallback: Check for session_id in query params (for testing)
    if not session_id:
        session_id = request.query_params.get("session_id")

    return session_id


async def get_user_id(request: Request) -> Optional[str]:
    """
    Extract user ID from validated session

    Validates the session and returns the authenticated user_id.
    Returns None only if no authentication is provided (for optional auth endpoints).
    """
    # Get session service for validation
    session_service = await get_session_service()

    # Check for session ID in headers (primary method)
    session_id = request.headers.get("X-Session-Id")

    # Fallback: Check for session_id in query params (for testing)
    if not session_id:
        session_id = request.query_params.get("session_id")

    if session_id:
        try:
            # Validate session and get user_id from it
            session = await session_service.get_session(session_id, validate=True)
            if session and session.user_id:
                return session.user_id
        except Exception:
            # Invalid session - do not return user_id
            pass

    # Legacy support: Direct X-User-Id header (for testing only)
    # In production, this should be removed or restricted to admin endpoints
    user_id = request.headers.get("X-User-Id")
    if user_id:
        return user_id

    # No valid authentication found
    return None


async def require_authenticated_user(request: Request) -> str:
    """
    Require authenticated user for protected endpoints

    Returns user_id for authenticated users, raises HTTPException for unauthenticated.
    Use this dependency for endpoints that require authentication.
    """
    user_id = await get_user_id(request)
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please log in to access this resource.",
        )
    return user_id


async def get_orchestration_service(request: Request):
    """Get OrchestrationService instance from app.state (Composition Root)"""
    return request.app.state.orchestration_service


async def get_data_service(request: Request):
    """Get DataService instance from app.state (Composition Root)"""
    return request.app.state.data_service


async def get_knowledge_service(request: Request) -> "KnowledgeService":
    """Get KnowledgeService instance from app.state (Composition Root)"""
    return request.app.state.knowledge_service


async def get_tracer(request: Request):
    """Get tracer instance from app.state (Composition Root)"""
    return request.app.state.tracer


async def get_protection_system(request: Request):
    """Get protection system instance from app.extra"""
    protection_system = request.app.extra.get("protection_system")
    if not protection_system:
        raise HTTPException(status_code=503, detail="Protection system not available")
    return protection_system


# Session Dependencies


async def get_current_session(
    session_id: str,
    session_service=Depends(get_session_service),
) -> SessionContext:
    """
    Get and validate current session

    Args:
        session_id: Session ID from request
        session_service: Injected session service

    Returns:
        Valid SessionContext

    Raises:
        HTTPException: If session not found or invalid
    """
    session = await session_service.get_session(session_id, validate=True)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    return session


async def get_optional_session(
    session_id: Optional[str] = None,
    session_service=Depends(get_session_service),
) -> Optional[SessionContext]:
    """
    Get optional session if ID provided

    Args:
        session_id: Optional session ID
        session_service: Injected session service

    Returns:
        SessionContext or None
    """
    if not session_id:
        return None

    return await session_service.get_session(session_id, validate=True)


# Request Context Dependencies


async def get_request_metadata(request: Request) -> dict:
    """
    Extract metadata from request

    Args:
        request: FastAPI request object

    Returns:
        Dictionary of request metadata
    """
    return {
        "client_host": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "request_id": request.headers.get("x-request-id"),
        "content_type": request.headers.get("content-type"),
    }


# Authentication Dependencies (placeholder for future implementation)


async def get_current_user(request: Request) -> Optional[dict]:
    """
    Get current authenticated user

    Args:
        request: FastAPI request object

    Returns:
        User info dict or None
    """
    # Placeholder for authentication logic
    # In production, this would validate JWT tokens, API keys, etc.
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        # Validate token and return user info
        return {"user_id": "anonymous", "roles": ["user"]}
    return None


# Rate Limiting Dependencies (placeholder)


async def check_rate_limit(
    request: Request,
    user: Optional[dict] = Depends(get_current_user),
) -> bool:
    """
    Check rate limits

    Args:
        request: FastAPI request
        user: Optional authenticated user

    Returns:
        True if within limits

    Raises:
        HTTPException: If rate limit exceeded
    """
    # Placeholder for rate limiting logic
    # In production, this would check Redis or similar
    return True


# Validation Dependencies


async def validate_content_size(
    content: str,
    max_size_mb: int = 10,
) -> str:
    """
    Validate content size

    Args:
        content: Content to validate
        max_size_mb: Maximum size in MB

    Returns:
        Validated content

    Raises:
        HTTPException: If content too large
    """
    size_bytes = len(content.encode("utf-8"))
    size_mb = size_bytes / (1024 * 1024)

    if size_mb > max_size_mb:
        raise HTTPException(
            status_code=413,
            detail=f"Content too large: {size_mb:.2f}MB (max: {max_size_mb}MB)",
        )

    return content


# Composite Dependencies
# NOTE: TroubleshootingContext and get_troubleshooting_context removed
# They referenced the archived AgentService. Use InvestigationService instead if needed.


# Health Check Dependencies


async def check_service_health(request: Request) -> dict:
    """
    Check health of all services

    Returns:
        Dictionary of service health status
    """
    health_status = {
        "session_manager": "unknown",
        "agent": "unknown",
        "data_processor": "unknown",
        "knowledge_base": "unknown",
    }

    try:
        # Check session service from app.state
        session_service = request.app.state.session_service
        if session_service:
            health_status["session_manager"] = "healthy"
        else:
            health_status["session_manager"] = "unhealthy"
    except Exception:
        health_status["session_manager"] = "unhealthy"

    # Add more health checks as needed

    return health_status


# Job Service Dependencies


async def get_job_service(request: Request) -> Optional[IJobService]:
    """Get JobService instance from app.state (Composition Root)"""
    return getattr(request.app.state, "job_service", None)


async def get_classification_engine(request: Request):
    """Get QueryClassificationEngine instance from app.state (Composition Root)"""
    return request.app.state.query_classification_engine


async def get_organization_service(request: Request):
    """Get OrganizationService instance from app.state (Composition Root)"""
    service = request.app.state.organization_service
    if service is None:
        raise HTTPException(
            status_code=503, detail="Organization service not available"
        )
    return service


async def get_team_service(request: Request):
    """Get TeamService instance from app.state (Composition Root)"""
    service = request.app.state.team_service
    if service is None:
        raise HTTPException(status_code=503, detail="Team service not available")
    return service
