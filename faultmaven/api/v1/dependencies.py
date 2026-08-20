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
from ...models.interfaces_user import IOrganizationRepository
from ...modules.auth.contracts import IUserQuery

# Lazy import to avoid circular dependency - DataService, SessionService imported in functions or TYPE_CHECKING
# OLD: from ...services.agentic.orchestration.agent_service import AgentService (ARCHIVED)
from ...modules.preprocessing import PreprocessingService
from ...providers.tenancy.base import TenantProvider

# Type hints for lazy imports
if TYPE_CHECKING:
    from ...services import DataService, SessionService


# Service Dependencies (Composition Root pattern - access via app.state)


async def get_session_service(request: Request):
    """Get SessionService instance from app.state (Composition Root)"""
    return request.app.state.session_service


async def get_organization_repository(
    request: Request,
) -> Optional[IOrganizationRepository]:
    """Get the organization repository from app.state (Composition Root).

    Optional rather than 503 on absence: the one core read path is the tenant
    label on ``/auth/me``, which degrades to an absent field rather than
    failing a profile request over a display string.
    """
    return getattr(request.app.state, "organization_repository", None)


async def get_user_service_optional(request: Request) -> Optional[IUserQuery]:
    """Get the UserService from app.state (Composition Root).

    Optional rather than 503 on absence, for the same reason as
    ``get_organization_repository``: the core read path is the persisted
    profile timestamps on ``/auth/me``, which degrade to the token
    principal's view rather than failing a profile request over display
    fields. Named ``_optional`` because ``api/routes/admin.py`` has a
    ``get_user_service`` reading the same slot with the opposite absence
    semantics (raise) — admin routes cannot work without the service, and
    a shared name would let an auto-import swap hard-fail for silent
    degrade (or leave a ``dependency_overrides`` entry binding the wrong
    symbol) without anything noticing.
    """
    return getattr(request.app.state, "user_service", None)


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


async def get_oauth_service(request: Request):
    """Get OAuth service instance from app.state (Composition Root).

    Returns:
        OAuth service instance or None if not enabled

    Raises:
        HTTPException: If OAuth is enabled but service unavailable
    """
    oauth_service = getattr(request.app.state, "oauth_service", None)

    # If OAuth is supposed to be enabled but service is None, that's an error
    # Check settings to see if OAuth should be enabled
    try:
        from faultmaven.config.settings import get_settings

        settings = get_settings()
        if settings.auth.oauth_enabled and oauth_service is None:
            raise HTTPException(
                status_code=503, detail="OAuth authentication not configured"
            )
    except HTTPException:
        raise
    except Exception:
        pass  # If settings unavailable, just return None

    return oauth_service


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


async def get_orchestration_service(request: Request):
    """Get OrchestrationService instance from app.state (Composition Root)"""
    return request.app.state.orchestration_service


async def get_data_service(request: Request):
    """Get DataService instance from app.state (Composition Root)"""
    return request.app.state.data_service


# A second ``get_knowledge_service`` used to sit here, reading the same
# app.state slot as the one in modules/knowledge/api/routes.py. Deleted rather
# than given the same 503 guard (#899): no route depended on it, and its only
# importer — tests/unit/api/conftest.py — sits in a try block whose companion
# import (faultmaven.api.v1.routes) does not resolve, so the import never ran.
# Two accessors for one slot means the next contract change lands on one of
# them; the knowledge module owns its own.


async def get_tracer(request: Request):
    """Get tracer instance from app.state (Composition Root)"""
    return request.app.state.tracer


# ``get_protection_system`` used to sit here, reading app.extra["protection_system"].
# Both writers of that slot were on the ``ProtectionSystem`` path, which never ran
# and is gone (#974); the live path writes ``protection_info`` instead. With no
# writer left the accessor could only ever raise 503, and no route depended on it.


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


async def get_llm_provider(request: Request):
    """Get LLM provider instance from app.extra (Composition Root)"""
    try:
        container = request.app.extra.get("di_container")
        if container:
            return getattr(container, "llm_provider", None)
        return None
    except Exception:
        # LLM provider is optional - return None if not available
        return None
