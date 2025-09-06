"""Request ID Middleware

Purpose: Generate and track X-Request-ID for request correlation and rate limiting headers

This middleware:
- Generates unique X-Request-ID for each request
- Adds rate limiting headers (X-RateLimit-Remaining, Retry-After)
- Integrates with logging system for correlation tracking
- Follows FastAPI middleware patterns

Architecture Integration:
- Works with existing logging system for correlation IDs
- Integrates with protection middleware for rate limiting
- Supports observability and tracing requirements
"""

import logging
import time
from typing import Optional
from uuid import uuid4

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Middleware to add X-Request-ID and rate limiting headers."""
    
    def __init__(self, app: ASGIApp):
        super().__init__(app)
        
    async def dispatch(self, request: Request, call_next):
        """Process request with ID generation and header addition."""
        
        # Generate or extract request ID
        request_id = request.headers.get("X-Request-ID")
        if not request_id:
            request_id = str(uuid4())
            
        # Store request ID in request state for access by other components
        request.state.request_id = request_id
        
        # Add to request headers for downstream processing
        request.headers.__dict__["_list"].append((b"x-request-id", request_id.encode()))
        
        # Process request
        start_time = time.time()
        response = await call_next(request)
        processing_time = time.time() - start_time
        
        # Add response headers
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Processing-Time"] = f"{processing_time:.3f}s"
        
        # Add rate limiting headers if available from protection middleware
        if hasattr(request.state, "rate_limit_remaining"):
            response.headers["X-RateLimit-Remaining"] = str(request.state.rate_limit_remaining)
            
        if hasattr(request.state, "rate_limit_reset"):
            response.headers["X-RateLimit-Reset"] = str(request.state.rate_limit_reset)
            
        # Add Retry-After header for 429 responses
        if response.status_code == 429:
            if hasattr(request.state, "retry_after"):
                response.headers["Retry-After"] = str(request.state.retry_after)
            else:
                # Default retry after 60 seconds
                response.headers["Retry-After"] = "60"
                
        # Log request completion with correlation
        logger.debug(
            f"Request completed: {request.method} {request.url.path} "
            f"-> {response.status_code} ({processing_time:.3f}s) "
            f"[{request_id}]"
        )
        
        return response


class RateLimitHeaderMiddleware(BaseHTTPMiddleware):
    """Enhanced middleware specifically for rate limiting headers."""
    
    def __init__(self, app: ASGIApp, default_limit: int = 1000, window_seconds: int = 3600):
        super().__init__(app)
        self.default_limit = default_limit
        self.window_seconds = window_seconds
        
    async def dispatch(self, request: Request, call_next):
        """Add rate limiting information to requests."""
        
        # Set default rate limit info if not set by protection middleware
        if not hasattr(request.state, "rate_limit_remaining"):
            current_time = int(time.time())
            request.state.rate_limit_remaining = self.default_limit - 1
            request.state.rate_limit_reset = current_time + self.window_seconds
            request.state.retry_after = 60
            
        response = await call_next(request)
        
        # Always add rate limit headers
        response.headers["X-RateLimit-Limit"] = str(self.default_limit)
        response.headers["X-RateLimit-Window"] = f"{self.window_seconds}s"
        
        return response


def create_request_id_middleware(app: ASGIApp) -> RequestIdMiddleware:
    """Factory function to create request ID middleware."""
    return RequestIdMiddleware(app)


def create_rate_limit_header_middleware(
    app: ASGIApp, 
    default_limit: int = 1000, 
    window_seconds: int = 3600
) -> RateLimitHeaderMiddleware:
    """Factory function to create rate limit header middleware."""
    return RateLimitHeaderMiddleware(app, default_limit, window_seconds)