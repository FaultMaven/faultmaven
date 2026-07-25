"""Token Revocation Store Implementation.

The single revocation store for the whole deployment (issue #767): every
revoke path (OAuth /revoke, refresh rotation in both auth modes, logout,
admin per-user revocation) writes here, and the request-path check
(``AuthService._is_revoked``) reads here. Redis expiry handles cleanup.

Two granularities (see ``ITokenRevocationStore``): individual tokens by JTI,
and whole users by revocation watermark (#769). Both live in this one store
with TTLs matching token expiration.

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
        # Derived from the same prefix rather than configured separately, so
        # the per-token and per-user namespaces can never be pointed at
        # different Redis instances or drift apart. JTIs are UUIDs, so no jti
        # key can collide with the "user:" sub-namespace.
        self._user_key_prefix = f"{key_prefix}user:"

    def _make_key(self, jti: str) -> str:
        return f"{self._key_prefix}{jti}"

    def _make_user_key(self, user_id: str) -> str:
        return f"{self._user_key_prefix}{user_id}"

    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        key = self._make_key(jti)
        await self.redis.setex(key, ttl, "revoked")

    async def is_revoked(self, jti: str) -> bool:
        key = self._make_key(jti)
        return await self.redis.exists(key) > 0

    async def revoke_user_tokens_before(
        self, user_id: str, revoked_at: int, ttl: int
    ) -> None:
        key = self._make_user_key(user_id)
        await self.redis.setex(key, ttl, str(revoked_at))

    async def is_user_revoked(self, user_id: str, issued_at: int) -> bool:
        key = self._make_user_key(user_id)
        raw = await self.redis.get(key)
        if raw is None:
            return False
        if isinstance(raw, bytes):
            raw = raw.decode()
        # A malformed watermark raises, and the caller's error posture decides:
        # the request path fails open, generator validation fails closed. Both
        # are preferable to silently guessing at a corrupt value here.
        return issued_at <= int(raw)

    async def cleanup_expired(self) -> int:
        return 0  # Redis handles expiration automatically
