"""Token Revocation Store Implementations.

Storage implementations for JWT token revocation tracking.
Revoked tokens are stored by their JTI (JWT ID) with TTL matching token expiration.

Three implementations:
1. InMemoryTokenRevocationStore - For local development (zero dependencies)
2. RedisTokenRevocationStore - For cloud deployment (ephemeral storage)
3. PostgresTokenRevocationStore - For enterprise deployment (persistent audit trail, ORM)
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Set

from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    ITokenRevocationStore,
)


class InMemoryTokenRevocationStore(ITokenRevocationStore):
    """In-memory implementation of token revocation store."""

    def __init__(self):
        self._revoked_tokens: Set[str] = set()
        self._expiry_times: Dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        async with self._lock:
            self._revoked_tokens.add(jti)
            self._expiry_times[jti] = datetime.now(timezone.utc) + timedelta(
                seconds=ttl
            )

    async def is_revoked(self, jti: str) -> bool:
        async with self._lock:
            if jti not in self._revoked_tokens:
                return False
            expiry = self._expiry_times.get(jti)
            if expiry and datetime.now(timezone.utc) > expiry:
                self._revoked_tokens.discard(jti)
                self._expiry_times.pop(jti, None)
                return False
            return True

    async def cleanup_expired(self) -> int:
        async with self._lock:
            now = datetime.now(timezone.utc)
            expired_jtis = [
                jti for jti, expiry in self._expiry_times.items() if now > expiry
            ]
            for jti in expired_jtis:
                self._revoked_tokens.discard(jti)
                self._expiry_times.pop(jti, None)
            return len(expired_jtis)


class RedisTokenRevocationStore(ITokenRevocationStore):
    """Redis implementation of token revocation store.

    Uses Redis TTL for automatic expiration.
    """

    def __init__(self, redis_client):
        self.redis = redis_client
        self._key_prefix = "oauth:revoked:"

    def _make_key(self, jti: str) -> str:
        return f"{self._key_prefix}{jti}"

    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        key = self._make_key(jti)
        await self.redis.setex(key, ttl, "1")

    async def is_revoked(self, jti: str) -> bool:
        key = self._make_key(jti)
        return await self.redis.exists(key) > 0

    async def cleanup_expired(self) -> int:
        return 0  # Redis handles expiration automatically


class PostgresTokenRevocationStore(ITokenRevocationStore):
    """SQLAlchemy ORM implementation of token revocation store."""

    def __init__(self, db_session_factory):
        self.session_factory = db_session_factory

    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        from faultmaven.infrastructure.persistence.models import OAuthRevokedTokenModel

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

        async with self.session_factory() as session:
            stmt = sqlite_insert(OAuthRevokedTokenModel).values(
                jti=jti,
                expires_at=expires_at,
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["jti"])
            await session.execute(stmt)
            await session.commit()

    async def is_revoked(self, jti: str) -> bool:
        from sqlalchemy import select

        from faultmaven.infrastructure.persistence.models import OAuthRevokedTokenModel

        async with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            stmt = select(OAuthRevokedTokenModel.jti).where(
                OAuthRevokedTokenModel.jti == jti,
                OAuthRevokedTokenModel.expires_at > now,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None

    async def cleanup_expired(self) -> int:
        from sqlalchemy import delete

        from faultmaven.infrastructure.persistence.models import OAuthRevokedTokenModel

        async with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            stmt = delete(OAuthRevokedTokenModel).where(
                OAuthRevokedTokenModel.expires_at <= now
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount
