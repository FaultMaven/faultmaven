"""Organization Repository - Re-exports from canonical location.

The canonical implementation lives in infrastructure/persistence/organization_repository.py.
This module re-exports for backward compatibility with existing imports.
"""

from faultmaven.infrastructure.persistence.organization_repository import (  # noqa: F401
    PostgreSQLOrganizationRepository,
)
