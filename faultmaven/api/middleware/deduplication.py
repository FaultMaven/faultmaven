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
    - Content-based request hashing with normalization
    - Configurable TTL per endpoint type
    - Redis-backed (real or FakeRedis via central client factory)
    - Optional response caching for duplicates
    - Special handling for title generation requests
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
        self.hasher = RequestHasher(salt="faultmaven_dedup_2025")

        # Redis connection: prefer injected client, fall back to URL-based init
        self.redis_url = redis_url or settings.redis_url
        self.redis_key_prefix = f"{settings.redis_key_prefix}:dedup"
        self._redis = redis_client

        # Endpoint configurations
        self.endpoint_configs = {
            "/api/v1/data/upload": {
                "ttl": self.settings.deduplication["default"].ttl,
                "cache_responses": False,
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
            # Initialize if needed
            if not self._initialized:
                await self._initialize()

            # Skip deduplication if disabled or no Redis available
            if not self.settings.deduplication_enabled or self._disabled:
                return await call_next(request)

            # Skip for certain request types
            if self._should_skip(request):
                return await call_next(request)

            # Check for duplicate
            is_duplicate, cached_response, original_timestamp, ttl = (
                await self._check_duplicate(request)
            )

            if is_duplicate:
                check_duration = time.time() - start_time
                self._update_metrics(check_duration, duplicate_found=True)

                if cached_response:
                    self.logger.debug(
                        f"Returning cached response for duplicate request"
                    )
                    self.metrics["cache_hits"] += 1
                    return JSONResponse(content=json.loads(cached_response))
                else:
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
                            f"Duplicate request TTL expired, allowing request through"
                        )
                    else:
                        return self._create_duplicate_response(
                            request, original_timestamp, ttl_remaining
                        )

            # Process request
            response = await call_next(request)

            # Cache response if configured
            await self._cache_response(request, response)

            # Update metrics
            check_duration = time.time() - start_time
            self._update_metrics(check_duration, duplicate_found=False)

            return response

        except DuplicateRequestError as e:
            check_duration = time.time() - start_time
            self._update_metrics(check_duration, duplicate_found=True)
            return self._create_duplicate_error_response(e, request)

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

    async def _initialize(self) -> None:
        """Ensure Redis client is available."""
        if self._redis is not None:
            # Client was injected via constructor — ready to go
            self._initialized = True
            self.logger.info(
                "Request deduplication middleware initialized (injected client)"
            )
            return

        # No client injected — should not happen in normal startup.
        # Disable dedup entirely so dispatch short-circuits.
        self.logger.warning(
            "Deduplication middleware has no Redis client — " "deduplication disabled"
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
    ) -> Tuple[bool, Optional[str], Optional[datetime], int]:
        """Check if request is a duplicate"""
        request_hash = await self._generate_request_hash(request)

        if not request_hash:
            return False, None, None, 0

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
                headers=dict(request.headers),
            )

        except Exception as e:
            self.logger.error(f"Failed to generate request hash: {e}")
            return None

    async def _get_request_body(self, request: Request) -> Optional[str]:
        """Get request body for hashing"""
        try:
            if hasattr(request, "_body"):
                body = request._body
            else:
                body = await request.body()
                request._body = body

            if body:
                return body.decode("utf-8")

        except Exception as e:
            self.logger.debug(f"Failed to read request body: {e}")

        return None

    async def _check_hash_duplicate(
        self, request_hash: str, endpoint: str
    ) -> Tuple[bool, Optional[str], Optional[datetime], int]:
        """Check if hash represents a duplicate request via Redis."""
        config = self.endpoint_configs.get(endpoint, {})
        ttl = config.get("ttl", self.settings.deduplication["default"].ttl)
        key = f"{self.redis_key_prefix}:{request_hash}"

        try:
            return await self._check_redis_duplicate(key, ttl)
        except Exception as e:
            self.logger.error(f"Duplicate check failed: {e}")
            return False, None, None, ttl

    async def _check_redis_duplicate(
        self, key: str, ttl: int
    ) -> Tuple[bool, Optional[str], Optional[datetime], int]:
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

        is_duplicate, cached_data = result

        if is_duplicate:
            self.logger.debug(f"Duplicate request detected: {key}")
            try:
                original_timestamp = datetime.fromisoformat(
                    cached_data.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                original_timestamp = current_time
            return True, cached_data, original_timestamp, ttl

        return False, None, None, ttl

    async def _cache_response(self, request: Request, response: Response) -> None:
        """Cache response for future duplicate requests"""
        if response.status_code != 200:
            return

        endpoint = request.url.path
        config = self.endpoint_configs.get(endpoint, {})

        if not config.get("cache_responses", False):
            return

        try:
            request_hash = await self._generate_request_hash(request)
            if not request_hash:
                return

            if hasattr(response, "body"):
                response_content = response.body.decode("utf-8")
            else:
                return

            key = f"{self.redis_key_prefix}:{request_hash}"
            ttl = config.get("ttl", self.settings.deduplication["default"].ttl)

            await self._redis.setex(f"{key}:response", ttl, response_content)

        except Exception as e:
            self.logger.debug(f"Response caching failed: {e}")

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

    def _create_duplicate_error_response(
        self, error: DuplicateRequestError, request: Request
    ) -> JSONResponse:
        """Create error response for duplicate request"""
        error_response = ProtectionErrorResponse.from_duplicate_error(error)

        self.logger.info(
            f"Duplicate request blocked: {request.url.path}, "
            f"session={self._extract_session_id(request)}, "
            f"ttl_remaining={error.ttl_remaining}s"
        )

        return JSONResponse(status_code=409, content=error_response.__dict__)

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
                        "cache_responses": config["cache_responses"],
                    }
                    for path, config in self.endpoint_configs.items()
                },
            },
        }
