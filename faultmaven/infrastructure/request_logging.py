"""
Simple middleware for request correlation and logging.
"""

import time
import uuid
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .logging_config import set_request_id, get_logger

logger = get_logger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Simple middleware for request correlation and basic logging."""
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Generate correlation ID
        correlation_id = str(uuid.uuid4())[:8]
        set_request_id(correlation_id)
        
        # Start timing
        start_time = time.time()
        
        # Log request start
        logger.info(f"Request started: {request.method} {request.url.path}")
        
        try:
            # Process request
            response = await call_next(request)
            
            # Calculate duration
            duration = time.time() - start_time
            
            # Log successful request
            logger.info(
                f"Request completed: {request.method} {request.url.path} "
                f"-> {response.status_code} in {duration:.3f}s"
            )
            
            # Add correlation header to response
            response.headers['X-Correlation-ID'] = correlation_id
            
            return response
            
        except Exception as e:
            # Calculate duration for failed requests
            duration = time.time() - start_time
            
            # Log failed request
            logger.error(
                f"Request failed: {request.method} {request.url.path} "
                f"after {duration:.3f}s: {str(e)}"
            )
            
            # Re-raise the exception
            raise