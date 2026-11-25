"""
FaultMaven Alembic Environment Configuration

This module configures Alembic for FaultMaven's database migration system.

FaultMaven keeps every table in ONE database: the one ``DATABASE_URL`` names,
which is also the database the application opens.

Features:
- Environment-based database URL configuration (``DATABASE_URL``)
- SQLite and PostgreSQL compatibility
- Automatic driver conversion (asyncpg -> psycopg2 for sync operations)

Environment Variables:
    DATABASE_URL: The database to migrate, bound as the app's settings bind it
        (in any case). Unset: ``data/faultmaven.db``
        (SQLite) under the project root. Set to a value that configures no
        persistent database (empty or blank, ``:memory:``, an in-memory
        SQLite URL, a value that does not parse), an online migration refuses
        and exits 1 (#1704). Offline (``--sql``) opens no database and only
        takes the dialect from the URL: an empty or blank value takes the
        default file's, and a value that does not parse as a database URL
        (``:memory:`` included) is refused, exit 1 (#2754). A value with
        whitespace around non-blank content (#1789), or whose query names a
        Postgres root input outside the TLS root set or holds a key that is
        not a plain option name (#2787), is refused in both modes, exit 1.
        Only the URL is read: no other database setting is validated here.

Usage:
    alembic upgrade head
"""

import logging
import sys
from logging.config import fileConfig
from pathlib import Path

project_root = Path(__file__).parent.parent

# Add project root to path for model imports
sys.path.insert(0, str(project_root))

# Log through the API's chain on stderr, as alembic.ini's levels set, before
# anything below can log (principles §7; #2888). It reads the log format alone
# (LOG_FORMAT, else LOG_OUTPUT_FORMAT).
# fileConfig, below, removes it with the root's handlers, and it is installed
# again after it, with the format chosen here.
from faultmaven.infrastructure.logging.config import install_log_handler

log_format = install_log_handler(sys.stderr, None)

from sqlalchemy import engine_from_config, pool, text
from sqlalchemy.exc import DBAPIError

from alembic import context

from faultmaven.config.settings import (
    DatabaseUrlWhitespaceError,
    _parse_database_url,
    configured_database_url,
    describe_database_url,
)
from faultmaven.config.tls_root_set import (
    apply_root_set,
    pg_root_inputs_in_url,
    pg_url_refusal,
)

# Load .env as the app does, before anything below reads the environment: the
# working directory's file only (run alembic from the repository root), and
# none under FAULTMAVEN_SKIP_DOTENV (#2140). Then name the TLS root set before
# psycopg2 is imported, so a TLS migration verifies against it (#2692).
apply_root_set()

# Alembic Config object
config = context.config

# Logger levels from alembic.ini, without disabling the loggers the imports
# above made. The ini names no handler: the root's is the chain's again.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
    install_log_handler(sys.stderr, None, log_format)

# Target metadata for autogenerate - import from models
from faultmaven.cli._database_gate import (
    MIGRATION_PRIVILEGE_REMEDY,
    exit_refusing,
    require_persistent_database_url_or_exit,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.value_free_errors import (
    install_value_free_errors,
)

target_metadata = Base.metadata

#: PostgreSQL's SQLSTATE for a refused privilege: a statement row-level security
#: would filter, under ``row_security = off``, raises it.
INSUFFICIENT_PRIVILEGE = "42501"


def get_database_url(*, require_persistent: bool) -> str:
    """
    Get the database URL to migrate.

    Priority:
    1. Environment: DATABASE_URL (async drivers converted to sync ones),
       refused unless it configures a persistent database when
       ``require_persistent`` (online mode)
    2. Default: SQLite ``data/faultmaven.db`` under the project root

    The value is read through ``configured_database_url()`` — the environment
    source of ``DatabaseSettings``, the class the app's settings are built from
    — so the migration binds the variable in the flat spellings the app does
    (``database_url`` included). Only the URL is read; no other database field
    is validated, so a setting alembic never uses cannot refuse a migration.

    A value with whitespace around non-blank content is refused in both modes,
    exit 1, as the app refuses it (#1789), and so is one whose query names a
    Postgres root input outside the TLS root set or holds a key that is not a
    plain option name (``pg_root_inputs_in_url``, #2787). Otherwise offline
    (``--sql``) opens no database: the URL only names the dialect, so a
    non-persistent one is not refused, an empty or blank value falls back to
    the default file URL, and a value that does not parse as a database URL
    names no dialect and is refused, exit 1 (#2754).

    Returns:
        str: Database connection URL
    """
    try:
        url = configured_database_url()
    except DatabaseUrlWhitespaceError as exc:
        exit_refusing(str(exc))
    # A blank value is no database, as every reader of the setting treats it
    # (``require_no_surrounding_whitespace``): it decides as the empty one does,
    # and a message shows the value as set (#2754).
    named = url if url and url.strip() else ""
    root_inputs = pg_root_inputs_in_url(named) if named else []
    if root_inputs:
        exit_refusing(pg_url_refusal(root_inputs))
    if require_persistent and url is not None:
        # Set but EMPTY or BLANK refuses too, rather than falling back to the
        # default file: the app's predicate refuses both, and the migration must
        # agree with the app on which database a value names (#1704). Judged on
        # the value as configured, before any driver conversion.
        require_persistent_database_url_or_exit(url)
    if named:
        # Refuses offline only: online, the persistence gate above refused a
        # value that does not parse already.
        if _parse_database_url(named) is None:
            exit_refusing(
                f"DATABASE_URL={describe_database_url(url)} does not parse as a "
                "database URL, so it names no dialect to render SQL for. Unset "
                "DATABASE_URL to render for the default SQLite file, or give a "
                "database URL."
            )
        return _convert_async_url(named)

    # Default to SQLite for development
    sqlite_path = project_root / "data" / "faultmaven.db"
    return f"sqlite:///{sqlite_path}"


def _convert_async_url(url: str) -> str:
    """
    Convert async database URLs to sync-compatible URLs.

    Alembic requires synchronous database drivers. This function converts:
    - postgresql+asyncpg:// -> postgresql://
    - postgresql+aiopg:// -> postgresql://

    Args:
        url: Original database URL (may use async driver)

    Returns:
        str: Sync-compatible database URL
    """
    async_drivers = [
        ("postgresql+asyncpg://", "postgresql://"),
        ("postgresql+aiopg://", "postgresql://"),
        ("mysql+aiomysql://", "mysql://"),
        ("sqlite+aiosqlite://", "sqlite://"),
    ]

    for async_pattern, sync_pattern in async_drivers:
        if url.startswith(async_pattern):
            return url.replace(async_pattern, sync_pattern, 1)

    return url


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine.
    By skipping the Engine creation, we don't even need a DBAPI available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = get_database_url(require_persistent=False)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    Creates an Engine and associates a connection with the context.
    """
    # Get database URL
    url = get_database_url(require_persistent=True)

    # Build engine configuration
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = url

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        hide_parameters=True,
    )
    # Data migrations write rows: a failed one's error names no values (#2377).
    install_value_free_errors(connectable)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            try:
                context.run_migrations()
            except DBAPIError as error:
                # A refused privilege, row-level security's included: the
                # server's text names a table, so the remedy is this literal,
                # on the log chain, beside the error's class and frames (#2888).
                if getattr(error.orig, "pgcode", None) == INSUFFICIENT_PRIVILEGE:
                    logging.getLogger("alembic.runtime.migration").error(
                        MIGRATION_PRIVILEGE_REMEDY
                    )
                raise


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
