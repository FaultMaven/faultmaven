"""
Unified logging middleware for FaultMaven API.

This middleware integrates with the new logging infrastructure (Phase 1 & 2)
using LoggingCoordinator for request-scoped coordination and the enhanced
logging configuration for structured output.
"""

import time
from typing import Callable
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from faultmaven.infrastructure.logging.coordinator import LoggingCoordinator
from faultmaven.infrastructure.logging.config import get_logger
from faultmaven.infrastructure.logging_config import set_request_id


logger = get_logger(__name__)


class LoggingMiddleware(BaseHTTPMiddleware):
    """
    Unified logging middleware using the new logging infrastructure.
    
    This middleware:
    - Integrates with LoggingCoordinator for request-scoped coordination
    - Uses the enhanced logging configuration for structured output
    - Prevents duplicate logging through operation tracking
    - Provides correlation IDs for request tracing
    - Handles errors gracefully with proper context
    """
    
    def __init__(self, app):
        super().__init__(app)
        self.coordinator = LoggingCoordinator()
        logger.info("LoggingMiddleware initialized with new logging infrastructure")
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Process request with unified logging coordination.
        
        This method coordinates all request-level logging activities using
        the LoggingCoordinator to ensure single-point-of-truth for request
        context and prevent duplicate log entries.
        """
        # Start coordinated request tracking
        start_time = time.time()
        
        # Initialize request context through coordinator
        # HTTP-specific context goes in attributes dict
        http_context = {
            'method': request.method,
            'path': request.url.path,
            'client_ip': request.client.host if request.client else 'unknown',
            'user_agent': request.headers.get('user-agent', 'unknown'),
            'query_params': str(request.query_params)
        }
        context = self.coordinator.start_request(attributes=http_context)
        
        # Set global request ID context for correlation
        set_request_id(context.correlation_id)
        
        # Log request start (coordinator ensures this happens only once)
        LoggingCoordinator.log_once(
            operation_key=f"request_start:{context.correlation_id}",
            logger=logger,
            level="info",
            message=f"Request started: {request.method} {request.url.path}",
            method=request.method,
            path=request.url.path,
            query_params=str(request.query_params),
            client_ip=context.attributes.get('client_ip', 'unknown'),
            user_agent=context.attributes.get('user_agent', 'unknown'),
            correlation_id=context.correlation_id,
            x_forwarded_for=request.headers.get('x-forwarded-for', 'none'),
            x_real_ip=request.headers.get('x-real-ip', 'none'),
            content_length=request.headers.get('content-length', 'none')
        )
        
        try:
            # Process request
            response = await call_next(request)
            
            # Calculate duration
            duration = time.time() - start_time
            
            # Track performance in coordinator
            if context.performance_tracker:
                exceeds_threshold, threshold = context.performance_tracker.record_timing(
                    layer='api',
                    operation='request_processing',
                    duration=duration
                )
                
                # Log performance warning if needed
                if exceeds_threshold:
                    LoggingCoordinator.log_once(
                        operation_key=f"performance_warning:{context.correlation_id}",
                        logger=logger,
                        level="warning",
                        message=f"Slow request detected: {request.method} {request.url.path} "
                               f"took {duration:.3f}s (threshold: {threshold:.3f}s)",
                        duration_seconds=duration,
                        threshold_seconds=threshold,
                        correlation_id=context.correlation_id
                    )
            
            # Log successful completion (coordinator ensures this happens only once)
            LoggingCoordinator.log_once(
                operation_key=f"request_complete:{context.correlation_id}",
                logger=logger,
                level="info",
                message=f"Request completed: {request.method} {request.url.path} "
                       f"-> {response.status_code} in {duration:.3f}s",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_seconds=duration,
                response_size=response.headers.get('content-length', 'unknown'),
                correlation_id=context.correlation_id
            )
            
            # Add correlation header to response
            response.headers['X-Correlation-ID'] = context.correlation_id
            
            # Generate request summary through coordinator
            summary = self.coordinator.end_request()
            
            # Log final request summary
            if summary:
                logger.info(
                    f"Request summary: {summary['operations_logged']} operations logged, "
                    f"{summary['errors_encountered']} errors, "
                    f"{summary['performance_violations']} performance violations",
                    extra=summary
                )
            
            return response
            
        except Exception as e:
            # Calculate duration for failed requests
            duration = time.time() - start_time
            
            # Add error to context for cascade prevention
            if context.error_context:
                context.error_context.add_layer_error('api', e)
                
                # Only log if this layer should handle it (prevents cascade)
                if context.error_context.should_log_error('api'):
                    LoggingCoordinator.log_once(
                        operation_key=f"request_error:{context.correlation_id}",
                        logger=logger,
                        level="error",
                        message=f"Request failed: {request.method} {request.url.path} "
                               f"after {duration:.3f}s: {str(e)}",
                        method=request.method,
                        path=request.url.path,
                        duration_seconds=duration,
                        error=str(e),
                        error_type=type(e).__name__,
                        correlation_id=context.correlation_id
                    )
            
            # Generate request summary even for failed requests
            summary = self.coordinator.end_request()
            
            # Log final error summary
            if summary:
                logger.error(
                    f"Failed request summary: {summary['operations_logged']} operations logged, "
                    f"{summary['errors_encountered']} errors encountered",
                    extra=summary,
                    exc_info=True
                )
            
            # Re-raise the exception to maintain FastAPI error handling
            raise