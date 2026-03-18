"""Team Repository - Re-exports from canonical location.

The canonical implementation lives in infrastructure/persistence/team_repository.py.
This module re-exports for backward compatibility with existing imports.
"""

from faultmaven.infrastructure.persistence.team_repository import (  # noqa: F401
    PostgreSQLTeamRepository,
)
