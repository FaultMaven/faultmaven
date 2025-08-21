"""
Unified logging middleware for FaultMaven API.

This middleware integrates with the new logging infrastructure (Phase 1 & 2)
using LoggingCoordinator for request-scoped coordination and the enhanced
logging configuration for structured output.

Enhanced with session context management to provide continuous user/session
context across requests within the same session.
"""

import json
import time
from typing import Callable, Optional
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from faultmaven.infrastructure.logging.coordinator import LoggingCoordinator
from faultmaven.infrastructure.logging.config import get_logger
from faultmaven.container import DIContainer


logger = get_logger(__name__)


class LoggingMiddleware(BaseHTTPMiddleware):
    """
    Unified logging middleware using the new logging infrastructure.
    
    This middleware:
    - Integrates with LoggingCoordinator for request-scoped coordination
    - Uses the enhanced logging configuration for structured output
    - Prevents duplicate logging through operation tracking
    - Provides correlation IDs for request tracing
    - Extracts and populates session/user context (ENHANCED)
    - Handles errors gracefully with proper context
    """
    
    def __init__(self, app):
        super().__init__(app)
        self.coordinator = LoggingCoordinator()
        logger.info("LoggingMiddleware initialized with session context management")
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Process request with unified logging coordination and session context.
        
        This method coordinates all request-level logging activities using
        the LoggingCoordinator to ensure single-point-of-truth for request
        context and prevent duplicate log entries.
        
        Enhanced with session context extraction and population for continuous
        user/session tracking across requests.
        """
        # Start coordinated request tracking
        start_time = time.time()
        
        # Extract session and business context from request
        session_id = await self._extract_session_id(request)
        user_id = None
        case_id = await self._extract_case_id(request)
        
        # Look up user_id from session if session_id is available
        if session_id:
            user_id = await self._get_user_id_from_session(session_id)
        
        # Initialize request context through coordinator with business context
        # HTTP-specific context goes in attributes dict
        http_context = {
            'method': request.method,
            'path': request.url.path,
            'client_ip': request.client.host if request.client else 'unknown',
            'user_agent': request.headers.get('user-agent', 'unknown'),
            'query_params': str(request.query_params)
        }
        
        # Create context with both business and HTTP context
        context = self.coordinator.start_request(
            session_id=session_id,
            user_id=user_id,
            case_id=case_id,
            attributes=http_context
        )
        
        # Request context is already set by LoggingCoordinator.start_request()
        
        # Log request start (coordinator ensures this happens only once)
        # Include session context in log message for better traceability
        session_info = f" [session: {session_id}]" if session_id else ""
        user_info = f" [user: {user_id}]" if user_id else ""
        
        # Reduce verbosity for heartbeat requests to prevent log spam
        is_heartbeat = request.url.path.endswith('/heartbeat')
        start_log_level = "debug" if is_heartbeat else "info"
        
        LoggingCoordinator.log_once(
            operation_key=f"request_start:{context.correlation_id}",
            logger=logger,
            level=start_log_level,
            message=f"Request started: {request.method} {request.url.path}{session_info}{user_info}",
            method=request.method,
            path=request.url.path,
            query_params=str(request.query_params),
            client_ip=context.attributes.get('client_ip', 'unknown'),
            user_agent=context.attributes.get('user_agent', 'unknown'),
            correlation_id=context.correlation_id,
            session_id=session_id,
            user_id=user_id,
            case_id=case_id,
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
            
            # Determine log level based on request type and status
            # Reduce verbosity for heartbeat 404s to prevent log spam
            is_heartbeat_404 = (
                request.url.path.endswith('/heartbeat') and 
                response.status_code == 404
            )
            log_level = "debug" if is_heartbeat_404 else "info"
            
            # Log completion (coordinator ensures this happens only once)
            LoggingCoordinator.log_once(
                operation_key=f"request_complete:{context.correlation_id}",
                logger=logger,
                level=log_level,
                message=f"Request completed: {request.method} {request.url.path}{session_info}{user_info} "
                       f"-> {response.status_code} in {duration:.3f}s",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_seconds=duration,
                response_size=response.headers.get('content-length', 'unknown'),
                correlation_id=context.correlation_id,
                session_id=session_id,
                user_id=user_id,
                case_id=case_id
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
                        message=f"Request failed: {request.method} {request.url.path}{session_info}{user_info} "
                               f"after {duration:.3f}s: {str(e)}",
                        method=request.method,
                        path=request.url.path,
                        duration_seconds=duration,
                        error=str(e),
                        error_type=type(e).__name__,
                        correlation_id=context.correlation_id,
                        session_id=session_id,
                        user_id=user_id,
                        case_id=case_id
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
    
    async def _extract_session_id(self, request: Request) -> Optional[str]:
        """
        Extract session_id from request using multiple sources with priority order.
        
        Priority:
        1. Header: X-Session-ID (preferred for API clients)
        2. Query parameter: session_id
        3. Request body: session_id field (using non-consuming method)
        
        Args:
            request: FastAPI request object
            
        Returns:
            session_id if found, None otherwise
        """
        try:
            # 1. Check header (preferred method)
            if session_id := request.headers.get("x-session-id"):
                return session_id
                
            # 2. Check query parameters
            if session_id := request.query_params.get("session_id"):
                return session_id
                
            # 3. Check request body for POST/PUT/PATCH requests
            if request.method in ["POST", "PUT", "PATCH"]:
                try:
                    # Use request.json() which handles body parsing correctly without consuming the stream
                    # This is the proper FastAPI way to access request body
                    data = await request.json()
                    if isinstance(data, dict) and (session_id := data.get("session_id")):
                        return session_id
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    # Invalid JSON, encoding, or empty body - continue without session context
                    pass
                    
        except Exception as e:
            # Log error but don't fail the request
            logger.warning(f"Failed to extract session_id: {e}")
            
        return None
    
    async def _extract_case_id(self, request: Request) -> Optional[str]:
        """
        Extract case_id from request headers or body.
        
        Args:
            request: FastAPI request object
            
        Returns:
            case_id if found, None otherwise
        """
        try:
            # Check header (support legacy X-Investigation-ID for compatibility)
            if case_id := request.headers.get("x-case-id"):
                return case_id
            legacy = request.headers.get("x-investigation-id")
            if legacy:
                return legacy
                
            # Check query parameters
            if case_id := request.query_params.get("case_id"):
                return case_id
            legacy_q = request.query_params.get("investigation_id")
            if legacy_q:
                return legacy_q
                
            # Check request body for POST/PUT/PATCH requests
            if request.method in ["POST", "PUT", "PATCH"]:
                try:
                    # Use request.json() which handles body parsing correctly without consuming the stream
                    data = await request.json()
                    if isinstance(data, dict) and (case_id := data.get("case_id")):
                        return case_id
                    if isinstance(data, dict) and (legacy_body := data.get("investigation_id")):
                        return legacy_body
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    # Invalid JSON, encoding, or empty body - continue without case context
                    pass
                    
        except Exception as e:
            logger.warning(f"Failed to extract case_id: {e}")
            
        return None
    
    async def _get_user_id_from_session(self, session_id: str) -> Optional[str]:
        """
        Look up user_id from session_id using SessionService.
        
        Uses graceful degradation - if session lookup fails, continues
        without user context rather than failing the request.
        
        Args:
            session_id: Session identifier
            
        Returns:
            user_id if found, None otherwise
        """
        try:
            # Get SessionService from a DI container instance.
            # Using DIContainer() allows tests to patch DIContainer.__new__ and inject a mock.
            container_instance = DIContainer()
            session_service = container_instance.get_session_service()
            
            # Look up session (non-validating to avoid exceptions)
            session = await session_service.get_session(session_id, validate=False)
            
            return session.user_id if session else None
            
        except Exception as e:
            # Graceful degradation - log warning but continue without user context
            logger.warning(f"Failed to lookup user_id for session {session_id}: {e}")
            return None