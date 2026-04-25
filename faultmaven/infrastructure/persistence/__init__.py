"""Persistence layer for FaultMaven.

This package provides database and storage implementations for the application.

Modules:
- models: SQLAlchemy ORM models for database tables
- database: Database session management and connection pooling
- repository_factory: Factories for investigation session and knowledge
  item repositories. (Case repositories live in
  ``faultmaven.modules.case.infrastructure``.)
"""

from faultmaven.infrastructure.persistence.database import (
    check_database_health,
    close_database,
    get_db_session,
    init_database,
    reset_engine,
)

__all__ = [
    "get_db_session",
    "init_database",
    "close_database",
    "reset_engine",
    "check_database_health",
]
