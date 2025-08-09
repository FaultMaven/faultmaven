"""
FaultMaven Unified Logging System

Provides a unified logging interface that integrates with the Phase 1 logging
infrastructure to provide consistent, deduplicated logging across all application
layers with performance tracking and error cascade prevention.
"""

import asyncio
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Dict, Iterator, Optional, Union
from datetime import datetime
import structlog
import uuid

from faultmaven.infrastructure.logging.coordinator import (
    LoggingCoordinator, 
    RequestContext, 
    ErrorContext, 
    PerformanceTracker,
    request_context
)
from faultmaven.infrastructure.logging.config import get_logger


class UnifiedLogger:
    """
    Unified logger that provides consistent logging patterns across all application layers.
    
    This class integrates with the Phase 1 logging infrastructure to provide:
    - Automatic deduplication of log entries
    - Performance tracking with layer-specific thresholds
    - Error cascade prevention
    - Unified operation logging with timing
    - Metric collection and context management
    - Business and technical event logging
    
    Attributes:
        logger_name: Name of the logger instance
        layer: Application layer (api, service, core, infrastructure)
        logger: Underlying structlog logger
        coordinator: Logging coordinator for request management
    """
    
    def __init__(self, logger_name: str, layer: str):
        """
        Initialize unified logger for specific layer.
        
        Args:
            logger_name: Name for the logger (typically module or class name)
            layer: Application layer (api, service, core, infrastructure)
        """
        self.logger_name = logger_name
        self.layer = layer
        self.logger = get_logger(logger_name)
        self.coordinator = LoggingCoordinator()
    
    def log_boundary(
        self, 
        operation: str, 
        direction: str, 
        data: Optional[Dict[str, Any]] = None,
        **extra_fields
    ) -> None:
        """
        Log service boundary crossings with automatic deduplication.
        
        This method logs when data crosses service boundaries (inbound/outbound)
        and prevents duplicate logging for the same boundary crossing within
        a request context.
        
        Args:
            operation: Name of the operation (e.g., "process_query", "get_knowledge")
            direction: Direction of boundary crossing ("inbound" or "outbound")  
            data: Optional data payload information (sanitized)
            **extra_fields: Additional fields to include in log
        """
        # Generate unique operation key for deduplication
        operation_key = f"{self.layer}.boundary.{operation}.{direction}"
        
        # Check if already logged in current request context
        ctx = request_context.get()
        if ctx and ctx.has_logged(operation_key):
            return
        
        # Prepare log data
        log_data = {
            "event_type": "service_boundary",
            "layer": self.layer,
            "operation": operation,
            "direction": direction,
            "boundary_key": operation_key,
            **extra_fields
        }
        
        # Add data payload information if provided (should be pre-sanitized)
        if data:
            log_data["payload_info"] = {
                "type": type(data).__name__,
                "size": len(str(data)) if data else 0,
                "keys": list(data.keys()) if isinstance(data, dict) else None
            }
        
        # Log with deduplication
        message = f"Service boundary {direction}: {operation}"
        if ctx:
            # Use coordinator for deduplication
            LoggingCoordinator.log_once(
                operation_key=operation_key,
                logger=self.logger,
                level="info",
                message=message,
                **log_data
            )
        else:
            # Fallback logging without coordination
            self.logger.info(message, **log_data)
    
    @asynccontextmanager
    async def operation(
        self,
        operation_name: str,
        **context_fields
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Context manager for unified operation logging with timing and error handling.
        
        This async context manager provides:
        - Automatic operation start/end logging
        - Performance timing with threshold checking
        - Error cascade prevention
        - Context field management
        - Resource cleanup
        
        Args:
            operation_name: Name of the operation being performed
            **context_fields: Additional context fields for the operation
            
        Yields:
            Dictionary containing operation context that can be updated during execution
            
        Example:
            >>> async with logger.operation("process_user_query", user_id="123") as ctx:
            ...     ctx["query_type"] = "troubleshooting"
            ...     result = await some_async_operation()
            ...     ctx["result_count"] = len(result)
        """
        start_time = time.time()
        operation_key = f"{self.layer}.operation.{operation_name}"
        
        # Initialize operation context
        operation_context = {
            "operation": operation_name,
            "layer": self.layer,
            "start_time": datetime.utcnow().isoformat(),
            **context_fields
        }
        
        # Get request context for coordination
        request_ctx = request_context.get()
        
        try:
            # Log operation start (with deduplication)
            start_key = f"{operation_key}.start"
            if request_ctx and not request_ctx.has_logged(start_key):
                self.logger.info(
                    f"Operation started: {operation_name}",
                    event_type="operation_start",
                    operation_key=operation_key,
                    **operation_context
                )
                request_ctx.mark_logged(start_key)
            elif not request_ctx:
                self.logger.info(
                    f"Operation started: {operation_name}",
                    event_type="operation_start",
                    operation_key=operation_key,
                    **operation_context
                )
            
            # Yield context for caller to modify
            yield operation_context
            
        except Exception as error:
            # Calculate duration for error logging
            duration = time.time() - start_time
            
            # Log error with cascade prevention
            if request_ctx and request_ctx.error_context:
                if request_ctx.error_context.should_log_error(self.layer):
                    # Filter out conflicting keys from operation_context
                    filtered_context = {k: v for k, v in operation_context.items() 
                                      if k not in {'event_type', 'operation_key', 'error_message', 'error_type', 'duration_seconds'}}
                    self.logger.error(
                        f"Operation failed: {operation_name}",
                        event_type="operation_error",
                        operation_key=operation_key,
                        error_message=str(error),
                        error_type=type(error).__name__,
                        duration_seconds=duration,
                        **filtered_context
                    )
                    # Record error in context
                    request_ctx.error_context.add_layer_error(self.layer, error)
            else:
                # Fallback error logging
                self.logger.error(
                    f"Operation failed: {operation_name}",
                    event_type="operation_error", 
                    operation_key=operation_key,
                    error_message=str(error),
                    duration_seconds=duration,
                    **operation_context
                )
            
            # Re-raise the exception
            raise
            
        else:
            # Calculate final duration
            duration = time.time() - start_time
            
            # Record performance timing
            performance_violation = False
            threshold = 1.0  # Default threshold
            
            if request_ctx and request_ctx.performance_tracker:
                violation, threshold = request_ctx.performance_tracker.record_timing(
                    self.layer, operation_name, duration
                )
                performance_violation = violation
            
            # Update context with final timing
            operation_context.update({
                "end_time": datetime.utcnow().isoformat(),
                "duration_seconds": duration,
                "performance_violation": performance_violation,
                "threshold_seconds": threshold
            })
            
            # Log operation completion (with deduplication)
            end_key = f"{operation_key}.end"
            log_level = "warning" if performance_violation else "info"
            
            if request_ctx and not request_ctx.has_logged(end_key):
                log_method = getattr(self.logger, log_level)
                log_method(
                    f"Operation completed: {operation_name}",
                    event_type="operation_end",
                    operation_key=operation_key,
                    **operation_context
                )
                request_ctx.mark_logged(end_key)
            elif not request_ctx:
                log_method = getattr(self.logger, log_level)
                log_method(
                    f"Operation completed: {operation_name}",
                    event_type="operation_end",
                    operation_key=operation_key,
                    **operation_context
                )
    
    @contextmanager
    def operation_sync(
        self,
        operation_name: str,
        **context_fields
    ) -> Iterator[Dict[str, Any]]:
        """
        Synchronous version of operation context manager.
        
        Provides the same functionality as the async operation() method
        but for synchronous operations.
        
        Args:
            operation_name: Name of the operation being performed
            **context_fields: Additional context fields for the operation
            
        Yields:
            Dictionary containing operation context that can be updated during execution
        """
        start_time = time.time()
        operation_key = f"{self.layer}.operation.{operation_name}"
        
        # Initialize operation context
        operation_context = {
            "operation": operation_name,
            "layer": self.layer,
            "start_time": datetime.utcnow().isoformat(),
            **context_fields
        }
        
        # Get request context for coordination
        request_ctx = request_context.get()
        
        try:
            # Log operation start (with deduplication)
            start_key = f"{operation_key}.start"
            if request_ctx and not request_ctx.has_logged(start_key):
                self.logger.info(
                    f"Operation started: {operation_name}",
                    event_type="operation_start",
                    operation_key=operation_key,
                    **operation_context
                )
                request_ctx.mark_logged(start_key)
            elif not request_ctx:
                self.logger.info(
                    f"Operation started: {operation_name}",
                    event_type="operation_start",
                    operation_key=operation_key,
                    **operation_context
                )
            
            # Yield context for caller to modify
            yield operation_context
            
        except Exception as error:
            # Calculate duration for error logging
            duration = time.time() - start_time
            
            # Log error with cascade prevention
            if request_ctx and request_ctx.error_context:
                if request_ctx.error_context.should_log_error(self.layer):
                    # Filter out conflicting keys from operation_context
                    filtered_context = {k: v for k, v in operation_context.items() 
                                      if k not in {'event_type', 'operation_key', 'error_message', 'error_type', 'duration_seconds'}}
                    self.logger.error(
                        f"Operation failed: {operation_name}",
                        event_type="operation_error",
                        operation_key=operation_key,
                        error_message=str(error),
                        error_type=type(error).__name__,
                        duration_seconds=duration,
                        **filtered_context
                    )
                    # Record error in context
                    request_ctx.error_context.add_layer_error(self.layer, error)
            else:
                # Fallback error logging
                self.logger.error(
                    f"Operation failed: {operation_name}",
                    event_type="operation_error",
                    operation_key=operation_key,
                    error_message=str(error),
                    duration_seconds=duration,
                    **operation_context
                )
            
            # Re-raise the exception
            raise
            
        else:
            # Calculate final duration
            duration = time.time() - start_time
            
            # Record performance timing
            performance_violation = False
            threshold = 1.0  # Default threshold
            
            if request_ctx and request_ctx.performance_tracker:
                violation, threshold = request_ctx.performance_tracker.record_timing(
                    self.layer, operation_name, duration
                )
                performance_violation = violation
            
            # Update context with final timing
            operation_context.update({
                "end_time": datetime.utcnow().isoformat(),
                "duration_seconds": duration,
                "performance_violation": performance_violation,
                "threshold_seconds": threshold
            })
            
            # Log operation completion (with deduplication)
            end_key = f"{operation_key}.end"
            log_level = "warning" if performance_violation else "info"
            
            if request_ctx and not request_ctx.has_logged(end_key):
                log_method = getattr(self.logger, log_level)
                log_method(
                    f"Operation completed: {operation_name}",
                    event_type="operation_end",
                    operation_key=operation_key,
                    **operation_context
                )
                request_ctx.mark_logged(end_key)
            elif not request_ctx:
                log_method = getattr(self.logger, log_level)
                log_method(
                    f"Operation completed: {operation_name}",
                    event_type="operation_end",
                    operation_key=operation_key,
                    **operation_context
                )
    
    def log_metric(
        self,
        metric_name: str,
        value: Union[int, float],
        unit: str = "count",
        tags: Optional[Dict[str, str]] = None,
        **extra_fields
    ) -> None:
        """
        Log metrics with context and deduplication.
        
        This method logs operational metrics with proper context
        and prevents duplicate metric logging within the same request.
        
        Args:
            metric_name: Name of the metric being logged
            value: Numeric value of the metric
            unit: Unit of measurement (count, seconds, bytes, etc.)
            tags: Optional metric tags/labels
            **extra_fields: Additional fields to include
        """
        # Generate unique metric key for deduplication within request
        metric_key = f"{self.layer}.metric.{metric_name}"
        
        # Prepare metric data
        metric_data = {
            "event_type": "metric",
            "layer": self.layer,
            "metric_name": metric_name,
            "metric_value": value,
            "metric_unit": unit,
            "metric_key": metric_key,
            **extra_fields
        }
        
        # Add tags if provided
        if tags:
            metric_data["metric_tags"] = tags
        
        # Get request context
        request_ctx = request_context.get()
        
        # Log metric (allow multiple metrics with same name but different values)
        # Use unique key to allow multiple metric values per request
        unique_key = f"{metric_key}.{uuid.uuid4().hex[:8]}"
        
        if request_ctx:
            LoggingCoordinator.log_once(
                operation_key=unique_key,
                logger=self.logger,
                level="info",
                message=f"Metric recorded: {metric_name}={value} {unit}",
                **metric_data
            )
        else:
            self.logger.info(
                f"Metric recorded: {metric_name}={value} {unit}",
                **metric_data
            )
    
    def log_event(
        self,
        event_type: str,
        event_name: str,
        severity: str = "info",
        data: Optional[Dict[str, Any]] = None,
        **extra_fields
    ) -> None:
        """
        Log business and technical events with context.
        
        This method logs significant business or technical events
        that occur during request processing.
        
        Args:
            event_type: Type of event (business, technical, system, etc.)
            event_name: Specific name of the event
            severity: Event severity (debug, info, warning, error, critical)
            data: Optional event data (should be pre-sanitized)
            **extra_fields: Additional fields to include
        """
        # Generate event key for potential deduplication
        event_key = f"{self.layer}.event.{event_type}.{event_name}"
        
        # Prepare event data
        event_data = {
            "event_type": "application_event",
            "layer": self.layer,
            "app_event_type": event_type,
            "event_name": event_name,
            "event_severity": severity,
            "event_key": event_key,
            **extra_fields
        }
        
        # Add event data if provided
        if data:
            event_data["event_data"] = data
        
        # Get request context
        request_ctx = request_context.get()
        
        # Log event (with unique key to allow multiple similar events)
        unique_key = f"{event_key}.{uuid.uuid4().hex[:8]}"
        
        if request_ctx:
            LoggingCoordinator.log_once(
                operation_key=unique_key,
                logger=self.logger,
                level=severity,
                message=f"Event: {event_type}.{event_name}",
                **event_data
            )
        else:
            log_method = getattr(self.logger, severity, self.logger.info)
            log_method(
                f"Event: {event_type}.{event_name}",
                **event_data
            )
    
    def debug(self, message: str, **extra_fields) -> None:
        """Log debug message with layer context."""
        self.logger.debug(message, layer=self.layer, **extra_fields)
    
    def info(self, message: str, **extra_fields) -> None:
        """Log info message with layer context."""
        self.logger.info(message, layer=self.layer, **extra_fields)
    
    def warning(self, message: str, **extra_fields) -> None:
        """Log warning message with layer context."""
        self.logger.warning(message, layer=self.layer, **extra_fields)
    
    def error(self, message: str, error: Optional[Exception] = None, **extra_fields) -> None:
        """Log error message with cascade prevention."""
        error_data = {"layer": self.layer, **extra_fields}
        
        if error:
            error_data.update({
                "error_message": str(error),
                "error_type": type(error).__name__
            })
        
        # Check for error cascade prevention
        request_ctx = request_context.get()
        if request_ctx and request_ctx.error_context:
            if request_ctx.error_context.should_log_error(self.layer):
                self.logger.error(message, **error_data)
                if error:
                    request_ctx.error_context.add_layer_error(self.layer, error)
        else:
            self.logger.error(message, **error_data)
    
    def critical(self, message: str, error: Optional[Exception] = None, **extra_fields) -> None:
        """Log critical message with cascade prevention."""
        error_data = {"layer": self.layer, **extra_fields}
        
        if error:
            error_data.update({
                "error_message": str(error),
                "error_type": type(error).__name__
            })
        
        # Check for error cascade prevention  
        request_ctx = request_context.get()
        if request_ctx and request_ctx.error_context:
            if request_ctx.error_context.should_log_error(self.layer):
                self.logger.critical(message, **error_data)
                if error:
                    request_ctx.error_context.add_layer_error(self.layer, error)
        else:
            self.logger.critical(message, **error_data)


# Global logger instances cache to avoid recreating loggers
_logger_instances: Dict[str, UnifiedLogger] = {}


def get_unified_logger(name: str, layer: str) -> UnifiedLogger:
    """
    Factory function to get or create a unified logger instance.
    
    This function ensures that logger instances are reused for the same
    name and layer combination, preventing resource waste and maintaining
    consistency.
    
    Args:
        name: Logger name (typically module or class name)
        layer: Application layer (api, service, core, infrastructure)
        
    Returns:
        UnifiedLogger instance configured for the specified name and layer
        
    Example:
        >>> logger = get_unified_logger(__name__, "service")
        >>> async with logger.operation("process_data") as ctx:
        ...     ctx["items_processed"] = 10
    """
    # Validate layer
    valid_layers = {"api", "service", "core", "infrastructure"}
    if layer not in valid_layers:
        raise ValueError(f"Invalid layer '{layer}'. Must be one of: {valid_layers}")
    
    # Create cache key
    cache_key = f"{name}:{layer}"
    
    # Return existing instance or create new one
    if cache_key not in _logger_instances:
        _logger_instances[cache_key] = UnifiedLogger(name, layer)
    
    return _logger_instances[cache_key]


def clear_logger_cache() -> None:
    """
    Clear the logger instance cache.
    
    This function is primarily used for testing to ensure clean state
    between test runs.
    """
    global _logger_instances
    _logger_instances.clear()