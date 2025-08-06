"""
Unified request coordination and logging management.

This module provides a centralized approach to request logging that prevents
duplicates across multiple middleware components.
"""

import time
import uuid
from typing import Dict, Optional, Any, Callable
from threading import Lock
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .logging_config import get_logger, set_request_id

logger = get_logger(__name__)

# Global request state management
request_state: ContextVar[Optional['RequestState']] = ContextVar('request_state', default=None)

@dataclass
class RequestState:
    """Centralized request state for coordination across middleware."""
    correlation_id: str
    start_time: float
    method: str
    path: str
    client_ip: str
    user_agent: str
    logged_start: bool = False
    logged_completion: bool = False
    logged_error: bool = False
    extra_context: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        self.request_signature = f"{self.method}:{self.path}:{self.client_ip}"

class RequestCoordinator:
    """Thread-safe request coordination to prevent duplicate logging."""
    
    def __init__(self):
        self._active_requests: Dict[str, RequestState] = {}
        self._lock = Lock()
    
    def start_request(self, request: Request) -> RequestState:
        """Start tracking a request and return its state."""
        correlation_id = str(uuid.uuid4())[:8]
        
        # Extract request details
        request_state = RequestState(
            correlation_id=correlation_id,
            start_time=time.time(),
            method=request.method,
            path=request.url.path,
            client_ip=request.client.host if request.client else 'unknown',
            user_agent=request.headers.get('user-agent', 'unknown')
        )
        
        with self._lock:
            if request_state.request_signature in self._active_requests:
                # Duplicate request detected
                existing = self._active_requests[request_state.request_signature]
                logger.warning(
                    f"DUPLICATE_REQUEST_DETECTED: Same request being processed twice! "
                    f"Original correlation_id: {existing.correlation_id}, "
                    f"New correlation_id: {correlation_id}, "
                    f"Request: {request_state.request_signature}"
                )
                return existing
            else:
                # New request
                self._active_requests[request_state.request_signature] = request_state
                return request_state
    
    def finish_request(self, request_signature: str, correlation_id: str):
        """Finish tracking a request."""
        with self._lock:
            if request_signature in self._active_requests:
                existing = self._active_requests[request_signature]
                if existing.correlation_id == correlation_id:
                    del self._active_requests[request_signature]
                else:
                    logger.warning(
                        f"TRACKING_MISMATCH: Tried to finish tracking with wrong correlation_id. "
                        f"Expected: {existing.correlation_id}, Got: {correlation_id}"
                    )

# Global coordinator instance
request_coordinator = RequestCoordinator()

class UnifiedRequestMiddleware(BaseHTTPMiddleware):
    """Unified middleware that coordinates all request-level logging."""
    
    def __init__(self, app):
        super().__init__(app)
        logger.info(f"UnifiedRequestMiddleware initialized: {id(self)}")
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Start coordinated request tracking
        request_state_obj = request_coordinator.start_request(request)
        
        # Set global context
        set_request_id(request_state_obj.correlation_id)
        request_state.set(request_state_obj)
        
        # Log request start only once
        if not request_state_obj.logged_start:
            logger.info(
                f"Request started: {request.method} {request.url.path}",
                extra={
                    'method': request.method,
                    'path': request.url.path,
                    'query_params': str(request.query_params),
                    'client_ip': request_state_obj.client_ip,
                    'user_agent': request_state_obj.user_agent,
                    'correlation_id': request_state_obj.correlation_id,
                    'request_signature': request_state_obj.request_signature,
                    'x_forwarded_for': request.headers.get('x-forwarded-for', 'none'),
                    'x_real_ip': request.headers.get('x-real-ip', 'none'),
                    'content_length': request.headers.get('content-length', 'none'),
                    'middleware_id': id(self)
                }
            )
            request_state_obj.logged_start = True
        
        try:
            # Process request
            response = await call_next(request)
            
            # Calculate duration
            duration = time.time() - request_state_obj.start_time
            
            # Log successful completion only once
            if not request_state_obj.logged_completion:
                logger.info(
                    f"Request completed: {request.method} {request.url.path} "
                    f"-> {response.status_code} in {duration:.3f}s",
                    extra={
                        'method': request.method,
                        'path': request.url.path,
                        'status_code': response.status_code,
                        'duration_seconds': duration,
                        'response_size': response.headers.get('content-length', 'unknown'),
                        'correlation_id': request_state_obj.correlation_id,
                        'request_signature': request_state_obj.request_signature
                    }
                )
                request_state_obj.logged_completion = True
            
            # Add correlation header to response
            response.headers['X-Correlation-ID'] = request_state_obj.correlation_id
            
            # Finish tracking
            request_coordinator.finish_request(request_state_obj.request_signature, request_state_obj.correlation_id)
            
            return response
            
        except Exception as e:
            # Calculate duration for failed requests
            duration = time.time() - request_state_obj.start_time
            
            # Log error only once
            if not request_state_obj.logged_error:
                logger.error(
                    f"Request failed: {request.method} {request.url.path} "
                    f"after {duration:.3f}s: {str(e)}",
                    extra={
                        'method': request.method,
                        'path': request.url.path,
                        'duration_seconds': duration,
                        'error': str(e),
                        'error_type': type(e).__name__,
                        'correlation_id': request_state_obj.correlation_id,
                        'request_signature': request_state_obj.request_signature
                    },
                    exc_info=True
                )
                request_state_obj.logged_error = True
            
            # Finish tracking even if it failed
            request_coordinator.finish_request(request_state_obj.request_signature, request_state_obj.correlation_id)
            
            # Re-raise the exception
            raise

def get_current_request_state() -> Optional[RequestState]:
    """Get the current request state from context."""
    return request_state.get()

def add_request_context(**kwargs):
    """Add context to the current request state."""
    current_state = get_current_request_state()
    if current_state:
        current_state.extra_context.update(kwargs) 