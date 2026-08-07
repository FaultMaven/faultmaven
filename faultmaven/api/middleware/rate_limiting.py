"""
Rate limiting middleware

FastAPI middleware for multi-level rate limiting with Redis backend and
graceful degradation.
"""

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ...infrastructure.protection import RedisRateLimiter
from ...models.protection import (
    LimitType,
    ProtectionErrorResponse,
    ProtectionSettings,
    RateLimitError,
    RateLimitResult,
)
from .client_ip import parse_trusted_proxies, resolve_client_ip

# How long to wait before re-attempting rate-limiter initialization.
# Initialization deliberately does not latch — neither on failure nor on a
# degraded rung — so one blip cannot disable (or permanently demote) rate
# limiting for the pod's whole lifetime. This bounds the retry so a persistent
# Redis outage does not attempt a connection on every request, and it doubles as
# the rate at which the "no client" condition is logged.
INIT_RETRY_COOLDOWN_SECONDS = 30.0

# Paths this middleware must never rate limit, 503, or even attempt Redis
# initialization for: the kubelet's liveness/readiness probes and the container
# healthcheck. A probe answered with 503 gets the pod killed, which turns a
# transient Redis blip into a restart loop — so the probes are resolved before
# anything Redis-dependent runs.
#
# Matched exactly, plus the ``/health/`` sub-tree, rather than by a loose
# ``/health`` prefix: real API routes must not escape limiting by starting with
# the same letters.
LIVENESS_PATHS = frozenset({"/health", "/readiness"})
LIVENESS_PATH_PREFIXES = ("/health/",)

# Read-only HTTP methods. Their *cheap* traffic — ordinary SPA navigation — is
# metered in the per-session read buckets rather than the write ones, so a burst
# of case-detail GETs cannot refuse the next POST turn (fm#994).
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Read endpoints that are *not* cheap: each runs a query embedding (BGE-M3,
# behind a process-wide lock) and a vector similarity search per call, which is
# exactly the compute the tight per-session bucket exists to protect. The verb
# is only a proxy for cost, and for these three it is the wrong one — so they
# are metered as writes despite being GETs.
#
# Anchored full-path patterns rather than prefixes: a read endpoint must not be
# able to buy the strict bucket's roominess, or escape it, by sharing a prefix
# with one of these.
#
# This list is the one thing here that rots in the permissive direction — an
# endpoint added later inherits "cheap" by saying nothing. It is guarded by
# reachability rather than by inventory:
# ``tests/unit/api/middleware/test_rate_limit_read_cost_classification.py`` asks,
# of every read route on every mounted router, whether the handler can reach an
# embedder or vector store at all. Only the routes that can need a recorded
# verdict, so adding an ordinary read endpoint costs nothing and adding one that
# touches the vector store fails until someone decides what it costs.
EXPENSIVE_READ_PATTERNS = (
    # Runbook similarity search over the knowledge base.
    re.compile(r"^/api/v1/cases/[^/]+/report-recommendations/?$"),
    re.compile(r"^/api/v1/reports/recommendations/[^/]+/?$"),
    # Semantic snippet lookup — embeds the query to locate the chunk.
    re.compile(r"^/api/v1/knowledge/documents/[^/]+/snippet/?$"),
)


def is_cheap_read(method: str, path: str) -> bool:
    """Whether a request is read-only *and* cheap enough for the read buckets.

    Exposed at module level so the classification the middleware enforces is the
    same one the route-inventory test asserts over — a second copy of this rule
    written in the test could agree with the docstring while disagreeing with
    the code.
    """
    if method.upper() not in READ_ONLY_METHODS:
        return False
    return not any(pattern.match(path) for pattern in EXPENSIVE_READ_PATTERNS)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Multi-level rate limiting middleware

    Features:
    - Global, per-session, and per-endpoint rate limits
    - Redis-backed with in-memory fallback
    - Detailed metrics and logging
    - Security headers in responses
    """

    def __init__(
        self, app, settings: ProtectionSettings, redis_url: Optional[str] = None
    ):
        super().__init__(app)
        self.settings = settings
        self.logger = logging.getLogger(__name__)

        # Parsed once here rather than per request: the list is operator
        # configuration, and an unparseable entry should be reported at
        # startup rather than on every request that arrives.
        self.trusted_proxies = parse_trusted_proxies(settings.trusted_proxies)

        # Initialize rate limiter
        effective_redis_url = redis_url or settings.redis_url
        self.rate_limiter = RedisRateLimiter(
            redis_url=effective_redis_url,
            key_prefix=f"{settings.redis_key_prefix}:rl",
            fallback_enabled=settings.fail_open_on_redis_error,
        )

        # Configure rate limits
        self.rate_limiter.configure_limits(settings.rate_limits)

        # Whether this deployment's configuration carries the read buckets.
        #
        # This has to be decided here rather than left to the limiter, because
        # ``check_rate_limit`` answers "allowed, limit 0" for a limit type it
        # holds no config for. Routing reads to an unconfigured bucket would
        # therefore not meter them at all — a settings object written before the
        # split would silently gain an unlimited read path. Absence narrows
        # instead: reads fall back to the write buckets, which is exactly the
        # behaviour that configuration already described.
        #
        # Presence, not ``enabled``, is the test. An operator who ships
        # ``per_session_read`` with ``enabled=False`` has said "do not meter
        # reads separately"; honouring that is not the same as never having been
        # told about the bucket at all.
        configured = set(settings.rate_limits)
        read_keys = {
            LimitType.PER_SESSION_READ.value,
            LimitType.PER_SESSION_READ_HOURLY.value,
        }
        self._read_limits_configured = read_keys <= configured

        if not self._read_limits_configured and read_keys & configured:
            # Half-configured is the dangerous shape: the per-minute read bucket
            # without its hourly partner is how a read flood gets an hour-long
            # ceiling of "whatever global allows". Refuse the split entirely and
            # say which key is missing.
            self.logger.error(
                "Rate limiting has a partial read-bucket configuration (%s "
                "missing); read requests will be metered against the write "
                "buckets until both %s are configured",
                ", ".join(sorted(read_keys - configured)),
                " and ".join(sorted(read_keys)),
            )
        elif not self._read_limits_configured:
            # Safe, but not silent: this is the state fm#994 was reported from,
            # where ordinary navigation competes with POST turns for one quota.
            self.logger.warning(
                "Rate limiting has no read buckets configured (%s); read "
                "requests are metered against the write buckets, so normal UI "
                "navigation consumes the quota that protects LLM compute",
                " and ".join(sorted(read_keys)),
            )

        # Endpoint-specific configurations
        self.endpoint_configs = {
            "/api/v1/data/upload": {
                "limit_types": [LimitType.PER_SESSION, LimitType.GLOBAL],
                "special_handling": None,
            },
            "/api/v1/sessions/": {
                "limit_types": [LimitType.PER_SESSION, LimitType.GLOBAL],
                "special_handling": None,
            },
        }

        # Metrics tracking
        self.metrics = {
            "requests_checked": 0,
            "requests_blocked": 0,
            "errors": 0,
            "avg_check_duration": 0.0,
        }

        # Whether the limiter holds a usable Redis client.
        self._initialized = False
        # Whether that client is the per-replica stand-in. Initialized *and*
        # degraded still enforces limits, but stays re-attemptable so the pod
        # can be promoted back to the shared Redis.
        self._degraded = False
        # Monotonic timestamp of the last initialization attempt that did not
        # reach the terminal rung (failed, or landed degraded). None once the
        # limiter is on the configured client and there is nothing to retry.
        self._last_attempt_at: Optional[float] = None
        # Monotonic timestamp of the last "no Redis client" log line, so the
        # degrade is reported once per cooldown window rather than per request.
        self._unavailable_logged_at: Optional[float] = None
        # The limiter's demotion counter as last acted on. A change means the
        # client in use stopped answering and the ladder must be re-entered.
        self._handled_demotion_generation = 0
        # Whether initialization has ever succeeded. Gates the ping-before-adopt
        # on re-entry: the first adoption trusts the composition root's own
        # validation, every later one proves the client for itself.
        self._ever_initialized = False
        # Serialises attempts, so requests arriving during one wait for its
        # verdict instead of each concluding "no limiter" independently. See
        # ``_initialize`` for why that matters more than it looks.
        self._init_lock = asyncio.Lock()

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Main middleware dispatch with rate limiting"""

        start_time = time.time()

        try:
            # Resolve "does this request get rate limited at all" BEFORE
            # touching initialization. Initialization talks to Redis, and a
            # Redis blip must not reach requests this middleware has no verdict
            # to give on — least of all the liveness probes, whose 503 gets the
            # pod killed.
            if not self.settings.rate_limiting_enabled:
                return await call_next(request)

            # Check for bypass headers (development/testing) and probe paths
            if self._should_bypass(request):
                self.logger.debug("Rate limiting bypassed")
                return await call_next(request)

            # Initialize the rate limiter if needed (resolves the Redis client
            # from app.state, which is populated by the lifespan composition
            # root). Returns whether a usable client is available.
            if not await self._initialize(request):
                return await self._serve_without_a_limiter(request, call_next)

            # Perform rate limit checks
            await self._check_rate_limits(request)

            # Process request
            response = await call_next(request)

            # Add rate limit headers to response
            await self._add_rate_limit_headers(request, response)

            # Update metrics
            check_duration = time.time() - start_time
            self._update_metrics(check_duration, blocked=False)

            return response

        except RateLimitError as e:
            # Rate limit exceeded
            check_duration = time.time() - start_time
            self._update_metrics(check_duration, blocked=True)

            return self._create_rate_limit_response(e, request)

        except Exception as e:
            # Log the error cleanly without trying to serialize exception objects
            self.logger.error(
                f"Rate limiting error: {type(e).__name__}: {str(e)}",
                exc_info=False,  # Avoid serialization issues
            )
            self.metrics["errors"] += 1

            # Fail open if configured
            if self.settings.fail_open_on_redis_error:
                self.logger.warning("Rate limiting failed, allowing request")
                return await call_next(request)
            else:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "service_unavailable",
                        "message": "Rate limiting service temporarily unavailable",
                    },
                )

    async def _initialize(self, request: Request) -> bool:
        """Ensure the limiter holds a usable Redis client; report whether it does.

        The client is resolved lazily from ``app.state`` for the same reason
        ``DeduplicationMiddleware`` does it: Starlette middleware is constructed
        at import time, before the lifespan startup that creates Redis, so it
        cannot be captured in ``__init__``.

        Nothing latches, in either direction:

        - **Failure** leaves ``_initialized`` false, so a later request retries.
          Latching on failure meant one blip on a pod's first request disabled
          rate limiting for that pod's entire lifetime, with no path back.
        - **A degraded rung** (the per-replica stand-in) counts as initialized —
          once adopted it keeps enforcing limits rather than opening a window of
          unlimited traffic — but stays re-attemptable, so the pod is promoted
          back to the shared Redis instead of running per-replica forever.

        Both are bounded by ``INIT_RETRY_COOLDOWN_SECONDS`` so a persistent
        outage does not open a connection on every request.

        The ladder is also re-entered when the client in use *stops answering*,
        not only when it never answered. The ordinary production Redis outage is
        a restart or failover mid-life, long after a successful adoption; the
        short-circuit above used to make that state permanent, so the limiter
        held a dead client and every check fell through to fail-open forever,
        with no rung below it ever reached. The limiter now declares its client
        dead after a run of failed checks and bumps a generation counter, which
        is handled here — once per death, not once per request.

        Being inside the cooldown is *not* itself a verdict: this method never
        raises and never synthesizes a response. Whether "no limiter" means pass
        or refuse is a request-path decision, taken in ``dispatch`` where a
        limit verdict is actually needed — see ``_serve_without_a_limiter``.

        **Attempts are serialised, and that is what bounds the re-entry window.**
        The window is not "one request wide": it is as wide as the attempt, which
        against a dead pool means the ping running to ``socket_connect_timeout``
        (5s) or ``socket_timeout`` (10s). Every request arriving in that time used
        to see ``_initialized`` false, fall to ``_serve_without_a_limiter`` and
        pass unlimited — measured at 30 of 30 in a concurrent burst. They now wait
        on the in-flight attempt and are checked against whatever it adopts, so
        the cost is that latency once rather than a hole in the limiter. The wait
        is bounded because the factory sets both socket timeouts; the fast path
        above returns before the lock, so a healthy limiter never touches it.
        """
        generation = self.rate_limiter.demotion_generation
        if generation != self._handled_demotion_generation:
            self._handled_demotion_generation = generation
            # Nothing usable until proven otherwise: this stops dispatch running
            # checks against the dead client while the ladder is re-entered.
            self._initialized = False
            self.logger.error(
                "Rate limiter's Redis client stopped answering; re-entering the "
                "degrade ladder"
            )

        if self._initialized and not self._degraded:
            return True

        async with self._init_lock:
            # Re-check under the lock: while this request waited, the attempt it
            # was waiting for may have produced a usable client.
            if self._initialized and not self._degraded:
                return True

            now = time.monotonic()
            if (
                self._last_attempt_at is not None
                and now - self._last_attempt_at < INIT_RETRY_COOLDOWN_SECONDS
            ):
                # Inside the back-off window. A degraded limiter keeps enforcing
                # against its stand-in; an uninitialized one has nothing to check.
                return self._initialized

            self._last_attempt_at = now
            client = getattr(request.app.state, "redis_client", None)
            try:
                # Re-entry proves the offered client before adopting it: on a
                # mid-life outage app.state holds the client that just died, and
                # re-adopting it unchecked would never reach the stand-in.
                await self.rate_limiter.initialize(
                    client=client, verify_client=self._ever_initialized
                )
            except Exception as e:
                # The underlying rate_limiter already logged the cause.
                self.logger.warning(
                    "Rate limiter initialization failed (%s); retrying in %.0fs",
                    e,
                    INIT_RETRY_COOLDOWN_SECONDS,
                )
                # A failed *promotion* keeps the degraded client it already had.
                return self._initialized

            was_degraded = self._degraded
            self._initialized = True
            self._ever_initialized = True
            # Read straight off the limiter rather than via getattr-with-default:
            # a stand-in that does not report degradation must break the test, not
            # silently look terminal.
            self._degraded = bool(self.rate_limiter.is_degraded)

            if self._degraded:
                # Serving now, but not done: leave ``_last_attempt_at`` set so the
                # next request past the cooldown re-attempts the shared client.
                self.logger.warning(
                    "Rate limiter running on the per-replica stand-in; "
                    "re-attempting the shared Redis client in %.0fs",
                    INIT_RETRY_COOLDOWN_SECONDS,
                )
            else:
                self._last_attempt_at = None
                self._unavailable_logged_at = None
                self.logger.info(
                    "Rate limiter promoted back to the shared Redis client"
                    if was_degraded
                    else "Rate limiting middleware initialized"
                )
            return True

    async def _serve_without_a_limiter(
        self, request: Request, call_next: Callable
    ) -> Response:
        """Decide what a request means when there is no client to check against.

        Called only for requests that *would* be rate limited, so this is where
        the fail-open/fail-closed policy belongs: it is a decision about serving
        unlimited traffic, not about being inside a retry cooldown.

        The check itself is skipped outright rather than run against a ``None``
        client — doing that emitted one to four ERROR lines per request for the
        whole cooldown window. The condition is logged once per window instead.
        """
        now = time.monotonic()
        if (
            self._unavailable_logged_at is None
            or now - self._unavailable_logged_at >= INIT_RETRY_COOLDOWN_SECONDS
        ):
            self._unavailable_logged_at = now
            self.logger.error(
                "Rate limiter has no Redis client; %s until it initializes",
                (
                    "requests pass unlimited"
                    if self.settings.fail_open_on_redis_error
                    else "requests are refused"
                ),
            )

        if self.settings.fail_open_on_redis_error:
            return await call_next(request)

        self.metrics["errors"] += 1
        return JSONResponse(
            status_code=503,
            content={
                "error": "service_unavailable",
                "message": "Rate limiting service temporarily unavailable",
            },
        )

    def _should_bypass(self, request: Request) -> bool:
        """Check if request should bypass rate limiting"""

        # Check bypass headers
        for header in self.settings.protection_bypass_headers:
            if header in request.headers:
                return True

        path = request.url.path

        # Liveness/readiness probes: never limited, never 503'd from here.
        if path in LIVENESS_PATHS or path.startswith(LIVENESS_PATH_PREFIXES):
            return True

        # Static assets
        if path.startswith("/static"):
            return True

        return False

    async def _check_rate_limits(self, request: Request) -> None:
        """Perform all applicable rate limit checks.

        Every allowed result is stashed on ``request.state`` for
        ``_add_rate_limit_headers``. The checks have already read the counts, so
        advertising them costs nothing; asking Redis again on the way out would
        be a second round trip per request to learn what is already in hand.
        """

        session_id = self._extract_session_id(request)
        endpoint = request.url.path
        client_ip = self._get_client_ip(request)

        results: List[RateLimitResult] = []
        request.state.rate_limit_results = results

        # Always check global rate limit
        results.append(await self._check_global_rate_limit(client_ip))

        # Check session-based limits if session available
        if session_id:
            results.extend(
                await self._check_session_rate_limits(session_id, endpoint, request)
            )

        # Check endpoint-specific limits
        await self._check_endpoint_rate_limits(endpoint, session_id, request)

    async def _check_global_rate_limit(self, client_ip: str) -> RateLimitResult:
        """Check global rate limit, returning the result it was decided on."""

        result = await self.rate_limiter.check_rate_limit(
            key=client_ip, limit_type=LimitType.GLOBAL, identifier=f"global:{client_ip}"
        )

        if not result.allowed:
            raise RateLimitError(
                retry_after=result.retry_after,
                limit_type="global",
                current_count=result.current_count,
                limit=result.limit,
                reset_time=result.reset_time,
            )

        return result

    async def _check_session_rate_limits(
        self, session_id: str, endpoint: str, request: Request
    ) -> List[RateLimitResult]:
        """Check session-based rate limits, returning the results they used.

        A session gets two independent pairs of buckets, chosen by what the
        request costs rather than by what it is called:

        - **Cheap reads** — GET/HEAD/OPTIONS that are not in
          ``EXPENSIVE_READ_PATTERNS`` — go to ``per_session_read`` and
          ``per_session_read_hourly``. Ordinary SPA navigation lives here.
        - **Everything else** — writes, and the read endpoints that run an
          embedding and a vector search — goes to ``per_session`` and
          ``per_session_hourly``, the quota that protects LLM compute.

        The pairing is the point, and it is what fm#994's first fix got wrong.
        Exempting reads from only the per-minute bucket left them counted in the
        shared *hourly* one, so a read burst still exhausted a session — now for
        up to an hour, and for its POST turns as well as its GETs. Reads can only
        exhaust reads; a flood of them cannot refuse the next turn.

        Both pairs are additionally bounded by the ``global`` limit, which is
        keyed on the client address rather than the session.

        Whichever pair is chosen, a blocked result always carries the wait and
        the instant the limiter measured — ``_check_redis_rate_limit`` sets both
        on the refusal path — so neither is defaulted here. The old ``or 60`` /
        ``or 3600`` fallbacks were unreachable, and unreachable is the point:
        had a limiter regression ever made them reachable they would have
        quietly resurrected the flat-window answer the honest formula replaced,
        on the one response where the client acts on it. Passing the measured
        value through unguarded means such a regression fails loudly at render
        instead. The hourly fallback was the worse of the two here — it would
        have answered an hour to a read refusal that measured seconds.
        """

        if self._read_limits_configured and is_cheap_read(
            request.method, request.url.path
        ):
            per_minute_type = LimitType.PER_SESSION_READ
            hourly_type = LimitType.PER_SESSION_READ_HOURLY
        else:
            per_minute_type = LimitType.PER_SESSION
            hourly_type = LimitType.PER_SESSION_HOURLY

        results: List[RateLimitResult] = []

        per_minute = await self.rate_limiter.check_rate_limit(
            key=session_id,
            limit_type=per_minute_type,
            identifier=f"{per_minute_type.value}:{session_id}",
        )

        if not per_minute.allowed:
            raise RateLimitError(
                retry_after=per_minute.retry_after,
                limit_type=per_minute_type.value,
                current_count=per_minute.current_count,
                limit=per_minute.limit,
                reset_time=per_minute.reset_time,
            )

        results.append(per_minute)

        hourly = await self.rate_limiter.check_rate_limit(
            key=session_id,
            limit_type=hourly_type,
            identifier=f"{hourly_type.value}:{session_id}",
        )

        if not hourly.allowed:
            raise RateLimitError(
                retry_after=hourly.retry_after,
                limit_type=hourly_type.value,
                current_count=hourly.current_count,
                limit=hourly.limit,
                reset_time=hourly.reset_time,
            )

        results.append(hourly)
        return results

    async def _check_endpoint_rate_limits(
        self, endpoint: str, session_id: Optional[str], request: Request
    ) -> None:
        """Check endpoint-specific rate limits"""

        # Check if this endpoint has special rate limiting
        config = self.endpoint_configs.get(endpoint)
        if not config:
            return

        # Special handling for specific endpoints
        if config.get("special_handling"):
            await config["special_handling"](request, session_id)

    def _extract_session_id(self, request: Request) -> Optional[str]:
        """Extract session ID from request"""

        # Try multiple methods to get session ID

        # 1. From headers
        session_id = request.headers.get("X-Session-ID")
        if session_id:
            return session_id

        # 2. From query parameters
        session_id = request.query_params.get("session_id")
        if session_id:
            return session_id

        # 3. From cookies
        session_id = request.cookies.get("session_id")
        if session_id:
            return session_id

        # 4. Try to get from request body (for POST requests)
        # Note: This is more complex and should be done carefully
        # to avoid consuming the request body

        return None

    def _get_client_ip(self, request: Request) -> str:
        """Get the client address the ``global`` limit is keyed on.

        Delegates to the shared resolver so that forwarding headers are
        honoured only from configured trusted proxies. This used to read
        ``X-Forwarded-For`` unconditionally, which meant the key was chosen by
        the party being limited: rotating the header drew a fresh quota on
        every request, and ``global`` is the only limit that covers
        unauthenticated traffic, so it was not a limit at all.
        """
        return resolve_client_ip(request, self.trusted_proxies)

    async def _add_rate_limit_headers(
        self, request: Request, response: Response
    ) -> None:
        """Advertise, on a served response, the limit closest to being exhausted.

        Emitted from the results ``_check_rate_limits`` already collected — no
        extra Redis round trip on the request path.

        Two defects are closed here. The old version was gated on a session id,
        so unauthenticated traffic — the only traffic the ``global`` limit
        exists for — was never told anything at all; and it was hardwired to
        ``PER_SESSION``, so even an authenticated client was never told about
        the global limit it might actually be hitting first.

        "Closest to exhausted" (least remaining quota) is the right one to
        report: a client that respects the tightest limit it is under respects
        all of them, and reporting a roomier one would invite it to speed up
        into the tighter one. Results with ``limit == 0`` carry no quota to
        advertise — that is a disabled limit, and also what a check reports
        after failing open — so they are skipped rather than published as a
        limit of zero.

        Two responses are left alone entirely, and both are the single-writer
        invariant expressed on the way out rather than on the way in:

        - **A refusal.** Advertising remaining quota beside a 429 is a
          contradiction the client resolves wrongly — ``Remaining: 994`` next to
          "you are being rate limited" reads as "the limit you hit is not this
          one, carry on". Whatever refused the request has already said what it
          measured; this middleware's own refusals never reach here (they
          short-circuit in ``dispatch``), so a 429 arriving through
          ``call_next`` came from an inner enforcer.
        - **A response that already carries ``X-RateLimit-Limit``.** An inner
          enforcer — the OAuth/SSO limiter dependencies — writes the header of
          the limit *it* enforced. This middleware holds results for the general
          limits only, and stamping them over an inner writer's would replace a
          measured tighter quota with a roomier one nobody hit. The middleware
          yields; it does not attempt to reconcile two enforcers' numbers,
          because "tightest wins" is only computable over results, and an
          already-written header is not a result it holds.
        """

        try:
            if response.status_code == 429:
                return
            if "X-RateLimit-Limit" in response.headers:
                return

            results = [
                result
                for result in getattr(request.state, "rate_limit_results", ())
                if result.limit > 0
            ]
            if not results:
                return

            tightest = min(results, key=lambda r: r.limit - r.current_count)

            response.headers["X-RateLimit-Limit"] = str(tightest.limit)
            response.headers["X-RateLimit-Remaining"] = str(
                max(0, tightest.limit - tightest.current_count)
            )
            if tightest.reset_time is not None:
                response.headers["X-RateLimit-Reset"] = str(
                    int(tightest.reset_time.timestamp())
                )

        except Exception as e:
            self.logger.debug(f"Failed to add rate limit headers: {e}")

    def _create_rate_limit_response(
        self, error: RateLimitError, request: Request
    ) -> JSONResponse:
        """Create rate limit exceeded response"""

        # Create standardized error response
        error_response = ProtectionErrorResponse.from_rate_limit_error(error)

        # Log the rate limit violation
        self.logger.warning(
            f"Rate limit exceeded: {error.limit_type}, "
            f"count={error.current_count}/{error.limit}, "
            f"retry_after={error.retry_after}s, "
            f"ip={self._get_client_ip(request)}, "
            f"session={self._extract_session_id(request)}"
        )

        # Create response with appropriate headers
        response = JSONResponse(status_code=429, content=error_response.__dict__)

        # Add rate limit headers. ``Retry-After`` is a duration and
        # ``X-RateLimit-Reset`` the same instant as a timestamp — a 429 used to
        # omit the second, so a client tracking reset instants across responses
        # lost the value on precisely the response that mattered.
        #
        # The instant is the one the limiter measured, carried on the error. It
        # is never re-derived from ``time.time() + retry_after``: that reads the
        # clock again, after the check, the raise and this construction, so the
        # timestamp a client received disagreed with the wait beside it by
        # however long that took. Absent means the limiter measured nothing, and
        # an unmeasured value is omitted rather than invented — a client reading
        # no reset falls back to its own policy, which beats planning against a
        # fabricated instant.
        # ``Retry-After`` obeys the same "unmeasured is absent" contract as the
        # reset instant below. A blocked result always carries
        # ``max(1, ceil(...))``, so ``None`` here means a limiter regression
        # produced a refusal it could not put a wait on — and the honest
        # response to that is silence, not ``str(None)`` rendering the literal
        # header value "None" for a client to parse as a duration.
        if error.retry_after is not None:
            response.headers["Retry-After"] = str(error.retry_after)
        response.headers["X-RateLimit-Limit"] = str(error.limit)
        response.headers["X-RateLimit-Remaining"] = "0"
        if error.reset_time is not None:
            response.headers["X-RateLimit-Reset"] = str(
                int(error.reset_time.timestamp())
            )

        return response

    def _update_metrics(self, check_duration: float, blocked: bool) -> None:
        """Update middleware metrics"""

        self.metrics["requests_checked"] += 1

        if blocked:
            self.metrics["requests_blocked"] += 1

        # Update average check duration
        total_requests = self.metrics["requests_checked"]
        current_avg = self.metrics["avg_check_duration"]
        self.metrics["avg_check_duration"] = (
            current_avg * (total_requests - 1) + check_duration
        ) / total_requests

    async def get_metrics(self) -> Dict[str, Any]:
        """Get middleware metrics"""

        # Get rate limiter health
        rate_limiter_health = await self.rate_limiter.health_check()

        return {
            "middleware_metrics": self.metrics.copy(),
            "rate_limiter_health": rate_limiter_health,
            "rate_limiter_stats": (
                self.rate_limiter.get_timeout_statistics()
                if hasattr(self.rate_limiter, "get_timeout_statistics")
                else {}
            ),
            "configuration": {
                "enabled": self.settings.rate_limiting_enabled,
                "fail_open": self.settings.fail_open_on_redis_error,
                "configured_limits": len(self.settings.rate_limits),
            },
        }
