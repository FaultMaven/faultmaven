"""User Repository - Re-exports from canonical location.

The canonical implementation lives in infrastructure/persistence/user_repository.py.
This module re-exports for backward compatibility with existing imports.
"""

from faultmaven.infrastructure.persistence.user_repository import (  # noqa: F401
    InMemoryUserRepository,
    PostgreSQLUserRepository,
    User,
    UserRepository,
)
