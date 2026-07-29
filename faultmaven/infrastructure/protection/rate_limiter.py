"""
Redis-backed rate limiting implementation

Provides sliding window rate limiting with multiple bucket types,
progressive penalties, and Redis-backed storage (real or FakeRedis).
"""

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from ...models.protection import (
    LimitType,
    RateLimitConfig,
    RateLimitError,
    RateLimitResult,
    RateLimitState,
)


class RedisRateLimiter:
    """
    Redis-backed sliding window rate limiter

    Features:
    - Multiple limit types (global, per-session, per-endpoint)
    - Sliding window algorithm for smooth rate limiting
    - Progressive penalties for repeated violations
    - Lua scripts for atomic operations
    """

    def __init__(
        self,
        redis_url: Optional[str] = None,
        key_prefix: str = "fm:rl",
        fallback_enabled: bool = True,
    ):
        # ``None`` means "resolve centrally" — the normal case. Only an
        # operator-configured REDIS_URL ever reaches here as a value.
        self.redis_url = redis_url
        self.key_prefix = key_prefix
        self.fallback_enabled = fallback_enabled
        self.logger = logging.getLogger(__name__)

        # Redis connection
        self._redis = None
        # Whether this limiter opened ``_redis`` itself and may therefore close
        # it. An adopted client belongs to the composition root.
        self._owns_client = False
        # Whether ``_redis`` is the per-replica stand-in rather than the
        # deployment's Redis. Limits are still enforced, just per replica, so
        # this is a *usable* client — but a re-attemptable one: the caller is
        # expected to keep retrying so the limiter can be promoted back.
        self._degraded = False

        # Rate limit configurations
        self._configs: Dict[str, RateLimitConfig] = {}

        # Penalty tracking
        self._penalty_multipliers = {
            "first_violation": 2.0,
            "second_violation": 4.0,
            "third_violation": 8.0,
            "persistent_violation": 16.0,
        }

    @property
    def is_degraded(self) -> bool:
        """Whether the limiter is running on the per-replica stand-in.

        ``True`` means limits *are* being enforced, but only within this
        process, so the caller should keep re-attempting initialization to be
        promoted back to the deployment's Redis. ``False`` after a successful
        ``initialize`` means the resolved client is the configured one and
        there is nothing left to retry.
        """
        return self._degraded

    async def initialize(self, client=None) -> None:
        """Adopt the application's Redis client, or build one as a fallback.

        ``client`` is the composition root's boot-validated client, resolved
        from ``app.state`` by the middleware. Adopting it is the preferred rung:
        it is already connected and already proven to answer, so there is no
        second connection pool, no second credential resolution and no
        redundant ping. Only when no shared client exists does this fall back to
        the central factory.

        **Returning normally means ``self._redis`` is usable.** The rungs are:

        1. the shared, boot-validated client (terminal — ``is_degraded`` False);
        2. a client built by the central factory (terminal);
        3. the in-process per-replica stand-in (``is_degraded`` True — limits
           are still enforced, and the caller keeps retrying for promotion).

        Anything else **raises**, and raises regardless of ``fallback_enabled``.
        ``fallback_enabled`` governs whether the *degraded* rung 3 client is an
        acceptable substitute; it is not a licence to report success with no
        client at all. It used to be exactly that: with the default
        ``fallback_enabled=True`` a connection failure returned normally with
        ``self._redis`` still ``None``, the middleware marked itself
        initialized, and every later check hit ``None.eval(...)`` → caught →
        fail-open. Rate limiting was off for the pod's whole lifetime.

        A failed attempt leaves any client from a previous attempt in place, so
        a failed *promotion* from rung 3 keeps enforcing against the stand-in
        rather than dropping to no limiting at all.

        Raises:
            Exception: when no usable client could be established.
        """
        from faultmaven.infrastructure.redis_client import (
            RedisUnavailableError,
            get_async_redis_client,
            get_fakeredis_client,
            is_fakeredis,
        )

        if client is not None:
            self._redis = client
            self._owns_client = False
            self._degraded = False
            self.logger.info(
                "Redis rate limiter using the shared application Redis client"
            )
            return

        try:
            resolved = await get_async_redis_client(redis_url=self.redis_url)
        except RedisUnavailableError as e:
            # Cloud refuses to substitute an in-process FakeRedis for the
            # deployment-wide Redis, and fails the boot. This subsystem is
            # initialized lazily on the first request, long after the boot gate
            # could have acted, so the refusal cannot fail the boot from here —
            # and letting it propagate leaves self._redis None, which means no
            # rate limiting at all, strictly worse than the per-replica limiting
            # FakeRedis gives. So degrade loudly instead, and stay re-attemptable.
            if not self.fallback_enabled:
                # The deployment asked to fail closed rather than approximate.
                self.logger.error(
                    f"Rate limiter Redis unavailable ({e}); the per-replica "
                    "stand-in is disabled by policy, so no client is available"
                )
                raise
            self.logger.error(
                f"Rate limiter Redis unavailable ({e}); falling back to in-process "
                "FakeRedis — rate limits are per-replica until Redis is reachable"
            )
            self._redis = get_fakeredis_client()
            # Process-wide singleton, shared with every other subsystem.
            self._owns_client = False
            self._degraded = True
            return
        except Exception as e:
            self.logger.error(f"Failed to initialize Redis rate limiter: {e}")
            raise

        self._redis = resolved
        # The FakeRedis stand-in is a process-wide singleton shared with
        # every other subsystem, so it is never this limiter's to close.
        self._owns_client = not is_fakeredis(resolved)
        # Whatever the factory returns here is the *configured* backend
        # (FakeRedis is standalone's real answer, not a degrade), so this rung
        # is terminal.
        self._degraded = False
        self.logger.info("Redis rate limiter initialized successfully")

    async def close(self) -> None:
        """Close the Redis connection — only one this limiter opened itself.

        The shared client belongs to the composition root and backs sessions,
        token revocation, deduplication and idempotency too. Closing it on
        middleware teardown would take all of them down with it.
        """
        if self._redis is not None and self._owns_client:
            try:
                await self._redis.close()
            except Exception:
                pass
            self._owns_client = False

    def configure_limits(self, limits: Dict[str, RateLimitConfig]) -> None:
        """Configure rate limits."""
        self._configs = limits.copy()
        self.logger.info(f"Configured {len(limits)} rate limit types")

    async def check_rate_limit(
        self, key: str, limit_type: LimitType, identifier: str = ""
    ) -> RateLimitResult:
        """Check if request is within rate limits."""
        start_time = time.time()

        try:
            config = self._configs.get(limit_type.value)
            if not config or not config.enabled:
                return RateLimitResult(
                    allowed=True, limit_type=limit_type, current_count=0, limit=0
                )

            rate_limit_key = f"{self.key_prefix}:{limit_type.value}:{key}"
            result = await self._check_redis_rate_limit(
                rate_limit_key, config, limit_type
            )

            duration = time.time() - start_time
            self.logger.debug(
                f"Rate limit check: key={key}, type={limit_type.value}, "
                f"allowed={result.allowed}, count={result.current_count}/"
                f"{result.limit}, duration={duration:.3f}s"
            )

            return result

        except Exception as e:
            self.logger.error(f"Rate limit check failed: {e}")

            if self.fallback_enabled:
                return RateLimitResult(
                    allowed=True, limit_type=limit_type, current_count=0, limit=0
                )
            else:
                raise RateLimitError(
                    retry_after=60,
                    limit_type=limit_type.value,
                    current_count=0,
                    limit=0,
                )

    async def _check_redis_rate_limit(
        self, key: str, config: RateLimitConfig, limit_type: LimitType
    ) -> RateLimitResult:
        """Check rate limit using Redis sliding window with Lua script."""
        current_time = int(time.time())
        window_start = current_time - config.window

        lua_script = """
        local key = KEYS[1]
        local window_start = tonumber(ARGV[1])
        local current_time = tonumber(ARGV[2])
        local limit = tonumber(ARGV[3])
        local ttl = tonumber(ARGV[4])

        -- Remove expired entries
        redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

        -- Count current entries
        local current_count = redis.call('ZCARD', key)

        -- Check if limit exceeded
        if current_count >= limit then
            return {current_count, limit, 0}  -- blocked
        end

        -- Add current request
        redis.call('ZADD', key, current_time, current_time)
        redis.call('EXPIRE', key, ttl)

        return {current_count + 1, limit, 1}  -- allowed
        """

        result = await self._redis.eval(
            lua_script,
            1,
            key,
            window_start,
            current_time,
            config.requests,
            config.window + 60,
        )

        current_count, limit, allowed = result

        if not allowed:
            retry_after = self._calculate_retry_after(key, config.window)

            return RateLimitResult(
                allowed=False,
                limit_type=limit_type,
                current_count=current_count,
                limit=limit,
                retry_after=retry_after,
                reset_time=datetime.fromtimestamp(
                    current_time + config.window, tz=timezone.utc
                ),
            )

        return RateLimitResult(
            allowed=True,
            limit_type=limit_type,
            current_count=current_count,
            limit=limit,
            reset_time=datetime.fromtimestamp(
                current_time + config.window, tz=timezone.utc
            ),
        )

    def _calculate_retry_after(self, key: str, base_window: int) -> int:
        """Calculate retry after time with penalties and jitter."""
        violation_key = f"{key}:violations"
        base_retry = base_window

        try:
            violation_count = asyncio.create_task(self._redis.incr(violation_key))
            asyncio.create_task(self._redis.expire(violation_key, base_window * 4))
            violation_count = violation_count.result() if violation_count.done() else 1
        except Exception:
            violation_count = 1

        if violation_count <= 1:
            multiplier = 1.0
        elif violation_count == 2:
            multiplier = self._penalty_multipliers["first_violation"]
        elif violation_count == 3:
            multiplier = self._penalty_multipliers["second_violation"]
        elif violation_count == 4:
            multiplier = self._penalty_multipliers["third_violation"]
        else:
            multiplier = self._penalty_multipliers["persistent_violation"]

        retry_after = int(base_retry * multiplier)

        # Add jitter to prevent thundering herd
        jitter = random.uniform(0, retry_after * 0.1)
        retry_after = int(retry_after + jitter)

        return min(retry_after, 300)

    async def get_rate_limit_status(
        self, key: str, limit_type: LimitType
    ) -> Optional[RateLimitState]:
        """Get current rate limit status without incrementing."""
        config = self._configs.get(limit_type.value)
        if not config:
            return None

        rate_limit_key = f"{self.key_prefix}:{limit_type.value}:{key}"

        try:
            current_time = int(time.time())
            window_start = current_time - config.window

            await self._redis.zremrangebyscore(rate_limit_key, "-inf", window_start)
            current_count = await self._redis.zcard(rate_limit_key)

            return RateLimitState(
                key=key,
                limit_type=limit_type,
                current_count=current_count,
                limit=config.requests,
                window=config.window,
                reset_time=datetime.fromtimestamp(
                    current_time + config.window, tz=timezone.utc
                ),
            )
        except Exception as e:
            self.logger.error(f"Failed to get rate limit status: {e}")

        return None

    async def reset_rate_limit(self, key: str, limit_type: LimitType) -> bool:
        """Reset rate limit for a specific key (admin function)."""
        rate_limit_key = f"{self.key_prefix}:{limit_type.value}:{key}"
        violation_key = f"{rate_limit_key}:violations"

        try:
            deleted = await self._redis.delete(rate_limit_key, violation_key)
            self.logger.info(
                f"Reset rate limit for {key}:{limit_type.value} (deleted {deleted} keys)"
            )
            return deleted > 0
        except Exception as e:
            self.logger.error(f"Failed to reset rate limit: {e}")

        return False

    async def health_check(self) -> Dict[str, any]:
        """Perform health check and return status."""
        status = {
            "redis_healthy": self._redis is not None,
            "degraded": self._degraded,
            "fallback_enabled": self.fallback_enabled,
            "configured_limits": len(self._configs),
        }

        try:
            ping_result = await self._redis.ping()
            status["redis_ping"] = ping_result
        except Exception as e:
            status["redis_error"] = str(e)

        return status
