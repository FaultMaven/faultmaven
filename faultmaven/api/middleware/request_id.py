"""Request Correlation Middleware

Purpose: Generate and track X-Correlation-ID for request correlation and rate limiting headers

This middleware:
- Generates unique X-Correlation-ID for each request (unified header name per Issue 23)
- Also accepts X-Request-ID for backwards compatibility
- Adds rate limiting headers (X-RateLimit-Remaining, Retry-After)
- Integrates with logging system for correlation tracking
- Follows FastAPI middleware patterns

Architecture Integration:
- Works with existing logging system for correlation IDs
- Integrates with protection middleware for rate limiting
- Supports observability and tracing requirements

Note: X-Correlation-ID is the canonical header. X-Request-ID is accepted for
backwards compatibility but X-Correlation-ID takes precedence.
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
    """Middleware to add X-Correlation-ID and rate limiting headers.

    Uses X-Correlation-ID as the canonical correlation header (Issue 23).
    Accepts X-Request-ID for backwards compatibility.
    """

    # Canonical header name (Issue 23: unified correlation ID)
    CORRELATION_HEADER = "X-Correlation-ID"
    # Legacy header for backwards compatibility
    LEGACY_HEADER = "X-Request-ID"

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        """Process request with correlation ID generation and header addition."""

        # Extract correlation ID: prefer X-Correlation-ID, fallback to X-Request-ID
        correlation_id = request.headers.get(self.CORRELATION_HEADER)
        if not correlation_id:
            correlation_id = request.headers.get(self.LEGACY_HEADER)
        if not correlation_id:
            correlation_id = str(uuid4())

        # Store correlation ID in request state for access by other components
        # Note: We use request.state instead of mutating headers, as Starlette's
        # Headers object is immutable and mutating internal _list is fragile.
        request.state.correlation_id = correlation_id
        # Also set as request_id for backwards compatibility
        request.state.request_id = correlation_id

        # Process request
        start_time = time.time()
        response = await call_next(request)
        processing_time = time.time() - start_time

        # Add response headers - use canonical X-Correlation-ID
        response.headers[self.CORRELATION_HEADER] = correlation_id
        # Also set X-Request-ID for backwards compatibility
        response.headers[self.LEGACY_HEADER] = correlation_id
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
            f"[{correlation_id}]"
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