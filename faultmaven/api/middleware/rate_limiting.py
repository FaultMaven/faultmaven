"""
Rate limiting middleware

FastAPI middleware for multi-level rate limiting with Redis backend and
graceful degradation.
"""

import asyncio
import logging
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
                retry_after=result.retry_after or 60,
                limit_type="global",
                current_count=result.current_count,
                limit=result.limit,
            )

        return result

    # Read-only HTTP methods whose UI navigation traffic should not consume the
    # tight per-session per-minute quota.  These requests are still bounded by
    # the ``global`` limit and the ``per_session_hourly`` limit — they are not
    # unlimited, just exempt from the per-minute bucket that was designed to
    # protect LLM compute on write operations (fm#994).
    _READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    async def _check_session_rate_limits(
        self, session_id: str, endpoint: str, request: Request
    ) -> List[RateLimitResult]:
        """Check session-based rate limits, returning the results they used.

        Read-only requests (GET, HEAD, OPTIONS) skip the strict per-session
        per-minute limit so that normal SPA navigation — loading case details,
        file lists, conversation history — does not exhaust a quota designed to
        throttle heavy AI operations (POST turns).  They remain subject to the
        ``global`` limit and the ``per_session_hourly`` limit.
        """

        results: List[RateLimitResult] = []
        is_read_only = request.method.upper() in self._READ_ONLY_METHODS

        # Per-session per-minute limit — only for write operations
        if not is_read_only:
            per_minute = await self.rate_limiter.check_rate_limit(
                key=session_id,
                limit_type=LimitType.PER_SESSION,
                identifier=f"session:{session_id}",
            )

            if not per_minute.allowed:
                raise RateLimitError(
                    retry_after=per_minute.retry_after or 60,
                    limit_type="per_session",
                    current_count=per_minute.current_count,
                    limit=per_minute.limit,
                )

            results.append(per_minute)

        # Per-session hourly limit — applies to all requests
        hourly = await self.rate_limiter.check_rate_limit(
            key=session_id,
            limit_type=LimitType.PER_SESSION_HOURLY,
            identifier=f"session_hourly:{session_id}",
        )

        if not hourly.allowed:
            raise RateLimitError(
                retry_after=hourly.retry_after or 3600,
                limit_type="per_session_hourly",
                current_count=hourly.current_count,
                limit=hourly.limit,
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
        """

        try:
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
        response.headers["Retry-After"] = str(error.retry_after)
        response.headers["X-RateLimit-Limit"] = str(error.limit)
        response.headers["X-RateLimit-Remaining"] = "0"
        response.headers["X-RateLimit-Reset"] = str(
            int(time.time() + error.retry_after)
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
