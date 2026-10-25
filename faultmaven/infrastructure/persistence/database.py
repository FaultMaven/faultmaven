"""Database session management for FaultMaven persistence layer.

This module provides async SQLAlchemy session management with support for
both SQLite (development) and PostgreSQL (production).

Features:
- Async session factory with connection pooling
- Settings-based database URL configuration
- Context manager for session lifecycle
- Pool pre-ping for connection health
- Bounded PostgreSQL connections: a silent server fails a command instead of
  holding it (``pg_connection``, #2280)
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

from sqlalchemy import event, text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from faultmaven.exceptions import DatabaseTimeout, walk_cause_chain
from faultmaven.infrastructure.persistence.db_errors import sqlstate
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.pg_connection import (
    PgBounds,
    enable_tcp_liveness,
    pg_bounds,
    pg_connect_args,
)

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


def active_database_backend() -> Optional[str]:
    """The dialect of the engine this process built (``"postgresql"``,
    ``"sqlite"``), or ``None`` before :func:`get_engine` has built one.

    Read from the engine, not from a URL: the engine is a first-caller-wins
    singleton that accepts an override, so the configured URL is only a claim
    about which database serves queries.
    """
    return _engine.dialect.name if _engine is not None else None


def active_database_bounds() -> Optional[PgBounds]:
    """The bounds the PostgreSQL engine this process built runs with (#2280).

    ``None`` before :func:`get_engine` has built one, and for a SQLite engine,
    which none of them applies to. Read from what the engine was built with,
    for the reason :func:`active_database_backend` is.
    """
    return _engine_bounds


# ============================================================
# Engine Configuration
# ============================================================

_engine: Optional[AsyncEngine] = None
_engine_bounds: Optional[PgBounds] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def get_engine(database_url: Optional[str] = None) -> AsyncEngine:
    """
    Get or create the async SQLAlchemy engine.

    Args:
        database_url: Optional database URL override

    Returns:
        AsyncEngine instance
    """
    global _engine, _engine_bounds

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
        # SQLite: Use NullPool (doesn't support connection pooling).
        # pool_pre_ping is intentionally NOT set — it's a no-op on NullPool
        # (no pooled connection to ping) and adds a SELECT 1 round-trip
        # per checkout for no benefit. Each NullPool checkout opens a
        # fresh DBAPI connection, which is its own implicit liveness check.
        _engine = create_async_engine(
            url,
            echo=db_config.database_echo,
            poolclass=NullPool,
            connect_args={"check_same_thread": False},
        )

        # SQLite production PRAGMAs. Set on every new connection because
        # PRAGMAs are per-connection state in SQLite (and NullPool opens
        # a fresh connection per checkout).
        #
        # Correctness:
        # - journal_mode=WAL: readers don't block writers and writers don't
        #   block readers. Without this the default rollback journal takes
        #   an exclusive write lock, so any concurrent read+write from a
        #   single async event loop will collide.
        # - busy_timeout=5000: when contention still happens (single-writer
        #   constraint persists under WAL), wait up to 5s before failing
        #   instead of the default 0ms (which surfaces as "database is
        #   locked" on the second concurrent commit — observed in eval
        #   turn 2 on case_78c6ad39e2d4).
        # - foreign_keys=ON: SQLite ignores FK constraints by default.
        #   Setting this here lets ON DELETE CASCADE work; otherwise
        #   each cascade-needing call site has to do an explicit
        #   multi-phase delete (see the workaround at
        #   sqlite_case_repository.py:3334).
        #
        # Performance (safe defaults — no durability cost worth flagging):
        # - synchronous=NORMAL: canonical pairing with WAL per SQLite docs.
        #   Default FULL fsyncs on every commit; NORMAL is safe under WAL
        #   (loses at most the last few commits on POWER LOSS, never on
        #   app/OS crash) and substantially faster on commit-heavy paths
        #   like the per-turn case save.
        # - temp_store=MEMORY: temp tables / sort spill / index build go
        #   to RAM instead of /tmp files. Temp data is ephemeral by
        #   definition, so MEMORY has no durability cost.
        # - cache_size=-64000: ~64 MB page cache (negative = KB; positive
        #   = page count). Default 2000 pages ≈ 8 MB is too small for our
        #   hot working set (cases + recent messages + evidence rows);
        #   64 MB keeps the working set resident without hogging RAM.
        @event.listens_for(_engine.sync_engine, "connect")
        def _sqlite_set_pragmas(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            try:
                # Correctness PRAGMAs (must succeed for the app to behave).
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA foreign_keys=ON")
                # recursive_triggers=ON so an INSERT OR REPLACE fires the
                # DELETE trigger of the row it displaces. Without it, REPLACE's
                # implicit delete is silent, which would let it overwrite a row
                # in the append-only operator_access_audit table (migration 035)
                # straight past the trigger meant to forbid exactly that.
                cursor.execute("PRAGMA recursive_triggers=ON")
                # Performance PRAGMAs (safe defaults under WAL).
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA temp_store=MEMORY")
                cursor.execute("PRAGMA cache_size=-64000")
            finally:
                cursor.close()

    else:
        # PostgreSQL: Use connection pooling. The connect args bound a server
        # that stops answering (#2280): a command or connect past its timeout
        # aborts the connection instead of sending a CancelRequest, which a
        # silent server never answers. ``pg_connection`` holds the reasoning.
        _engine = create_async_engine(
            url,
            echo=db_config.database_echo,
            pool_pre_ping=True,  # Check connection health before use
            pool_size=db_config.database_pool_size,
            max_overflow=db_config.database_max_overflow,
            pool_timeout=db_config.database_pool_timeout,
            pool_recycle=db_config.database_pool_recycle,
            connect_args=pg_connect_args(url, db_config),
        )
        _engine_bounds = pg_bounds(url, db_config)
        event.listen(_engine.sync_engine, "connect", enable_tcp_liveness)
        _begin_statement = _transaction_settings_statement(_engine_bounds)
        _begin_params = _transaction_settings_params(_engine_bounds)

        # Re-apply the RLS tenant scope on EVERY transaction begin. The scope is
        # transaction-local (set_config(..., is_local=true)), so setting it once
        # per session would leave any query after a mid-session commit unscoped
        # (a non-superuser role would then read zero rows). The enterprise — the
        # isolation boundary under ADR-017 — comes from the request/task
        # contextvar (defaults to the Standalone enterprise). PostgreSQL only —
        # this branch is PG.
        #
        # ORDERING INVARIANT — bind the tenant BEFORE the transaction's first
        # statement. This listener samples the contextvar once, at BEGIN. A
        # set_current_enterprise_id() call after that reaches no further: the GUC
        # keeps whatever was bound at BEGIN for the rest of the transaction, and
        # there is no error to notice, so the caller reads and writes under the
        # stale scope believing it rebound. What makes this safe in practice is
        # that the repositories are sessionless — one get_db_session(), and so
        # one transaction, per operation — which puts almost every rebind between
        # transactions rather than inside one. Code that holds a single session
        # across several steps (the fm-* provisioning CLIs) does not get that
        # for free and must bind first or not at all; #935 was this bug.
        #
        # The same SELECT sets the server's statement_timeout (and the opted-in
        # client_connection_check_interval), transaction-scoped too, so a live
        # but slow server cancels its own statement before the client's command
        # timeout aborts the connection (#2280). No extra round trip, and safe
        # behind a pooler, which refuses or drops a startup parameter.
        @event.listens_for(_engine.sync_engine, "begin")
        def _scope_tenant_per_transaction(conn):
            from faultmaven.config.tenant_context import (
                get_current_enterprise_id,
            )

            conn.execute(
                _begin_statement,
                {"enterprise_id": get_current_enterprise_id(), **_begin_params},
            )

    return _engine


def _transaction_settings_statement(timeouts: PgBounds):
    """The ``begin`` listener's one SELECT: the tenant GUC, then each server timeout set."""
    settings = ["set_config('app.current_enterprise_id', :enterprise_id, true)"]
    if timeouts.statement_timeout_ms is not None:
        settings.append("set_config('statement_timeout', :statement_timeout, true)")
    if timeouts.client_connection_check_interval_ms is not None:
        settings.append(
            "set_config('client_connection_check_interval', "
            ":client_connection_check_interval, true)"
        )
    return text("SELECT " + ", ".join(settings))


def _transaction_settings_params(timeouts: PgBounds) -> dict[str, str]:
    """The timeouts' bind values, in milliseconds as text, as ``set_config`` takes them."""
    params = {}
    if timeouts.statement_timeout_ms is not None:
        params["statement_timeout"] = str(timeouts.statement_timeout_ms)
    if timeouts.client_connection_check_interval_ms is not None:
        params["client_connection_check_interval"] = str(
            timeouts.client_connection_check_interval_ms
        )
    return params


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
    - A database timeout raised as ``DatabaseTimeout`` (#2280)
    - Session cleanup

    Args:
        database_url: Optional database URL override

    Yields:
        AsyncSession for database operations

    Raises:
        DatabaseTimeout: The database did not answer in time: a driver or pool
            timeout, or the server's statement_timeout, escaped the session,
            bare or wrapped.

    Example:
        async with get_db_session() as session:
            result = await session.execute(query)
            session.add(new_model)
            # Commits automatically on exit
    """
    factory = get_session_factory(database_url)
    session = factory()

    try:
        # The PostgreSQL RLS tenant scope is applied per-transaction by the
        # engine "begin" listener (get_engine), so it survives mid-session
        # commits. No-op on SQLite.
        yield session
        await session.commit()
    except Exception as failure:
        await session.rollback()
        # A timeout out of a session is the database's (#2280): a session body
        # awaits nothing else. Named here, at the one door every repository
        # goes through, so the callers above can tell it from their own
        # timeouts by type, however a repository wrapped it on the way out.
        if _is_database_timeout(failure):
            raise DatabaseTimeout(
                f"The database did not answer in time: {failure}"
            ) from failure
        raise
    finally:
        await session.close()


#: SQLSTATE ``query_canceled``: what PostgreSQL sends when its own
#: ``statement_timeout`` cancels a statement, the first bound a live but slow
#: server meets (set below the command timeout by the ``begin`` listener). An
#: operator's ``pg_cancel_backend`` sends the same code, and only the message
#: tells them apart; both mean nothing was saved and a retry is safe, so both
#: are a ``DatabaseTimeout``.
QUERY_CANCELED_SQLSTATE = "57014"


def _is_database_timeout(failure: BaseException) -> bool:
    """A database timeout on ``failure``'s cause chain, not yet named.

    Three shapes, each read by type or by the SQLSTATE the server sent, never by
    message: the driver's command and connect timeouts arrive as a bare
    ``TimeoutError`` (SQLAlchemy does not wrap it), the pool's as
    ``sqlalchemy.exc.TimeoutError``, and the server's ``statement_timeout`` as a
    ``DBAPIError`` carrying SQLSTATE 57014. One already named
    ``DatabaseTimeout`` (an inner session's) is left as it is, so it is never
    wrapped twice.
    """
    chain = list(walk_cause_chain(failure))
    if any(isinstance(cause, DatabaseTimeout) for cause in chain):
        return False
    return any(
        isinstance(cause, (TimeoutError, PoolTimeoutError))
        or sqlstate(cause) == QUERY_CANCELED_SQLSTATE
        for cause in chain
    )


# ============================================================
# Database Initialization
# ============================================================


async def init_database(database_url: Optional[str] = None) -> None:
    """
    Initialize database tables via SQLAlchemy `Base.metadata.create_all`.

    **TEST/DEV ONLY.** Production must always use Alembic migrations
    (`alembic upgrade head`). This function emits the *current ORM-model
    view* of the schema, which omits Tier 2 PostgreSQL augmentations
    (CHECK constraints, GIN/partial/expression indexes, JSONB column
    types, RLS policies) that live exclusively in Alembic migrations
    per the deployment-aware schema strategy.

    Calling this on a running production database WILL produce a
    schema that lacks the integrity / performance / security guarantees
    the migrations install.

    Args:
        database_url: Optional database URL override
    """
    engine = get_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialized (test/dev only — production uses Alembic)")


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
    global _engine, _engine_bounds, _session_factory

    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _engine_bounds = None
        _session_factory = None
        logger.info("Database connections closed")


def reset_engine() -> None:
    """
    Reset engine and session factory.

    Use for testing to create fresh database connections.
    """
    global _engine, _engine_bounds, _session_factory
    _engine = None
    _engine_bounds = None
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
            # Execute a simple query to test connection.
            # SQLAlchemy 2.0 async API requires text() wrapper for raw SQL.
            result = await session.execute(text("SELECT 1"))
            result.fetchone()
            # The dialect of the connection that just answered — not the
            # configured URL, which the ``database_url`` override bypasses.
            database_type = session.get_bind().dialect.name

        return {
            "status": "healthy",
            "database_type": database_type,
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
        }
