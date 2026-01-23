"""Authentication Infrastructure

Purpose: Infrastructure components for authentication and user management

This package provides the infrastructure layer components for FaultMaven's
authentication system, including token management, user storage, and
security utilities.

Key Components:
- RedisTokenManager: Redis-based token generation, validation, and lifecycle management
- RedisUserStore: Redis-based user account storage and retrieval
- InMemoryTokenManager: In-memory token management for local development
- InMemoryUserStore: In-memory user storage for local development
- DatabaseUserStore: Database-backed user storage (SQLite/PostgreSQL) for persistent local deployment
- Authentication utilities: Token hashing, validation, cleanup
"""

from .database_user_store import DatabaseUserStore
from .inmemory_token_manager import InMemoryTokenManager
from .inmemory_user_store import InMemoryUserStore
from .token_manager import RedisTokenManager
from .user_store import RedisUserStore

__all__ = [
    "RedisTokenManager",
    "RedisUserStore",
    "InMemoryTokenManager",
    "InMemoryUserStore",
    "DatabaseUserStore",
]
