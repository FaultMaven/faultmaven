"""Request ID Middleware

Purpose: Generate and track X-Request-ID for request correlation

This middleware:
- Generates a unique X-Request-ID for each request
- Binds it into structlog contextvars so every log line correlates
- Echoes it back to the caller alongside a processing-time header

It deliberately produces **no** rate-limit headers. Rate limiting has exactly
one header authority — the component that performed the enforcement
(``api/middleware/rate_limiting.RateLimitMiddleware`` for the general limits,
the OAuth limiter dependencies for the auth endpoints). A second writer can
only ever agree by coincidence, and when it disagrees the client believes the
outer one. See ``docs/architecture/security/rate-limiting-sliding-window.md``.

Architecture Integration:
- Works with existing logging system for correlation IDs
- Supports observability and tracing requirements
"""

import logging
import time
from uuid import uuid4

import structlog
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Middleware to add X-Request-ID for request correlation."""

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        """Process request with ID generation and header addition."""

        # Generate or extract request ID
        request_id = request.headers.get("X-Request-ID")
        if not request_id:
            request_id = str(uuid4())

        # Store request ID in request state for access by other components
        # Note: We use request.state instead of mutating headers, as Starlette's
        # Headers object is immutable and mutating internal _list is fragile.
        request.state.request_id = request_id

        # Bind into structlog contextvars so every log line emitted while
        # handling this request carries request_id (merge_contextvars is the
        # first processor in the structlog chain — see logging/config.py).
        structlog.contextvars.bind_contextvars(request_id=request_id)

        # Process request
        start_time = time.time()
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        processing_time = time.time() - start_time

        # Add response headers
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Processing-Time"] = f"{processing_time:.3f}s"

        # No rate-limit headers here — not even a default on a 429. This layer
        # has no idea which limiter refused the request or how long its window
        # takes to free quota, so anything it wrote would be a guess overwriting
        # a measurement.

        # Log request completion with correlation
        logger.debug(
            f"Request completed: {request.method} {request.url.path} "
            f"-> {response.status_code} ({processing_time:.3f}s) "
            f"[{request_id}]"
        )

        return response


def create_request_id_middleware(app: ASGIApp) -> RequestIdMiddleware:
    """Factory function to create request ID middleware."""
    return RequestIdMiddleware(app)
