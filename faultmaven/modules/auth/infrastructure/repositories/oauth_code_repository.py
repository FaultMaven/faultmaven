"""OAuth Authorization Code Repository Implementations.

This module provides three implementations of IOAuthCodeRepository:
1. InMemoryOAuthCodeRepository - For local development (zero dependencies)
2. RedisOAuthCodeRepository - For cloud deployment (ephemeral storage)
3. PostgresOAuthCodeRepository - For enterprise deployment (persistent storage)

All implementations follow the deployment-agnostic design principle where
the OAuth service receives the repository via dependency injection.
"""

import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional

from faultmaven.modules.auth.contracts import IOAuthCodeRepository, OAuthCodeDTO


class InMemoryOAuthCodeRepository(IOAuthCodeRepository):
    """In-memory implementation of OAuth code repository.

    Stores authorization codes in Python dictionaries with automatic TTL expiration.
    Suitable for local development with zero infrastructure dependencies.

    Data is lost on application restart (by design - codes are short-lived anyway).
    """

    def __init__(self):
        """Initialize in-memory storage with Python dicts."""
        self._codes: Dict[str, OAuthCodeDTO] = {}  # code -> OAuthCodeDTO
        self._lock = asyncio.Lock()  # Thread-safe operations

    async def save_code(self, code_data: OAuthCodeDTO) -> None:
        """Store authorization code with PKCE challenge.

        Args:
            code_data: Authorization code and associated metadata

        The code expires automatically based on expires_at timestamp.
        """
        async with self._lock:
            self._codes[code_data.code] = code_data

    async def get_code(self, code: str) -> Optional[OAuthCodeDTO]:
        """Retrieve authorization code data.

        Args:
            code: The authorization code

        Returns:
            Code data if found and not expired, None otherwise
        """
        async with self._lock:
            code_data = self._codes.get(code)

            if not code_data:
                return None

            # Check expiration
            if datetime.now(timezone.utc) > code_data.expires_at:
                # Expired, clean up
                del self._codes[code]
                return None

            return code_data

    async def mark_code_used(self, code: str) -> None:
        """Mark code as used (prevents replay attacks).

        Args:
            code: The authorization code to mark as used
        """
        async with self._lock:
            if code in self._codes:
                # Create new DTO with used=True to maintain immutability
                existing = self._codes[code]
                self._codes[code] = OAuthCodeDTO(
                    code=existing.code,
                    user_id=existing.user_id,
                    redirect_uri=existing.redirect_uri,
                    code_challenge=existing.code_challenge,
                    expires_at=existing.expires_at,
                    used=True,
                )

    async def delete_expired_codes(self) -> int:
        """Clean up expired codes (maintenance operation).

        Returns:
            Count of codes deleted
        """
        async with self._lock:
            now = datetime.now(timezone.utc)
            expired_codes = [
                code
                for code, code_data in self._codes.items()
                if now > code_data.expires_at
            ]

            for code in expired_codes:
                del self._codes[code]

            return len(expired_codes)


class RedisOAuthCodeRepository(IOAuthCodeRepository):
    """Redis implementation of OAuth code repository.

    Stores authorization codes in Redis with automatic TTL expiration.
    Suitable for cloud deployment with ephemeral storage requirements.

    Uses Redis features:
    - Automatic TTL expiration (no manual cleanup needed)
    - Atomic operations
    - High performance
    """

    def __init__(self, redis_client):
        """Initialize Redis repository.

        Args:
            redis_client: Redis client instance (from redis-py or aioredis)
        """
        self.redis = redis_client
        self._key_prefix = "oauth:code:"

    def _make_key(self, code: str) -> str:
        """Generate Redis key for authorization code."""
        return f"{self._key_prefix}{code}"

    async def save_code(self, code_data: OAuthCodeDTO) -> None:
        """Store authorization code with PKCE challenge in Redis.

        Args:
            code_data: Authorization code and associated metadata

        Redis automatically expires the key based on TTL.
        """
        import json

        key = self._make_key(code_data.code)

        # Serialize code data to JSON
        value = json.dumps(
            {
                "code": code_data.code,
                "user_id": code_data.user_id,
                "redirect_uri": code_data.redirect_uri,
                "code_challenge": code_data.code_challenge,
                "expires_at": code_data.expires_at.isoformat(),
                "used": code_data.used,
            }
        )

        # Calculate TTL in seconds
        now = datetime.now(timezone.utc)
        ttl = int((code_data.expires_at - now).total_seconds())

        if ttl > 0:
            # Set with expiration
            await self.redis.setex(key, ttl, value)

    async def get_code(self, code: str) -> Optional[OAuthCodeDTO]:
        """Retrieve authorization code data from Redis.

        Args:
            code: The authorization code

        Returns:
            Code data if found and not expired, None otherwise
        """
        import json

        key = self._make_key(code)
        value = await self.redis.get(key)

        if not value:
            return None

        # Deserialize from JSON
        data = json.loads(value)

        return OAuthCodeDTO(
            code=data["code"],
            user_id=data["user_id"],
            redirect_uri=data["redirect_uri"],
            code_challenge=data["code_challenge"],
            expires_at=datetime.fromisoformat(data["expires_at"]),
            used=data["used"],
        )

    async def mark_code_used(self, code: str) -> None:
        """Mark code as used in Redis.

        Args:
            code: The authorization code to mark as used
        """
        import json

        key = self._make_key(code)

        # Get existing code data
        value = await self.redis.get(key)
        if not value:
            return

        # Update used flag
        data = json.loads(value)
        data["used"] = True

        # Get remaining TTL
        ttl = await self.redis.ttl(key)

        if ttl > 0:
            # Update with same TTL
            await self.redis.setex(key, ttl, json.dumps(data))

    async def delete_expired_codes(self) -> int:
        """Clean up expired codes (maintenance operation).

        Returns:
            Count of codes deleted

        Note: Redis automatically expires keys, so this is a no-op.
        Included for interface compliance.
        """
        # Redis handles expiration automatically via TTL
        return 0


class PostgresOAuthCodeRepository(IOAuthCodeRepository):
    """PostgreSQL implementation of OAuth code repository.

    Stores authorization codes in PostgreSQL table with expiration tracking.
    Suitable for enterprise deployment requiring persistent audit trail.

    Table schema:
        CREATE TABLE oauth_authorization_codes (
            code VARCHAR(64) PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            redirect_uri TEXT NOT NULL,
            code_challenge VARCHAR(64) NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW(),
            INDEX idx_expires_at (expires_at)
        );
    """

    def __init__(self, db_session_factory):
        """Initialize PostgreSQL repository.

        Args:
            db_session_factory: SQLAlchemy session factory
        """
        self.session_factory = db_session_factory

    async def save_code(self, code_data: OAuthCodeDTO) -> None:
        """Store authorization code with PKCE challenge in PostgreSQL.

        Args:
            code_data: Authorization code and associated metadata
        """
        from sqlalchemy import text

        async with self.session_factory() as session:
            query = text("""
                INSERT INTO oauth_authorization_codes
                (code, user_id, redirect_uri, code_challenge, expires_at, used)
                VALUES (:code, :user_id, :redirect_uri, :code_challenge, :expires_at, :used)
            """)

            await session.execute(
                query,
                {
                    "code": code_data.code,
                    "user_id": code_data.user_id,
                    "redirect_uri": code_data.redirect_uri,
                    "code_challenge": code_data.code_challenge,
                    "expires_at": code_data.expires_at,
                    "used": code_data.used,
                },
            )
            await session.commit()

    async def get_code(self, code: str) -> Optional[OAuthCodeDTO]:
        """Retrieve authorization code data from PostgreSQL.

        Args:
            code: The authorization code

        Returns:
            Code data if found and not expired, None otherwise
        """
        from sqlalchemy import text

        async with self.session_factory() as session:
            query = text("""
                SELECT code, user_id, redirect_uri, code_challenge, expires_at, used
                FROM oauth_authorization_codes
                WHERE code = :code AND expires_at > NOW()
            """)

            result = await session.execute(query, {"code": code})
            row = result.fetchone()

            if not row:
                return None

            return OAuthCodeDTO(
                code=row[0],
                user_id=row[1],
                redirect_uri=row[2],
                code_challenge=row[3],
                expires_at=row[4],
                used=row[5],
            )

    async def mark_code_used(self, code: str) -> None:
        """Mark code as used in PostgreSQL.

        Args:
            code: The authorization code to mark as used
        """
        from sqlalchemy import text

        async with self.session_factory() as session:
            query = text("""
                UPDATE oauth_authorization_codes
                SET used = TRUE
                WHERE code = :code
            """)

            await session.execute(query, {"code": code})
            await session.commit()

    async def delete_expired_codes(self) -> int:
        """Clean up expired codes from PostgreSQL.

        Returns:
            Count of codes deleted
        """
        from sqlalchemy import text

        async with self.session_factory() as session:
            query = text("""
                DELETE FROM oauth_authorization_codes
                WHERE expires_at <= NOW()
            """)

            result = await session.execute(query)
            await session.commit()

            return result.rowcount
