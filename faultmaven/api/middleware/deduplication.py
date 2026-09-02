"""
Request deduplication middleware

FastAPI middleware for detecting and preventing duplicate requests
within configured time windows using content-based hashing.
Uses Redis (real or FakeRedis) for all storage — no dict fallbacks.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ...infrastructure.protection import RequestHasher
from ...models.protection import (
    DuplicateRequestError,
    ProtectionErrorResponse,
    ProtectionSettings,
)
from ...utils.serialization import to_json_compatible
from .route_policy import RoutePolicy, normalize_path, policy_for

#: Shared default so the lookup below needs no branch. Frozen and
#: withholding nothing, which is what an undeclared route gets.
_NO_POLICY = RoutePolicy()


class DeduplicationMiddleware(BaseHTTPMiddleware):
    """
    Request deduplication middleware

    Features:
    - Exact request hashing (session + method + path + query + body bytes)
    - Configurable TTL per endpoint type
    - Redis-backed (real or FakeRedis via central client factory)

    A duplicate is answered with a labelled 409 and a ``Retry-After``. There is
    no response cache: the writer stored responses under ``{key}:response`` while
    the only read was ``GET {key}``, so nothing was ever served from it, and the
    read path instead fed the stored *timestamp* to ``json.loads`` -- which threw
    on every duplicate and was swallowed by the outer handler. The 409 below was
    unreachable until that was removed.
    """

    def __init__(
        self,
        app,
        settings: ProtectionSettings,
        redis_client=None,
        redis_url: Optional[str] = None,
    ):
        super().__init__(app)
        self.settings = settings
        self.logger = logging.getLogger(__name__)

        # Initialize request hasher
        self.hasher = RequestHasher()

        # Redis connection: prefer injected client, fall back to URL-based init
        self.redis_url = redis_url or settings.redis_url
        self.redis_key_prefix = f"{settings.redis_key_prefix}:dedup"
        self._redis = redis_client

        # Endpoint configurations
        self.endpoint_configs = {
            "/api/v1/data/upload": {
                "ttl": self.settings.deduplication["default"].ttl,
                "special_handler": None,
            }
        }

        # Metrics
        self.metrics = {
            "requests_checked": 0,
            "duplicates_found": 0,
            "errors": 0,
            "avg_check_duration": 0.0,
        }

        self._initialized = False
        self._disabled = False

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Main middleware dispatch with deduplication.

        Only the deduplication decision is guarded. ``call_next`` sits outside
        the handler deliberately: wrapping it made this middleware the reporter
        of *any* unhandled route exception, so a bug in a handler surfaced as
        ``503 "Deduplication service temporarily unavailable"`` under the
        fail-closed setting production pins -- and under fail-open the handler
        re-entered ``call_next`` on a request whose body stream had already been
        consumed. A middleware may only answer for its own failures.
        """
        start_time = time.time()
        duplicate: Optional[Response] = None

        try:
            # Initialize if needed (resolves the Redis client from app.state,
            # which is populated by the lifespan composition root)
            if not self._initialized:
                await self._initialize(request)

            if (
                self.settings.deduplication_enabled
                and not self._disabled
                and not self._should_skip(request)
            ):
                duplicate = await self._duplicate_response_for(request, start_time)

        except Exception as e:
            self.logger.error(
                f"Deduplication error: {type(e).__name__}: {str(e)}",
                exc_info=False,
            )
            self.metrics["errors"] += 1

            if not self.settings.fail_open_on_redis_error:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "service_unavailable",
                        "message": "Deduplication service temporarily unavailable",
                    },
                )

        if duplicate is not None:
            return duplicate

        response = await call_next(request)

        self._update_metrics(time.time() - start_time, duplicate_found=False)
        return response

    async def _duplicate_response_for(
        self, request: Request, start_time: float
    ) -> Optional[Response]:
        """Return the 409 for a duplicate, or ``None`` to let the request run."""
        is_duplicate, original_timestamp, ttl = await self._check_duplicate(request)
        if not is_duplicate:
            return None

        self._update_metrics(time.time() - start_time, duplicate_found=True)

        if original_timestamp:
            elapsed = (datetime.now(timezone.utc) - original_timestamp).total_seconds()
            ttl_remaining = max(0, int(ttl - elapsed))
        else:
            ttl_remaining = ttl

        if ttl_remaining == 0:
            self.logger.debug("Duplicate request TTL expired, allowing request through")
            return None

        return self._create_duplicate_response(
            request, original_timestamp, ttl_remaining
        )

    async def _initialize(self, request: Request) -> None:
        """Resolve the Redis client lazily on the first request.

        Starlette middleware is constructed at import time, before the lifespan
        startup that creates the Redis client — so the client cannot be captured
        in ``__init__``. ``resolve_redis_client`` performs the shared lazy
        resolution (injected → app.state → central factory) and always returns a
        working client (real Redis or FakeRedis).
        """
        from ...infrastructure.redis_client import resolve_redis_client

        self._redis = resolve_redis_client(
            request, injected=self._redis, redis_url=self.redis_url
        )

        if self._redis is not None:
            self._initialized = True
            self.logger.info("Request deduplication middleware initialized")
            return

        # Genuinely no client available — degrade gracefully (fail-open dispatch).
        self.logger.warning(
            "Deduplication middleware has no Redis client — deduplication disabled"
        )
        self._disabled = True
        self._initialized = True

    def _should_skip(self, request: Request) -> bool:
        """Whether this request is exempt from deduplication.

        ⚠ This list also carries an ordering constraint. ``main.py`` installs
        protection *after* the idempotency middleware, and a later
        ``add_middleware`` sits further out — so deduplication sees a request
        first. A client resending with a stable ``Idempotency-Key`` expects the
        cached replay; a 409 from here would pre-empt it. That is safe today
        only because both paths the copilot sends a key on are exempt below:
        ``POST /api/v1/cases`` explicitly, and the turn POST as multipart.
        ``test_idempotency_bearing_paths_are_skipped`` pins it. Removing either
        exemption means reordering the two middlewares, not just editing here.

        A composed route reaches the same guarantee by declaring it: fm#1303
        added the ``route_policy`` read below, and ``declare_credential_mint``
        sets ``never_collapsed`` alongside ``never_replayed`` precisely so a
        composed mint cannot be exempted from one door and stopped at the other.
        """
        if request.method == "GET":
            return True
        if request.url.path.startswith("/health"):
            return True
        if request.url.path.startswith("/metrics"):
            return True
        if request.url.path.startswith("/static"):
            return True
        if request.method == "POST" and request.url.path == "/api/v1/cases":
            return True
        if request.method == "POST" and request.url.path == "/api/v1/sessions":
            return True

        # Exemptions this repository cannot write down. Every entry above names
        # a route this package serves, and the served route table is larger than
        # this package: faultmaven-cloud mounts its routers onto the same ``app``
        # singleton, and one of them mints a service-account refresh token
        # (ADR-012 D10). The ordering warning in this docstring is exactly what
        # such a route cannot obtain on its own — it is "safe today" only for
        # paths that appear in a list it cannot appear in (fm#1303).
        #
        # Read from the same declaration ``IdempotencyMiddleware`` reads, so the
        # two can never disagree about what the composition root asked for.
        if (
            policy_for(request)
            .get(normalize_path(request.url.path), _NO_POLICY)
            .never_collapsed
        ):
            return True

        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            return True

        return False

    async def _check_duplicate(
        self, request: Request
    ) -> Tuple[bool, Optional[datetime], int]:
        """Check if request is a duplicate"""
        request_hash = await self._generate_request_hash(request)

        if not request_hash:
            return False, None, 0

        return await self._check_hash_duplicate(request_hash, request.url.path)

    async def _generate_request_hash(self, request: Request) -> Optional[str]:
        """Generate hash for request"""
        try:
            session_id = self._extract_session_id(request)
            if not session_id:
                return None

            body = await self._get_request_body(request)

            endpoint = request.url.path
            config = self.endpoint_configs.get(endpoint)

            if config and config.get("special_handler"):
                return await config["special_handler"](request, session_id, body)

            return self.hasher.hash_request(
                session_id=session_id,
                endpoint=endpoint,
                method=request.method,
                body=body,
                query_params=dict(request.query_params),
            )

        except Exception as e:
            self.logger.error(f"Failed to generate request hash: {e}")
            return None

    async def _get_request_body(self, request: Request) -> Optional[bytes]:
        """Get the raw request body for hashing.

        Returned undecoded: decoding here used to fold every non-UTF-8 body onto
        ``None`` -- and so onto the same digest as a genuinely empty body, and as
        each other. The hasher wants bytes anyway; nothing downstream reads this
        as text.

        A read failure is *not* caught here either. Swallowing it returned
        ``None``, which the hasher maps to ``b""`` -- so an unreadable body
        deduplicated against a genuinely empty one, and against every other
        unreadable body. It propagates to ``_generate_request_hash``, which
        declines to identify the request rather than misidentifying it: a
        request we could not read is not a request we can call a duplicate.
        """
        if hasattr(request, "_body"):
            return request._body

        body = await request.body()
        request._body = body
        return body

    async def _check_hash_duplicate(
        self, request_hash: str, endpoint: str
    ) -> Tuple[bool, Optional[datetime], int]:
        """Check if hash represents a duplicate request via Redis.

        Redis failures are *not* caught here. They belong to ``dispatch``, which
        is where ``fail_open_on_redis_error`` is honoured. Swallowing them here
        answered "not a duplicate" on any backend error, so the fail-closed
        setting production pins did not cover this path at all: a broken Redis
        silently admitted every duplicate while the policy claimed the opposite.
        Reporting an absence of duplicates is a claim, and it needs the store to
        have actually answered.
        """
        config = self.endpoint_configs.get(endpoint, {})
        ttl = config.get("ttl", self.settings.deduplication["default"].ttl)
        key = f"{self.redis_key_prefix}:{request_hash}"

        return await self._check_redis_duplicate(key, ttl)

    async def _check_redis_duplicate(
        self, key: str, ttl: int
    ) -> Tuple[bool, Optional[datetime], int]:
        """Check for duplicate using Redis (real or FakeRedis)."""
        lua_script = """
        local key = KEYS[1]
        local ttl = tonumber(ARGV[1])
        local timestamp = ARGV[2]

        local existing = redis.call('GET', key)
        if existing then
            return {1, existing}  -- duplicate found
        end

        -- Store timestamp
        redis.call('SETEX', key, ttl, timestamp)
        return {0, nil}  -- not duplicate
        """

        current_time = datetime.now(timezone.utc)
        current_time_str = to_json_compatible(current_time)

        result = await self._redis.eval(lua_script, 1, key, ttl, current_time_str)

        # Redis truncates trailing nils in Lua array replies, so the
        # not-duplicate branch ({0, nil}) comes back as a 1-element array ([0]).
        # Unpack defensively instead of assuming a fixed length.
        # A falsy reply (``None``, ``[]``) means the script told us nothing, so
        # the same guard has to cover *both* reads -- indexing element 1 after
        # only element 0 was guarded raised ``TypeError`` on ``len(None)``,
        # which, now that the surrounding catch is gone, 503s every request
        # under the fail-closed setting.
        is_duplicate = bool(result[0]) if result else False
        # The stored value is the *first* request's timestamp -- it is what the
        # Retry-After is computed from. It has never been a cached response.
        stored_timestamp = result[1] if result and len(result) > 1 else None

        if is_duplicate:
            self.logger.debug(f"Duplicate request detected: {key}")
            try:
                original_timestamp = datetime.fromisoformat(
                    stored_timestamp.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                original_timestamp = current_time
            return True, original_timestamp, ttl

        return False, None, ttl

    def _extract_session_id(self, request: Request) -> Optional[str]:
        """Extract session ID from request"""
        session_id = request.headers.get("X-Session-ID")
        if session_id:
            return session_id

        session_id = request.query_params.get("session_id")
        if session_id:
            return session_id

        session_id = request.cookies.get("session_id")
        if session_id:
            return session_id

        return None

    def _create_duplicate_response(
        self,
        request: Request,
        original_timestamp: Optional[datetime],
        ttl_remaining: int,
    ) -> JSONResponse:
        """Create error response for duplicate request"""
        error = DuplicateRequestError(
            original_timestamp=original_timestamp or datetime.now(timezone.utc),
            ttl_remaining=ttl_remaining,
            correlation_id=request.headers.get("x-correlation-id", ""),
        )

        error_response = ProtectionErrorResponse.from_duplicate_error(error)

        session_id = self._extract_session_id(request)
        self.logger.info(
            f"Duplicate request blocked: {request.url.path}, "
            f"session={session_id}, "
            f"ttl_remaining={ttl_remaining}s"
        )

        return JSONResponse(
            status_code=409,
            headers={
                "Retry-After": str(ttl_remaining),
                "x-error-code": error_response.error_code,
            },
            content={
                "error_type": error_response.error_type,
                "error_code": error_response.error_code,
                "message": error_response.message,
                "retry_after": error_response.retry_after,
                "correlation_id": error_response.correlation_id,
                "timestamp": error_response.timestamp,
                "suggestions": error_response.suggestions,
            },
        )

    # ``_create_duplicate_error_response`` used to live here, reached from an
    # ``except DuplicateRequestError`` arm in ``dispatch``. Both are removed:
    # ``DuplicateRequestError`` is never raised anywhere in the codebase — it is
    # only ever *constructed*, by ``_create_duplicate_response`` above, which
    # returns its own labelled JSONResponse. The handler could not run.
    #
    # It is worth saying why the dead path mattered rather than just deleting
    # it quietly. Unlike ``_create_duplicate_response`` it emitted a 409 with
    # **no** ``x-error-code`` header, and the Slack agent reads an unlabelled
    # 409 on the turn POST as "this case is terminal" — so had anything ever
    # raised ``DuplicateRequestError``, a user with a live case would have been
    # told their investigation was closed. The guarantee that no non-terminal
    # 409 is unlabelled is now pinned by
    # ``tests/unit/api/middleware/test_conflict_labelling.py``.

    def _update_metrics(self, check_duration: float, duplicate_found: bool) -> None:
        """Update middleware metrics"""
        self.metrics["requests_checked"] += 1

        if duplicate_found:
            self.metrics["duplicates_found"] += 1

        total_requests = self.metrics["requests_checked"]
        current_avg = self.metrics["avg_check_duration"]
        self.metrics["avg_check_duration"] = (
            current_avg * (total_requests - 1) + check_duration
        ) / total_requests

    async def get_metrics(self) -> Dict[str, Any]:
        """Get middleware metrics"""
        duplicate_rate = 0.0
        if self.metrics["requests_checked"] > 0:
            duplicate_rate = (
                self.metrics["duplicates_found"] / self.metrics["requests_checked"]
            )

        return {
            "middleware_metrics": {
                **self.metrics,
                "duplicate_rate": duplicate_rate,
            },
            "redis_health": {
                "healthy": True,
                "initialized": self._initialized,
            },
            "configuration": {
                "enabled": self.settings.deduplication_enabled,
                "fail_open": self.settings.fail_open_on_redis_error,
                "endpoint_configs": {
                    path: {
                        "ttl": config["ttl"],
                    }
                    for path, config in self.endpoint_configs.items()
                },
            },
        }
