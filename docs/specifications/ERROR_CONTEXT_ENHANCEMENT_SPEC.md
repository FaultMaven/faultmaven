# Error Context Enhancement Specification

## Overview
This specification defines enhancements to the error context management system to provide more granular error handling, recovery strategies, and error pattern detection across the FaultMaven architecture.

## Current State Analysis

### Current Implementation
- **Location**: `faultmaven/infrastructure/logging/coordinator.py:80-100`
- **Current Capabilities**:
  - Basic error tracking across layers
  - Simple cascade prevention
  - Recovery attempt counting

### Identified Limitations
- **No Layer-Specific Thresholds**: All errors treated equally regardless of layer
- **Limited Recovery Strategies**: No automated recovery mechanisms
- **No Pattern Detection**: Missing correlation and pattern analysis
- **Basic Escalation Logic**: No intelligent escalation based on error context

## Technical Requirements

### 1. Enhanced ErrorContext Class

**File**: `faultmaven/infrastructure/logging/coordinator.py`

```python
from typing import Dict, List, Callable, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import logging

class ErrorSeverity(Enum):
    """Error severity levels for intelligent escalation."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class RecoveryResult(Enum):
    """Results of recovery attempts."""
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    NOT_ATTEMPTED = "not_attempted"

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
        
        # Store error in layer errors
        if layer not in self.layer_errors:
            self.layer_errors[layer] = {
                "errors": [],
                "last_error_time": None,
                "error_count": 0,
                "severity_score": 0.0
            }
        
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
    
    def _update_escalation_level(self, layer: str, severity: ErrorSeverity) -> None:
        """Update overall escalation level based on new error."""
        if layer not in self.layer_configs:
            return
            
        config = self.layer_configs[layer]
        layer_errors = self.layer_errors.get(layer, {})
        
        # Check if we've exceeded escalation threshold
        if layer_errors.get("error_count", 0) >= config.escalation_threshold:
            if severity.value == "critical" or self.escalation_level.value == "critical":
                self.escalation_level = ErrorSeverity.CRITICAL
            elif severity.value == "high" or self.escalation_level.value == "high":
                self.escalation_level = ErrorSeverity.HIGH
            elif severity.value == "medium" or self.escalation_level.value == "medium":
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
```

### 2. Recovery Strategy Implementation

**File**: `faultmaven/infrastructure/error_recovery.py`

```python
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from enum import Enum
import asyncio
import logging

class RecoveryStrategy(ABC):
    """Abstract base class for error recovery strategies."""
    
    @abstractmethod
    async def execute(self, error: Exception, context: Dict[str, Any]) -> RecoveryResult:
        """Execute the recovery strategy."""
        pass
    
    @abstractmethod
    def is_applicable(self, error: Exception, layer: str) -> bool:
        """Check if this strategy is applicable to the given error and layer."""
        pass

class RetryRecoveryStrategy(RecoveryStrategy):
    """Recovery strategy that retries the failed operation."""
    
    def __init__(self, max_retries: int = 3, backoff_factor: float = 1.5):
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
    
    async def execute(self, error: Exception, context: Dict[str, Any]) -> RecoveryResult:
        """Execute retry recovery strategy."""
        # Implementation would retry the operation
        return RecoveryResult.SUCCESS
    
    def is_applicable(self, error: Exception, layer: str) -> bool:
        """Check if retry is applicable."""
        # Retry is applicable for transient errors
        transient_errors = ["TimeoutError", "ConnectionError", "TemporaryFailure"]
        return type(error).__name__ in transient_errors

class CircuitBreakerRecoveryStrategy(RecoveryStrategy):
    """Recovery strategy that implements circuit breaker pattern."""
    
    async def execute(self, error: Exception, context: Dict[str, Any]) -> RecoveryResult:
        """Execute circuit breaker recovery."""
        # Implementation would open circuit breaker
        return RecoveryResult.PARTIAL
    
    def is_applicable(self, error: Exception, layer: str) -> bool:
        """Check if circuit breaker is applicable."""
        return layer in ["service", "infrastructure"]

class FallbackRecoveryStrategy(RecoveryStrategy):
    """Recovery strategy that uses fallback mechanisms."""
    
    async def execute(self, error: Exception, context: Dict[str, Any]) -> RecoveryResult:
        """Execute fallback recovery."""
        # Implementation would use fallback service/data
        return RecoveryResult.SUCCESS
    
    def is_applicable(self, error: Exception, layer: str) -> bool:
        """Check if fallback is applicable."""
        return True  # Fallback can be applicable to most errors

class RecoveryManager:
    """Manages error recovery strategies and execution."""
    
    def __init__(self):
        self.strategies: Dict[str, RecoveryStrategy] = {
            "retry": RetryRecoveryStrategy(),
            "circuit_breaker": CircuitBreakerRecoveryStrategy(),
            "fallback": FallbackRecoveryStrategy()
        }
    
    async def execute_recovery(
        self, 
        strategy_name: str, 
        error: Exception, 
        context: Dict[str, Any]
    ) -> RecoveryResult:
        """Execute a named recovery strategy."""
        if strategy_name not in self.strategies:
            return RecoveryResult.NOT_ATTEMPTED
        
        strategy = self.strategies[strategy_name]
        layer = context.get("layer", "unknown")
        
        if not strategy.is_applicable(error, layer):
            return RecoveryResult.NOT_ATTEMPTED
        
        try:
            return await strategy.execute(error, context)
        except Exception:
            return RecoveryResult.FAILED
```

## Implementation Steps

### Step 1: Enhanced ErrorContext (Days 1-4)
1. Implement enhanced ErrorContext class with all new features
2. Add pattern detection algorithms
3. Implement recovery attempt tracking

### Step 2: Recovery Strategy Framework (Days 5-8)
1. Implement RecoveryManager and strategy classes
2. Add concrete recovery strategies
3. Integrate with ErrorContext

### Step 3: Integration and Testing (Days 9-12)
1. Integrate enhanced error context with logging coordinator
2. Update all error handling to use new context
3. Add comprehensive testing

### Step 4: Monitoring and Documentation (Days 13-14)
1. Add error pattern monitoring to health checks
2. Update documentation with new error handling patterns
3. Create error handling guidelines

## Testing Requirements

### Unit Tests
```python
# tests/unit/test_enhanced_error_context.py
class TestEnhancedErrorContext:
    def test_layer_specific_error_tracking(self):
        """Test layer-specific error tracking and thresholds."""
        
    def test_pattern_detection_algorithms(self):
        """Test all pattern detection algorithms."""
        
    def test_recovery_strategy_execution(self):
        """Test recovery strategy selection and execution."""
        
    def test_escalation_logic(self):
        """Test intelligent escalation based on context."""
```

### Integration Tests
```python
# tests/integration/test_error_recovery.py
class TestErrorRecovery:
    async def test_end_to_end_error_recovery(self):
        """Test complete error recovery workflow."""
        
    async def test_pattern_detection_in_real_scenarios(self):
        """Test pattern detection with realistic error sequences."""
```

## Success Criteria

1. **Enhanced Error Tracking**: Layer-specific error metrics and thresholds
2. **Pattern Detection**: Automatic detection of error patterns with high accuracy
3. **Automated Recovery**: Successful recovery from common error scenarios
4. **Intelligent Escalation**: Context-aware error escalation decisions
5. **Comprehensive Monitoring**: Detailed error analytics and reporting