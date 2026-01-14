"""Database session management for FaultMaven persistence layer.

This module provides async SQLAlchemy session management with support for
both SQLite (development) and PostgreSQL (production).

Features:
- Async session factory with connection pooling
- Settings-based database URL configuration
- Context manager for session lifecycle
- Pool pre-ping for connection health
- Proper transaction handling

Configuration is read from the unified settings system (faultmaven.config.settings).

Usage:
    from faultmaven.infrastructure.persistence.database import get_db_session

    async def my_function():
        async with get_db_session() as session:
            # Use session for database operations
            result = await session.execute(query)
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from faultmaven.infrastructure.persistence.models import Base

logger = logging.getLogger(__name__)


# ============================================================
# Configuration
# ============================================================


def get_database_url() -> str:
    """
    Get database URL from unified settings.

    Supports:
    - SQLite: sqlite+aiosqlite:///./faultmaven.db
    - PostgreSQL: postgresql+asyncpg://user:pass@host:port/dbname

    Returns:
        Database URL string
    """
    from faultmaven.config.settings import get_settings

    return get_settings().database.database_url


def is_sqlite(database_url: str) -> bool:
    """Check if database URL is for SQLite."""
    return database_url.startswith("sqlite")


def is_postgresql(database_url: str) -> bool:
    """Check if database URL is for PostgreSQL."""
    return database_url.startswith("postgresql")


# ============================================================
# Engine Configuration
# ============================================================

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def get_engine(database_url: Optional[str] = None) -> AsyncEngine:
    """
    Get or create the async SQLAlchemy engine.

    Args:
        database_url: Optional database URL override

    Returns:
        AsyncEngine instance
    """
    global _engine

    if _engine is not None:
        return _engine

    # Get settings for database configuration
    from faultmaven.config.settings import get_settings

    settings = get_settings()
    db_config = settings.database

    url = database_url or db_config.database_url
    logger.info(
        f"Creating async engine for: {url.split('@')[-1] if '@' in url else url}"
    )

    # Configure engine based on database type
    if is_sqlite(url):
        # SQLite: Use NullPool (doesn't support connection pooling)
        _engine = create_async_engine(
            url,
            echo=db_config.database_echo,
            pool_pre_ping=True,
            poolclass=NullPool,
            # SQLite-specific: enable foreign keys
            connect_args={"check_same_thread": False},
        )
    else:
        # PostgreSQL: Use connection pooling
        _engine = create_async_engine(
            url,
            echo=db_config.database_echo,
            pool_pre_ping=True,  # Check connection health before use
            pool_size=db_config.database_pool_size,
            max_overflow=db_config.database_max_overflow,
            pool_timeout=db_config.database_pool_timeout,
            pool_recycle=db_config.database_pool_recycle,
        )

    return _engine


def get_session_factory(
    database_url: Optional[str] = None,
) -> async_sessionmaker[AsyncSession]:
    """
    Get or create the async session factory.

    Args:
        database_url: Optional database URL override

    Returns:
        Async session factory
    """
    global _session_factory

    if _session_factory is not None:
        return _session_factory

    engine = get_engine(database_url)
    _session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,  # Prevent detached instance errors
        autocommit=False,
        autoflush=False,
    )

    return _session_factory


# ============================================================
# Session Context Manager
# ============================================================


@asynccontextmanager
async def get_db_session(
    database_url: Optional[str] = None,
) -> AsyncGenerator[AsyncSession, None]:
    """
    Get an async database session with proper lifecycle management.

    This context manager handles:
    - Session creation
    - Automatic commit on success
    - Automatic rollback on exception
    - Session cleanup

    Args:
        database_url: Optional database URL override

    Yields:
        AsyncSession for database operations

    Example:
        async with get_db_session() as session:
            result = await session.execute(query)
            session.add(new_model)
            # Commits automatically on exit
    """
    factory = get_session_factory(database_url)
    session = factory()

    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# ============================================================
# Database Initialization
# ============================================================


async def init_database(database_url: Optional[str] = None) -> None:
    """
    Initialize database tables.

    Creates all tables defined in SQLAlchemy models if they don't exist.
    For production, use Alembic migrations instead.

    Args:
        database_url: Optional database URL override
    """
    engine = get_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialized")


async def drop_database(database_url: Optional[str] = None) -> None:
    """
    Drop all database tables.

    WARNING: This will delete all data. Use only for testing.

    Args:
        database_url: Optional database URL override
    """
    engine = get_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    logger.info("Database tables dropped")


# ============================================================
# Engine/Session Reset
# ============================================================


async def close_database() -> None:
    """
    Close database connections and reset engine.

    Call this during application shutdown or between tests.
    """
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("Database connections closed")


def reset_engine() -> None:
    """
    Reset engine and session factory.

    Use for testing to create fresh database connections.
    """
    global _engine, _session_factory
    _engine = None
    _session_factory = None


# ============================================================
# Health Check
# ============================================================


async def check_database_health(database_url: Optional[str] = None) -> dict:
    """
    Check database connection health.

    Returns:
        Dictionary with health status and details
    """
    try:
        async with get_db_session(database_url) as session:
            # Execute a simple query to test connection
            result = await session.execute("SELECT 1")
            result.fetchone()

        return {
            "status": "healthy",
            "database_type": (
                "sqlite" if is_sqlite(get_database_url()) else "postgresql"
            ),
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
        }
