"""
Request deduplication middleware

FastAPI middleware for detecting and preventing duplicate requests
within configured time windows using content-based hashing.
"""

import time
import logging
import json
from typing import Callable, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from ...models.protection import (
    ProtectionSettings,
    DuplicateRequestError,
    ProtectionErrorResponse
)
from ...infrastructure.protection import RequestHasher


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
        self,
        app,
        settings: ProtectionSettings,
        redis_url: Optional[str] = None
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
            "/api/v1/agent/query": {
                "ttl": self.settings.deduplication.get("agent_query", self.settings.deduplication["default"]).ttl,
                "cache_responses": False,
                "special_handler": self._handle_agent_query
            },
            "/api/v1/agent/troubleshoot": {
                "ttl": self.settings.deduplication.get("agent_query", self.settings.deduplication["default"]).ttl,
                "cache_responses": False,
                "special_handler": self._handle_agent_query
            },
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
            is_duplicate, cached_response = await self._check_duplicate(request)
            
            if is_duplicate:
                check_duration = time.time() - start_time
                self._update_metrics(check_duration, duplicate_found=True)
                
                if cached_response:
                    self.logger.debug(f"Returning cached response for duplicate request")
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
            self.logger.error(f"Deduplication error: {e}")
            self.metrics["errors"] += 1
            
            # Fail open
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
    
    async def _initialize(self) -> None:
        """Initialize Redis connection"""
        try:
            self._redis = aioredis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )
            
            await self._redis.ping()
            self._redis_healthy = True
            self.logger.info("Request deduplication middleware initialized")
            self._initialized = True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize deduplication Redis: {e}")
            self._redis_healthy = False
            self._initialized = True  # Continue with fallback
            
            if not self.settings.fail_open_on_redis_error:
                raise
    
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
    
    async def _handle_agent_query(
        self,
        request: Request,
        session_id: str,
        body: Optional[str]
    ) -> Optional[str]:
        """Special handling for agent query requests"""
        
        try:
            # Check if this is title generation
            if body and await self._is_title_generation_request(body):
                # Use specialized title generation hash
                return self.hasher.hash_title_generation_request(
                    session_id=session_id,
                    conversation_context=self._extract_conversation_context(body)
                )
            
            # Regular agent query hash
            return self.hasher.hash_request(
                session_id=session_id,
                endpoint=request.url.path,
                method=request.method,
                body=body,
                query_params=dict(request.query_params),
                headers=dict(request.headers)
            )
            
        except Exception as e:
            self.logger.error(f"Agent query hash generation failed: {e}")
            return None
    
    async def _is_title_generation_request(self, body: str) -> bool:
        """Check if request is for title generation"""
        
        if not body:
            return False
        
        body_lower = body.lower()
        title_indicators = [
            "generate a title",
            "title generation", 
            "conversation title",
            "is_title_generation",
            "concise, descriptive title",
            "3-6 words"
        ]
        
        return any(indicator in body_lower for indicator in title_indicators)
    
    def _extract_conversation_context(self, body: str) -> Optional[str]:
        """Extract conversation context from request body"""
        
        try:
            if body.strip().startswith('{'):
                data = json.loads(body)
                
                # Look for conversation or context fields
                context_fields = ["context", "conversation", "history", "messages"]
                for field in context_fields:
                    if field in data:
                        return str(data[field])
                
                # Check if there's meaningful query content
                query = data.get("query", "")
                if len(query) > 10:  # Non-trivial query
                    return "has_content"
            
        except Exception:
            pass
        
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
        """Check if hash represents a duplicate request"""
        
        # Get TTL for this endpoint
        config = self.endpoint_configs.get(endpoint, {})
        ttl = config.get("ttl", self.settings.deduplication["default"].ttl)
        
        key = f"{self.redis_key_prefix}:{request_hash}"
        
        try:
            if self._redis and self._redis_healthy:
                return await self._check_redis_duplicate(key, ttl)
            else:
                return await self._check_fallback_duplicate(key, ttl)
                
        except Exception as e:
            self.logger.error(f"Duplicate check failed: {e}")
            return False, None
    
    async def _check_redis_duplicate(
        self,
        key: str,
        ttl: int
    ) -> Tuple[bool, Optional[str]]:
        """Check for duplicate using Redis"""
        
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
        
        current_time = datetime.utcnow().isoformat() + 'Z'
        
        try:
            result = await self._redis.eval(
                lua_script,
                1,  # number of keys
                key,
                ttl,
                current_time
            )
            
            is_duplicate, cached_data = result
            
            if is_duplicate:
                self.logger.debug(f"Duplicate request detected: {key}")
                return True, cached_data
            
            return False, None
            
        except RedisError as e:
            self.logger.warning(f"Redis duplicate check failed: {e}")
            self._redis_healthy = False
            return await self._check_fallback_duplicate(key, ttl)
    
    async def _check_fallback_duplicate(
        self,
        key: str,
        ttl: int
    ) -> Tuple[bool, Optional[str]]:
        """Check for duplicate using in-memory store"""
        
        current_time = time.time()
        
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
                return True, cached_response
            else:
                # Expired, remove it
                del self._fallback_store[key]
        
        # Store new entry
        self._fallback_store[key] = (current_time, None)
        return False, None
    
    async def _cleanup_fallback_store(self) -> None:
        """Clean up expired entries from fallback store"""
        
        current_time = time.time()
        expired_keys = []
        
        for key, (timestamp, _) in self._fallback_store.items():
            # Use max TTL for cleanup (conservative approach)
            max_ttl = max(config.get("ttl", 300) for config in self.endpoint_configs.values())
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
            if hasattr(response, 'body'):
                response_content = response.body.decode('utf-8')
            else:
                return  # Can't cache without content
            
            # Store in Redis or fallback
            key = f"{self.redis_key_prefix}:{request_hash}"
            ttl = config.get("ttl", self.settings.deduplication["default"].ttl)
            
            if self._redis and self._redis_healthy:
                await self._redis.setex(
                    f"{key}:response",
                    ttl,
                    response_content
                )
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
    
    def _create_duplicate_response(self, request: Request) -> JSONResponse:
        """Create response for duplicate request"""
        
        # Create a polite response that doesn't reveal duplicate detection
        return JSONResponse(
            status_code=200,
            content={
                "message": "Request processed successfully",
                "note": "This appears to be a recent request. If you need a fresh response, please wait a moment and try again.",
                "timestamp": datetime.utcnow().isoformat() + 'Z'
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
        
        return {
            "middleware_metrics": {
                **self.metrics,
                "duplicate_rate": duplicate_rate,
                "fallback_entries": len(self._fallback_store)
            },
            "redis_health": {
                "healthy": self._redis_healthy,
                "initialized": self._initialized
            },
            "configuration": {
                "enabled": self.settings.deduplication_enabled,
                "fail_open": self.settings.fail_open_on_redis_error,
                "endpoint_configs": {
                    path: {"ttl": config["ttl"], "cache_responses": config["cache_responses"]}
                    for path, config in self.endpoint_configs.items()
                }
            }
        }