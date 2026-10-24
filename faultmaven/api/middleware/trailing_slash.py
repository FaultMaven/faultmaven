"""Trailing slash middleware to prevent automatic redirects.

This middleware handles requests with trailing slashes by routing them
directly to the equivalent endpoint without trailing slashes, preventing
the 307 redirects that violate the API specification.
"""

import logging

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

logger = logging.getLogger(__name__)


class TrailingSlashMiddleware(BaseHTTPMiddleware):
    """Middleware to handle trailing slash requests without redirects.

    This middleware intercepts requests with trailing slashes and routes them
    directly to the equivalent endpoint without trailing slashes, preventing
    automatic 307 redirects from FastAPI/Starlette.
    """

    async def dispatch(self, request: Request, call_next):
        """Handle the request and remove trailing slashes if present.

        Args:
            request: The incoming HTTP request
            call_next: The next middleware or endpoint handler

        Returns:
            Response: The response from the handler or a 404 if no match
        """
        # Check if the path ends with a trailing slash (but not just "/").
        # The ASGI scope's own values are edited, each in its own form:
        # ``path`` is decoded and ``raw_path`` is the percent-encoded bytes.
        # ``request.url.path`` is neither (Starlette re-encodes it), so a path
        # rebuilt from it reached the route double-encoded (``a%5Cb``, not
        # ``a\b``).
        path = request.scope["path"]
        if path.endswith("/") and len(path) > 1:
            new_path = path.rstrip("/")
            request.scope["path"] = new_path
            raw_path = request.scope.get("raw_path")
            if raw_path:
                request.scope["raw_path"] = raw_path.rstrip(b"/")

            logger.debug(f"Trailing slash middleware: {path} -> {new_path}")

        # Proceed with the modified request
        try:
            response = await call_next(request)
            return response
        except Exception as e:
            # Log the error cleanly without trying to serialize exception objects
            logger.error(
                f"Error in trailing slash middleware: {type(e).__name__}: {str(e)}",
                exc_info=False,  # Avoid serialization issues with exception objects
            )
            # Re-raise to let FastAPI handle it properly
            raise
