"""Token Revocation Store Implementation.

The single revocation store for the whole deployment (issue #767): every
revoke path (OAuth /revoke, refresh rotation in both auth modes, logout)
writes here, and the request-path check (``AuthService._is_token_revoked``)
reads here. Revoked tokens are stored by their JTI (JWT ID) with TTL matching
token expiration; Redis expiry handles cleanup.

The key prefix comes from ``settings.security.token_revocation_prefix`` so
writers and the reader can never disagree on the namespace.
"""

from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    ITokenRevocationStore,
)


class RedisTokenRevocationStore(ITokenRevocationStore):
    """Redis implementation of token revocation store.

    Works against real Redis (cloud) or FakeRedis (standalone).
    Uses Redis TTL for automatic expiration.
    """

    def __init__(self, redis_client, key_prefix: str = "revoked:token:"):
        self.redis = redis_client
        self._key_prefix = key_prefix

    def _make_key(self, jti: str) -> str:
        return f"{self._key_prefix}{jti}"

    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        key = self._make_key(jti)
        await self.redis.setex(key, ttl, "revoked")

    async def is_revoked(self, jti: str) -> bool:
        key = self._make_key(jti)
        return await self.redis.exists(key) > 0

    async def cleanup_expired(self) -> int:
        return 0  # Redis handles expiration automatically
