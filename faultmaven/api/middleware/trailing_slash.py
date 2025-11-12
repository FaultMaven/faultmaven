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
        # Check if the path ends with a trailing slash (but not just "/")
        if request.url.path.endswith("/") and len(request.url.path) > 1:
            # Remove the trailing slash
            new_path = request.url.path.rstrip("/")
            
            # Create a new request with the modified path
            # We need to modify the request's path_info and URL
            request.scope["path"] = new_path
            request.scope["path_info"] = new_path
            
            # Update the raw path as well for consistency
            if "raw_path" in request.scope:
                # Convert to bytes if it's a string
                if isinstance(request.scope["raw_path"], str):
                    request.scope["raw_path"] = new_path.encode('utf-8')
                else:
                    request.scope["raw_path"] = new_path.encode('utf-8')
            
            logger.debug(f"Trailing slash middleware: {request.url.path} -> {new_path}")
        
        # Proceed with the modified request
        try:
            response = await call_next(request)
            return response
        except Exception as e:
            # Log the error cleanly without trying to serialize exception objects
            logger.error(
                f"Error in trailing slash middleware: {type(e).__name__}: {str(e)}",
                exc_info=False  # Avoid serialization issues with exception objects
            )
            # Re-raise to let FastAPI handle it properly
            raise