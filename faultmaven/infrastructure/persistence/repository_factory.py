"""Repository Factory for Case Persistence.

This module provides a factory pattern for creating case repositories.
It enables switching between different storage backends via configuration.

Supported backends:
- inmemory: In-memory storage (testing/development)
- database: SQLAlchemy ORM with SQLite/PostgreSQL

Usage:
    from faultmaven.infrastructure.persistence.repository_factory import (
        get_case_repository,
        get_case_repository_async,
    )

    # Sync context (uses default session)
    repo = get_case_repository()

    # Async context (with explicit session)
    async with get_db_session() as session:
        repo = await get_case_repository_async(session)
"""

import os
import logging
from typing import Optional, AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.case_repository import (
    CaseRepository,
    InMemoryCaseRepository,
)
from faultmaven.infrastructure.persistence.database_case_repository import (
    DatabaseCaseRepository,
)
from faultmaven.infrastructure.persistence.session_repository import (
    SessionRepository,
    DatabaseSessionRepository,
    InMemorySessionRepository,
)
from faultmaven.infrastructure.persistence.evidence_artifact_repository import (
    EvidenceArtifactRepository,
    DatabaseEvidenceArtifactRepository,
    InMemoryEvidenceArtifactRepository,
)
from faultmaven.infrastructure.persistence.agent_execution_repository import (
    AgentExecutionRepository,
    DatabaseAgentExecutionRepository,
    InMemoryAgentExecutionRepository,
)
from faultmaven.infrastructure.persistence.investigation_session_repository import (
    InvestigationSessionRepository,
    DatabaseInvestigationSessionRepository,
    InMemoryInvestigationSessionRepository,
)
from faultmaven.infrastructure.persistence.knowledge_item_repository import (
    KnowledgeItemRepository,
    DatabaseKnowledgeItemRepository,
    InMemoryKnowledgeItemRepository,
)
from faultmaven.infrastructure.persistence.database import get_db_session

logger = logging.getLogger(__name__)


# Storage type constants
STORAGE_TYPE_INMEMORY = "inmemory"
STORAGE_TYPE_DATABASE = "database"

# Singleton in-memory repositories (for consistency across calls)
_inmemory_repository: Optional[InMemoryCaseRepository] = None
_inmemory_session_repository: Optional[InMemorySessionRepository] = None
_inmemory_evidence_artifact_repository: Optional[InMemoryEvidenceArtifactRepository] = None
_inmemory_agent_execution_repository: Optional[InMemoryAgentExecutionRepository] = None
_inmemory_investigation_session_repository: Optional[InMemoryInvestigationSessionRepository] = None
_inmemory_knowledge_item_repository: Optional[InMemoryKnowledgeItemRepository] = None


def get_storage_type() -> str:
    """
    Get configured storage type from environment.

    Environment variable: CASE_STORAGE_TYPE
    Default: "database"

    Returns:
        Storage type string ("inmemory" or "database")
    """
    return os.getenv("CASE_STORAGE_TYPE", STORAGE_TYPE_DATABASE)


def get_session_storage_type() -> str:
    """
    Get configured session storage type from environment.

    Environment variable: SESSION_STORAGE_TYPE
    Default: Falls back to CASE_STORAGE_TYPE, then "database"

    Returns:
        Storage type string ("inmemory" or "database")
    """
    return os.getenv("SESSION_STORAGE_TYPE", get_storage_type())


def get_case_repository(
    storage_type: Optional[str] = None,
    session: Optional[AsyncSession] = None,
) -> CaseRepository:
    """
    Get a case repository instance based on configuration.

    This is the main factory function for obtaining a CaseRepository.

    Args:
        storage_type: Optional override for storage type.
                     If None, uses CASE_STORAGE_TYPE env var.
        session: Optional database session for database storage.
                Required for database storage type.

    Returns:
        CaseRepository implementation

    Raises:
        ValueError: If storage type is unknown
        RuntimeError: If database session is required but not provided

    Example:
        # In-memory (for testing)
        os.environ["CASE_STORAGE_TYPE"] = "inmemory"
        repo = get_case_repository()

        # Database (production)
        os.environ["CASE_STORAGE_TYPE"] = "database"
        async with get_db_session() as session:
            repo = get_case_repository(session=session)
    """
    global _inmemory_repository

    effective_type = storage_type or get_storage_type()
    logger.debug(f"Creating case repository with storage type: {effective_type}")

    if effective_type == STORAGE_TYPE_INMEMORY:
        # Return singleton in-memory repository
        if _inmemory_repository is None:
            _inmemory_repository = InMemoryCaseRepository()
            logger.info("Created InMemoryCaseRepository (singleton)")
        return _inmemory_repository

    elif effective_type == STORAGE_TYPE_DATABASE:
        if session is None:
            raise RuntimeError(
                "Database session is required for database storage type. "
                "Use get_case_repository_async() or provide a session."
            )
        logger.debug("Created DatabaseCaseRepository")
        return DatabaseCaseRepository(session)

    else:
        raise ValueError(f"Unknown storage type: {effective_type}")


@asynccontextmanager
async def get_case_repository_async(
    storage_type: Optional[str] = None,
) -> AsyncGenerator[CaseRepository, None]:
    """
    Get a case repository with automatic session management.

    This is the recommended way to obtain a repository in async contexts.
    It automatically handles database session lifecycle.

    Args:
        storage_type: Optional override for storage type.
                     If None, uses CASE_STORAGE_TYPE env var.

    Yields:
        CaseRepository implementation

    Raises:
        ValueError: If storage type is unknown

    Example:
        async with get_case_repository_async() as repo:
            case = await repo.get("case_abc123def456")
            await repo.save(case)
    """
    global _inmemory_repository

    effective_type = storage_type or get_storage_type()

    if effective_type == STORAGE_TYPE_INMEMORY:
        # Return singleton in-memory repository
        if _inmemory_repository is None:
            _inmemory_repository = InMemoryCaseRepository()
            logger.info("Created InMemoryCaseRepository (singleton)")
        yield _inmemory_repository

    elif effective_type == STORAGE_TYPE_DATABASE:
        # Create database repository with session
        async with get_db_session() as session:
            repo = DatabaseCaseRepository(session)
            yield repo

    else:
        raise ValueError(f"Unknown storage type: {effective_type}")


def create_case_repository(
    session: Optional[AsyncSession] = None,
    force_inmemory: bool = False,
) -> CaseRepository:
    """
    Create a case repository instance.

    Alternative factory function with explicit control.

    Args:
        session: Database session (required for database mode)
        force_inmemory: Force in-memory mode regardless of config

    Returns:
        CaseRepository implementation
    """
    if force_inmemory:
        return InMemoryCaseRepository()

    storage_type = get_storage_type()

    if storage_type == STORAGE_TYPE_INMEMORY:
        return InMemoryCaseRepository()

    elif storage_type == STORAGE_TYPE_DATABASE:
        if session is None:
            raise RuntimeError(
                "Database session is required for database storage type"
            )
        return DatabaseCaseRepository(session)

    else:
        raise ValueError(f"Unknown storage type: {storage_type}")


def reset_inmemory_repository() -> None:
    """
    Reset the singleton in-memory repository.

    Useful for testing to ensure clean state between tests.
    """
    global _inmemory_repository
    if _inmemory_repository is not None:
        _inmemory_repository.clear()
        _inmemory_repository = None
        logger.debug("Reset in-memory repository singleton")


def get_inmemory_repository() -> InMemoryCaseRepository:
    """
    Get or create the singleton in-memory repository.

    Useful when you specifically need in-memory storage.

    Returns:
        InMemoryCaseRepository singleton instance
    """
    global _inmemory_repository
    if _inmemory_repository is None:
        _inmemory_repository = InMemoryCaseRepository()
    return _inmemory_repository


# ============================================================
# Session Repository Factory
# ============================================================


def get_session_repository(
    storage_type: Optional[str] = None,
    session: Optional[AsyncSession] = None,
) -> SessionRepository:
    """
    Get a session repository instance based on configuration.

    Args:
        storage_type: Optional override for storage type.
                     If None, uses SESSION_STORAGE_TYPE env var.
        session: Optional database session for database storage.
                Required for database storage type.

    Returns:
        SessionRepository implementation

    Raises:
        ValueError: If storage type is unknown
        RuntimeError: If database session is required but not provided
    """
    global _inmemory_session_repository

    effective_type = storage_type or get_session_storage_type()
    logger.debug(f"Creating session repository with storage type: {effective_type}")

    if effective_type == STORAGE_TYPE_INMEMORY:
        # Return singleton in-memory repository
        if _inmemory_session_repository is None:
            _inmemory_session_repository = InMemorySessionRepository()
            logger.info("Created InMemorySessionRepository (singleton)")
        return _inmemory_session_repository

    elif effective_type == STORAGE_TYPE_DATABASE:
        if session is None:
            raise RuntimeError(
                "Database session is required for database storage type. "
                "Use get_session_repository_async() or provide a session."
            )
        logger.debug("Created DatabaseSessionRepository")
        return DatabaseSessionRepository(session)

    else:
        raise ValueError(f"Unknown storage type: {effective_type}")


@asynccontextmanager
async def get_session_repository_async(
    storage_type: Optional[str] = None,
) -> AsyncGenerator[SessionRepository, None]:
    """
    Get a session repository with automatic session management.

    This is the recommended way to obtain a session repository in async contexts.
    It automatically handles database session lifecycle.

    Args:
        storage_type: Optional override for storage type.
                     If None, uses SESSION_STORAGE_TYPE env var.

    Yields:
        SessionRepository implementation

    Raises:
        ValueError: If storage type is unknown

    Example:
        async with get_session_repository_async() as repo:
            session = await repo.get_session("session_123")
    """
    global _inmemory_session_repository

    effective_type = storage_type or get_session_storage_type()

    if effective_type == STORAGE_TYPE_INMEMORY:
        # Return singleton in-memory repository
        if _inmemory_session_repository is None:
            _inmemory_session_repository = InMemorySessionRepository()
            logger.info("Created InMemorySessionRepository (singleton)")
        yield _inmemory_session_repository

    elif effective_type == STORAGE_TYPE_DATABASE:
        # Create database repository with session
        async with get_db_session() as db_session:
            repo = DatabaseSessionRepository(db_session)
            yield repo

    else:
        raise ValueError(f"Unknown storage type: {effective_type}")


def reset_inmemory_session_repository() -> None:
    """
    Reset the singleton in-memory session repository.

    Useful for testing to ensure clean state between tests.
    """
    global _inmemory_session_repository
    if _inmemory_session_repository is not None:
        _inmemory_session_repository.clear()
        _inmemory_session_repository = None
        logger.debug("Reset in-memory session repository singleton")


def get_inmemory_session_repository() -> InMemorySessionRepository:
    """
    Get or create the singleton in-memory session repository.

    Useful when you specifically need in-memory storage.

    Returns:
        InMemorySessionRepository singleton instance
    """
    global _inmemory_session_repository
    if _inmemory_session_repository is None:
        _inmemory_session_repository = InMemorySessionRepository()
    return _inmemory_session_repository


# ============================================================
# Evidence Artifact Repository Factory
# ============================================================


def get_evidence_artifact_storage_type() -> str:
    """
    Get configured evidence artifact storage type from environment.

    Environment variable: EVIDENCE_ARTIFACT_STORAGE_TYPE
    Default: Falls back to CASE_STORAGE_TYPE, then "database"

    Returns:
        Storage type string ("inmemory" or "database")
    """
    return os.getenv("EVIDENCE_ARTIFACT_STORAGE_TYPE", get_storage_type())


def get_evidence_artifact_repository(
    storage_type: Optional[str] = None,
    session: Optional[AsyncSession] = None,
) -> EvidenceArtifactRepository:
    """
    Get an evidence artifact repository instance based on configuration.

    Args:
        storage_type: Optional override for storage type.
                     If None, uses EVIDENCE_ARTIFACT_STORAGE_TYPE env var.
        session: Optional database session for database storage.
                Required for database storage type.

    Returns:
        EvidenceArtifactRepository implementation

    Raises:
        ValueError: If storage type is unknown
        RuntimeError: If database session is required but not provided
    """
    global _inmemory_evidence_artifact_repository

    effective_type = storage_type or get_evidence_artifact_storage_type()
    logger.debug(f"Creating evidence artifact repository with storage type: {effective_type}")

    if effective_type == STORAGE_TYPE_INMEMORY:
        # Return singleton in-memory repository
        if _inmemory_evidence_artifact_repository is None:
            _inmemory_evidence_artifact_repository = InMemoryEvidenceArtifactRepository()
            logger.info("Created InMemoryEvidenceArtifactRepository (singleton)")
        return _inmemory_evidence_artifact_repository

    elif effective_type == STORAGE_TYPE_DATABASE:
        if session is None:
            raise RuntimeError(
                "Database session is required for database storage type. "
                "Use get_evidence_artifact_repository_async() or provide a session."
            )
        logger.debug("Created DatabaseEvidenceArtifactRepository")
        return DatabaseEvidenceArtifactRepository(session)

    else:
        raise ValueError(f"Unknown storage type: {effective_type}")


@asynccontextmanager
async def get_evidence_artifact_repository_async(
    storage_type: Optional[str] = None,
) -> AsyncGenerator[EvidenceArtifactRepository, None]:
    """
    Get an evidence artifact repository with automatic session management.

    This is the recommended way to obtain a repository in async contexts.
    It automatically handles database session lifecycle.

    Args:
        storage_type: Optional override for storage type.
                     If None, uses EVIDENCE_ARTIFACT_STORAGE_TYPE env var.

    Yields:
        EvidenceArtifactRepository implementation

    Raises:
        ValueError: If storage type is unknown

    Example:
        async with get_evidence_artifact_repository_async() as repo:
            evidence = await repo.get_evidence("ev_abc123def456")
    """
    global _inmemory_evidence_artifact_repository

    effective_type = storage_type or get_evidence_artifact_storage_type()

    if effective_type == STORAGE_TYPE_INMEMORY:
        # Return singleton in-memory repository
        if _inmemory_evidence_artifact_repository is None:
            _inmemory_evidence_artifact_repository = InMemoryEvidenceArtifactRepository()
            logger.info("Created InMemoryEvidenceArtifactRepository (singleton)")
        yield _inmemory_evidence_artifact_repository

    elif effective_type == STORAGE_TYPE_DATABASE:
        # Create database repository with session
        async with get_db_session() as db_session:
            repo = DatabaseEvidenceArtifactRepository(db_session)
            yield repo

    else:
        raise ValueError(f"Unknown storage type: {effective_type}")


def reset_inmemory_evidence_artifact_repository() -> None:
    """
    Reset the singleton in-memory evidence artifact repository.

    Useful for testing to ensure clean state between tests.
    """
    global _inmemory_evidence_artifact_repository
    if _inmemory_evidence_artifact_repository is not None:
        _inmemory_evidence_artifact_repository.clear()
        _inmemory_evidence_artifact_repository = None
        logger.debug("Reset in-memory evidence artifact repository singleton")


def get_inmemory_evidence_artifact_repository() -> InMemoryEvidenceArtifactRepository:
    """
    Get or create the singleton in-memory evidence artifact repository.

    Useful when you specifically need in-memory storage.

    Returns:
        InMemoryEvidenceArtifactRepository singleton instance
    """
    global _inmemory_evidence_artifact_repository
    if _inmemory_evidence_artifact_repository is None:
        _inmemory_evidence_artifact_repository = InMemoryEvidenceArtifactRepository()
    return _inmemory_evidence_artifact_repository


# ============================================================
# Dependency Injection Helpers
# ============================================================


async def get_repository_dependency() -> AsyncGenerator[CaseRepository, None]:
    """
    FastAPI dependency for obtaining a case repository.

    Use this in FastAPI route dependencies:

        @app.get("/cases/{case_id}")
        async def get_case(
            case_id: str,
            repo: CaseRepository = Depends(get_repository_dependency)
        ):
            return await repo.get(case_id)

    Yields:
        CaseRepository instance
    """
    async with get_case_repository_async() as repo:
        yield repo


async def get_session_repository_dependency() -> AsyncGenerator[SessionRepository, None]:
    """
    FastAPI dependency for obtaining a session repository.

    Use this in FastAPI route dependencies:

        @app.get("/sessions/{session_id}")
        async def get_session(
            session_id: str,
            repo: SessionRepository = Depends(get_session_repository_dependency)
        ):
            return await repo.get_session(session_id)

    Yields:
        SessionRepository instance
    """
    async with get_session_repository_async() as repo:
        yield repo


async def get_evidence_artifact_repository_dependency() -> AsyncGenerator[EvidenceArtifactRepository, None]:
    """
    FastAPI dependency for obtaining an evidence artifact repository.

    Use this in FastAPI route dependencies:

        @app.get("/evidence/{evidence_id}")
        async def get_evidence(
            evidence_id: str,
            repo: EvidenceArtifactRepository = Depends(get_evidence_artifact_repository_dependency)
        ):
            return await repo.get_evidence(evidence_id)

    Yields:
        EvidenceArtifactRepository instance
    """
    async with get_evidence_artifact_repository_async() as repo:
        yield repo


# ============================================================
# Agent Execution Repository Factory
# ============================================================


def get_agent_execution_storage_type() -> str:
    """
    Get configured agent execution storage type from environment.

    Environment variable: AGENT_EXECUTION_STORAGE_TYPE
    Default: Falls back to CASE_STORAGE_TYPE, then "database"

    Returns:
        Storage type string ("inmemory" or "database")
    """
    return os.getenv("AGENT_EXECUTION_STORAGE_TYPE", get_storage_type())


def get_agent_execution_repository(
    storage_type: Optional[str] = None,
    session: Optional[AsyncSession] = None,
) -> AgentExecutionRepository:
    """
    Get an agent execution repository instance based on configuration.

    Args:
        storage_type: Optional override for storage type.
                     If None, uses AGENT_EXECUTION_STORAGE_TYPE env var.
        session: Optional database session for database storage.
                Required for database storage type.

    Returns:
        AgentExecutionRepository implementation

    Raises:
        ValueError: If storage type is unknown
        RuntimeError: If database session is required but not provided
    """
    global _inmemory_agent_execution_repository

    effective_type = storage_type or get_agent_execution_storage_type()
    logger.debug(f"Creating agent execution repository with storage type: {effective_type}")

    if effective_type == STORAGE_TYPE_INMEMORY:
        # Return singleton in-memory repository
        if _inmemory_agent_execution_repository is None:
            _inmemory_agent_execution_repository = InMemoryAgentExecutionRepository()
            logger.info("Created InMemoryAgentExecutionRepository (singleton)")
        return _inmemory_agent_execution_repository

    elif effective_type == STORAGE_TYPE_DATABASE:
        if session is None:
            raise RuntimeError(
                "Database session is required for database storage type. "
                "Use get_agent_execution_repository_async() or provide a session."
            )
        logger.debug("Created DatabaseAgentExecutionRepository")
        return DatabaseAgentExecutionRepository(session)

    else:
        raise ValueError(f"Unknown storage type: {effective_type}")


@asynccontextmanager
async def get_agent_execution_repository_async(
    storage_type: Optional[str] = None,
) -> AsyncGenerator[AgentExecutionRepository, None]:
    """
    Get an agent execution repository with automatic session management.

    This is the recommended way to obtain a repository in async contexts.
    It automatically handles database session lifecycle.

    Args:
        storage_type: Optional override for storage type.
                     If None, uses AGENT_EXECUTION_STORAGE_TYPE env var.

    Yields:
        AgentExecutionRepository implementation

    Raises:
        ValueError: If storage type is unknown

    Example:
        async with get_agent_execution_repository_async() as repo:
            execution = await repo.get_execution("exec_abc123def456")
    """
    global _inmemory_agent_execution_repository

    effective_type = storage_type or get_agent_execution_storage_type()

    if effective_type == STORAGE_TYPE_INMEMORY:
        # Return singleton in-memory repository
        if _inmemory_agent_execution_repository is None:
            _inmemory_agent_execution_repository = InMemoryAgentExecutionRepository()
            logger.info("Created InMemoryAgentExecutionRepository (singleton)")
        yield _inmemory_agent_execution_repository

    elif effective_type == STORAGE_TYPE_DATABASE:
        # Create database repository with session
        async with get_db_session() as db_session:
            repo = DatabaseAgentExecutionRepository(db_session)
            yield repo

    else:
        raise ValueError(f"Unknown storage type: {effective_type}")


def reset_inmemory_agent_execution_repository() -> None:
    """
    Reset the singleton in-memory agent execution repository.

    Useful for testing to ensure clean state between tests.
    """
    global _inmemory_agent_execution_repository
    if _inmemory_agent_execution_repository is not None:
        _inmemory_agent_execution_repository.clear()
        _inmemory_agent_execution_repository = None
        logger.debug("Reset in-memory agent execution repository singleton")


def get_inmemory_agent_execution_repository() -> InMemoryAgentExecutionRepository:
    """
    Get or create the singleton in-memory agent execution repository.

    Useful when you specifically need in-memory storage.

    Returns:
        InMemoryAgentExecutionRepository singleton instance
    """
    global _inmemory_agent_execution_repository
    if _inmemory_agent_execution_repository is None:
        _inmemory_agent_execution_repository = InMemoryAgentExecutionRepository()
    return _inmemory_agent_execution_repository


async def get_agent_execution_repository_dependency() -> AsyncGenerator[AgentExecutionRepository, None]:
    """
    FastAPI dependency for obtaining an agent execution repository.

    Use this in FastAPI route dependencies:

        @app.get("/executions/{execution_id}")
        async def get_execution(
            execution_id: str,
            repo: AgentExecutionRepository = Depends(get_agent_execution_repository_dependency)
        ):
            return await repo.get_execution(execution_id)

    Yields:
        AgentExecutionRepository instance
    """
    async with get_agent_execution_repository_async() as repo:
        yield repo


# ============================================================
# Investigation Session Repository Factory
# ============================================================


def get_investigation_session_storage_type() -> str:
    """
    Get configured investigation session storage type from environment.

    Environment variable: INVESTIGATION_SESSION_STORAGE_TYPE
    Default: Falls back to CASE_STORAGE_TYPE, then "database"

    Returns:
        Storage type string ("inmemory" or "database")
    """
    return os.getenv("INVESTIGATION_SESSION_STORAGE_TYPE", get_storage_type())


def get_investigation_session_repository(
    storage_type: Optional[str] = None,
    session: Optional[AsyncSession] = None,
) -> InvestigationSessionRepository:
    """
    Get an investigation session repository instance based on configuration.

    Args:
        storage_type: Optional override for storage type.
                     If None, uses INVESTIGATION_SESSION_STORAGE_TYPE env var.
        session: Optional database session for database storage.
                Required for database storage type.

    Returns:
        InvestigationSessionRepository implementation

    Raises:
        ValueError: If storage type is unknown
        RuntimeError: If database session is required but not provided
    """
    global _inmemory_investigation_session_repository

    effective_type = storage_type or get_investigation_session_storage_type()
    logger.debug(f"Creating investigation session repository with storage type: {effective_type}")

    if effective_type == STORAGE_TYPE_INMEMORY:
        # Return singleton in-memory repository
        if _inmemory_investigation_session_repository is None:
            _inmemory_investigation_session_repository = InMemoryInvestigationSessionRepository()
            logger.info("Created InMemoryInvestigationSessionRepository (singleton)")
        return _inmemory_investigation_session_repository

    elif effective_type == STORAGE_TYPE_DATABASE:
        if session is None:
            raise RuntimeError(
                "Database session is required for database storage type. "
                "Use get_investigation_session_repository_async() or provide a session."
            )
        logger.debug("Created DatabaseInvestigationSessionRepository")
        return DatabaseInvestigationSessionRepository(session)

    else:
        raise ValueError(f"Unknown storage type: {effective_type}")


@asynccontextmanager
async def get_investigation_session_repository_async(
    storage_type: Optional[str] = None,
) -> AsyncGenerator[InvestigationSessionRepository, None]:
    """
    Get an investigation session repository with automatic session management.

    This is the recommended way to obtain a repository in async contexts.
    It automatically handles database session lifecycle.

    Args:
        storage_type: Optional override for storage type.
                     If None, uses INVESTIGATION_SESSION_STORAGE_TYPE env var.

    Yields:
        InvestigationSessionRepository implementation

    Raises:
        ValueError: If storage type is unknown

    Example:
        async with get_investigation_session_repository_async() as repo:
            session = await repo.get_by_id("sess_abc123def456")
    """
    global _inmemory_investigation_session_repository

    effective_type = storage_type or get_investigation_session_storage_type()

    if effective_type == STORAGE_TYPE_INMEMORY:
        # Return singleton in-memory repository
        if _inmemory_investigation_session_repository is None:
            _inmemory_investigation_session_repository = InMemoryInvestigationSessionRepository()
            logger.info("Created InMemoryInvestigationSessionRepository (singleton)")
        yield _inmemory_investigation_session_repository

    elif effective_type == STORAGE_TYPE_DATABASE:
        # Create database repository with session
        async with get_db_session() as db_session:
            repo = DatabaseInvestigationSessionRepository(db_session)
            yield repo

    else:
        raise ValueError(f"Unknown storage type: {effective_type}")


def reset_inmemory_investigation_session_repository() -> None:
    """
    Reset the singleton in-memory investigation session repository.

    Useful for testing to ensure clean state between tests.
    """
    global _inmemory_investigation_session_repository
    if _inmemory_investigation_session_repository is not None:
        _inmemory_investigation_session_repository.clear()
        _inmemory_investigation_session_repository = None
        logger.debug("Reset in-memory investigation session repository singleton")


def get_inmemory_investigation_session_repository() -> InMemoryInvestigationSessionRepository:
    """
    Get or create the singleton in-memory investigation session repository.

    Useful when you specifically need in-memory storage.

    Returns:
        InMemoryInvestigationSessionRepository singleton instance
    """
    global _inmemory_investigation_session_repository
    if _inmemory_investigation_session_repository is None:
        _inmemory_investigation_session_repository = InMemoryInvestigationSessionRepository()
    return _inmemory_investigation_session_repository


async def get_investigation_session_repository_dependency() -> AsyncGenerator[InvestigationSessionRepository, None]:
    """
    FastAPI dependency for obtaining an investigation session repository.

    Use this in FastAPI route dependencies:

        @app.get("/sessions/{session_id}")
        async def get_session(
            session_id: str,
            repo: InvestigationSessionRepository = Depends(get_investigation_session_repository_dependency)
        ):
            return await repo.get_by_id(session_id)

    Yields:
        InvestigationSessionRepository instance
    """
    async with get_investigation_session_repository_async() as repo:
        yield repo


# ============================================================
# Knowledge Item Repository Factory
# ============================================================


def get_knowledge_item_storage_type() -> str:
    """
    Get configured knowledge item storage type from environment.

    Environment variable: KNOWLEDGE_ITEM_STORAGE_TYPE
    Default: Falls back to CASE_STORAGE_TYPE, then "database"

    Returns:
        Storage type string ("inmemory" or "database")
    """
    return os.getenv("KNOWLEDGE_ITEM_STORAGE_TYPE", get_storage_type())


def get_knowledge_item_repository(
    storage_type: Optional[str] = None,
    session: Optional[AsyncSession] = None,
) -> KnowledgeItemRepository:
    """
    Get a knowledge item repository instance based on configuration.

    Args:
        storage_type: Optional override for storage type.
                     If None, uses KNOWLEDGE_ITEM_STORAGE_TYPE env var.
        session: Optional database session for database storage.
                Required for database storage type.

    Returns:
        KnowledgeItemRepository implementation

    Raises:
        ValueError: If storage type is unknown
        RuntimeError: If database session is required but not provided
    """
    global _inmemory_knowledge_item_repository

    effective_type = storage_type or get_knowledge_item_storage_type()
    logger.debug(f"Creating knowledge item repository with storage type: {effective_type}")

    if effective_type == STORAGE_TYPE_INMEMORY:
        # Return singleton in-memory repository
        if _inmemory_knowledge_item_repository is None:
            _inmemory_knowledge_item_repository = InMemoryKnowledgeItemRepository()
            logger.info("Created InMemoryKnowledgeItemRepository (singleton)")
        return _inmemory_knowledge_item_repository

    elif effective_type == STORAGE_TYPE_DATABASE:
        if session is None:
            raise RuntimeError(
                "Database session is required for database storage type. "
                "Use get_knowledge_item_repository_async() or provide a session."
            )
        logger.debug("Created DatabaseKnowledgeItemRepository")
        return DatabaseKnowledgeItemRepository(session)

    else:
        raise ValueError(f"Unknown storage type: {effective_type}")


@asynccontextmanager
async def get_knowledge_item_repository_async(
    storage_type: Optional[str] = None,
) -> AsyncGenerator[KnowledgeItemRepository, None]:
    """
    Get a knowledge item repository with automatic session management.

    This is the recommended way to obtain a repository in async contexts.
    It automatically handles database session lifecycle.

    Args:
        storage_type: Optional override for storage type.
                     If None, uses KNOWLEDGE_ITEM_STORAGE_TYPE env var.

    Yields:
        KnowledgeItemRepository implementation

    Raises:
        ValueError: If storage type is unknown

    Example:
        async with get_knowledge_item_repository_async() as repo:
            item = await repo.get_by_id("ki_abc123def456")
    """
    global _inmemory_knowledge_item_repository

    effective_type = storage_type or get_knowledge_item_storage_type()

    if effective_type == STORAGE_TYPE_INMEMORY:
        # Return singleton in-memory repository
        if _inmemory_knowledge_item_repository is None:
            _inmemory_knowledge_item_repository = InMemoryKnowledgeItemRepository()
            logger.info("Created InMemoryKnowledgeItemRepository (singleton)")
        yield _inmemory_knowledge_item_repository

    elif effective_type == STORAGE_TYPE_DATABASE:
        # Create database repository with session
        async with get_db_session() as db_session:
            repo = DatabaseKnowledgeItemRepository(db_session)
            yield repo

    else:
        raise ValueError(f"Unknown storage type: {effective_type}")


def reset_inmemory_knowledge_item_repository() -> None:
    """
    Reset the singleton in-memory knowledge item repository.

    Useful for testing to ensure clean state between tests.
    """
    global _inmemory_knowledge_item_repository
    if _inmemory_knowledge_item_repository is not None:
        _inmemory_knowledge_item_repository.clear()
        _inmemory_knowledge_item_repository = None
        logger.debug("Reset in-memory knowledge item repository singleton")


def get_inmemory_knowledge_item_repository() -> InMemoryKnowledgeItemRepository:
    """
    Get or create the singleton in-memory knowledge item repository.

    Useful when you specifically need in-memory storage.

    Returns:
        InMemoryKnowledgeItemRepository singleton instance
    """
    global _inmemory_knowledge_item_repository
    if _inmemory_knowledge_item_repository is None:
        _inmemory_knowledge_item_repository = InMemoryKnowledgeItemRepository()
    return _inmemory_knowledge_item_repository


async def get_knowledge_item_repository_dependency() -> AsyncGenerator[KnowledgeItemRepository, None]:
    """
    FastAPI dependency for obtaining a knowledge item repository.

    Use this in FastAPI route dependencies:

        @app.get("/knowledge/{item_id}")
        async def get_knowledge_item(
            item_id: str,
            repo: KnowledgeItemRepository = Depends(get_knowledge_item_repository_dependency)
        ):
            return await repo.get_by_id(item_id)

    Yields:
        KnowledgeItemRepository instance
    """
    async with get_knowledge_item_repository_async() as repo:
        yield repo
