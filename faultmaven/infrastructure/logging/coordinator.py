"""
FaultMaven Logging Coordinator

Provides request-scoped logging coordination with deduplication, error cascade
prevention, and performance tracking across application layers.
"""

from contextvars import ContextVar
from typing import Dict, Any, Optional, Set, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import uuid
import logging
import os

# Import enhanced error handling components
from ...exceptions import ErrorSeverity, RecoveryResult


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
    
    def __enter__(self):
        """Enter the context manager - set this context as active."""
        request_context.set(self)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context manager - clear the active context."""
        request_context.set(None)
        return False


@dataclass
class LayerErrorConfig:
    """Configuration for layer-specific error handling."""
    max_errors_per_minute: int = 10
    escalation_threshold: int = 5
    recovery_strategies: List[str] = field(default_factory=list)
    automatic_recovery: bool = True
    severity_weights: Dict[ErrorSeverity, float] = field(default_factory=dict)


@dataclass
class ErrorPattern:
    """Represents a detected error pattern."""
    pattern_id: str
    pattern_type: str  # "recurring", "cascade", "burst", "degradation"
    first_occurrence: datetime
    last_occurrence: datetime
    occurrence_count: int
    affected_layers: List[str]
    confidence_score: float
    suggested_actions: List[str]


@dataclass
class RecoveryAction:
    """Represents a recovery action attempt."""
    action_name: str
    attempted_at: datetime
    result: RecoveryResult
    duration_ms: int
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ErrorContext:
    """Enhanced error context with intelligent handling capabilities.
    
    This enhanced version provides layer-specific error thresholds, automated
    recovery strategies, pattern detection, and intelligent escalation logic.
    """
    original_error: Optional[Exception] = None
    layer_errors: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    recovery_attempts: int = 0
    
    # Enhanced features
    layer_configs: Dict[str, LayerErrorConfig] = field(default_factory=dict)
    error_timeline: List[Tuple[datetime, str, Exception]] = field(default_factory=list)
    detected_patterns: List[ErrorPattern] = field(default_factory=list)
    recovery_actions: List[RecoveryAction] = field(default_factory=list)
    escalation_level: ErrorSeverity = ErrorSeverity.LOW
    correlation_metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Initialize default layer configurations."""
        if not self.layer_configs:
            self.layer_configs = self._get_default_layer_configs()
    
    def _get_default_layer_configs(self) -> Dict[str, LayerErrorConfig]:
        """Get default configuration for each architectural layer."""
        return {
            "api": LayerErrorConfig(
                max_errors_per_minute=20,
                escalation_threshold=10,
                recovery_strategies=["retry_request", "fallback_response"],
                automatic_recovery=True,
                severity_weights={
                    ErrorSeverity.LOW: 1.0,
                    ErrorSeverity.MEDIUM: 2.0,
                    ErrorSeverity.HIGH: 4.0,
                    ErrorSeverity.CRITICAL: 8.0
                }
            ),
            "service": LayerErrorConfig(
                max_errors_per_minute=15,
                escalation_threshold=8,
                recovery_strategies=["circuit_breaker", "fallback_service"],
                automatic_recovery=True,
                severity_weights={
                    ErrorSeverity.LOW: 1.5,
                    ErrorSeverity.MEDIUM: 3.0,
                    ErrorSeverity.HIGH: 6.0,
                    ErrorSeverity.CRITICAL: 12.0
                }
            ),
            "core": LayerErrorConfig(
                max_errors_per_minute=10,
                escalation_threshold=5,
                recovery_strategies=["reset_state", "fallback_algorithm"],
                automatic_recovery=False,  # Core errors need manual intervention
                severity_weights={
                    ErrorSeverity.LOW: 2.0,
                    ErrorSeverity.MEDIUM: 4.0,
                    ErrorSeverity.HIGH: 8.0,
                    ErrorSeverity.CRITICAL: 16.0
                }
            ),
            "infrastructure": LayerErrorConfig(
                max_errors_per_minute=5,
                escalation_threshold=3,
                recovery_strategies=["reconnect", "failover"],
                automatic_recovery=True,
                severity_weights={
                    ErrorSeverity.LOW: 3.0,
                    ErrorSeverity.MEDIUM: 6.0,
                    ErrorSeverity.HIGH: 12.0,
                    ErrorSeverity.CRITICAL: 24.0
                }
            )
        }
    
    def add_layer_error(
        self, 
        layer: str, 
        error: Exception, 
        severity: ErrorSeverity = ErrorSeverity.MEDIUM,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Add error from specific layer with enhanced tracking.
        
        Args:
            layer: The architectural layer where error occurred
            error: The exception that occurred
            severity: Severity level of the error
            metadata: Additional context about the error
        """
        current_time = datetime.utcnow()
        
        # Store error in layer errors - format expected by tests
        if layer not in self.layer_errors:
            self.layer_errors[layer] = {
                "error": str(error),
                "type": type(error).__name__,
                "timestamp": current_time,
                "severity": severity,
                "metadata": metadata or {},
                "errors": [],
                "last_error_time": None,
                "error_count": 0,
                "severity_score": 0.0
            }
        else:
            # Update the main error info for the layer
            self.layer_errors[layer]["error"] = str(error)
            self.layer_errors[layer]["type"] = type(error).__name__
            self.layer_errors[layer]["timestamp"] = current_time
            self.layer_errors[layer]["severity"] = severity
            if metadata:
                self.layer_errors[layer]["metadata"].update(metadata)
        
        # Also maintain the detailed error history
        error_info = {
            "error": error,
            "timestamp": current_time,
            "severity": severity,
            "metadata": metadata or {}
        }
        
        self.layer_errors[layer]["errors"].append(error_info)
        self.layer_errors[layer]["last_error_time"] = current_time
        self.layer_errors[layer]["error_count"] += 1
        
        # Update severity score using configured weights
        if layer in self.layer_configs:
            weight = self.layer_configs[layer].severity_weights.get(severity, 1.0)
            self.layer_errors[layer]["severity_score"] += weight
        
        # Add to timeline for pattern detection
        self.error_timeline.append((current_time, layer, error))
        
        # Update escalation level
        self._update_escalation_level(layer, severity)
        
        # Detect patterns
        self._detect_error_patterns()
        
        # Attempt automatic recovery if configured
        if self._should_attempt_recovery(layer):
            self._attempt_automatic_recovery(layer, error)
        
        # Maintain original compatibility
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
    
    def _update_escalation_level(self, layer: str, severity: ErrorSeverity) -> None:
        """Update overall escalation level based on new error."""
        if layer not in self.layer_configs:
            return
            
        config = self.layer_configs[layer]
        layer_errors = self.layer_errors.get(layer, {})
        
        # Check if we've exceeded escalation threshold
        if layer_errors.get("error_count", 0) >= config.escalation_threshold:
            if severity == ErrorSeverity.CRITICAL or self.escalation_level == ErrorSeverity.CRITICAL:
                self.escalation_level = ErrorSeverity.CRITICAL
            elif severity == ErrorSeverity.HIGH or self.escalation_level == ErrorSeverity.HIGH:
                self.escalation_level = ErrorSeverity.HIGH
            elif severity == ErrorSeverity.MEDIUM or self.escalation_level == ErrorSeverity.MEDIUM:
                self.escalation_level = ErrorSeverity.MEDIUM
    
    def _detect_error_patterns(self) -> None:
        """Detect error patterns in the timeline."""
        if len(self.error_timeline) < 3:
            return
            
        current_time = datetime.utcnow()
        
        # Pattern 1: Recurring errors (same error type repeating)
        self._detect_recurring_pattern()
        
        # Pattern 2: Error cascade (errors propagating through layers)
        self._detect_cascade_pattern()
        
        # Pattern 3: Error burst (multiple errors in short time)
        self._detect_burst_pattern(current_time)
        
        # Pattern 4: System degradation (increasing error rate)
        self._detect_degradation_pattern()
    
    def _detect_recurring_pattern(self) -> None:
        """Detect recurring error patterns."""
        error_types = {}
        for timestamp, layer, error in self.error_timeline[-10:]:  # Check last 10 errors
            error_type = type(error).__name__
            if error_type not in error_types:
                error_types[error_type] = {"count": 0, "first": timestamp, "last": timestamp, "layers": set()}
            
            error_types[error_type]["count"] += 1
            error_types[error_type]["last"] = timestamp
            error_types[error_type]["layers"].add(layer)
        
        for error_type, info in error_types.items():
            if info["count"] >= 3:  # 3 or more occurrences
                pattern = ErrorPattern(
                    pattern_id=f"recurring_{error_type}_{info['first'].timestamp()}",
                    pattern_type="recurring",
                    first_occurrence=info["first"],
                    last_occurrence=info["last"],
                    occurrence_count=info["count"],
                    affected_layers=list(info["layers"]),
                    confidence_score=min(0.9, info["count"] / 10.0),
                    suggested_actions=[
                        f"Investigate root cause of {error_type}",
                        "Consider implementing circuit breaker",
                        "Review error handling in affected layers"
                    ]
                )
                
                # Add pattern if not already detected
                if not any(p.pattern_id == pattern.pattern_id for p in self.detected_patterns):
                    self.detected_patterns.append(pattern)
    
    def _detect_cascade_pattern(self) -> None:
        """Detect error cascade patterns across layers."""
        if len(self.error_timeline) < 3:
            return
            
        # Look for errors spreading from infrastructure -> core -> service -> api
        layer_order = ["infrastructure", "core", "service", "api"]
        recent_errors = self.error_timeline[-5:]  # Check last 5 errors
        
        cascade_sequence = []
        for timestamp, layer, error in recent_errors:
            if layer in layer_order:
                cascade_sequence.append((layer_order.index(layer), timestamp))
        
        # Check if sequence shows upward cascade
        if len(cascade_sequence) >= 3:
            is_cascade = True
            for i in range(1, len(cascade_sequence)):
                if cascade_sequence[i][0] <= cascade_sequence[i-1][0]:
                    is_cascade = False
                    break
            
            if is_cascade:
                pattern = ErrorPattern(
                    pattern_id=f"cascade_{datetime.utcnow().timestamp()}",
                    pattern_type="cascade",
                    first_occurrence=cascade_sequence[0][1],
                    last_occurrence=cascade_sequence[-1][1],
                    occurrence_count=len(cascade_sequence),
                    affected_layers=[layer_order[idx] for idx, _ in cascade_sequence],
                    confidence_score=0.8,
                    suggested_actions=[
                        "Investigate infrastructure layer issue",
                        "Implement better error isolation",
                        "Add circuit breakers between layers"
                    ]
                )
                self.detected_patterns.append(pattern)
    
    def _detect_burst_pattern(self, current_time: datetime) -> None:
        """Detect error burst patterns."""
        # Check for multiple errors in last 60 seconds
        recent_threshold = current_time - timedelta(seconds=60)
        recent_errors = [
            (ts, layer, error) for ts, layer, error in self.error_timeline
            if ts >= recent_threshold
        ]
        
        if len(recent_errors) >= 5:  # 5+ errors in 60 seconds
            pattern = ErrorPattern(
                pattern_id=f"burst_{current_time.timestamp()}",
                pattern_type="burst",
                first_occurrence=recent_errors[0][0],
                last_occurrence=recent_errors[-1][0],
                occurrence_count=len(recent_errors),
                affected_layers=list(set(layer for _, layer, _ in recent_errors)),
                confidence_score=0.9,
                suggested_actions=[
                    "Implement rate limiting",
                    "Check for external system overload",
                    "Scale infrastructure resources"
                ]
            )
            self.detected_patterns.append(pattern)
    
    def _detect_degradation_pattern(self) -> None:
        """Detect system degradation patterns."""
        if len(self.error_timeline) < 6:
            return
            
        # Check if error rate is increasing over time
        current_time = datetime.utcnow()
        
        # Split recent errors into two time windows
        window_size = timedelta(minutes=5)
        mid_time = current_time - window_size
        old_threshold = current_time - (window_size * 2)
        
        old_errors = [ts for ts, _, _ in self.error_timeline if old_threshold <= ts < mid_time]
        recent_errors = [ts for ts, _, _ in self.error_timeline if ts >= mid_time]
        
        if len(old_errors) > 0 and len(recent_errors) > len(old_errors) * 1.5:
            pattern = ErrorPattern(
                pattern_id=f"degradation_{current_time.timestamp()}",
                pattern_type="degradation",
                first_occurrence=old_errors[0] if old_errors else current_time,
                last_occurrence=recent_errors[-1] if recent_errors else current_time,
                occurrence_count=len(recent_errors),
                affected_layers=list(set(layer for _, layer, _ in self.error_timeline[-len(recent_errors):])),
                confidence_score=0.7,
                suggested_actions=[
                    "Monitor system resources",
                    "Check for memory leaks",
                    "Review recent deployments"
                ]
            )
            self.detected_patterns.append(pattern)
    
    def _should_attempt_recovery(self, layer: str) -> bool:
        """Determine if automatic recovery should be attempted."""
        if layer not in self.layer_configs:
            return False
            
        config = self.layer_configs[layer]
        if not config.automatic_recovery:
            return False
            
        # Don't attempt if too many recent attempts
        recent_attempts = [
            action for action in self.recovery_actions
            if (datetime.utcnow() - action.attempted_at).total_seconds() < 300  # 5 minutes
        ]
        
        return len(recent_attempts) < 3
    
    def _attempt_automatic_recovery(self, layer: str, error: Exception) -> None:
        """Attempt automatic recovery for the given layer and error."""
        if layer not in self.layer_configs:
            return
            
        config = self.layer_configs[layer]
        
        for strategy in config.recovery_strategies:
            start_time = datetime.utcnow()
            
            try:
                result = self._execute_recovery_strategy(layer, strategy, error)
                duration = int((datetime.utcnow() - start_time).total_seconds() * 1000)
                
                recovery_action = RecoveryAction(
                    action_name=strategy,
                    attempted_at=start_time,
                    result=result,
                    duration_ms=duration,
                    metadata={"layer": layer, "error_type": type(error).__name__}
                )
                
                self.recovery_actions.append(recovery_action)
                
                if result == RecoveryResult.SUCCESS:
                    break  # Stop trying other strategies
                    
            except Exception as recovery_error:
                duration = int((datetime.utcnow() - start_time).total_seconds() * 1000)
                recovery_action = RecoveryAction(
                    action_name=strategy,
                    attempted_at=start_time,
                    result=RecoveryResult.FAILED,
                    duration_ms=duration,
                    error_message=str(recovery_error),
                    metadata={"layer": layer, "error_type": type(error).__name__}
                )
                self.recovery_actions.append(recovery_action)
    
    def _execute_recovery_strategy(self, layer: str, strategy: str, error: Exception) -> RecoveryResult:
        """Execute a specific recovery strategy."""
        # This would be implemented with actual recovery logic
        # For now, return a placeholder result
        logger = logging.getLogger(__name__)
        logger.info(f"Executing recovery strategy '{strategy}' for layer '{layer}'")
        
        # Placeholder implementation - would be replaced with actual strategies
        if strategy == "retry_request":
            return RecoveryResult.SUCCESS
        elif strategy == "circuit_breaker":
            return RecoveryResult.PARTIAL
        elif strategy == "fallback_service":
            return RecoveryResult.SUCCESS
        else:
            return RecoveryResult.NOT_ATTEMPTED
    
    def should_escalate_error(self, layer: str) -> bool:
        """Determine if error should be escalated based on current context."""
        if layer not in self.layer_configs:
            return True  # Escalate unknown layers
            
        config = self.layer_configs[layer]
        layer_info = self.layer_errors.get(layer, {})
        
        # Escalate if error count exceeds threshold
        if layer_info.get("error_count", 0) >= config.escalation_threshold:
            return True
            
        # Escalate if severity score is too high
        if layer_info.get("severity_score", 0) >= config.escalation_threshold * 2:
            return True
            
        # Escalate if critical patterns detected
        critical_patterns = [
            p for p in self.detected_patterns
            if p.confidence_score > 0.8 and p.pattern_type in ["cascade", "degradation"]
        ]
        if critical_patterns:
            return True
            
        return False
    
    def get_recovery_summary(self) -> Dict[str, Any]:
        """Get summary of recovery attempts and their effectiveness."""
        total_attempts = len(self.recovery_actions)
        successful_attempts = len([a for a in self.recovery_actions if a.result == RecoveryResult.SUCCESS])
        
        return {
            "total_attempts": total_attempts,
            "successful_attempts": successful_attempts,
            "success_rate": successful_attempts / total_attempts if total_attempts > 0 else 0,
            "average_duration_ms": sum(a.duration_ms for a in self.recovery_actions) / total_attempts if total_attempts > 0 else 0,
            "recent_attempts": [
                {
                    "action": a.action_name,
                    "result": a.result.value,
                    "duration_ms": a.duration_ms,
                    "attempted_at": a.attempted_at.isoformat()
                }
                for a in self.recovery_actions[-5:]  # Last 5 attempts
            ]
        }
    
    def get_pattern_summary(self) -> Dict[str, Any]:
        """Get summary of detected error patterns."""
        pattern_counts = {}
        for pattern in self.detected_patterns:
            pattern_counts[pattern.pattern_type] = pattern_counts.get(pattern.pattern_type, 0) + 1
        
        return {
            "total_patterns": len(self.detected_patterns),
            "pattern_types": pattern_counts,
            "high_confidence_patterns": len([p for p in self.detected_patterns if p.confidence_score > 0.8]),
            "recent_patterns": [
                {
                    "type": p.pattern_type,
                    "confidence": p.confidence_score,
                    "affected_layers": p.affected_layers,
                    "suggested_actions": p.suggested_actions
                }
                for p in sorted(self.detected_patterns, key=lambda x: x.last_occurrence, reverse=True)[:3]
            ]
        }


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
    
    def get_health_status(self) -> Dict[str, Any]:
        """
        Get health status of the logging system.
        
        This method provides comprehensive health information about the logging
        system including active context, configuration, and performance metrics.
        
        Returns:
            Dictionary with logging system health metrics and configuration
        """
        ctx = request_context.get()
        
        return {
            "status": "healthy",
            "active_context": ctx is not None,
            "correlation_id": ctx.correlation_id if ctx else None,
            "operations_logged": len(ctx.logged_operations) if ctx else 0,
            "errors_tracked": len(ctx.error_context.layer_errors) if ctx and ctx.error_context else 0,
            "performance_violations": sum(
                1 for k, v in ctx.performance_tracker.layer_timings.items()
                if ctx and ctx.performance_tracker and 
                v > ctx.performance_tracker.thresholds.get(k.split('.')[0], 1.0)
            ) if ctx and ctx.performance_tracker else 0,
            "configuration": {
                "log_level": os.getenv('LOG_LEVEL', 'INFO'),
                "log_format": os.getenv('LOG_FORMAT', 'json'),
                "deduplication": os.getenv('LOG_DEDUPE', 'true'),
                "buffer_size": os.getenv('LOG_BUFFER_SIZE', '100'),
                "flush_interval": os.getenv('LOG_FLUSH_INTERVAL', '5'),
            }
        }