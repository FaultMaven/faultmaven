"""
Redis-backed rate limiting implementation

Provides sliding window rate limiting with multiple bucket types and
Redis-backed storage (real or FakeRedis).
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from ...models.protection import (
    RateLimitConfig,
    RateLimitResult,
    RateLimitSpec,
)
from .window_math import quota_frees_at, retry_after_seconds

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

# Every sliding window a request is subject to, decided as one atomic script:
# prune and count them all, refuse without inserting anywhere, otherwise insert
# into all of them. It generates neither time nor randomness — the caller passes
# both in, so the script stays deterministic.
#
# **All-or-nothing is the correctness property, not an optimisation.** Checked
# one window at a time, each allowed check inserts before the next one has had a
# chance to refuse: a request that global and per-minute admitted but hourly
# turned away had already consumed a unit of global and per-minute quota it was
# never served for. Behind a NAT that is how one throttled client fills the
# shared ``global`` window with entries for requests nobody received. Counting
# every window first and inserting only if all of them admit is what makes a
# refusal free — and it collapses three serial round trips into one.
#
# Two elements come back per window: the count, and the score of the oldest
# entry still inside it. The second is what makes the client-facing signalling
# honest — that entry ages out exactly one window after it arrived, so
# ``oldest + window`` is the instant the *next* unit of quota frees. Without it
# the only answer available was "a whole window", which is the truth only for a
# client refused at the instant its window opened.
#
# The leading element is the 1-based position of the first window that refused,
# or 0 when every window admitted. First, not tightest: the caller passes its
# windows in precedence order, so the limit a client is told about is the same
# one it would have been told about when these were three separate checks.
#
# Multi-key by construction, so it assumes a single Redis keyspace — the windows
# are keyed on different identities (address, session) and would not share a hash
# slot under Redis Cluster. FaultMaven runs standalone Redis with replicas; a
# move to Cluster would need these keys hash-tagged onto one slot.
_WINDOWS_SCRIPT = """
local score = ARGV[1]
local member = ARGV[2]
local n = #KEYS

local counts = {}
local oldest_scores = {}
local blocked = 0

-- Pass one: prune each window, count what survives, and note the oldest
-- survivor. Nothing is written here, so the decision below is taken against a
-- consistent view of every window.
for i = 1, n do
    local key = KEYS[i]
    local base = 2 + (i - 1) * 3
    local window_start = ARGV[base + 1]
    local limit = tonumber(ARGV[base + 2])

    redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)
    local count = redis.call('ZCARD', key)
    counts[i] = count

    -- Read BEFORE any insert. On the admitted path that is deliberate: if the
    -- set was empty this request is the oldest entry, and ``now + window`` —
    -- the caller's fallback for an empty answer — is the same number, so
    -- reading first costs nothing and keeps one code path.
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    if oldest[2] then
        oldest_scores[i] = oldest[2]
    else
        oldest_scores[i] = ''
    end

    if blocked == 0 and count >= limit then
        blocked = i
    end
end

-- Pass two: insert only if EVERY window admitted. A refused request must
-- neither consume quota nor extend a window — in any of them — or a client
-- that keeps hammering one limit would hold its own quota shut everywhere.
if blocked == 0 then
    for i = 1, n do
        local base = 2 + (i - 1) * 3
        local ttl = tonumber(ARGV[base + 3])
        redis.call('ZADD', KEYS[i], score, member)
        redis.call('EXPIRE', KEYS[i], ttl)
        counts[i] = counts[i] + 1
    end
end

local reply = {blocked}
for i = 1, n do
    reply[#reply + 1] = counts[i]
    reply[#reply + 1] = oldest_scores[i]
end
return reply
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


def _parse_oldest_score(raw) -> Optional[float]:
    """The script's fourth element as a float, or ``None`` when the window is empty.

    The script returns an empty string rather than nil for "no entries", because
    a nil inside a Lua table truncates the array at that point and the caller
    would see a three-element reply it could not distinguish from the old
    contract. Anything that does not parse is treated as absent, which falls the
    caller back to ``now + window`` — the conservative answer, never a shorter
    wait than the truth.
    """
    if raw is None or raw == "" or raw == b"":
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


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

        # Background closes of replaced clients, held only so the event loop's
        # weak reference to a running task is not the only one — an unreferenced
        # task can be collected mid-close, and the pool it was dropping leaks.
        self._teardown_tasks: set = set()

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
        ``check_rate_limits`` snapshots the client and its epoch together with no
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
            await self._adopt(client, owns=False, degraded=False)
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
            await self._adopt(get_fakeredis_client(), owns=False, degraded=True)
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
        await self._adopt(
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

    async def _adopt(self, client, *, owns: bool, degraded: bool) -> None:
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

        The outgoing client is closed **only when this limiter owned it**, and
        the invariant behind that word is narrow: owned means a real connection
        pool this limiter had the factory build for it. Never the composition
        root's shared client (sessions, revocation, deduplication and
        idempotency all hold it) and never the process-wide FakeRedis stand-in.
        ``_owns_client`` already encodes both exclusions; the ``is_fakeredis``
        test is belt and braces, because closing the stand-in would break every
        other subsystem in the process and the cost of the extra check is one
        function call per adoption.

        Without the close, a flapping Redis leaked one pool (``max_connections``
        20) per demotion cycle — the ladder is re-entered on every death, and
        nothing else ever dropped the previous pool.

        Everything from the ownership capture to the last assignment runs with no
        ``await`` in it, and the close is dispatched rather than awaited. The two
        properties are related: it is the *absence of a yield point*, not the
        relative position of the dispatch, that makes the install atomic.

        - **No await between capture and install.** Any ``await`` in that span is
          a point at which a concurrent ``check_rate_limits`` can snapshot
          ``_window_script`` — and when the close was awaited *first*, that
          snapshot was the script bound to the client being torn down, so the
          check went to a pool that was closing or already closed. Awaiting the
          close after the install fixed that instance; dispatching it removes the
          yield point altogether, so no observer can see a half-installed
          limiter regardless of where the dispatch sits.
        - **Close off the adoption path.** Closing the outgoing pool is
          best-effort cleanup of a connection that is usually already dead;
          installing the replacement is what ends the outage. Awaiting the close
          put an unbounded teardown *on* that path — and not only on the adopting
          request: ``RateLimitMiddleware._initialize`` serialises re-entry behind
          one lock, so every request queued on it waited for a socket that may
          never answer. Dispatching it means adoption returns as soon as the
          replacement is installed, whatever the dead pool does afterwards.

        The task reference is held in ``_teardown_tasks`` until it completes.
        ``asyncio`` keeps only a weak reference to a running task, so a
        fire-and-forget task with no strong reference anywhere can be garbage
        collected mid-flight and the close silently never happens — which would
        reinstate the pool leak this close exists to fix.

        The ownership test reads the values ``self`` held *before* the install,
        captured first: ``_owns_client`` is about to be overwritten with the
        incoming client's ownership, and testing the post-install value would
        decide the outgoing client's fate from its successor's provenance.
        """
        from faultmaven.infrastructure.redis_client import is_fakeredis

        outgoing = self._redis
        owned_outgoing = self._owns_client

        self._redis = client
        self._window_script = client.register_script(_WINDOWS_SCRIPT)
        self._owns_client = owns
        self._degraded = degraded
        self._adoption_epoch += 1
        self._consecutive_check_failures = 0
        self._client_declared_dead = False
        self._last_check_failure_log_at = None
        if not is_fakeredis(client):
            self._had_real_client = True

        if (
            owned_outgoing
            and outgoing is not None
            and outgoing is not client
            and not is_fakeredis(outgoing)
        ):
            self._dispatch_teardown(outgoing)

    def _dispatch_teardown(self, outgoing) -> None:
        """Close a replaced client in the background, holding a reference to it.

        The reference is the whole point: ``asyncio`` holds tasks weakly, so a
        task nothing else names may be collected before it finishes and the close
        would silently not happen. Discarded on completion so the set does not
        grow with the process's uptime.
        """
        task = asyncio.ensure_future(self._close_outgoing(outgoing))
        self._teardown_tasks.add(task)
        task.add_done_callback(self._teardown_tasks.discard)

    async def _close_outgoing(self, outgoing) -> None:
        """Best-effort close of a pool that is usually already dead."""
        try:
            await outgoing.close()
        except Exception as e:
            # A pool that is already dead is exactly the case this runs in, so a
            # failure to close it is expected and not worth surfacing — nothing
            # is waiting on the outcome, and the replacement is already serving.
            self.logger.debug(f"Closing the replaced Redis client failed: {e}")

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

    async def check_rate_limits(
        self, specs: Sequence[RateLimitSpec]
    ) -> List[RateLimitResult]:
        """Decide every window a request is subject to, all or nothing.

        Returns one result per spec, in spec order. Either every window admitted
        the request and every one of them counted it, or one refused and **none**
        of them counted it — there is no partial outcome, which is the whole
        reason the windows are passed together rather than checked in sequence.

        At most one result carries ``allowed=False``: the first window in
        precedence order that refused. The rest report what they measured
        without having been consumed, so a caller can still advertise them.

        A spec whose limit type has no configuration, or a disabled one, is not
        a window at all. It never reaches Redis and comes back as "allowed,
        limit 0" — the same answer the single-window path gave, and the one
        ``RateLimitMiddleware`` reads as "nothing to advertise here".
        """
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
            # Partition first: unconfigured and disabled limits are answered
            # locally, so they neither occupy a KEYS slot nor shift the
            # positions the script reports blockage by.
            by_index: Dict[int, RateLimitResult] = {}
            enforced: List[Tuple[int, RateLimitSpec, RateLimitConfig]] = []
            for index, spec in enumerate(specs):
                config = self._configs.get(spec.limit_type.value)
                if not config or not config.enabled:
                    by_index[index] = RateLimitResult(
                        allowed=True,
                        limit_type=spec.limit_type,
                        current_count=0,
                        limit=0,
                    )
                    continue
                enforced.append((index, spec, config))

            if enforced:
                by_index.update(await self._check_redis_rate_limits(script, enforced))

                duration = time.time() - start_time
                refused = next(
                    (r for r in by_index.values() if not r.allowed),
                    None,
                )
                self.logger.debug(
                    "Rate limit check: windows=%s, allowed=%s%s, duration=%.3fs",
                    len(enforced),
                    refused is None,
                    "" if refused is None else f", refused_by={refused.limit_type}",
                    duration,
                )

            self._record_check_success(epoch)
            return [by_index[index] for index in range(len(specs))]

        except asyncio.CancelledError as e:
            # ``CancelledError`` has been a ``BaseException`` since 3.8, so the
            # generic handler below never saw it. The shape that matters: a
            # check stalled against a dead pool, cancelled by an outer
            # per-request timeout, left the failure run untouched — so a pool
            # that only ever stalls could never reach the demotion threshold and
            # the ladder was never re-entered. Counting it is what makes a
            # stalling death indistinguishable from a raising one.
            #
            # This must sit ABOVE the generic ``except Exception``: cancellation
            # is not an Exception, and moving it below would delete it.
            #
            # Accepted trade-off: a client that aborts its own request also
            # cancels the check in flight and contributes a spurious failure.
            # The counter is consecutive and resets on any success, so it takes
            # three back-to-back aborts with no successful check in between to
            # matter, and the cost of being wrong is one ping and one epoch bump
            # against a healthy client that immediately re-adopts.
            self._record_check_failure(e, epoch)
            raise

        except Exception as e:
            # A client can die long after initialization. Counting the failures
            # is what lets the middleware notice and re-enter the degrade
            # ladder; without it a mid-life Redis outage left the limiter
            # holding a dead client and passing every request unlimited,
            # forever, with no rung below it ever reached.
            self._record_check_failure(e, epoch)

            if self.fallback_enabled:
                # Fail open across every window at once. Nothing was inserted —
                # the script is all-or-nothing and it did not complete — so this
                # is "unmeasured", not "measured as empty": limit 0 is what the
                # middleware reads as "no quota to advertise".
                return [
                    RateLimitResult(
                        allowed=True,
                        limit_type=spec.limit_type,
                        current_count=0,
                        limit=0,
                    )
                    for spec in specs
                ]

            # Fail closed means "refuse the request", not "claim the client
            # exceeded a limit". This used to manufacture a
            # ``RateLimitError(retry_after=60, current_count=0, limit=0)``,
            # which the middleware rendered as a 429 reading "0/0 requests",
            # counted in ``requests_blocked``, and WARN-logged with the caller's
            # address as a rate-limit violator — a fabricated accusation, up to
            # three times per client death before the demotion threshold even
            # engages. Re-raising the original error sends it to the
            # middleware's dispatch catch-all instead: the 503 rung, the
            # ``errors`` counter, and no violator log. The cause also survives,
            # so the log names the Redis failure rather than a limit nobody hit.
            raise

    async def _check_redis_rate_limits(
        self, script, enforced
    ) -> Dict[int, RateLimitResult]:
        """Run every enforced window through the atomic script, in one round trip.

        ``script`` is passed in rather than read off ``self`` so the command and
        the epoch stamped on its outcome refer to the same client even if an
        adoption lands mid-check — the registered script is bound to the client
        it was registered against.

        ``enforced`` is the ``(spec_index, spec, config)`` triples that actually
        have a configured, enabled window, in precedence order. The spec index
        travels with each one so the caller can put the results back in the
        order it asked for after the unconfigured ones were filtered out.

        Each window is a sorted set holding one element per request inside it.
        Two constraints on those elements are load-bearing:

        - the **score** is wall-clock ``time.time()``, not ``time.monotonic()``:
          entries are shared across processes and replicas, so scores have to be
          comparable across hosts;
        - the **member** is unique per request, so ZADD grows the set instead of
          updating an existing member's score.

        Both are computed here and passed to the script as arguments (fm#920; see
        ``docs/architecture/security/rate-limiting-sliding-window.md``). One
        member is generated for the whole request and written into every window,
        which is what lets the windows be reasoned about as one decision: the
        entry that appears in each of them names the same request.

        What the caller is *told* is derived from each window's oldest entry.
        That entry ages out one window after it arrived, so ``oldest + window``
        is the instant the next unit of quota frees — the only honest answer to
        "when may I retry". Every window derives it the same way, so
        ``reset_time`` means the same thing whether the request was admitted or
        refused, and whichever window did the refusing.
        """
        current_time = time.time()
        member = uuid.uuid4().hex

        keys = []
        args: list = [f"{current_time:.6f}", member]
        for _, spec, config in enforced:
            keys.append(f"{self.key_prefix}:{spec.limit_type.value}:{spec.key}")
            args.extend(
                [
                    _format_window_start(current_time, config.window),
                    config.requests,
                    config.window + 60,
                ]
            )

        reply = await script(keys=keys, args=args)

        # ``reply[0]`` is the 1-based position of the first window that refused,
        # 0 if none did; then two elements per window, in the order they were
        # passed.
        blocked_position = int(reply[0])

        results: Dict[int, RateLimitResult] = {}
        for position, (index, spec, config) in enumerate(enforced):
            current_count = int(reply[1 + position * 2])
            oldest_raw = reply[2 + position * 2]

            # One derivation for every number, through the shared helper: a
            # window whose oldest entry is ``None`` held nothing to age out (on
            # the admitted path this request is that entry, so a full window is
            # exactly right; on the refused path it cannot happen with a
            # positive limit), and a score from a host whose clock runs ahead is
            # clamped so the answer can never exceed one window.
            frees_at = quota_frees_at(
                _parse_oldest_score(oldest_raw), config.window, current_time
            )
            reset_time = datetime.fromtimestamp(frees_at, tz=timezone.utc)

            refused = blocked_position == position + 1

            results[index] = RateLimitResult(
                allowed=not refused,
                limit_type=spec.limit_type,
                current_count=current_count,
                limit=config.requests,
                # The same instant ``reset_time`` names, expressed as a wait.
                # Only the window that actually refused carries one: a wait
                # advertised beside an admitted result would invite a client to
                # sleep on a limit it is nowhere near.
                retry_after=(
                    retry_after_seconds(frees_at, current_time) if refused else None
                ),
                reset_time=reset_time,
            )

        return results

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
