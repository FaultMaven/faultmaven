"""
Redis-backed rate limiting implementation

Provides sliding window rate limiting with multiple bucket types and
Redis-backed storage (real or FakeRedis).
"""

import logging
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from ...models.protection import (
    LimitType,
    RateLimitConfig,
    RateLimitError,
    RateLimitResult,
    RateLimitState,
)

# Consecutive failed checks against the current client before the limiter
# declares it dead and asks to be re-initialized.
#
# Three, not one: a single timeout is the shape of a transient blip (one slow
# round trip, one failover hiccup) and demoting on it would re-enter the ladder
# constantly on a healthy but busy deployment. Three, not thirty: every limited
# request performs at least one check, so on any pod carrying traffic three
# consecutive failures is sub-second — a genuinely dead client cannot survive
# long. The count is *consecutive* and resets on any success, so an intermittent
# one-in-N error never accumulates into a demotion.
#
# What the threshold does *not* bound is how much traffic goes unlimited before
# the ladder is re-entered. That is a duration, not a request count: three
# failing checks plus however long the re-entry attempt takes — against a dead
# pool, the ping running to socket_connect_timeout/socket_timeout. Concurrent
# arrivals during the attempt wait for it rather than each concluding "no
# limiter" (RateLimitMiddleware._initialize serialises attempts), which is what
# keeps that duration from meaning "every request in it passes free". It is a
# bounded window either way, and bounded is the whole improvement over the
# previous behaviour, which was unlimited traffic for the pod's entire life.
CHECK_FAILURE_DEMOTION_THRESHOLD = 3

# Floor on how often a run of failing checks may log at ERROR. Without it the
# catch-all logged once per check — up to four lines per request, indefinitely.
CHECK_FAILURE_LOG_INTERVAL_SECONDS = 30.0

# The sliding-window check, as one atomic script: prune, count, refuse without
# inserting, otherwise insert and refresh the TTL. It generates neither time nor
# randomness — the caller passes both in, so the script stays deterministic.
_WINDOW_SCRIPT = """
local key = KEYS[1]
local window_start = ARGV[1]
local score = ARGV[2]
local member = ARGV[3]
local limit = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

-- Remove entries that have fallen out of the window
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

-- Count the requests still inside it
local current_count = redis.call('ZCARD', key)

-- Check if limit exceeded. Nothing is inserted on this path: a refused
-- request must neither consume quota nor extend the window, or a client
-- that keeps hammering a limit would hold its own quota shut.
if current_count >= limit then
    return {current_count, limit, 0}  -- blocked
end

-- Add current request
redis.call('ZADD', key, score, member)
redis.call('EXPIRE', key, ttl)

return {current_count + 1, limit, 1}  -- allowed
"""


def _format_window_start(now: float, window: int) -> str:
    """The window's lower edge, formatted for Redis.

    One helper for both the enforcement and the status path so the two cannot
    drift apart on where the window begins. Formatted to a fixed number of
    decimals rather than handed over as a Lua number: Redis parses the string as
    a double, so sub-second precision cannot be lost to Lua's default number
    formatting.

    The bound itself is *inclusive* on the enforcement path — the prune removes
    entries scored at or below it — which makes the window the half-open interval
    (now − window, now]. The status path counts from the exclusive form of the
    same bound so it sees exactly the entries the prune would have left.
    """
    return f"{now - window:.6f}"


class RedisRateLimiter:
    """
    Redis-backed sliding window rate limiter

    Features:
    - Multiple limit types (global, per-session, per-endpoint)
    - Sliding window algorithm for smooth rate limiting
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
        # The window script, registered against ``_redis`` once per adoption.
        # redis-py's Script object caches the SHA and calls EVALSHA, reloading
        # the body only on NOSCRIPT, so the script text is not re-sent on every
        # request the way ``eval`` re-sent it.
        self._window_script = None
        # Whether this limiter opened ``_redis`` itself and may therefore close
        # it. An adopted client belongs to the composition root.
        self._owns_client = False
        # Whether ``_redis`` is the per-replica stand-in rather than the
        # deployment's Redis. Limits are still enforced, just per replica, so
        # this is a *usable* client — but a re-attemptable one: the caller is
        # expected to keep retrying so the limiter can be promoted back.
        self._degraded = False
        # Whether a real (non-stand-in) client has ever been held. Distinguishes
        # "standalone chose FakeRedis by design" (terminal) from "the real Redis
        # died and the factory handed back the stand-in" (a degrade to undo).
        self._had_real_client = False

        # Liveness of the *current* client, tracked on the check path. A client
        # can die long after initialization — the ordinary production outage is
        # a Redis restart or failover mid-life, not a failure at boot.
        self._consecutive_check_failures = 0
        # Bumped each time a run of failures crosses the demotion threshold.
        # The middleware compares generations, so one death produces exactly one
        # re-entry into the ladder rather than one per subsequent request.
        self._demotion_generation = 0
        # Whether the *current* client has already been declared dead. This, not
        # an exact-equality test on the failure count, is what keeps one death to
        # one generation bump — and it is cleared by ``_adopt``, so a later
        # client that dies is demoted too.
        self._client_declared_dead = False
        self._last_check_failure_log_at: Optional[float] = None
        # Bumped on every adoption, and stamped onto each check as it is issued.
        # A check outlives the client it was issued against: when a pool dies
        # under traffic the commands already on the wire block until
        # ``socket_timeout`` (10s), while commands issued afterwards fail fast —
        # so the fast failures demote and re-enter the ladder, and *then* the
        # slow ones land. Counted blindly they crossed the threshold a second
        # time and declared the healthy replacement dead, which dropped the
        # middleware's ``_initialized`` inside a freshly-armed cooldown and
        # served unlimited traffic for the rest of it. Comparing epochs is what
        # makes a failure attributable to one specific client.
        self._adoption_epoch = 0

        # Rate limit configurations
        self._configs: Dict[str, RateLimitConfig] = {}

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

    @property
    def demotion_generation(self) -> int:
        """Counter bumped each time the current client is declared dead.

        The middleware holds the generation it last acted on; a change means
        "the client you are using stopped answering — re-enter the ladder".
        Comparing generations rather than reading a boolean keeps one death to
        one re-entry, however many requests observe it.
        """
        return self._demotion_generation

    def _is_current(self, epoch: int) -> bool:
        """Whether a check stamped with ``epoch`` was issued against this client.

        Liveness is a property of one client, so only checks issued against the
        client currently installed may move its failure run — in either
        direction. A stale *failure* must not condemn the healthy client that
        replaced the dead one; a stale *success* must not clear a genuine
        failure run belonging to the new one.

        This cannot hide a genuine failure of the current client:
        ``check_rate_limit`` snapshots the client and its epoch together with no
        await in between and issues the command against that snapshot, so a
        stamp that still matches ``_adoption_epoch`` names the installed client.
        """
        return epoch == self._adoption_epoch

    def _record_check_success(self, epoch: int) -> None:
        """A working check clears the failure run — its own client's, only."""
        if not self._is_current(epoch):
            return
        if self._consecutive_check_failures:
            self._consecutive_check_failures = 0
            self._last_check_failure_log_at = None

    def _record_check_failure(self, error: Exception, epoch: int) -> None:
        """Count a failed check, declaring the client dead past the threshold."""
        if not self._is_current(epoch):
            # Issued against a client that has since been replaced. Counting it
            # would demote its successor for a predecessor's death.
            self.logger.debug(
                f"Ignoring a rate limit check failure from a replaced client: {error}"
            )
            return

        self._consecutive_check_failures += 1

        now = time.monotonic()
        crossed = (
            self._consecutive_check_failures >= CHECK_FAILURE_DEMOTION_THRESHOLD
            and not self._client_declared_dead
        )
        due = (
            self._last_check_failure_log_at is None
            or now - self._last_check_failure_log_at
            >= CHECK_FAILURE_LOG_INTERVAL_SECONDS
        )
        if crossed or due:
            self._last_check_failure_log_at = now
            self.logger.error(
                "Rate limit check failed (%s consecutive): %s",
                self._consecutive_check_failures,
                error,
            )
        else:
            self.logger.debug(f"Rate limit check failed: {error}")

        if crossed:
            self._client_declared_dead = True
            self._demotion_generation += 1
            self.logger.error(
                "Rate limiter's Redis client has failed %s consecutive checks; "
                "marking it dead so the degrade ladder is re-entered",
                CHECK_FAILURE_DEMOTION_THRESHOLD,
            )

    async def _client_answers(self, client) -> bool:
        """Whether a client responds to a ping.

        Used before *re-adopting* a shared client. On a mid-life outage the
        client in ``app.state`` is precisely the one that just stopped
        answering, so adopting it again would re-enter the same dead state and
        never reach the stand-in.
        """
        try:
            await client.ping()
            return True
        except Exception as e:
            self.logger.warning(
                f"Shared Redis client did not answer ({e}); not re-adopting it"
            )
            return False

    async def initialize(self, client=None, verify_client: bool = False) -> None:
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

        ``verify_client`` pings the offered ``client`` before adopting it. The
        caller sets it on every re-entry into the ladder, because a re-entry
        means the previous client stopped answering — and on a mid-life Redis
        outage the client sitting in ``app.state`` *is* that client. Adopting it
        unchecked would re-enter the dead state on every retry and never reach
        the stand-in. It is left off for the very first initialization, where the
        composition root has already pinged and a second ping is pure cost.

        Raises:
            Exception: when no usable client could be established.
        """
        from faultmaven.infrastructure.redis_client import (
            RedisUnavailableError,
            get_async_redis_client,
            get_fakeredis_client,
            is_fakeredis,
        )

        if client is not None and (
            not verify_client or await self._client_answers(client)
        ):
            self._adopt(client, owns=False, degraded=False)
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
            # Process-wide singleton, shared with every other subsystem.
            self._adopt(get_fakeredis_client(), owns=False, degraded=True)
            return
        except Exception as e:
            self.logger.error(f"Failed to initialize Redis rate limiter: {e}")
            raise

        # The FakeRedis stand-in is a process-wide singleton shared with every
        # other subsystem, so it is never this limiter's to close.
        stand_in = is_fakeredis(resolved)
        # A stand-in is only a *degrade* if this limiter has held a real client
        # before. On standalone with no Redis configured the factory returns
        # FakeRedis by design and that rung is terminal; the same return value
        # after a real client died means the factory fell back, and the limiter
        # must keep retrying so it can be promoted when Redis recovers.
        self._adopt(
            resolved,
            owns=not stand_in,
            degraded=stand_in and self._had_real_client,
        )
        if self._degraded:
            self.logger.error(
                "Rate limiter fell back to the in-process FakeRedis — rate "
                "limits are per-replica until Redis is reachable"
            )
        else:
            self.logger.info("Redis rate limiter initialized successfully")

    def _adopt(self, client, *, owns: bool, degraded: bool) -> None:
        """Install a client and clear the liveness state tracked against the old one.

        Every rung goes through here so no rung can forget to re-arm the
        liveness tracking. Leaving ``_client_declared_dead`` set would mean the
        *next* client's death is never declared, and the ladder would be entered
        exactly once in the process's life.

        The window script is registered here for the same reason: it is bound to
        one client, so a rung change has to re-register it.

        The epoch bump is what makes checks still in flight against the outgoing
        client stop counting: it is monotonic rather than an identity test, so
        re-adopting the *same* object after a recovery still opens a new epoch
        and the previous life's stragglers cannot reach into it.
        """
        from faultmaven.infrastructure.redis_client import is_fakeredis

        self._redis = client
        self._window_script = client.register_script(_WINDOW_SCRIPT)
        self._owns_client = owns
        self._degraded = degraded
        self._adoption_epoch += 1
        self._consecutive_check_failures = 0
        self._client_declared_dead = False
        self._last_check_failure_log_at = None
        if not is_fakeredis(client):
            self._had_real_client = True

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

        # Snapshot the window script and the epoch its client was adopted under
        # together — no await in between — and issue the command against the
        # snapshot. The registered script is bound to the client it was
        # registered against, so this is what makes the outcome attributable: a
        # stamp that still matches ``_adoption_epoch`` when the check completes
        # names the client the command actually went to.
        script = self._window_script
        epoch = self._adoption_epoch

        try:
            config = self._configs.get(limit_type.value)
            if not config or not config.enabled:
                return RateLimitResult(
                    allowed=True, limit_type=limit_type, current_count=0, limit=0
                )

            rate_limit_key = f"{self.key_prefix}:{limit_type.value}:{key}"
            result = await self._check_redis_rate_limit(
                script, rate_limit_key, config, limit_type
            )

            duration = time.time() - start_time
            self.logger.debug(
                f"Rate limit check: key={key}, type={limit_type.value}, "
                f"allowed={result.allowed}, count={result.current_count}/"
                f"{result.limit}, duration={duration:.3f}s"
            )

            self._record_check_success(epoch)
            return result

        except Exception as e:
            # A client can die long after initialization. Counting the failures
            # is what lets the middleware notice and re-enter the degrade
            # ladder; without it a mid-life Redis outage left the limiter
            # holding a dead client and passing every request unlimited,
            # forever, with no rung below it ever reached.
            self._record_check_failure(e, epoch)

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
        self, script, key: str, config: RateLimitConfig, limit_type: LimitType
    ) -> RateLimitResult:
        """Check rate limit using Redis sliding window with Lua script.

        ``script`` is passed in rather than read off ``self`` so the command and
        the epoch stamped on its outcome refer to the same client even if an
        adoption lands mid-check — the registered script is bound to the client
        it was registered against.

        The window is a sorted set holding one element per request inside it. Two
        constraints on those elements are load-bearing:

        - the **score** is wall-clock ``time.time()``, not ``time.monotonic()``:
          entries are shared across processes and replicas, so scores have to be
          comparable across hosts;
        - the **member** is unique per request, so ZADD grows the set instead of
          updating an existing member's score.

        Both are computed here and passed to the script as arguments (fm#920; see
        ``docs/architecture/security/rate-limiting-sliding-window.md``).
        """
        current_time = time.time()
        member = uuid.uuid4().hex

        result = await script(
            keys=[key],
            args=[
                _format_window_start(current_time, config.window),
                f"{current_time:.6f}",
                member,
                config.requests,
                config.window + 60,
            ],
        )

        current_count, limit, allowed = result

        if not allowed:
            retry_after = self._calculate_retry_after(config.window)

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

    def _calculate_retry_after(self, base_window: int) -> int:
        """How long a refused client is told to wait."""
        retry_after = base_window

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
            # ZCOUNT rather than prune-then-ZCARD: reporting status must not
            # mutate the window a concurrent check is deciding against, and a
            # read-only path is also safe to serve from a read replica.
            #
            # The lower bound is the *exclusive* form of the same bound
            # enforcement prunes by. The prune removes entries scored at or below
            # it, so the entries it would have left are exactly those scored
            # strictly above it — and the shared helper keeps the two from
            # drifting apart on where the window begins.
            current_time = time.time()
            window_start = "(" + _format_window_start(current_time, config.window)

            current_count = await self._redis.zcount(
                rate_limit_key, window_start, "+inf"
            )

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
        """Reset rate limit for a specific key (admin function).

        The window key is the only key a limit owns. A companion
        ``:violations`` counter used to be deleted alongside it; nothing writes
        one any more, so deleting it would only ever inflate the returned count
        by zero and imply state that does not exist.
        """
        rate_limit_key = f"{self.key_prefix}:{limit_type.value}:{key}"

        try:
            deleted = await self._redis.delete(rate_limit_key)
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
