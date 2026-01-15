"""API Dependencies Module (TASK-011, TASK-012, TASK-013)

Purpose: FastAPI dependency injection functions for API service layer.

This module provides dependency injection functions for FastAPI endpoints,
integrating with the service factory and database session management.

Usage:
    from faultmaven.api.dependencies import get_api_case_service, get_evidence_artifact_service

    @app.get("/cases/{case_id}")
    async def get_case(
        case_id: str,
        case_service: APICaseService = Depends(get_api_case_service)
    ):
        return await case_service.get_case(case_id, org_id)

    @app.post("/evidence")
    async def upload_evidence(
        file: UploadFile,
        evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service)
    ):
        return await evidence_service.upload_evidence(...)
"""

from typing import AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.api.v1.dependencies import (
    get_current_user,
    get_session_id,
    get_user_id,
    require_authenticated_user,
)
from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.services.case_service import APICaseService
from faultmaven.services.evidence_artifact_service import APIEvidenceArtifactService
from faultmaven.services.file_storage_service import FileStorageService
from faultmaven.services.investigation_session_service import (
    APIInvestigationSessionService,
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
    "get_evidence_artifact_service",
    "get_agent_orchestration_service",
    # Re-exported from v1.dependencies (legacy)
    "get_current_user",
    "require_authenticated_user",
    "get_user_id",
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


async def get_file_storage_service(
    factory: ServiceFactory = Depends(get_service_factory),
) -> FileStorageService:
    """Get file storage service for request.

    Creates a FileStorageService with default settings from configuration.

    Args:
        factory: Service factory from get_service_factory

    Returns:
        FileStorageService instance

    Example:
        @app.get("/storage/stats")
        async def get_storage_stats(
            file_storage: FileStorageService = Depends(get_file_storage_service)
        ):
            return await file_storage.get_storage_stats()
    """
    return factory.create_file_storage_service()


async def get_evidence_artifact_service(
    factory: ServiceFactory = Depends(get_service_factory),
) -> APIEvidenceArtifactService:
    """Get evidence artifact service for request.

    Creates an APIEvidenceArtifactService with all required dependencies
    from the service factory.

    Args:
        factory: Service factory from get_service_factory

    Returns:
        APIEvidenceArtifactService instance

    Example:
        @app.post("/cases/{case_id}/evidence")
        async def upload_evidence(
            case_id: str,
            file: UploadFile,
            evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service)
        ):
            file_data = await file.read()
            return await evidence_service.upload_evidence(
                case_id=case_id,
                organization_id=org_id,
                user_id=user_id,
                file_data=file_data,
                original_filename=file.filename,
                mime_type=file.content_type,
                evidence_type=EvidenceArtifactType.OTHER,
            )
    """
    return factory.create_evidence_artifact_service()


async def get_agent_orchestration_service(
    factory: ServiceFactory = Depends(get_service_factory),
) -> "AgentOrchestrationService":
    """Get agent orchestration service for request.

    Creates an AgentOrchestrationService with all required dependencies
    from the service factory. This service handles AI agent execution
    with streaming support.

    Args:
        factory: Service factory from get_service_factory

    Returns:
        AgentOrchestrationService instance

    Example:
        @app.post("/sessions/{session_id}/execute")
        async def execute_agent(
            session_id: str,
            request: AgentExecutionRequest,
            agent_service: AgentOrchestrationService = Depends(get_agent_orchestration_service)
        ):
            async for event in agent_service.execute_agent(
                session_id=session_id,
                organization_id=org_id,
                user_message=request.user_message,
            ):
                yield event
    """
    from faultmaven.modules.agent.domain.services.agent_orchestration_service import (
        AgentOrchestrationService,
    )

    return factory.create_agent_orchestration_service()


# Future service dependencies:

# async def get_knowledge_service(
#     factory: ServiceFactory = Depends(get_service_factory),
# ) -> KnowledgeService:
#     """Get knowledge service for request."""
#     return factory.create_knowledge_service()
