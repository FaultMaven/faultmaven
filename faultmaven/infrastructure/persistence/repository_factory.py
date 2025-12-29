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
from faultmaven.infrastructure.persistence.database import get_db_session

logger = logging.getLogger(__name__)


# Storage type constants
STORAGE_TYPE_INMEMORY = "inmemory"
STORAGE_TYPE_DATABASE = "database"

# Singleton in-memory repositories (for consistency across calls)
_inmemory_repository: Optional[InMemoryCaseRepository] = None
_inmemory_session_repository: Optional[InMemorySessionRepository] = None


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
