"""Token Revocation Store Implementations.

Storage implementations for JWT token revocation tracking.
Revoked tokens are stored by their JTI (JWT ID) with TTL matching token expiration.

Two implementations:
1. RedisTokenRevocationStore - For all deployments (real Redis or FakeRedis)
2. PostgresTokenRevocationStore - For enterprise deployment (persistent audit trail, ORM)
"""

from datetime import datetime, timedelta, timezone

from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    ITokenRevocationStore,
)


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
        from faultmaven.infrastructure.persistence.db_compat import dialect_insert
        from faultmaven.infrastructure.persistence.models import OAuthRevokedTokenModel

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

        async with self.session_factory() as session:
            stmt = dialect_insert(session, OAuthRevokedTokenModel).values(
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
