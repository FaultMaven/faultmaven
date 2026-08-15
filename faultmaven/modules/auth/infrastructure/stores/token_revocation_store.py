"""Token Revocation Store Implementation.

The single revocation store for the whole deployment (issue #767): every
revoke path (OAuth /revoke, refresh rotation in both auth modes, logout,
admin per-user revocation) writes here, and the request-path check
(``AuthService._is_revoked``) reads here. Redis expiry handles cleanup.

Two granularities (see ``ITokenRevocationStore``): individual tokens by JTI,
and whole users by revocation watermark (#769). Both live in this one store
with TTLs matching token expiration.

The key prefix comes from ``settings.security.token_revocation_prefix`` so
writers and the reader can never disagree on the namespace. Per-token entries
live under a ``jti:`` sub-namespace and per-user watermarks under ``user:``;
see ``ITokenRevocationStore`` for why they must not overlap.

Deploy note: moving per-token entries under ``jti:`` orphans any revocation
keys written by a previous build (they sat directly under the prefix), so
tokens revoked before the upgrade are no longer seen as revoked. Acceptable
pre-production; flush the ``{prefix}*`` keyspace on upgrade if any revoked
refresh token must stay dead across it.
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
        # Both namespaces derive from the same prefix, so they can never be
        # pointed at different Redis instances or drift apart. They sit under
        # DISTINCT literal segments ("jti:" vs "user:") so no jti value can
        # ever produce a per-user key. That separation does not depend on how
        # trustworthy the jti is: it arrives inside a submitted token, on an
        # endpoint (POST /auth/oauth/revoke) that is unauthenticated by RFC
        # 7009 design. A jti of "user:<victim>" must not be able to overwrite
        # that victim's watermark, whatever else changes upstream.
        self._jti_key_prefix = f"{key_prefix}jti:"
        self._user_key_prefix = f"{key_prefix}user:"

    def _make_key(self, jti: str) -> str:
        return f"{self._jti_key_prefix}{jti}"

    def _make_user_key(self, user_id: str) -> str:
        return f"{self._user_key_prefix}{user_id}"

    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        key = self._make_key(jti)
        await self.redis.setex(key, ttl, "revoked")

    async def is_revoked(self, jti: str) -> bool:
        key = self._make_key(jti)
        return await self.redis.exists(key) > 0

    async def revoke_user_tokens_before(
        self, user_id: str, revoked_at: float, ttl: int
    ) -> None:
        key = self._make_user_key(user_id)
        # Stored at full precision, compared at whole seconds. `is_user_revoked`
        # floors it back, so the revocation rule is bit-for-bit what it was when
        # this held an int — the fraction exists only for
        # `clear_user_revocation_if_before`, which has to order this instant
        # against a caller's capture and cannot do that at second granularity.
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
        #
        # `float` then `int`, so a watermark written by a pre-fraction build
        # ("1700000000") reads identically to one written by this one.
        return issued_at <= int(float(raw))

    #: Delete the watermark only if it is strictly older than ARGV[1].
    #:
    #: Server-side so the read and the delete are one operation. Read-then-
    #: delete in Python would let a revocation landing between the two be
    #: deleted on the strength of having inspected the *previous* watermark —
    #: which is the straddle (#831) this comparison exists to respect, arrived
    #: at by a different route.
    _CLEAR_IF_BEFORE = """
    local raw = redis.call('GET', KEYS[1])
    if raw and tonumber(raw) and tonumber(raw) < tonumber(ARGV[1]) then
        redis.call('DEL', KEYS[1])
        return 1
    end
    return 0
    """

    async def clear_user_revocation_if_before(
        self, user_id: str, instant: float
    ) -> bool:
        key = self._make_user_key(user_id)
        cleared = await self.redis.eval(self._CLEAR_IF_BEFORE, 1, key, repr(instant))
        return bool(cleared)

    async def cleanup_expired(self) -> int:
        return 0  # Redis handles expiration automatically
