"""Persistence layer for FaultMaven.

This package provides database and storage implementations for the application.

Modules:
- models: SQLAlchemy ORM models for database tables
- database: Database session management and connection pooling
- database_case_repository: SQLAlchemy ORM-based case repository
- repository_factory: Factory pattern for repository creation
- case_repository: Base repository interfaces and implementations
"""

from faultmaven.infrastructure.persistence.case_repository import (
    CaseRepository,
    InMemoryCaseRepository,
    RepositoryException,
)
from faultmaven.infrastructure.persistence.database_case_repository import (
    DatabaseCaseRepository,
)
from faultmaven.infrastructure.persistence.repository_factory import (
    get_case_repository,
    get_case_repository_async,
    create_case_repository,
    reset_inmemory_repository,
    get_repository_dependency,
)
from faultmaven.infrastructure.persistence.database import (
    get_db_session,
    init_database,
    close_database,
    reset_engine,
    check_database_health,
)

__all__ = [
    # Base interfaces
    "CaseRepository",
    "InMemoryCaseRepository",
    "RepositoryException",
    # Database repository
    "DatabaseCaseRepository",
    # Factory functions
    "get_case_repository",
    "get_case_repository_async",
    "create_case_repository",
    "reset_inmemory_repository",
    "get_repository_dependency",
    # Database management
    "get_db_session",
    "init_database",
    "close_database",
    "reset_engine",
    "check_database_health",
]
