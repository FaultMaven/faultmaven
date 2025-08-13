"""
Performance Tracking Middleware

Provides request-level performance tracking with minimal overhead
and integration with the monitoring framework.
"""

import time
import logging
from typing import Dict, Any, Optional
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from datetime import datetime

from ...infrastructure.monitoring.metrics_collector import metrics_collector
from ...infrastructure.monitoring.apm_integration import apm_integration
from ...infrastructure.monitoring.alerting import alert_manager
from ...infrastructure.logging.coordinator import LoggingCoordinator


class PerformanceTrackingMiddleware(BaseHTTPMiddleware):
    """Middleware for tracking request performance with minimal overhead."""
    
    def __init__(self, app, service_name: str = "faultmaven_api"):
        """Initialize performance tracking middleware.
        
        Args:
            app: FastAPI application
            service_name: Service name for metrics
        """
        super().__init__(app)
        self.service_name = service_name
        self.logger = logging.getLogger(__name__)
        
        # Performance thresholds for different endpoint types
        self.endpoint_thresholds = {
            "/health": 100.0,  # Health checks should be very fast
            "/health/": 100.0,
            "/api/v1/agent": 5000.0,  # Agent endpoints can be slower
            "/api/v1/data": 2000.0,  # Data endpoints moderate
            "/api/v1/knowledge": 3000.0,  # Knowledge endpoints moderate
            "/api/v1/session": 500.0,  # Session endpoints should be fast
        }
        
        # Track request statistics
        self.request_count = 0
        self.total_duration = 0.0
        self.error_count = 0
        
    async def dispatch(self, request: Request, call_next):
        """Process request with performance tracking.
        
        Args:
            request: Incoming HTTP request
            call_next: Next middleware in chain
            
        Returns:
            HTTP response with performance headers
        """
        # Start timing
        start_time = time.time()
        start_timestamp = datetime.utcnow()
        
        # Generate unique request ID for correlation
        request_id = f"req_{int(start_time * 1000000)}"
        
        # Extract request information
        method = request.method
        path = request.url.path
        user_agent = request.headers.get("user-agent", "unknown")
        client_ip = self._get_client_ip(request)
        
        # Determine endpoint category for thresholds
        endpoint_category = self._categorize_endpoint(path)
        expected_threshold = self._get_threshold_for_endpoint(path)
        
        # Initialize request context if not exists
        coordinator = LoggingCoordinator()
        request_context = coordinator.get_context()
        
        # Track request start
        self.request_count += 1
        
        response = None
        error = None
        status_code = 500
        
        try:
            # Process request
            response = await call_next(request)
            status_code = response.status_code
            
        except Exception as e:
            error = e
            status_code = 500
            self.error_count += 1
            self.logger.error(f"Request {request_id} failed: {e}")
            
            # Create error response
            from fastapi.responses import JSONResponse
            response = JSONResponse(
                status_code=500,
                content={"detail": "Internal server error", "request_id": request_id}
            )
        
        finally:
            # Calculate timing
            end_time = time.time()
            duration_ms = (end_time - start_time) * 1000
            self.total_duration += duration_ms
            
            # Determine if request was successful
            is_success = 200 <= status_code < 400 and error is None
            is_error = status_code >= 400 or error is not None
            
            # Record performance metrics
            await self._record_performance_metrics(
                request_id=request_id,
                method=method,
                path=path,
                endpoint_category=endpoint_category,
                duration_ms=duration_ms,
                status_code=status_code,
                is_success=is_success,
                client_ip=client_ip,
                user_agent=user_agent,
                expected_threshold=expected_threshold
            )
            
            # Add performance headers to response
            if response:
                response.headers["X-Request-ID"] = request_id
                response.headers["X-Response-Time"] = f"{duration_ms:.2f}ms"
                response.headers["X-Performance-Category"] = endpoint_category
                
                # Add performance warning if slow
                if duration_ms > expected_threshold:
                    response.headers["X-Performance-Warning"] = "slow_response"
        
        return response
    
    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP address from request.
        
        Args:
            request: HTTP request
            
        Returns:
            Client IP address
        """
        # Check for forwarded headers first
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip
        
        # Fall back to direct client
        if hasattr(request, "client") and request.client:
            return request.client.host
        
        return "unknown"
    
    def _categorize_endpoint(self, path: str) -> str:
        """Categorize endpoint for performance tracking.
        
        Args:
            path: Request path
            
        Returns:
            Endpoint category
        """
        if path.startswith("/health"):
            return "health"
        elif path.startswith("/api/v1/agent"):
            return "agent"
        elif path.startswith("/api/v1/data"):
            return "data"
        elif path.startswith("/api/v1/knowledge"):
            return "knowledge"
        elif path.startswith("/api/v1/session"):
            return "session"
        elif path.startswith("/docs") or path.startswith("/redoc"):
            return "docs"
        elif path.startswith("/"):
            return "root"
        else:
            return "other"
    
    def _get_threshold_for_endpoint(self, path: str) -> float:
        """Get performance threshold for endpoint.
        
        Args:
            path: Request path
            
        Returns:
            Expected response time threshold in milliseconds
        """
        # Check exact matches first
        for endpoint_prefix, threshold in self.endpoint_thresholds.items():
            if path.startswith(endpoint_prefix):
                return threshold
        
        # Default threshold
        return 1000.0
    
    async def _record_performance_metrics(
        self,
        request_id: str,
        method: str,
        path: str,
        endpoint_category: str,
        duration_ms: float,
        status_code: int,
        is_success: bool,
        client_ip: str,
        user_agent: str,
        expected_threshold: float
    ) -> None:
        """Record comprehensive performance metrics.
        
        Args:
            request_id: Unique request identifier
            method: HTTP method
            path: Request path
            endpoint_category: Categorized endpoint type
            duration_ms: Request duration in milliseconds
            status_code: HTTP status code
            is_success: Whether request was successful
            client_ip: Client IP address
            user_agent: User agent string
            expected_threshold: Expected response time threshold
        """
        try:
            # Record in metrics collector
            metrics_collector.record_performance_metric(
                component="api",
                operation=f"{method.lower()}_request",
                duration_ms=duration_ms,
                success=is_success,
                metadata={
                    "request_id": request_id,
                    "endpoint_category": endpoint_category,
                    "path": path,
                    "status_code": status_code,
                    "client_ip": client_ip,
                    "user_agent": user_agent[:100],  # Truncate user agent
                    "expected_threshold": expected_threshold,
                    "performance_ratio": duration_ms / expected_threshold
                },
                tags={
                    "method": method,
                    "endpoint": endpoint_category,
                    "status_class": f"{status_code // 100}xx",
                    "success": str(is_success).lower()
                }
            )
            
            # Record specific endpoint metrics
            metrics_collector.record_performance_metric(
                component=endpoint_category,
                operation="request",
                duration_ms=duration_ms,
                success=is_success,
                metadata={
                    "method": method,
                    "path": path,
                    "status_code": status_code
                },
                tags={
                    "method": method,
                    "status_class": f"{status_code // 100}xx"
                }
            )
            
            # Record APM metrics
            apm_integration.record_operation(
                operation_name=f"{method} {path}",
                duration_ms=duration_ms,
                status="success" if is_success else "error",
                tags={
                    "http.method": method,
                    "http.path": path,
                    "http.status_code": str(status_code),
                    "endpoint.category": endpoint_category
                },
                attributes={
                    "request_id": request_id,
                    "client_ip": client_ip,
                    "user_agent": user_agent,
                    "performance_threshold": expected_threshold,
                    "exceeded_threshold": duration_ms > expected_threshold
                }
            )
            
            # Check for performance alerts
            if duration_ms > expected_threshold:
                # Calculate how much the threshold was exceeded
                threshold_ratio = duration_ms / expected_threshold
                
                if threshold_ratio > 2.0:  # More than 2x expected time
                    alert_manager.evaluate_metric(
                        metric_name=f"{endpoint_category}.response_time",
                        metric_value=duration_ms,
                        metadata={
                            "request_id": request_id,
                            "path": path,
                            "method": method,
                            "threshold_ratio": threshold_ratio,
                            "client_ip": client_ip
                        }
                    )
            
            # Record error rate metrics
            error_rate_metric = 0.0 if is_success else 100.0
            metrics_collector.record_gauge_metric(
                metric_name=f"{endpoint_category}.error_rate",
                value=error_rate_metric,
                tags={
                    "method": method,
                    "endpoint": endpoint_category
                }
            )
            
            # Update throughput metrics
            metrics_collector.record_counter_metric(
                metric_name=f"{endpoint_category}.requests",
                value=1.0,
                tags={
                    "method": method,
                    "status_class": f"{status_code // 100}xx"
                }
            )
            
        except Exception as e:
            self.logger.error(f"Failed to record performance metrics: {e}")
    
    def get_middleware_statistics(self) -> Dict[str, Any]:
        """Get middleware performance statistics.
        
        Returns:
            Dictionary with middleware statistics
        """
        avg_duration = self.total_duration / self.request_count if self.request_count > 0 else 0.0
        error_rate = (self.error_count / self.request_count * 100) if self.request_count > 0 else 0.0
        
        return {
            "service_name": self.service_name,
            "total_requests": self.request_count,
            "total_errors": self.error_count,
            "error_rate_percentage": round(error_rate, 2),
            "average_duration_ms": round(avg_duration, 2),
            "total_duration_ms": round(self.total_duration, 2),
            "configured_thresholds": self.endpoint_thresholds
        }


class PerformanceMetricsEndpoint:
    """Provides performance metrics endpoint for monitoring."""
    
    def __init__(self, middleware: PerformanceTrackingMiddleware):
        """Initialize metrics endpoint.
        
        Args:
            middleware: Performance tracking middleware instance
        """
        self.middleware = middleware
        self.logger = logging.getLogger(__name__)
    
    async def get_performance_metrics(self) -> Dict[str, Any]:
        """Get comprehensive performance metrics.
        
        Returns:
            Performance metrics data
        """
        try:
            # Get middleware statistics
            middleware_stats = self.middleware.get_middleware_statistics()
            
            # Get metrics collector data
            metrics_summary = metrics_collector.get_metrics_summary()
            dashboard_data = metrics_collector.get_dashboard_data(time_window_minutes=60)
            
            # Get APM integration statistics
            apm_stats = apm_integration.get_export_statistics()
            
            # Get alert statistics
            alert_stats = alert_manager.get_alert_statistics()
            
            return {
                "timestamp": datetime.utcnow().isoformat(),
                "middleware": middleware_stats,
                "metrics_collector": metrics_summary,
                "dashboard": dashboard_data,
                "apm_integration": apm_stats,
                "alerting": alert_stats
            }
            
        except Exception as e:
            self.logger.error(f"Failed to get performance metrics: {e}")
            return {
                "error": f"Failed to get performance metrics: {e}",
                "timestamp": datetime.utcnow().isoformat()
            }
    
    async def get_real_time_metrics(self, time_window_minutes: int = 5) -> Dict[str, Any]:
        """Get real-time performance metrics.
        
        Args:
            time_window_minutes: Time window for metrics
            
        Returns:
            Real-time metrics data
        """
        try:
            dashboard_data = metrics_collector.get_dashboard_data(time_window_minutes)
            active_alerts = alert_manager.get_active_alerts()
            
            return {
                "timestamp": datetime.utcnow().isoformat(),
                "time_window_minutes": time_window_minutes,
                "dashboard": dashboard_data,
                "active_alerts": [
                    {
                        "rule_name": alert.rule_name,
                        "severity": alert.severity.value,
                        "metric_name": alert.metric_name,
                        "metric_value": alert.metric_value,
                        "threshold_value": alert.threshold_value,
                        "triggered_at": alert.triggered_at.isoformat(),
                        "message": alert.message
                    }
                    for alert in active_alerts[:10]  # Last 10 alerts
                ]
            }
            
        except Exception as e:
            self.logger.error(f"Failed to get real-time metrics: {e}")
            return {
                "error": f"Failed to get real-time metrics: {e}",
                "timestamp": datetime.utcnow().isoformat()
            }