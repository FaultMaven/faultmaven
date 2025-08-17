"""
Rate limiting middleware

FastAPI middleware for multi-level rate limiting with Redis backend,
progressive penalties, and graceful degradation.
"""

import time
import logging
from typing import Callable, Dict, Any, Optional
from datetime import datetime

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ...models.protection import (
    ProtectionSettings,
    LimitType,
    RateLimitError,
    ProtectionErrorResponse
)
from ...infrastructure.protection import RedisRateLimiter


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Multi-level rate limiting middleware
    
    Features:
    - Global, per-session, and per-endpoint rate limits
    - Progressive penalties for repeated violations
    - Redis-backed with in-memory fallback
    - Detailed metrics and logging
    - Security headers in responses
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
        
        # Initialize rate limiter
        effective_redis_url = redis_url or settings.redis_url
        self.rate_limiter = RedisRateLimiter(
            redis_url=effective_redis_url,
            key_prefix=f"{settings.redis_key_prefix}:rl",
            fallback_enabled=settings.fail_open_on_redis_error
        )
        
        # Configure rate limits
        self.rate_limiter.configure_limits(settings.rate_limits)
        
        # Endpoint-specific configurations
        self.endpoint_configs = {
            "/api/v1/agent/query": {
                "limit_types": [LimitType.PER_SESSION, LimitType.GLOBAL],
                "special_handling": self._handle_agent_query
            },
            "/api/v1/agent/troubleshoot": {
                "limit_types": [LimitType.PER_SESSION, LimitType.GLOBAL],
                "special_handling": self._handle_agent_query
            },
            "/api/v1/data/upload": {
                "limit_types": [LimitType.PER_SESSION, LimitType.GLOBAL],
                "special_handling": None
            },
            "/api/v1/sessions/": {
                "limit_types": [LimitType.PER_SESSION, LimitType.GLOBAL],
                "special_handling": None
            }
        }
        
        # Metrics tracking
        self.metrics = {
            "requests_checked": 0,
            "requests_blocked": 0,
            "errors": 0,
            "avg_check_duration": 0.0
        }
        
        # Initialize rate limiter
        self._initialized = False
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Main middleware dispatch with rate limiting"""
        
        start_time = time.time()
        
        try:
            # Initialize rate limiter if needed
            if not self._initialized:
                await self._initialize()
            
            # Skip rate limiting if disabled
            if not self.settings.rate_limiting_enabled:
                return await call_next(request)
            
            # Check for bypass headers (development/testing)
            if self._should_bypass(request):
                self.logger.debug("Rate limiting bypassed via header")
                return await call_next(request)
            
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
            # Unexpected error
            self.logger.error(f"Rate limiting error: {e}")
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
                        "message": "Rate limiting service temporarily unavailable"
                    }
                )
    
    async def _initialize(self) -> None:
        """Initialize rate limiter connection"""
        try:
            await self.rate_limiter.initialize()
            self._initialized = True
            self.logger.info("Rate limiting middleware initialized")
        except Exception as e:
            self.logger.error(f"Failed to initialize rate limiter: {e}")
            if not self.settings.fail_open_on_redis_error:
                raise
    
    def _should_bypass(self, request: Request) -> bool:
        """Check if request should bypass rate limiting"""
        
        # Check bypass headers
        for header in self.settings.protection_bypass_headers:
            if header in request.headers:
                return True
        
        # Health check endpoints
        if request.url.path.startswith("/health"):
            return True
        
        # Static assets
        if request.url.path.startswith("/static"):
            return True
        
        return False
    
    async def _check_rate_limits(self, request: Request) -> None:
        """Perform all applicable rate limit checks"""
        
        session_id = self._extract_session_id(request)
        endpoint = request.url.path
        client_ip = self._get_client_ip(request)
        
        # Always check global rate limit
        await self._check_global_rate_limit(client_ip)
        
        # Check session-based limits if session available
        if session_id:
            await self._check_session_rate_limits(session_id, endpoint, request)
        
        # Check endpoint-specific limits
        await self._check_endpoint_rate_limits(endpoint, session_id, request)
    
    async def _check_global_rate_limit(self, client_ip: str) -> None:
        """Check global rate limit"""
        
        result = await self.rate_limiter.check_rate_limit(
            key=client_ip,
            limit_type=LimitType.GLOBAL,
            identifier=f"global:{client_ip}"
        )
        
        if not result.allowed:
            raise RateLimitError(
                retry_after=result.retry_after or 60,
                limit_type="global",
                current_count=result.current_count,
                limit=result.limit
            )
    
    async def _check_session_rate_limits(
        self,
        session_id: str,
        endpoint: str,
        request: Request
    ) -> None:
        """Check session-based rate limits"""
        
        # Per-session per-minute limit
        result = await self.rate_limiter.check_rate_limit(
            key=session_id,
            limit_type=LimitType.PER_SESSION,
            identifier=f"session:{session_id}"
        )
        
        if not result.allowed:
            raise RateLimitError(
                retry_after=result.retry_after or 60,
                limit_type="per_session",
                current_count=result.current_count,
                limit=result.limit
            )
        
        # Per-session hourly limit
        result = await self.rate_limiter.check_rate_limit(
            key=session_id,
            limit_type=LimitType.PER_SESSION_HOURLY,
            identifier=f"session_hourly:{session_id}"
        )
        
        if not result.allowed:
            raise RateLimitError(
                retry_after=result.retry_after or 3600,
                limit_type="per_session_hourly",
                current_count=result.current_count,
                limit=result.limit
            )
    
    async def _check_endpoint_rate_limits(
        self,
        endpoint: str,
        session_id: Optional[str],
        request: Request
    ) -> None:
        """Check endpoint-specific rate limits"""
        
        # Check if this endpoint has special rate limiting
        config = self.endpoint_configs.get(endpoint)
        if not config:
            return
        
        # Special handling for specific endpoints
        if config.get("special_handling"):
            await config["special_handling"](request, session_id)
    
    async def _handle_agent_query(
        self,
        request: Request,
        session_id: Optional[str]
    ) -> None:
        """Special handling for agent query endpoints"""
        
        if not session_id:
            return
        
        # Check if this is a title generation request
        if await self._is_title_generation_request(request):
            result = await self.rate_limiter.check_rate_limit(
                key=session_id,
                limit_type=LimitType.TITLE_GENERATION,
                identifier=f"title_gen:{session_id}"
            )
            
            if not result.allowed:
                raise RateLimitError(
                    retry_after=result.retry_after or 300,
                    limit_type="title_generation",
                    current_count=result.current_count,
                    limit=result.limit
                )
    
    async def _is_title_generation_request(self, request: Request) -> bool:
        """Detect if request is for title generation"""
        
        try:
            # Check request body for title generation indicators
            if hasattr(request, '_body'):
                body = request._body
            else:
                # Read body (this consumes it, so we need to be careful)
                body = await request.body()
                # Store it for later use
                request._body = body
            
            if body:
                body_str = body.decode('utf-8').lower()
                title_indicators = [
                    "generate a title",
                    "title generation",
                    "conversation title",
                    "is_title_generation",
                    "concise, descriptive title"
                ]
                
                return any(indicator in body_str for indicator in title_indicators)
            
        except Exception as e:
            self.logger.warning(f"Failed to check for title generation: {e}")
        
        return False
    
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
        """Get client IP address for global rate limiting"""
        
        # Check for forwarded headers (behind proxy)
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            # Take the first IP (original client)
            return forwarded_for.split(",")[0].strip()
        
        # Check other proxy headers
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip
        
        # Fall back to direct connection
        if hasattr(request, 'client') and request.client:
            return request.client.host
        
        return "unknown"
    
    async def _add_rate_limit_headers(self, request: Request, response: Response) -> None:
        """Add rate limit information to response headers"""
        
        try:
            session_id = self._extract_session_id(request)
            if session_id:
                # Get current rate limit status
                status = await self.rate_limiter.get_rate_limit_status(
                    session_id, LimitType.PER_SESSION
                )
                
                if status:
                    response.headers["X-RateLimit-Limit"] = str(status.limit)
                    response.headers["X-RateLimit-Remaining"] = str(
                        max(0, status.limit - status.current_count)
                    )
                    response.headers["X-RateLimit-Reset"] = str(
                        int(status.reset_time.timestamp())
                    )
        
        except Exception as e:
            self.logger.debug(f"Failed to add rate limit headers: {e}")
    
    def _create_rate_limit_response(
        self,
        error: RateLimitError,
        request: Request
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
        response = JSONResponse(
            status_code=429,
            content=error_response.__dict__
        )
        
        # Add rate limit headers
        response.headers["Retry-After"] = str(error.retry_after)
        response.headers["X-RateLimit-Limit"] = str(error.limit)
        response.headers["X-RateLimit-Remaining"] = "0"
        
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
            (current_avg * (total_requests - 1) + check_duration) / total_requests
        )
    
    async def get_metrics(self) -> Dict[str, Any]:
        """Get middleware metrics"""
        
        # Get rate limiter health
        rate_limiter_health = await self.rate_limiter.health_check()
        
        return {
            "middleware_metrics": self.metrics.copy(),
            "rate_limiter_health": rate_limiter_health,
            "rate_limiter_stats": self.rate_limiter.get_timeout_statistics()
            if hasattr(self.rate_limiter, 'get_timeout_statistics') else {},
            "configuration": {
                "enabled": self.settings.rate_limiting_enabled,
                "fail_open": self.settings.fail_open_on_redis_error,
                "configured_limits": len(self.settings.rate_limits)
            }
        }