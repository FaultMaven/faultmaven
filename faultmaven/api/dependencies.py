"""API Dependencies Module (TASK-011)

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
        return await case_service.get_case(case_id, org_id)
"""

from typing import AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.services.service_factory import ServiceFactory
from faultmaven.services.case_service import APICaseService
from faultmaven.services.investigation_session_service import APIInvestigationSessionService


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
) -> ServiceFactory:
    """Get service factory for request.

    Creates a ServiceFactory with the request's database session,
    providing access to all service instances with proper
    repository dependencies.

    Args:
        db_session: Database session from get_async_db_session

    Returns:
        ServiceFactory instance

    Example:
        @app.get("/stats")
        async def get_stats(
            factory: ServiceFactory = Depends(get_service_factory)
        ):
            case_service = factory.create_case_service()
            return await case_service.get_case_statistics(org_id)
    """
    return ServiceFactory(db_session)


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
            org_id: str,
            case_service: APICaseService = Depends(get_api_case_service)
        ):
            case = await case_service.get_case(case_id, org_id)
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
            org_id: str,
            session_service: APIInvestigationSessionService = Depends(get_investigation_session_service)
        ):
            session = await session_service.get_session(session_id, org_id)
            if not session:
                raise HTTPException(404, "Session not found")
            return session
    """
    return factory.create_investigation_session_service()


# Future service dependencies:

# async def get_evidence_service(
#     factory: ServiceFactory = Depends(get_service_factory),
# ) -> EvidenceService:
#     """Get evidence service for request."""
#     return factory.create_evidence_service()
