"""Repository Factory for Investigation Session and Knowledge persistence.

This module provides factories for creating session and knowledge-item
repositories backed by InMemory or SQLAlchemy implementations, with
storage selection driven by environment / settings.

The case-repository factories that historically lived here were removed
when the parallel ``infrastructure.persistence`` and
``modules.case.infrastructure`` hierarchies were consolidated. Case
repositories now come from ``modules.case.infrastructure``:

- ``InMemoryCaseRepository`` — instantiate directly for tests.
- ``get_repository_for_session(session)`` (in
  ``sessionless_case_repository``) — returns the dialect-appropriate
  ``SQLiteCaseRepository`` or ``PostgreSQLHybridCaseRepository``.
- ``SessionlessCaseRepository`` — no-arg, opens its own session per op.

Storage type helpers (``STORAGE_TYPE_*``, ``get_storage_type``) remain
here because session/knowledge factories share the same env-var fallback.
"""

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.investigation_session_repository import (
    DatabaseInvestigationSessionRepository,
    InMemoryInvestigationSessionRepository,
    InvestigationSessionRepository,
)

# Knowledge repository imports moved inside factory functions to avoid circular dependencies

logger = logging.getLogger(__name__)


# Storage type constants
STORAGE_TYPE_INMEMORY = "inmemory"
STORAGE_TYPE_DATABASE = "database"

# `_inmemory_session_repository` and the SQL session-repository factory
# (`get_session_repository*`, `get_session_storage_type`,
# `get_inmemory_session_repository`, `reset_inmemory_session_repository`,
# `get_session_repository_dependency`) were removed in storage redesign
# 2026-04 phase 3. Auth sessions are Redis-only (RedisSessionStore over
# real Redis cloud, FakeRedis local). See deployment-schema-strategy.md
# §11.1.
_inmemory_investigation_session_repository: (
    InMemoryInvestigationSessionRepository | None
) = None
_inmemory_knowledge_item_repository: Any | None = None


def get_storage_type() -> str:
    """
    Get configured storage type from settings (deployment-agnostic).

    Uses settings.database.case_storage_type (env: CASE_STORAGE_TYPE)
    Default: "database"

    Returns:
        Storage type string ("inmemory" or "database")

    Design Notes:
        - Always attempts to use settings first (deployment-agnostic).
        - Degrades to ``os.getenv()`` only if settings construction itself
          raises (very early init, or env-var validator rejection). Lets
          pytest collection survive a partly-broken environment.
    """
    from faultmaven.config.settings import get_settings

    try:
        settings = get_settings()
        return settings.database.case_storage_type
    except Exception:
        logger.debug(
            "Settings unavailable, falling back to os.getenv() for storage type"
        )
        return os.getenv("CASE_STORAGE_TYPE", STORAGE_TYPE_DATABASE)


# ============================================================
# Case repository factories REMOVED in 2026-04 hierarchy consolidation.
# Use the unified ``modules.case.infrastructure`` hierarchy directly:
#   - InMemoryCaseRepository() for tests / ephemeral storage.
#   - get_repository_for_session(session) for session-bound DB use.
#   - SessionlessCaseRepository() for stateless cloud/local DB.
# See ``faultmaven.modules.case.infrastructure.case_repository`` and
# ``sessionless_case_repository`` for the canonical types.

# ============================================================
# Session Repository Factory: REMOVED in storage redesign 2026-04 phase 3.
# ============================================================
# The SQL session repository (DatabaseSessionRepository /
# InMemorySessionRepository) and its factory functions were removed.
# Auth sessions live in Redis only via RedisSessionStore (real Redis in
# cloud, FakeRedis in local). The SQL `sessions` table itself was dropped
# at the same time. See deployment-schema-strategy.md §11.1 + §12 decisions
# #14, #15.

# Evidence Artifact Repository Factory was removed in storage redesign 2026-04 phase 2.
# The standalone evidence path (POST /api/v1/evidence + evidence_artifacts /
# standalone_evidence tables) is deleted. Evidence is now case-tied only,
# accessed via the case repository's case.evidence list.


# ============================================================
# Dependency Injection Helpers
# ============================================================
#
# get_repository_dependency was removed in 2026-04 hierarchy consolidation.
# FastAPI routes that need a case repository should depend on the
# request-scoped ``ServiceFactory`` (see ``faultmaven.api.dependencies``)
# or instantiate ``InMemoryCaseRepository`` directly for tests.
#
# get_session_repository_dependency was removed in storage redesign
# 2026-04 phase 3 (auth sessions are Redis-only via RedisSessionStore).
# get_evidence_artifact_repository_dependency was removed in storage redesign
# 2026-04 phase 2 alongside the rest of the evidence artifact repository code.


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
    # Use settings (deployment-agnostic) instead of os.getenv()
    from faultmaven.config.settings import get_settings

    try:
        settings = get_settings()
        # Note: investigation_session_storage_type may not exist in settings yet
        # Use case_storage_type as fallback
        return (
            getattr(settings.database, "investigation_session_storage_type", None)
            or get_storage_type()
        )
    except Exception:
        # Degrade gracefully when settings construction itself raises
        logger.debug(
            "Settings unavailable, falling back to os.getenv() for investigation session storage type"
        )
        return os.getenv("INVESTIGATION_SESSION_STORAGE_TYPE", get_storage_type())


def get_investigation_session_repository(
    storage_type: str | None = None,
    session: AsyncSession | None = None,
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
    logger.debug(
        f"Creating investigation session repository with storage type: {effective_type}"
    )

    if effective_type == STORAGE_TYPE_INMEMORY:
        # Return singleton in-memory repository
        if _inmemory_investigation_session_repository is None:
            _inmemory_investigation_session_repository = (
                InMemoryInvestigationSessionRepository()
            )
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
    storage_type: str | None = None,
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
            _inmemory_investigation_session_repository = (
                InMemoryInvestigationSessionRepository()
            )
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


def get_inmemory_investigation_session_repository() -> (
    InMemoryInvestigationSessionRepository
):
    """
    Get or create the singleton in-memory investigation session repository.

    Useful when you specifically need in-memory storage.

    Returns:
        InMemoryInvestigationSessionRepository singleton instance
    """
    global _inmemory_investigation_session_repository
    if _inmemory_investigation_session_repository is None:
        _inmemory_investigation_session_repository = (
            InMemoryInvestigationSessionRepository()
        )
    return _inmemory_investigation_session_repository


async def get_investigation_session_repository_dependency() -> (
    AsyncGenerator[InvestigationSessionRepository, None]
):
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
    # Use settings (deployment-agnostic) instead of os.getenv()
    from faultmaven.config.settings import get_settings

    try:
        settings = get_settings()
        # Note: knowledge_item_storage_type may not exist in settings yet
        # Use case_storage_type as fallback
        return (
            getattr(settings.database, "knowledge_item_storage_type", None)
            or get_storage_type()
        )
    except Exception:
        # Degrade gracefully when settings construction itself raises
        logger.debug(
            "Settings unavailable, falling back to os.getenv() for knowledge item storage type"
        )
        return os.getenv("KNOWLEDGE_ITEM_STORAGE_TYPE", get_storage_type())


def get_knowledge_item_repository(
    storage_type: str | None = None,
    session: AsyncSession | None = None,
) -> Any:
    from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
        DatabaseKnowledgeItemRepository,
        InMemoryKnowledgeItemRepository,
    )

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
    logger.debug(
        f"Creating knowledge item repository with storage type: {effective_type}"
    )

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
    storage_type: str | None = None,
) -> AsyncGenerator[Any, None]:
    from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
        DatabaseKnowledgeItemRepository,
        InMemoryKnowledgeItemRepository,
    )

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


def get_inmemory_knowledge_item_repository() -> Any:
    from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
        InMemoryKnowledgeItemRepository,
    )

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


async def get_knowledge_item_repository_dependency() -> Any:
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
