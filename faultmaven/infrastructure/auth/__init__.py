"""Authentication Infrastructure

Purpose: Infrastructure components for authentication and user management

This package provides the infrastructure layer components for FaultMaven's
authentication system, including token management, user storage, and
security utilities.

Key Components:
- RedisUserStore: User account storage and retrieval (works with FakeRedis)
- DatabaseUserStore: Database-backed user storage (SQLite/PostgreSQL) for persistent local deployment

Token lifecycle lives in the auth module: JWTs are minted by the generators in
``modules/auth/domain/services/jwt_token_generator.py`` and revoked through the
single deployment-wide store in
``modules/auth/infrastructure/stores/token_revocation_store.py``.
"""

from .database_user_store import DatabaseUserStore
from .user_store import RedisUserStore

__all__ = [
    "RedisUserStore",
    "DatabaseUserStore",
]
