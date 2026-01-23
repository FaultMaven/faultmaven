"""Token Revocation Store Implementations.

This module provides storage implementations for JWT token revocation tracking.
Revoked tokens are stored by their JTI (JWT ID) with TTL matching token expiration.

Three implementations:
1. InMemoryTokenRevocationStore - For local development (zero dependencies)
2. RedisTokenRevocationStore - For cloud deployment (ephemeral storage)
3. PostgresTokenRevocationStore - For enterprise deployment (persistent audit trail)
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Set

from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    ITokenRevocationStore,
)


class InMemoryTokenRevocationStore(ITokenRevocationStore):
    """In-memory implementation of token revocation store.

    Stores revoked JTIs in Python set with automatic TTL expiration.
    Suitable for local development with zero infrastructure dependencies.
    """

    def __init__(self):
        """Initialize in-memory revocation store."""
        self._revoked_tokens: Set[str] = set()
        self._expiry_times: Dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        """Add token JTI to revocation list.

        Args:
            jti: JWT ID to revoke
            ttl: Time to live in seconds
        """
        async with self._lock:
            self._revoked_tokens.add(jti)
            self._expiry_times[jti] = datetime.now(timezone.utc) + timedelta(
                seconds=ttl
            )

    async def is_revoked(self, jti: str) -> bool:
        """Check if token JTI is revoked.

        Args:
            jti: JWT ID to check

        Returns:
            True if revoked and not expired, False otherwise
        """
        async with self._lock:
            if jti not in self._revoked_tokens:
                return False

            # Check if revocation entry expired
            expiry = self._expiry_times.get(jti)
            if expiry and datetime.now(timezone.utc) > expiry:
                # Expired, clean up
                self._revoked_tokens.discard(jti)
                self._expiry_times.pop(jti, None)
                return False

            return True

    async def cleanup_expired(self) -> int:
        """Clean up expired revocation entries.

        Returns:
            Count of entries cleaned up
        """
        async with self._lock:
            now = datetime.now(timezone.utc)
            expired_jtis = [
                jti
                for jti, expiry in self._expiry_times.items()
                if now > expiry
            ]

            for jti in expired_jtis:
                self._revoked_tokens.discard(jti)
                self._expiry_times.pop(jti, None)

            return len(expired_jtis)


class RedisTokenRevocationStore(ITokenRevocationStore):
    """Redis implementation of token revocation store.

    Stores revoked JTIs in Redis with automatic TTL expiration.
    Suitable for cloud deployment with ephemeral storage.
    """

    def __init__(self, redis_client):
        """Initialize Redis revocation store.

        Args:
            redis_client: Redis client instance
        """
        self.redis = redis_client
        self._key_prefix = "oauth:revoked:"

    def _make_key(self, jti: str) -> str:
        """Generate Redis key for revoked token."""
        return f"{self._key_prefix}{jti}"

    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        """Add token JTI to revocation list in Redis.

        Args:
            jti: JWT ID to revoke
            ttl: Time to live in seconds
        """
        key = self._make_key(jti)
        # Store with TTL - value doesn't matter, existence indicates revocation
        await self.redis.setex(key, ttl, "1")

    async def is_revoked(self, jti: str) -> bool:
        """Check if token JTI is revoked in Redis.

        Args:
            jti: JWT ID to check

        Returns:
            True if revoked, False otherwise
        """
        key = self._make_key(jti)
        return await self.redis.exists(key) > 0

    async def cleanup_expired(self) -> int:
        """Clean up expired revocation entries.

        Returns:
            Count of entries cleaned up

        Note: Redis automatically expires keys via TTL, so this is a no-op.
        """
        # Redis handles expiration automatically
        return 0


class PostgresTokenRevocationStore(ITokenRevocationStore):
    """PostgreSQL implementation of token revocation store.

    Stores revoked JTIs in PostgreSQL table for persistent audit trail.
    Suitable for enterprise deployment requiring compliance/audit.

    Table schema:
        CREATE TABLE oauth_revoked_tokens (
            jti VARCHAR(64) PRIMARY KEY,
            revoked_at TIMESTAMP NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMP NOT NULL,
            INDEX idx_expires_at (expires_at)
        );
    """

    def __init__(self, db_session_factory):
        """Initialize PostgreSQL revocation store.

        Args:
            db_session_factory: SQLAlchemy session factory
        """
        self.session_factory = db_session_factory

    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        """Add token JTI to revocation list in PostgreSQL.

        Args:
            jti: JWT ID to revoke
            ttl: Time to live in seconds
        """
        from sqlalchemy import text

        async with self.session_factory() as session:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

            query = text("""
                INSERT INTO oauth_revoked_tokens (jti, expires_at)
                VALUES (:jti, :expires_at)
                ON CONFLICT (jti) DO NOTHING
            """)

            await session.execute(query, {
                "jti": jti,
                "expires_at": expires_at,
            })
            await session.commit()

    async def is_revoked(self, jti: str) -> bool:
        """Check if token JTI is revoked in PostgreSQL.

        Args:
            jti: JWT ID to check

        Returns:
            True if revoked and not expired, False otherwise
        """
        from sqlalchemy import text

        async with self.session_factory() as session:
            query = text("""
                SELECT 1 FROM oauth_revoked_tokens
                WHERE jti = :jti AND expires_at > NOW()
            """)

            result = await session.execute(query, {"jti": jti})
            return result.fetchone() is not None

    async def cleanup_expired(self) -> int:
        """Clean up expired revocation entries from PostgreSQL.

        Returns:
            Count of entries cleaned up
        """
        from sqlalchemy import text

        async with self.session_factory() as session:
            query = text("""
                DELETE FROM oauth_revoked_tokens
                WHERE expires_at <= NOW()
            """)

            result = await session.execute(query)
            await session.commit()

            return result.rowcount
