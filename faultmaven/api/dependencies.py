"""API Dependencies Module (TASK-011, TASK-012, TASK-013)

Purpose: FastAPI dependency injection functions for API service layer.

This module provides dependency injection functions for FastAPI endpoints,
integrating with the service factory and database session management.

Usage:
    from faultmaven.api.dependencies import get_api_case_service

    @app.get("/cases/{case_id}")
    async def get_case(
        case_id: str,
        case_service: APICaseService = Depends(get_api_case_service)
    ):
        return await case_service.get_case(case_id, organization_id)

Note: get_evidence_artifact_service was removed in storage redesign 2026-04
phase 2 along with the standalone evidence path. Evidence is now created
case-tied via the milestone engine; no separate evidence service needed.
"""

from typing import AsyncGenerator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.api.v1.dependencies import (
    get_session_id,
    get_session_service,
)
from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.modules.case.domain.services.api_case_service import APICaseService
from faultmaven.modules.case.domain.services.investigation_session_service import (
    APIInvestigationSessionService,
)
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    FileStorageService,
)
from faultmaven.services.service_factory import ServiceFactory

# ============================================================
# Re-exports from v1.dependencies
# ============================================================
# These functions are defined in api.v1.dependencies but re-exported here to
# provide a canonical import path for all API dependencies.
#
# NOTE: We avoid re-exporting functions that cause circular imports
# (e.g., get_knowledge_service) - import those directly from v1.dependencies.


__all__ = [
    # Service Factory Dependencies (TASK-011/012/013)
    "get_async_db_session",
    "get_service_factory",
    "get_api_case_service",
    "get_investigation_session_service",
    "get_file_storage_service",
    # Re-exported from v1.dependencies (legacy)
    "get_session_id",
]


# ============================================================
# Database Session Dependencies
# ============================================================


async def get_async_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Get database session for request.

    This provides a database session that is automatically
    committed on success and rolled back on exception.

    Yields:
        AsyncSession: Database session for the request

    Example:
        @app.get("/items")
        async def get_items(
            session: AsyncSession = Depends(get_async_db_session)
        ):
            result = await session.execute(query)
            return result.scalars().all()
    """
    async with get_db_session() as session:
        yield session


# ============================================================
# Service Factory Dependencies
# ============================================================


async def get_service_factory(
    db_session: AsyncSession = Depends(get_async_db_session),
    request: Request = None,
) -> ServiceFactory:
    """Get service factory for request.

    Creates a ServiceFactory with the request's database session,
    providing access to all service instances with proper
    repository dependencies.

    Args:
        db_session: Database session from get_async_db_session
        request: FastAPI request (optional, for tenant_provider access)

    Returns:
        ServiceFactory instance

    Example:
        @app.get("/stats")
        async def get_stats(
            factory: ServiceFactory = Depends(get_service_factory)
        ):
            case_service = factory.create_case_service()
            return await case_service.get_case_statistics(organization_id)
    """
    # Get tenant_provider from app.state if request is available
    tenant_provider = None
    if request is not None:
        tenant_provider = getattr(request.app.state, "tenant_provider", None)

    return ServiceFactory(db_session, tenant_provider=tenant_provider)


# ============================================================
# Service Dependencies
# ============================================================


async def get_api_case_service(
    factory: ServiceFactory = Depends(get_service_factory),
) -> APICaseService:
    """Get API case service for request.

    Creates an APICaseService with all required repository dependencies
    from the service factory.

    Args:
        factory: Service factory from get_service_factory

    Returns:
        APICaseService instance

    Example:
        @app.get("/cases/{case_id}")
        async def get_case(
            case_id: str,
            organization_id: str,
            case_service: APICaseService = Depends(get_api_case_service)
        ):
            case = await case_service.get_case(case_id, organization_id)
            if not case:
                raise HTTPException(404, "Case not found")
            return case
    """
    return factory.create_case_service()


async def get_investigation_session_service(
    factory: ServiceFactory = Depends(get_service_factory),
) -> APIInvestigationSessionService:
    """Get investigation session service for request.

    Creates an APIInvestigationSessionService with all required repository
    dependencies from the service factory.

    Args:
        factory: Service factory from get_service_factory

    Returns:
        APIInvestigationSessionService instance

    Example:
        @app.get("/sessions/{session_id}")
        async def get_session(
            session_id: str,
            organization_id: str,
            session_service: APIInvestigationSessionService = Depends(get_investigation_session_service)
        ):
            session = await session_service.get_session(session_id, organization_id)
            if not session:
                raise HTTPException(404, "Session not found")
            return session
    """
    return factory.create_investigation_session_service()


async def get_file_storage_service(
    factory: ServiceFactory = Depends(get_service_factory),
) -> FileStorageService:
    """Get file storage service for request.

    Creates a FileStorageService with default settings from configuration,
    backed by whichever storage backend STORAGE_BACKEND selects.

    Args:
        factory: Service factory from get_service_factory

    Returns:
        FileStorageService instance

    Example:
        @app.get("/evidence/{key}")
        async def read_evidence(
            key: str,
            file_storage: FileStorageService = Depends(get_file_storage_service)
        ):
            return await file_storage.retrieve_file(key)
    """
    return factory.create_file_storage_service()


# Future service dependencies:

# async def get_knowledge_service(
#     factory: ServiceFactory = Depends(get_service_factory),
# ) -> KnowledgeService:
#     """Get knowledge service for request."""
#     return factory.create_knowledge_service()
