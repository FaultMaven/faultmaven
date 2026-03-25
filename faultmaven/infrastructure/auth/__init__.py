"""Authentication Infrastructure

Purpose: Infrastructure components for authentication and user management

This package provides the infrastructure layer components for FaultMaven's
authentication system, including token management, user storage, and
security utilities.

Key Components:
- RedisTokenManager: Token generation, validation, and lifecycle management (works with FakeRedis)
- RedisUserStore: User account storage and retrieval (works with FakeRedis)
- DatabaseUserStore: Database-backed user storage (SQLite/PostgreSQL) for persistent local deployment
- Authentication utilities: Token hashing, validation, cleanup
"""

from .database_user_store import DatabaseUserStore
from .token_manager import RedisTokenManager
from .user_store import RedisUserStore

__all__ = [
    "RedisTokenManager",
    "RedisUserStore",
    "DatabaseUserStore",
]
