"""
Request deduplication middleware

FastAPI middleware for detecting and preventing duplicate requests
within configured time windows using content-based hashing.

Principle 1 Compliance: Uses IDeduplicationStore interface instead of
direct Redis imports, making it deployment-agnostic.
"""

import time
import logging
import json
from typing import Callable, Dict, Any, Optional, Tuple, TYPE_CHECKING

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ...models.protection import (
    ProtectionSettings,
    DuplicateRequestError,
    ProtectionErrorResponse
)
from ...infrastructure.protection import RequestHasher
from ...utils.serialization import to_json_compatible
from ...infrastructure.persistence.deduplication_store import (
    IDeduplicationStore,
    InMemoryDeduplicationStore,
    get_deduplication_store,
)

if TYPE_CHECKING:
    pass  # For type hints only


class DeduplicationMiddleware(BaseHTTPMiddleware):
    """
    Request deduplication middleware

    Features:
    - Content-based request hashing with normalization
    - Configurable TTL per endpoint type
    - Backend-agnostic storage via IDeduplicationStore interface
    - Optional response caching for duplicates
    - Special handling for title generation requests

    Principle 1 Compliance: Uses IDeduplicationStore interface instead of
    direct redis imports, making it deployment-agnostic.
    """

    def __init__(
        self,
        app,
        settings: ProtectionSettings,
        dedup_store: Optional[IDeduplicationStore] = None,
        redis_url: Optional[str] = None,
    ):
        super().__init__(app)
        self.settings = settings
        self.logger = logging.getLogger(__name__)

        # Initialize request hasher
        self.hasher = RequestHasher(salt="faultmaven_dedup_2025")

        # Use injected store or create from settings (Principle 1: Deployment Agnostic)
        effective_redis_url = redis_url or settings.redis_url
        self.redis_key_prefix = f"{settings.redis_key_prefix}:dedup"

        if dedup_store is not None:
            self._store = dedup_store
            self.logger.info("Using injected deduplication store")
        else:
            # Fall back to factory for backwards compatibility
            self._store = get_deduplication_store(
                redis_url=effective_redis_url,
                key_prefix=self.redis_key_prefix,
                fallback_to_memory=settings.fail_open_on_redis_error,
            )
            self.logger.info("Using factory deduplication store")

        # In-memory fallback store (used when primary store fails)
        self._fallback_store = InMemoryDeduplicationStore()

        # Endpoint configurations
        self.endpoint_configs = {
            "/api/v1/data/upload": {
                "ttl": self.settings.deduplication["default"].ttl,
                "cache_responses": False,
                "special_handler": None
            }
        }

        # Metrics
        self.metrics = {
            "requests_checked": 0,
            "duplicates_found": 0,
            "cache_hits": 0,
            "errors": 0,
            "avg_check_duration": 0.0
        }

        self._initialized = True  # Now initialized during __init__
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Main middleware dispatch with deduplication"""

        start_time = time.time()

        try:
            # Skip deduplication if disabled
            if not self.settings.deduplication_enabled:
                return await call_next(request)

            # Skip for certain request types
            if self._should_skip(request):
                return await call_next(request)

            # Check for duplicate
            is_duplicate, cached_response = await self._check_duplicate(request)

            if is_duplicate:
                check_duration = time.time() - start_time
                self._update_metrics(check_duration, duplicate_found=True)

                if cached_response:
                    self.logger.debug("Returning cached response for duplicate request")
                    self.metrics["cache_hits"] += 1
                    return JSONResponse(content=json.loads(cached_response))
                else:
                    return self._create_duplicate_response(request)

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
                exc_info=False  # Avoid serialization issues
            )
            self.metrics["errors"] += 1

            # Fail open - continue processing request
            if self.settings.fail_open_on_redis_error:
                return await call_next(request)
            else:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "service_unavailable",
                        "message": "Deduplication service temporarily unavailable"
                    }
                )
    
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
        
        # Skip certain content types
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            return True  # File uploads are typically unique
        
        return False
    
    async def _check_duplicate(self, request: Request) -> Tuple[bool, Optional[str]]:
        """Check if request is a duplicate"""
        
        # Generate request hash
        request_hash = await self._generate_request_hash(request)
        
        if not request_hash:
            return False, None
        
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
                headers=dict(request.headers)
            )
            
        except Exception as e:
            self.logger.error(f"Failed to generate request hash: {e}")
            return None
    
    
    async def _get_request_body(self, request: Request) -> Optional[str]:
        """Get request body for hashing"""
        
        try:
            # Check if body was already read
            if hasattr(request, '_body'):
                body = request._body
            else:
                body = await request.body()
                request._body = body  # Cache for later use
            
            if body:
                return body.decode('utf-8')
        
        except Exception as e:
            self.logger.debug(f"Failed to read request body: {e}")
        
        return None
    
    async def _check_hash_duplicate(
        self,
        request_hash: str,
        endpoint: str
    ) -> Tuple[bool, Optional[str]]:
        """Check if hash represents a duplicate request via IDeduplicationStore"""

        # Get TTL for this endpoint
        config = self.endpoint_configs.get(endpoint, {})
        ttl = config.get("ttl", self.settings.deduplication["default"].ttl)

        key = request_hash  # Store handles key prefixing

        try:
            # Use primary store (via IDeduplicationStore interface)
            is_duplicate, cached_data = await self._store.check_and_set(key, ttl)

            if is_duplicate:
                self.logger.debug(f"Duplicate request detected: {key}")

            return is_duplicate, cached_data

        except Exception as e:
            self.logger.warning(f"Primary store duplicate check failed: {e}")

            # Fall back to in-memory store
            try:
                return await self._fallback_store.check_and_set(key, ttl)
            except Exception as fallback_e:
                self.logger.error(f"Fallback store also failed: {fallback_e}")
                return False, None
    
    async def _cache_response(self, request: Request, response: Response) -> None:
        """Cache response for future duplicate requests via IDeduplicationStore"""

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
            if hasattr(response, 'body'):
                response_content = response.body.decode('utf-8')
            else:
                return  # Can't cache without content

            # Store via IDeduplicationStore interface
            key = f"{request_hash}:response"
            ttl = config.get("ttl", self.settings.deduplication["default"].ttl)

            try:
                await self._store.set_value(key, response_content, ttl)
            except Exception:
                # Fall back to in-memory
                await self._fallback_store.set_value(key, response_content, ttl)

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
    
    def _create_duplicate_response(self, request: Request) -> JSONResponse:
        """Create response for duplicate request - MUST conform to AgentResponse schema"""

        # Extract session_id from request to maintain API contract
        session_id = self._extract_session_id(request)
        if not session_id:
            session_id = "session_unknown"

        # Return AgentResponse-compliant structure
        return JSONResponse(
            status_code=200,
            content={
                "schema_version": "3.1.0",
                "content": "I'm processing your request. This appears to be a recent request - if you need a fresh response, please wait a moment and try again.",
                "response_type": "ANSWER",
                "session_id": session_id,
                "view_state": {
                    "session_id": session_id,
                    "user": {
                        "user_id": "anonymous",
                        "email": "user@example.com",
                        "name": "User",
                        "created_at": to_json_compatible(datetime.now(timezone.utc))
                    },
                    "active_case": None,
                    "cases": [],
                    "messages": [],
                    "uploaded_data": [],
                    "show_case_selector": False,
                    "show_data_upload": True,
                    "loading_state": None
                },
                "sources": [{"type": "SYSTEM", "content": "Duplicate request detected", "metadata": {"type": "deduplication"}}],
                "plan": None
            }
        )
    
    def _create_duplicate_error_response(
        self,
        error: DuplicateRequestError,
        request: Request
    ) -> JSONResponse:
        """Create error response for duplicate request"""
        
        error_response = ProtectionErrorResponse.from_duplicate_error(error)
        
        self.logger.info(
            f"Duplicate request blocked: {request.url.path}, "
            f"session={self._extract_session_id(request)}, "
            f"ttl_remaining={error.ttl_remaining}s"
        )
        
        return JSONResponse(
            status_code=409,  # Conflict
            content=error_response.__dict__
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
            (current_avg * (total_requests - 1) + check_duration) / total_requests
        )
    
    async def get_metrics(self) -> Dict[str, Any]:
        """Get middleware metrics"""

        duplicate_rate = 0.0
        if self.metrics["requests_checked"] > 0:
            duplicate_rate = self.metrics["duplicates_found"] / self.metrics["requests_checked"]

        # Get store health via interface
        try:
            store_health = await self._store.health_check()
        except Exception:
            store_health = {"status": "unknown", "error": "health_check_failed"}

        return {
            "middleware_metrics": {
                **self.metrics,
                "duplicate_rate": duplicate_rate,
            },
            "store_health": store_health,
            "configuration": {
                "enabled": self.settings.deduplication_enabled,
                "fail_open": self.settings.fail_open_on_redis_error,
                "endpoint_configs": {
                    path: {"ttl": config["ttl"], "cache_responses": config["cache_responses"]}
                    for path, config in self.endpoint_configs.items()
                }
            }
        }