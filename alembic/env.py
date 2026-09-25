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
    DATABASE_URL: The database to migrate. Unset: ``data/faultmaven.db``
        (SQLite) under the project root.

Usage:
    alembic upgrade head
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool, text

from alembic import context

# Load .env file from project root
project_root = Path(__file__).parent.parent
env_file = project_root / ".env"
if env_file.exists():
    load_dotenv(env_file)

# Alembic Config object
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Add project root to path for model imports
sys.path.insert(0, str(project_root))

# Target metadata for autogenerate - import from models
from faultmaven.infrastructure.persistence.models import Base

target_metadata = Base.metadata


def get_database_url() -> str:
    """
    Get the database URL to migrate.

    Priority:
    1. Environment: DATABASE_URL (async drivers converted to sync ones)
    2. Default: SQLite ``data/faultmaven.db`` under the project root

    Returns:
        str: Database connection URL
    """
    url = os.getenv("DATABASE_URL")
    if url:
        return _convert_async_url(url)

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
    url = get_database_url()
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
    url = get_database_url()

    # Build engine configuration
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = url

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
