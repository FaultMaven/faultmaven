"""
FaultMaven Logging Coordinator

Provides request-scoped logging coordination with deduplication, error cascade
prevention, and performance tracking across application layers.
"""

from contextvars import ContextVar
from typing import Dict, Any, Optional, Set
from dataclasses import dataclass, field
from datetime import datetime
import uuid
import logging


@dataclass
class RequestContext:
    """
    Single source of truth for request-scoped data and logging coordination.
    
    This class manages all request-related context and prevents duplicate logging
    operations through operation tracking and cascade prevention.
    
    Attributes:
        correlation_id: Unique identifier for request tracing
        session_id: Optional session identifier
        user_id: Optional user identifier  
        investigation_id: Optional troubleshooting session identifier
        agent_phase: Current agent phase (e.g., "define_blast_radius")
        start_time: Request start timestamp
        attributes: Additional request-scoped metadata
        logged_operations: Set of logged operation keys for deduplication
        error_context: Error tracking across layers
        performance_tracker: Performance monitoring with layer-specific thresholds
    """
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    investigation_id: Optional[str] = None
    agent_phase: Optional[str] = None
    start_time: datetime = field(default_factory=datetime.utcnow)
    attributes: Dict[str, Any] = field(default_factory=dict)
    logged_operations: Set[str] = field(default_factory=set)
    error_context: Optional['ErrorContext'] = None
    performance_tracker: Optional['PerformanceTracker'] = None
    
    def has_logged(self, operation_key: str) -> bool:
        """
        Check if an operation has already been logged to prevent duplicates.
        
        Args:
            operation_key: Unique key identifying the operation
            
        Returns:
            True if operation has been logged, False otherwise
        """
        return operation_key in self.logged_operations
    
    def mark_logged(self, operation_key: str) -> None:
        """
        Mark an operation as logged to prevent future duplicates.
        
        Args:
            operation_key: Unique key identifying the operation
        """
        self.logged_operations.add(operation_key)


@dataclass
class ErrorContext:
    """
    Track error context across layers to prevent cascade logging.
    
    This class ensures that errors are only logged at the appropriate layer,
    preventing duplicate error entries when errors bubble up through the
    application stack.
    
    Attributes:
        original_error: The first exception that occurred
        layer_errors: Map of layer -> error details
        recovery_attempts: Number of recovery attempts made
    """
    original_error: Optional[Exception] = None
    layer_errors: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    recovery_attempts: int = 0
    
    def add_layer_error(self, layer: str, error: Exception) -> None:
        """
        Add error from specific layer.
        
        Args:
            layer: Layer name where error occurred (api, service, core, infrastructure)
            error: The exception that occurred
        """
        self.layer_errors[layer] = {
            'error': str(error),
            'type': type(error).__name__,
            'timestamp': datetime.utcnow().isoformat()
        }
        if not self.original_error:
            self.original_error = error
    
    def should_log_error(self, layer: str) -> bool:
        """
        Determine if layer should log error to prevent cascade logging.
        
        Args:
            layer: Layer name checking if it should log
            
        Returns:
            True if layer should log the error, False to prevent duplicates
        """
        # Only log at the first layer that catches it or during recovery
        return layer not in self.layer_errors or self.recovery_attempts > 0


class PerformanceTracker:
    """
    Track performance metrics across layers with configurable thresholds.
    
    This class monitors operation performance and flags slow operations
    based on layer-specific thresholds, enabling proactive performance
    monitoring and alerting.
    """
    
    def __init__(self):
        """Initialize with default performance thresholds per layer."""
        self.layer_timings: Dict[str, float] = {}
        self.thresholds = {
            'api': 0.1,           # 100ms - API should be fast
            'service': 0.5,       # 500ms - Service orchestration
            'core': 0.3,          # 300ms - Core domain logic
            'infrastructure': 1.0  # 1s - External calls can be slower
        }
    
    def record_timing(self, layer: str, operation: str, duration: float) -> tuple[bool, float]:
        """
        Record timing and return if it exceeds threshold.
        
        Args:
            layer: Layer name (api, service, core, infrastructure)
            operation: Operation name
            duration: Operation duration in seconds
            
        Returns:
            Tuple of (exceeds_threshold, threshold_value)
        """
        key = f"{layer}.{operation}"
        self.layer_timings[key] = duration
        
        threshold = self.thresholds.get(layer, 1.0)
        exceeds_threshold = duration > threshold
        
        return exceeds_threshold, threshold


# Thread-safe context variable for request context
request_context: ContextVar[Optional[RequestContext]] = ContextVar(
    'request_context', 
    default=None
)


class LoggingCoordinator:
    """
    Coordinates all logging for a request lifecycle.
    
    This class manages request-scoped logging context and provides methods
    for coordinated logging across all application layers. It ensures that
    each request has a single point of coordination for all logging activities.
    """
    
    def __init__(self):
        """Initialize the logging coordinator."""
        self.context: Optional[RequestContext] = None
        
    def start_request(self, **initial_context) -> RequestContext:
        """
        Initialize request context - called ONCE per request.
        
        This method should be called at the beginning of each request to establish
        the logging context that will be used throughout the request lifecycle.
        
        Args:
            **initial_context: Initial context attributes (session_id, user_id, etc.)
            
        Returns:
            RequestContext: The initialized request context
        """
        # Separate known RequestContext fields from arbitrary attributes
        known_fields = {
            'correlation_id', 'session_id', 'user_id', 'investigation_id', 
            'agent_phase', 'start_time'
        }
        
        # Extract known fields for RequestContext constructor
        context_args = {k: v for k, v in initial_context.items() if k in known_fields}
        
        # Extract additional attributes
        additional_attrs = {k: v for k, v in initial_context.items() if k not in known_fields}
        
        # Create context with known fields
        self.context = RequestContext(**context_args)
        
        # Add additional attributes to the attributes dict
        if additional_attrs:
            self.context.attributes.update(additional_attrs)
        
        self.context.error_context = ErrorContext()
        self.context.performance_tracker = PerformanceTracker()
        request_context.set(self.context)
        return self.context
    
    def end_request(self) -> Dict[str, Any]:
        """
        Finalize request - returns metrics for single summary log.
        
        This method should be called at the end of each request to generate
        a summary of the request's logging activity and performance metrics.
        
        Returns:
            Dict containing request summary metrics
        """
        if not self.context:
            return {}
            
        duration = (datetime.utcnow() - self.context.start_time).total_seconds()
        
        # Calculate performance violations
        performance_violations = 0
        if self.context.performance_tracker and hasattr(self.context.performance_tracker, 'layer_timings'):
            try:
                for timing_key, timing_value in self.context.performance_tracker.layer_timings.items():
                    layer = timing_key.split('.')[0]
                    threshold = self.context.performance_tracker.thresholds.get(layer, 1.0)
                    if timing_value > threshold:
                        performance_violations += 1
            except (AttributeError, TypeError):
                # Handle case where performance_tracker is mocked or malformed
                performance_violations = 0
        
        summary = {
            'correlation_id': self.context.correlation_id,
            'duration_seconds': duration,
            'operations_logged': len(self.context.logged_operations),
            'errors_encountered': (len(self.context.error_context.layer_errors) 
                                 if self.context.error_context else 0),
            'performance_violations': performance_violations,
            **self.context.attributes
        }
        
        # Clear context
        request_context.set(None)
        self.context = None
        
        return summary
    
    @staticmethod
    def get_context() -> Optional[RequestContext]:
        """
        Get current request context.
        
        Returns:
            Current RequestContext if available, None otherwise
        """
        return request_context.get()
    
    @staticmethod
    def log_once(operation_key: str, logger: logging.Logger, level: str, 
                 message: str, **extra) -> None:
        """
        Log an operation only if it hasn't been logged yet.
        
        This method prevents duplicate logging by checking if the operation
        has already been logged in the current request context.
        
        Args:
            operation_key: Unique key identifying the operation
            logger: Logger instance to use
            level: Log level (debug, info, warning, error, critical)
            message: Log message
            **extra: Additional fields to include in log
        """
        ctx = request_context.get()
        if ctx and not ctx.has_logged(operation_key):
            # Get the logging method for the specified level
            log_method = getattr(logger, level.lower(), logger.info)
            log_method(message, **extra)
            # Mark as logged to prevent duplicates
            ctx.mark_logged(operation_key)