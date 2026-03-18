"""PostgreSQL Hybrid Case Repository - Re-exports from canonical location.

The canonical implementation lives in modules/case/infrastructure/postgresql_hybrid_case_repository.py.
This module re-exports for backward compatibility with existing imports.
"""

from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository import (  # noqa: F401
    PostgreSQLHybridCaseRepository,
)
