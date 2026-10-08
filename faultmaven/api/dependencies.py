"""API Dependencies Module (TASK-011, TASK-012, TASK-013)

Purpose: FastAPI dependency injection functions for API service layer.

This module provides dependency injection functions for FastAPI endpoints,
integrating with the service factory and database session management.

Usage:
    from faultmaven.api.dependencies import get_investigation_session_service

    @app.get("/sessions/{session_id}")
    async def get_session(
        session_id: str,
        session_service: APIInvestigationSessionService = Depends(
            get_investigation_session_service
        ),
    ):
        ...

Note: get_evidence_artifact_service was removed in storage redesign 2026-04
phase 2 along with the standalone evidence path. Evidence is now created
case-tied via the milestone engine; no separate evidence service needed.
"""

from typing import Any, AsyncGenerator, Optional

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.chroma_client import is_server_backed
from faultmaven.infrastructure.persistence.database import (
    active_database_backend,
    get_db_session,
)
from faultmaven.infrastructure.redis_client import is_fakeredis
from faultmaven.modules.case.domain.services.investigation_session_service import (
    APIInvestigationSessionService,
)
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    FileStorageService,
)
from faultmaven.services.service_factory import ServiceFactory

# This module used to re-export ``get_session_id`` (and import
# ``get_session_service`` without using it) from ``api.v1.dependencies``, to give
# "a canonical import path for all API dependencies". ``get_session_id`` was
# deleted in #1554 — nothing depended on it, and what it did was turn a
# caller-supplied ``X-Session-Id`` into a session id, which is the shape #1461
# ruled out. Nothing re-exported remains, so the section is gone rather than
# emptied: a second import path for a dependency is how two accessors for one
# slot start, and the modules that define these own them.


__all__ = [
    # Service Factory Dependencies (TASK-011/012/013)
    "get_async_db_session",
    "get_service_factory",
    "get_investigation_session_service",
    "get_file_storage_service",
    "database_backend_name",
    "session_storage_backend_name",
    "vector_storage_backend_name",
]


# ============================================================
# Runtime Backends
# ============================================================
#
# What ``GET /admin/config/status`` reports as the database, session and vector
# backends. Each is read from the live object the process serves with and
# decided by the infrastructure predicate that owns the question; this module
# only names the answers, because routes may not import the infrastructure layer
# (``tests/unit/architecture/test_architecture_boundaries.py``) and this is the
# route-facing seam that may.

NOT_INITIALIZED = "not initialized"


def database_backend_name() -> str:
    """The dialect of the engine this process built, e.g. ``"postgresql"`` or
    ``"sqlite"``; ``"not initialized"`` before one exists."""
    return active_database_backend() or NOT_INITIALIZED


def session_storage_backend_name(redis_client: Any) -> str:
    """``"redis"`` or ``"fakeredis (inmemory)"`` for the container's Redis client
    — the one the session store is built with; ``"not initialized"`` for
    ``None``."""
    if redis_client is None:
        return NOT_INITIALIZED
    return "fakeredis (inmemory)" if is_fakeredis(redis_client) else "redis"


def vector_storage_backend_name(kb_client: Any, evidence_client: Any) -> str:
    """What the KB and evidence ChromaDB clients the container built talk to.

    ``"chromadb (server)"`` when both address a ChromaDB server,
    ``"chromadb (persistent, split: kb + evidence)"`` when both are local trees,
    ``"disabled"`` when neither was built (``SKIP_SERVICE_CHECKS`` on
    standalone), and a per-client breakdown when they differ — which the
    standalone fallback can produce if the server drops between the two
    constructions.
    """

    def kind(client: Any) -> Optional[str]:
        if client is None:
            return None
        return "server" if is_server_backed(client) else "persistent"

    kb, evidence = kind(kb_client), kind(evidence_client)
    if kb is None and evidence is None:
        return "disabled"
    if kb == evidence == "server":
        return "chromadb (server)"
    if kb == evidence == "persistent":
        return "chromadb (persistent, split: kb + evidence)"
    return f"chromadb (kb: {kb or 'disabled'}, evidence: {evidence or 'disabled'})"


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
            session_service = factory.create_investigation_session_service()
            return await session_service.list_sessions(case_id)
    """
    # Get tenant_provider from app.state if request is available
    tenant_provider = None
    if request is not None:
        tenant_provider = getattr(request.app.state, "tenant_provider", None)

    return ServiceFactory(db_session, tenant_provider=tenant_provider)


# ============================================================
# Service Dependencies
# ============================================================


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
