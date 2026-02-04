"""
Request deduplication middleware

FastAPI middleware for detecting and preventing duplicate requests
within configured time windows using content-based hashing.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# Conditional Redis import (enterprise-only dependency)
try:
    import redis.asyncio as aioredis
    from redis.exceptions import RedisError

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    aioredis = Any  # type: ignore
    RedisError = Exception  # Fallback to base Exception

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
    - Redis-backed with in-memory fallback
    - Optional response caching for duplicates
    - Special handling for title generation requests
    """

    def __init__(
        self, app, settings: ProtectionSettings, redis_url: Optional[str] = None
    ):
        super().__init__(app)
        self.settings = settings
        self.logger = logging.getLogger(__name__)

        # Initialize request hasher
        self.hasher = RequestHasher(salt="faultmaven_dedup_2025")

        # Redis connection
        effective_redis_url = redis_url or settings.redis_url
        self.redis_url = effective_redis_url
        self.redis_key_prefix = f"{settings.redis_key_prefix}:dedup"
        self._redis: Optional[aioredis.Redis] = None
        self._redis_healthy = True

        # In-memory fallback
        self._fallback_store: Dict[str, Tuple[float, Optional[str]]] = {}
        self._fallback_cleanup_interval = 60
        self._last_fallback_cleanup = time.time()

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

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Main middleware dispatch with deduplication"""

        start_time = time.time()

        try:
            # Initialize if needed
            if not self._initialized:
                await self._initialize()

            # Skip deduplication if disabled
            if not self.settings.deduplication_enabled:
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
            # Log the error cleanly without trying to serialize exception objects
            self.logger.error(
                f"Deduplication error: {type(e).__name__}: {str(e)}",
                exc_info=False,  # Avoid serialization issues
            )
            self.metrics["errors"] += 1

            # Fail open - re-raise to let upstream handle it
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
        """Initialize Redis connection"""
        try:
            self._redis = aioredis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )

            await self._redis.ping()
            self._redis_healthy = True
            self.logger.info("Request deduplication middleware initialized")
            self._initialized = True

        except Exception as e:
            self._redis_healthy = False
            self._initialized = True  # Continue with fallback

            if not self.settings.fail_open_on_redis_error:
                # Redis is required but unavailable - fail hard
                self.logger.error(f"Failed to initialize deduplication Redis: {e}")
                raise
            else:
                # Graceful degradation: requests will not be deduplicated
                self.logger.warning(
                    f"Redis unavailable ({e}), request deduplication disabled"
                )

        return False

    def _should_skip(self, request: Request) -> bool:
        """Check if request should skip deduplication"""

        # Skip GET requests (typically idempotent)
        if request.method == "GET":
            return True

        # Skip health checks
        if request.url.path.startswith("/health"):
            return True

        # Skip metrics endpoints
        if request.url.path.startswith("/metrics"):
            return True

        # Skip static content
        if request.url.path.startswith("/static"):
            return True

        # Skip POST /api/v1/cases - handled by IdempotencyMiddleware with correct CaseSummary schema
        if request.method == "POST" and request.url.path == "/api/v1/cases":
            return True

        # Skip POST /api/v1/sessions - handled by IdempotencyMiddleware
        if request.method == "POST" and request.url.path == "/api/v1/sessions":
            return True

        # Skip certain content types
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            return True  # File uploads are typically unique

        return False

    async def _check_duplicate(
        self, request: Request
    ) -> Tuple[bool, Optional[str], Optional[datetime], int]:
        """Check if request is a duplicate

        Returns:
            Tuple of (is_duplicate, cached_response, original_timestamp, ttl)
        """

        # Generate request hash
        request_hash = await self._generate_request_hash(request)

        if not request_hash:
            return False, None, None, 0

        # Check for duplicate
        return await self._check_hash_duplicate(request_hash, request.url.path)

    async def _generate_request_hash(self, request: Request) -> Optional[str]:
        """Generate hash for request"""

        try:
            # Extract session ID
            session_id = self._extract_session_id(request)
            if not session_id:
                # Can't deduplicate without session context
                return None

            # Get request body
            body = await self._get_request_body(request)

            # Get endpoint config
            endpoint = request.url.path
            config = self.endpoint_configs.get(endpoint)

            # Use special handler if available
            if config and config.get("special_handler"):
                return await config["special_handler"](request, session_id, body)

            # Standard hash generation
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
            # Check if body was already read
            if hasattr(request, "_body"):
                body = request._body
            else:
                body = await request.body()
                request._body = body  # Cache for later use

            if body:
                return body.decode("utf-8")

        except Exception as e:
            self.logger.debug(f"Failed to read request body: {e}")

        return None

    async def _check_hash_duplicate(
        self, request_hash: str, endpoint: str
    ) -> Tuple[bool, Optional[str], Optional[datetime], int]:
        """Check if hash represents a duplicate request

        Returns:
            Tuple of (is_duplicate, cached_response, original_timestamp, ttl)
        """

        # Get TTL for this endpoint
        config = self.endpoint_configs.get(endpoint, {})
        ttl = config.get("ttl", self.settings.deduplication["default"].ttl)

        key = f"{self.redis_key_prefix}:{request_hash}"

        try:
            if self._redis and self._redis_healthy:
                is_dup, cached, orig_time = await self._check_redis_duplicate(key, ttl)
                return is_dup, cached, orig_time, ttl
            else:
                is_dup, cached, orig_time = await self._check_fallback_duplicate(
                    key, ttl
                )
                return is_dup, cached, orig_time, ttl

        except Exception as e:
            self.logger.error(f"Duplicate check failed: {e}")
            return False, None, None, ttl

    async def _check_redis_duplicate(
        self, key: str, ttl: int
    ) -> Tuple[bool, Optional[str], Optional[datetime]]:
        """Check for duplicate using Redis

        Returns:
            Tuple of (is_duplicate, cached_response, original_timestamp)
        """

        # Lua script for atomic check-and-set with TTL
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

        try:
            result = await self._redis.eval(
                lua_script, 1, key, ttl, current_time_str  # number of keys
            )

            is_duplicate, cached_data = result

            if is_duplicate:
                self.logger.debug(f"Duplicate request detected: {key}")
                # Parse original timestamp from cached data
                try:
                    original_timestamp = datetime.fromisoformat(
                        cached_data.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    original_timestamp = current_time
                return True, cached_data, original_timestamp

            return False, None, None

        except RedisError as e:
            self.logger.warning(f"Redis duplicate check failed: {e}")
            self._redis_healthy = False
            return await self._check_fallback_duplicate(key, ttl)

    async def _check_fallback_duplicate(
        self, key: str, ttl: int
    ) -> Tuple[bool, Optional[str], Optional[datetime]]:
        """Check for duplicate using in-memory store

        Returns:
            Tuple of (is_duplicate, cached_response, original_timestamp)
        """

        current_time = time.time()
        current_datetime = datetime.now(timezone.utc)

        # Clean up expired entries periodically
        if current_time - self._last_fallback_cleanup > self._fallback_cleanup_interval:
            await self._cleanup_fallback_store()
            self._last_fallback_cleanup = current_time

        # Check for existing entry
        if key in self._fallback_store:
            timestamp, cached_response = self._fallback_store[key]

            # Check if still valid
            if current_time - timestamp < ttl:
                self.logger.debug(f"Duplicate found in fallback store: {key}")
                # Calculate original timestamp
                original_timestamp = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                return True, cached_response, original_timestamp
            else:
                # Expired, remove it
                del self._fallback_store[key]

        # Store new entry
        self._fallback_store[key] = (current_time, None)
        return False, None, None

    async def _cleanup_fallback_store(self) -> None:
        """Clean up expired entries from fallback store"""

        current_time = time.time()
        expired_keys = []

        for key, (timestamp, _) in self._fallback_store.items():
            # Use max TTL for cleanup (conservative approach)
            max_ttl = max(
                config.get("ttl", 300) for config in self.endpoint_configs.values()
            )
            if current_time - timestamp > max_ttl:
                expired_keys.append(key)

        for key in expired_keys:
            del self._fallback_store[key]

        if expired_keys:
            self.logger.debug(f"Cleaned up {len(expired_keys)} expired dedup entries")

    async def _cache_response(self, request: Request, response: Response) -> None:
        """Cache response for future duplicate requests"""

        # Only cache for certain endpoints and response codes
        if response.status_code != 200:
            return

        endpoint = request.url.path
        config = self.endpoint_configs.get(endpoint, {})

        if not config.get("cache_responses", False):
            return

        try:
            # Generate hash again
            request_hash = await self._generate_request_hash(request)
            if not request_hash:
                return

            # Get response content
            if hasattr(response, "body"):
                response_content = response.body.decode("utf-8")
            else:
                return  # Can't cache without content

            # Store in Redis or fallback
            key = f"{self.redis_key_prefix}:{request_hash}"
            ttl = config.get("ttl", self.settings.deduplication["default"].ttl)

            if self._redis and self._redis_healthy:
                await self._redis.setex(f"{key}:response", ttl, response_content)
            else:
                # Store in fallback
                if key in self._fallback_store:
                    timestamp, _ = self._fallback_store[key]
                    self._fallback_store[key] = (timestamp, response_content)

        except Exception as e:
            self.logger.debug(f"Response caching failed: {e}")

    def _extract_session_id(self, request: Request) -> Optional[str]:
        """Extract session ID from request"""

        # Try headers first
        session_id = request.headers.get("X-Session-ID")
        if session_id:
            return session_id

        # Try query parameters
        session_id = request.query_params.get("session_id")
        if session_id:
            return session_id

        # Try cookies
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
        """Create error response for duplicate request using ProtectionErrorResponse schema"""

        # Create DuplicateRequestError
        error = DuplicateRequestError(
            original_timestamp=original_timestamp or datetime.now(timezone.utc),
            ttl_remaining=ttl_remaining,
            correlation_id=request.headers.get("x-correlation-id", ""),
        )

        # Convert to ProtectionErrorResponse
        error_response = ProtectionErrorResponse.from_duplicate_error(error)

        # Log the duplicate detection
        session_id = self._extract_session_id(request)
        self.logger.info(
            f"Duplicate request blocked: {request.url.path}, "
            f"session={session_id}, "
            f"ttl_remaining={ttl_remaining}s"
        )

        # Return 409 Conflict with Retry-After header
        return JSONResponse(
            status_code=409,  # Conflict - duplicate resource creation attempt
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

        return JSONResponse(
            status_code=409, content=error_response.__dict__  # Conflict
        )

    def _update_metrics(self, check_duration: float, duplicate_found: bool) -> None:
        """Update middleware metrics"""

        self.metrics["requests_checked"] += 1

        if duplicate_found:
            self.metrics["duplicates_found"] += 1

        # Update average duration
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
                "fallback_entries": len(self._fallback_store),
            },
            "redis_health": {
                "healthy": self._redis_healthy,
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
