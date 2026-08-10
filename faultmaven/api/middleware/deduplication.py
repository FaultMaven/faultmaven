"""
Request deduplication middleware

FastAPI middleware for detecting and preventing duplicate requests
within configured time windows using content-based hashing.
Uses Redis (real or FakeRedis) for all storage — no dict fallbacks.
"""

import json
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
            "cache_hits": 0,
            "errors": 0,
            "avg_check_duration": 0.0,
        }

        self._initialized = False
        self._disabled = False

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Main middleware dispatch with deduplication"""

        start_time = time.time()

        try:
            # Initialize if needed (resolves the Redis client from app.state,
            # which is populated by the lifespan composition root)
            if not self._initialized:
                await self._initialize(request)

            # Skip deduplication if disabled or no Redis available
            if not self.settings.deduplication_enabled or self._disabled:
                return await call_next(request)

            # Skip for certain request types
            if self._should_skip(request):
                return await call_next(request)

            # Check for duplicate
            is_duplicate, original_timestamp, ttl = await self._check_duplicate(request)

            if is_duplicate:
                check_duration = time.time() - start_time
                self._update_metrics(check_duration, duplicate_found=True)

                # Calculate TTL remaining
                if original_timestamp:
                    elapsed = (
                        datetime.now(timezone.utc) - original_timestamp
                    ).total_seconds()
                    ttl_remaining = max(0, int(ttl - elapsed))
                else:
                    ttl_remaining = ttl

                # If TTL has expired, allow the request through
                if ttl_remaining == 0:
                    self.logger.debug(
                        "Duplicate request TTL expired, allowing request through"
                    )
                else:
                    return self._create_duplicate_response(
                        request, original_timestamp, ttl_remaining
                    )

            # Process request
            response = await call_next(request)

            # Update metrics
            check_duration = time.time() - start_time
            self._update_metrics(check_duration, duplicate_found=False)

            return response

        except Exception as e:
            self.logger.error(
                f"Deduplication error: {type(e).__name__}: {str(e)}",
                exc_info=False,
            )
            self.metrics["errors"] += 1

            if self.settings.fail_open_on_redis_error:
                return await call_next(request)
            else:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "service_unavailable",
                        "message": "Deduplication service temporarily unavailable",
                    },
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
        """Check if request should skip deduplication"""

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
        """
        try:
            if hasattr(request, "_body"):
                return request._body

            body = await request.body()
            request._body = body
            return body

        except Exception as e:
            self.logger.debug(f"Failed to read request body: {e}")
            return None

    async def _check_hash_duplicate(
        self, request_hash: str, endpoint: str
    ) -> Tuple[bool, Optional[datetime], int]:
        """Check if hash represents a duplicate request via Redis."""
        config = self.endpoint_configs.get(endpoint, {})
        ttl = config.get("ttl", self.settings.deduplication["default"].ttl)
        key = f"{self.redis_key_prefix}:{request_hash}"

        try:
            return await self._check_redis_duplicate(key, ttl)
        except Exception as e:
            self.logger.error(f"Duplicate check failed: {e}")
            return False, None, ttl

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
        is_duplicate = bool(result[0]) if result else False
        # The stored value is the *first* request's timestamp -- it is what the
        # Retry-After is computed from. It has never been a cached response.
        stored_timestamp = result[1] if len(result) > 1 else None

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
