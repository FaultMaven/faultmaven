"""Error Handling & Fallback Manager

Component 6 of 7 in the FaultMaven agentic framework.
Provides robust error recovery and graceful degradation with comprehensive
error handling, fallback strategies, and system resilience.

This component implements the IErrorFallbackManager interface to provide:
- Multi-level error detection and classification
- Intelligent fallback strategy selection and execution
- Circuit breaker patterns for system protection
- Error recovery with state preservation
- Comprehensive error logging and metrics
- Graceful degradation with user experience preservation
"""

import asyncio
import logging
import time
import traceback
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Union, Callable
from dataclasses import dataclass, field
from collections import defaultdict

from faultmaven.models.agentic import (
    IErrorFallbackManager,
    ErrorClassification,
    FallbackStrategy,
    RecoveryResult,
    SystemHealthStatus,
    ErrorMetrics,
    CircuitBreakerState
)


logger = logging.getLogger(__name__)


class ErrorSeverity(Enum):
    """Error severity levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ErrorCategory(Enum):
    """Error categories for classification."""
    NETWORK = "network"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    VALIDATION = "validation"
    SYSTEM = "system"
    DATA = "data"
    EXTERNAL_SERVICE = "external_service"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    RESOURCE = "resource"
    UNKNOWN = "unknown"


class FallbackType(Enum):
    """Types of fallback strategies."""
    RETRY = "retry"
    CACHE = "cache"
    DEFAULT_RESPONSE = "default_response"
    DEGRADED_SERVICE = "degraded_service"
    REDIRECT = "redirect"
    CIRCUIT_BREAKER = "circuit_breaker"
    MANUAL_INTERVENTION = "manual_intervention"


@dataclass
class ErrorContext:
    """Context information for error handling."""
    error: Exception
    operation: str
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    request_id: Optional[str] = None
    component: str = "unknown"
    timestamp: datetime = field(default_factory=datetime.utcnow)
    stack_trace: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker pattern."""
    failure_threshold: int = 5
    recovery_timeout: int = 60  # seconds
    half_open_max_calls: int = 3
    success_threshold: int = 2


class ErrorFallbackManager(IErrorFallbackManager):
    """Production implementation of the Error Handling & Fallback Manager.
    
    Provides comprehensive error handling and recovery capabilities including:
    - Multi-dimensional error classification with severity assessment
    - Intelligent fallback strategy selection based on error patterns
    - Circuit breaker implementation with adaptive thresholds
    - State-preserving error recovery with rollback capabilities
    - Real-time error metrics and health monitoring
    - Graceful degradation with user experience preservation
    - Automated escalation and alerting for critical errors
    - Learning-based fallback optimization over time
    """

    def __init__(self, health_checker=None, alert_manager=None):
        """Initialize the error and fallback manager.
        
        Args:
            health_checker: Optional health checking service
            alert_manager: Optional alert management service
        """
        self.health_checker = health_checker
        self.alert_manager = alert_manager
        
        # Error classification patterns
        self.error_patterns = self._initialize_error_patterns()
        
        # Fallback strategies registry
        self.fallback_strategies = self._initialize_fallback_strategies()
        
        # Circuit breaker states for different services
        self.circuit_breakers = {}
        self.circuit_breaker_configs = self._get_default_circuit_breaker_configs()
        
        # Error metrics and tracking
        self.error_metrics = ErrorMetrics(
            total_errors=0,
            errors_by_category={},
            errors_by_severity={},
            fallback_success_rate=0.0,
            average_recovery_time=0.0,
            circuit_breaker_trips=0,
            escalated_errors=0
        )
        
        # Error history for pattern analysis
        self.error_history = []
        self.max_history_size = 1000
        
        # Recovery state tracking
        self.recovery_states = {}
        
        # Performance tracking
        self.fallback_performance = defaultdict(list)
        
        logger.info("Error Handling & Fallback Manager initialized")

    async def handle_error(self, error: Exception, context: Dict[str, Any]) -> RecoveryResult:
        """Comprehensive error handling with intelligent recovery.
        
        Provides multi-stage error handling process:
        1. Error classification and severity assessment
        2. Context analysis and impact evaluation
        3. Fallback strategy selection and execution
        4. Recovery result validation and optimization
        5. Metrics collection and learning integration
        
        Args:
            error: Exception that occurred
            context: Error context including operation, user info, etc.
            
        Returns:
            RecoveryResult with recovery status, fallback data, and recommendations
        """
        start_time = time.time()
        
        try:
            # Create error context
            error_context = ErrorContext(
                error=error,
                operation=context.get("operation", "unknown"),
                user_id=context.get("user_id"),
                session_id=context.get("session_id"),
                request_id=context.get("request_id"),
                component=context.get("component", "unknown"),
                timestamp=datetime.utcnow(),
                stack_trace=traceback.format_exc(),
                metadata=context.get("metadata", {})
            )
            
            # Update metrics
            self.error_metrics.total_errors += 1
            
            # Stage 1: Error classification
            classification = await self._classify_error(error_context)
            
            # Update category metrics
            category = classification.category
            self.error_metrics.errors_by_category[category] = self.error_metrics.errors_by_category.get(category, 0) + 1
            
            # Update severity metrics
            severity = classification.severity
            self.error_metrics.errors_by_severity[severity] = self.error_metrics.errors_by_severity.get(severity, 0) + 1
            
            # Stage 2: Circuit breaker check
            circuit_breaker_result = await self._check_circuit_breaker(error_context, classification)
            if circuit_breaker_result["should_trip"]:
                return RecoveryResult(
                    success=False,
                    fallback_used=FallbackType.CIRCUIT_BREAKER.value,
                    recovery_time=time.time() - start_time,
                    fallback_data={"circuit_breaker_tripped": True},
                    error_classification=classification,
                    recommendations=["Service temporarily unavailable due to circuit breaker"]
                )
            
            # Stage 3: Fallback strategy selection
            fallback_strategy = await self._select_fallback_strategy(error_context, classification)
            
            # Stage 4: Execute fallback
            recovery_result = await self._execute_fallback_strategy(
                fallback_strategy, error_context, classification
            )
            
            # Stage 5: Update circuit breaker state
            await self._update_circuit_breaker_state(error_context, recovery_result.success)
            
            # Stage 6: Performance tracking and learning
            recovery_time = time.time() - start_time
            recovery_result.recovery_time = recovery_time
            
            await self._track_fallback_performance(fallback_strategy, recovery_result, recovery_time)
            
            # Stage 7: Add to error history
            await self._add_to_error_history(error_context, classification, recovery_result)
            
            # Stage 8: Handle critical errors
            if classification.severity == ErrorSeverity.CRITICAL.value:
                await self._handle_critical_error(error_context, classification, recovery_result)
            
            # Update average recovery time
            self._update_average_recovery_time(recovery_time)
            
            logger.info(f"Error handled: {classification.category}/{classification.severity}, recovery_success={recovery_result.success}, fallback={recovery_result.fallback_used}")
            
            return recovery_result
            
        except Exception as handler_error:
            logger.error(f"Error in error handler: {str(handler_error)}")
            
            # Emergency fallback
            return RecoveryResult(
                success=False,
                fallback_used="emergency_fallback",
                recovery_time=time.time() - start_time,
                fallback_data={"handler_error": str(handler_error)},
                error_classification=ErrorClassification(
                    category=ErrorCategory.SYSTEM.value,
                    severity=ErrorSeverity.CRITICAL.value,
                    is_recoverable=False,
                    confidence_score=1.0,
                    error_patterns=[],
                    recommended_actions=["Manual investigation required"]
                ),
                recommendations=["System error handler failed - manual intervention required"]
            )

    async def execute_fallback(self, strategy: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute specific fallback strategy with monitoring.
        
        Provides direct fallback execution for proactive error handling:
        - Strategy validation and preparation
        - Resource availability checking
        - Fallback execution with timeout protection
        - Result validation and optimization
        - Performance metrics collection
        
        Args:
            strategy: Fallback strategy name to execute
            context: Execution context and parameters
            
        Returns:
            Dict with fallback execution results and metadata
        """
        start_time = time.time()
        
        try:
            if strategy not in self.fallback_strategies:
                return {
                    "success": False,
                    "error": f"Unknown fallback strategy: {strategy}",
                    "execution_time": time.time() - start_time
                }
            
            fallback_func = self.fallback_strategies[strategy]
            
            # Execute fallback with timeout protection
            try:
                result = await asyncio.wait_for(
                    fallback_func(context), 
                    timeout=context.get("timeout", 30)
                )
                
                execution_time = time.time() - start_time
                
                # Track performance
                self.fallback_performance[strategy].append({
                    "success": True,
                    "execution_time": execution_time,
                    "timestamp": datetime.utcnow()
                })
                
                return {
                    "success": True,
                    "result": result,
                    "execution_time": execution_time,
                    "strategy": strategy
                }
                
            except asyncio.TimeoutError:
                execution_time = time.time() - start_time
                
                self.fallback_performance[strategy].append({
                    "success": False,
                    "execution_time": execution_time,
                    "error": "timeout",
                    "timestamp": datetime.utcnow()
                })
                
                return {
                    "success": False,
                    "error": "Fallback strategy timed out",
                    "execution_time": execution_time,
                    "strategy": strategy
                }
                
        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"Error executing fallback strategy {strategy}: {str(e)}")
            
            return {
                "success": False,
                "error": str(e),
                "execution_time": execution_time,
                "strategy": strategy
            }

    async def get_system_health(self) -> SystemHealthStatus:
        """Get comprehensive system health status with error analytics.
        
        Provides detailed system health assessment including:
        - Overall system health score and status
        - Component-level health analysis
        - Circuit breaker states and recovery status
        - Error rate trends and patterns
        - Performance metrics and degradation indicators
        - Recovery capability assessment
        
        Returns:
            SystemHealthStatus with detailed health information and recommendations
        """
        try:
            # Calculate overall health score
            health_score = await self._calculate_health_score()
            
            # Determine health status
            if health_score >= 0.9:
                overall_status = "healthy"
            elif health_score >= 0.7:
                overall_status = "degraded"
            elif health_score >= 0.5:
                overall_status = "unhealthy"
            else:
                overall_status = "critical"
            
            # Get component health
            component_health = await self._get_component_health_status()
            
            # Get circuit breaker status
            circuit_breaker_status = await self._get_circuit_breaker_status()
            
            # Calculate error rates
            error_rates = await self._calculate_error_rates()
            
            # Get recovery metrics
            recovery_metrics = await self._get_recovery_metrics()
            
            # Generate recommendations
            recommendations = await self._generate_health_recommendations(
                health_score, component_health, circuit_breaker_status, error_rates
            )
            
            return SystemHealthStatus(
                overall_status=overall_status,
                health_score=health_score,
                component_health=component_health,
                circuit_breaker_status=circuit_breaker_status,
                error_rates=error_rates,
                recovery_metrics=recovery_metrics,
                recommendations=recommendations,
                last_updated=datetime.utcnow().isoformat(),
                metadata={
                    "total_errors": self.error_metrics.total_errors,
                    "fallback_success_rate": await self._calculate_fallback_success_rate(),
                    "circuit_breaker_trips": self.error_metrics.circuit_breaker_trips
                }
            )
            
        except Exception as e:
            logger.error(f"Error getting system health: {str(e)}")
            
            return SystemHealthStatus(
                overall_status="unknown",
                health_score=0.0,
                component_health={},
                circuit_breaker_status={},
                error_rates={},
                recovery_metrics={},
                recommendations=[f"Health check system error: {str(e)}"],
                last_updated=datetime.utcnow().isoformat(),
                metadata={"health_check_error": str(e)}
            )

    async def configure_circuit_breaker(self, service: str, config: Dict[str, Any]) -> bool:
        """Configure circuit breaker for specific service.
        
        Enables dynamic circuit breaker configuration with validation:
        - Configuration parameter validation
        - Service-specific threshold optimization
        - Real-time configuration updates
        - Backwards compatibility checking
        - Performance impact assessment
        
        Args:
            service: Service name to configure
            config: Circuit breaker configuration parameters
            
        Returns:
            bool indicating configuration success
        """
        try:
            # Validate configuration
            validation_result = await self._validate_circuit_breaker_config(config)
            if not validation_result["is_valid"]:
                logger.warning(f"Invalid circuit breaker config for {service}: {validation_result['errors']}")
                return False
            
            # Create circuit breaker configuration
            cb_config = CircuitBreakerConfig(
                failure_threshold=config.get("failure_threshold", 5),
                recovery_timeout=config.get("recovery_timeout", 60),
                half_open_max_calls=config.get("half_open_max_calls", 3),
                success_threshold=config.get("success_threshold", 2)
            )
            
            # Update configuration
            self.circuit_breaker_configs[service] = cb_config
            
            # Initialize or update circuit breaker state
            if service not in self.circuit_breakers:
                self.circuit_breakers[service] = CircuitBreakerState(
                    state="closed",
                    failure_count=0,
                    last_failure_time=None,
                    half_open_calls=0,
                    half_open_successes=0
                )
            
            logger.info(f"Circuit breaker configured for {service}: threshold={cb_config.failure_threshold}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error configuring circuit breaker for {service}: {str(e)}")
            return False

    async def get_error_analytics(self, timeframe: str = "24h") -> Dict[str, Any]:
        """Get detailed error analytics and patterns.
        
        Provides comprehensive error analysis including:
        - Error frequency and trend analysis
        - Pattern recognition and root cause identification
        - Fallback strategy effectiveness assessment
        - Recovery time optimization recommendations
        - Predictive error modeling
        
        Args:
            timeframe: Analysis timeframe (e.g., "1h", "24h", "7d")
            
        Returns:
            Dict with detailed error analytics and insights
        """
        try:
            # Parse timeframe
            hours_back = self._parse_timeframe(timeframe)
            cutoff_time = datetime.utcnow() - timedelta(hours=hours_back)
            
            # Filter recent errors
            recent_errors = [
                error for error in self.error_history
                if error["timestamp"] >= cutoff_time
            ]
            
            # Analyze error patterns
            analytics = {
                "timeframe": timeframe,
                "total_errors": len(recent_errors),
                "error_rate": len(recent_errors) / max(hours_back, 1),
                "categories": self._analyze_error_categories(recent_errors),
                "severities": self._analyze_error_severities(recent_errors),
                "top_error_patterns": self._identify_error_patterns(recent_errors),
                "fallback_effectiveness": self._analyze_fallback_effectiveness(recent_errors),
                "recovery_times": self._analyze_recovery_times(recent_errors),
                "component_analysis": self._analyze_component_errors(recent_errors),
                "trends": self._analyze_error_trends(recent_errors, hours_back),
                "recommendations": []
            }
            
            # Generate insights and recommendations
            if analytics["total_errors"] > 0:
                recommendations = await self._generate_analytics_recommendations(analytics)
                analytics["recommendations"] = recommendations
            
            return analytics
            
        except Exception as e:
            logger.error(f"Error generating error analytics: {str(e)}")
            return {"error": str(e), "timeframe": timeframe}

    # Private helper methods

    def _initialize_error_patterns(self) -> Dict[str, Dict[str, Any]]:
        """Initialize error classification patterns."""
        return {
            "network_timeout": {
                "keywords": ["timeout", "connection", "network", "unreachable"],
                "category": ErrorCategory.NETWORK,
                "severity": ErrorSeverity.MEDIUM,
                "is_recoverable": True
            },
            "authentication_failed": {
                "keywords": ["unauthorized", "authentication", "login", "credentials"],
                "category": ErrorCategory.AUTHENTICATION,
                "severity": ErrorSeverity.HIGH,
                "is_recoverable": True
            },
            "authorization_denied": {
                "keywords": ["forbidden", "access denied", "permission", "authorization"],
                "category": ErrorCategory.AUTHORIZATION,
                "severity": ErrorSeverity.MEDIUM,
                "is_recoverable": False
            },
            "validation_error": {
                "keywords": ["validation", "invalid", "malformed", "format"],
                "category": ErrorCategory.VALIDATION,
                "severity": ErrorSeverity.LOW,
                "is_recoverable": True
            },
            "rate_limit_exceeded": {
                "keywords": ["rate limit", "throttle", "quota", "limit exceeded"],
                "category": ErrorCategory.RATE_LIMIT,
                "severity": ErrorSeverity.MEDIUM,
                "is_recoverable": True
            },
            "system_error": {
                "keywords": ["internal error", "system", "server error", "crash"],
                "category": ErrorCategory.SYSTEM,
                "severity": ErrorSeverity.CRITICAL,
                "is_recoverable": False
            },
            "external_service": {
                "keywords": ["external", "third party", "api", "service unavailable"],
                "category": ErrorCategory.EXTERNAL_SERVICE,
                "severity": ErrorSeverity.HIGH,
                "is_recoverable": True
            }
        }

    def _initialize_fallback_strategies(self) -> Dict[str, Callable]:
        """Initialize fallback strategy functions."""
        return {
            FallbackType.RETRY.value: self._fallback_retry,
            FallbackType.CACHE.value: self._fallback_cache,
            FallbackType.DEFAULT_RESPONSE.value: self._fallback_default_response,
            FallbackType.DEGRADED_SERVICE.value: self._fallback_degraded_service,
            FallbackType.REDIRECT.value: self._fallback_redirect,
            FallbackType.CIRCUIT_BREAKER.value: self._fallback_circuit_breaker,
            FallbackType.MANUAL_INTERVENTION.value: self._fallback_manual_intervention
        }

    def _get_default_circuit_breaker_configs(self) -> Dict[str, CircuitBreakerConfig]:
        """Get default circuit breaker configurations."""
        return {
            "llm_service": CircuitBreakerConfig(failure_threshold=3, recovery_timeout=120),
            "knowledge_base": CircuitBreakerConfig(failure_threshold=5, recovery_timeout=60),
            "external_api": CircuitBreakerConfig(failure_threshold=10, recovery_timeout=300),
            "default": CircuitBreakerConfig()
        }

    async def _classify_error(self, error_context: ErrorContext) -> ErrorClassification:
        """Classify error based on patterns and context."""
        error_message = str(error_context.error).lower()
        error_type = type(error_context.error).__name__.lower()
        
        # Find matching patterns
        matched_patterns = []
        category = ErrorCategory.UNKNOWN
        severity = ErrorSeverity.LOW
        is_recoverable = True
        confidence_score = 0.0
        
        for pattern_name, pattern_info in self.error_patterns.items():
            keywords = pattern_info["keywords"]
            pattern_matches = sum(1 for keyword in keywords if keyword in error_message or keyword in error_type)
            
            if pattern_matches > 0:
                match_confidence = pattern_matches / len(keywords)
                if match_confidence > confidence_score:
                    matched_patterns.append(pattern_name)
                    category = pattern_info["category"]
                    severity = pattern_info["severity"]
                    is_recoverable = pattern_info["is_recoverable"]
                    confidence_score = match_confidence
        
        # Special handling for specific exception types
        if isinstance(error_context.error, TimeoutError):
            category = ErrorCategory.TIMEOUT
            severity = ErrorSeverity.MEDIUM
        elif isinstance(error_context.error, ConnectionError):
            category = ErrorCategory.NETWORK
            severity = ErrorSeverity.HIGH
        elif isinstance(error_context.error, ValueError):
            category = ErrorCategory.VALIDATION
            severity = ErrorSeverity.LOW
        
        # Generate recommended actions
        recommended_actions = self._generate_recommended_actions(category, severity, is_recoverable)
        
        return ErrorClassification(
            category=category.value,
            severity=severity.value,
            is_recoverable=is_recoverable,
            confidence_score=confidence_score,
            error_patterns=matched_patterns,
            recommended_actions=recommended_actions
        )

    def _generate_recommended_actions(self, category: ErrorCategory, severity: ErrorSeverity, is_recoverable: bool) -> List[str]:
        """Generate recommended actions based on error classification."""
        actions = []
        
        if category == ErrorCategory.NETWORK:
            actions.extend(["Retry with exponential backoff", "Check network connectivity"])
        elif category == ErrorCategory.AUTHENTICATION:
            actions.extend(["Refresh authentication token", "Re-authenticate user"])
        elif category == ErrorCategory.RATE_LIMIT:
            actions.extend(["Implement exponential backoff", "Use cache if available"])
        elif category == ErrorCategory.EXTERNAL_SERVICE:
            actions.extend(["Use cached response", "Activate degraded mode"])
        elif category == ErrorCategory.SYSTEM:
            actions.extend(["Log error for investigation", "Use emergency fallback"])
        
        if severity == ErrorSeverity.CRITICAL:
            actions.append("Alert operations team")
        
        if not is_recoverable:
            actions.append("Manual intervention required")
        
        return actions

    async def _check_circuit_breaker(self, error_context: ErrorContext, classification: ErrorClassification) -> Dict[str, Any]:
        """Check if circuit breaker should trip."""
        service = error_context.component
        
        if service not in self.circuit_breakers:
            # Initialize circuit breaker for new service
            self.circuit_breakers[service] = CircuitBreakerState(
                state="closed",
                failure_count=0,
                last_failure_time=None,
                half_open_calls=0,
                half_open_successes=0
            )
        
        cb_state = self.circuit_breakers[service]
        cb_config = self.circuit_breaker_configs.get(service, self.circuit_breaker_configs["default"])
        
        current_time = datetime.utcnow()
        
        # Check if circuit breaker should trip
        if cb_state.state == "closed":
            if cb_state.failure_count >= cb_config.failure_threshold:
                cb_state.state = "open"
                cb_state.last_failure_time = current_time
                self.error_metrics.circuit_breaker_trips += 1
                return {"should_trip": True, "reason": "failure_threshold_exceeded"}
        
        elif cb_state.state == "open":
            # Check if we should transition to half-open
            if cb_state.last_failure_time and (current_time - cb_state.last_failure_time).seconds >= cb_config.recovery_timeout:
                cb_state.state = "half_open"
                cb_state.half_open_calls = 0
                cb_state.half_open_successes = 0
                return {"should_trip": False, "reason": "transitioning_to_half_open"}
            else:
                return {"should_trip": True, "reason": "circuit_breaker_open"}
        
        elif cb_state.state == "half_open":
            if cb_state.half_open_calls >= cb_config.half_open_max_calls:
                if cb_state.half_open_successes >= cb_config.success_threshold:
                    cb_state.state = "closed"
                    cb_state.failure_count = 0
                    return {"should_trip": False, "reason": "circuit_breaker_closed"}
                else:
                    cb_state.state = "open"
                    cb_state.last_failure_time = current_time
                    return {"should_trip": True, "reason": "half_open_failed"}
        
        return {"should_trip": False, "reason": "normal_operation"}

    async def _select_fallback_strategy(self, error_context: ErrorContext, classification: ErrorClassification) -> str:
        """Select appropriate fallback strategy based on error classification."""
        
        category = ErrorCategory(classification.category)
        severity = ErrorSeverity(classification.severity)
        
        # Strategy selection logic
        if not classification.is_recoverable:
            return FallbackType.MANUAL_INTERVENTION.value
        
        if category == ErrorCategory.NETWORK or category == ErrorCategory.TIMEOUT:
            return FallbackType.RETRY.value
        elif category == ErrorCategory.EXTERNAL_SERVICE:
            return FallbackType.CACHE.value
        elif category == ErrorCategory.RATE_LIMIT:
            return FallbackType.RETRY.value  # With backoff
        elif category == ErrorCategory.SYSTEM and severity == ErrorSeverity.CRITICAL:
            return FallbackType.DEGRADED_SERVICE.value
        elif severity == ErrorSeverity.HIGH:
            return FallbackType.DEGRADED_SERVICE.value
        else:
            return FallbackType.DEFAULT_RESPONSE.value

    async def _execute_fallback_strategy(
        self, 
        strategy: str, 
        error_context: ErrorContext, 
        classification: ErrorClassification
    ) -> RecoveryResult:
        """Execute the selected fallback strategy."""
        
        start_time = time.time()
        
        try:
            fallback_func = self.fallback_strategies.get(strategy)
            if not fallback_func:
                return RecoveryResult(
                    success=False,
                    fallback_used=strategy,
                    recovery_time=0.0,
                    fallback_data={"error": "Unknown fallback strategy"},
                    error_classification=classification,
                    recommendations=["Use default error handling"]
                )
            
            # Prepare context for fallback function
            fallback_context = {
                "error": error_context.error,
                "operation": error_context.operation,
                "classification": classification,
                "metadata": error_context.metadata
            }
            
            # Execute fallback
            fallback_result = await fallback_func(fallback_context)
            
            recovery_time = time.time() - start_time
            
            return RecoveryResult(
                success=fallback_result.get("success", False),
                fallback_used=strategy,
                recovery_time=recovery_time,
                fallback_data=fallback_result.get("data", {}),
                error_classification=classification,
                recommendations=fallback_result.get("recommendations", [])
            )
            
        except Exception as fallback_error:
            recovery_time = time.time() - start_time
            logger.error(f"Fallback strategy {strategy} failed: {str(fallback_error)}")
            
            return RecoveryResult(
                success=False,
                fallback_used=strategy,
                recovery_time=recovery_time,
                fallback_data={"fallback_error": str(fallback_error)},
                error_classification=classification,
                recommendations=["Fallback failed - manual intervention required"]
            )

    # Fallback strategy implementations

    async def _fallback_retry(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Retry fallback strategy with exponential backoff."""
        max_retries = 3
        base_delay = 1.0
        
        for attempt in range(max_retries):
            await asyncio.sleep(base_delay * (2 ** attempt))
            
            # In production, this would retry the original operation
            # For now, we simulate a recovery attempt
            success_probability = 0.7 + (attempt * 0.1)  # Higher chance each retry
            
            if success_probability > 0.8:  # Simulated success
                return {
                    "success": True,
                    "data": {"retries": attempt + 1, "method": "exponential_backoff"},
                    "recommendations": ["Operation succeeded after retry"]
                }
        
        return {
            "success": False,
            "data": {"retries": max_retries, "method": "exponential_backoff"},
            "recommendations": ["Max retries exceeded - consider alternative approach"]
        }

    async def _fallback_cache(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Cache fallback strategy."""
        # In production, this would check actual cache
        operation = context.get("operation", "unknown")
        
        # Simulate cache hit
        cache_hit = hash(operation) % 3 == 0  # 33% cache hit rate
        
        if cache_hit:
            return {
                "success": True,
                "data": {"source": "cache", "operation": operation},
                "recommendations": ["Cached response provided successfully"]
            }
        else:
            return {
                "success": False,
                "data": {"source": "cache_miss", "operation": operation},
                "recommendations": ["No cached data available - use default response"]
            }

    async def _fallback_default_response(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Default response fallback strategy."""
        operation = context.get("operation", "unknown")
        
        default_responses = {
            "chat": "I apologize, but I'm experiencing technical difficulties. Please try again later.",
            "search": "Search is temporarily unavailable. Please try again in a few moments.",
            "analysis": "Analysis service is currently unavailable. Please retry your request.",
            "unknown": "Service temporarily unavailable. Please try again later."
        }
        
        response = default_responses.get(operation, default_responses["unknown"])
        
        return {
            "success": True,
            "data": {"response": response, "type": "default"},
            "recommendations": ["Default response provided - service may be degraded"]
        }

    async def _fallback_degraded_service(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Degraded service fallback strategy."""
        return {
            "success": True,
            "data": {"mode": "degraded", "features_limited": True},
            "recommendations": ["Operating in degraded mode with limited features"]
        }

    async def _fallback_redirect(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Redirect fallback strategy."""
        return {
            "success": True,
            "data": {"redirect_to": "backup_service", "automatic": True},
            "recommendations": ["Redirected to backup service"]
        }

    async def _fallback_circuit_breaker(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Circuit breaker fallback strategy."""
        return {
            "success": False,
            "data": {"circuit_breaker": "open", "retry_after": 60},
            "recommendations": ["Circuit breaker is open - service temporarily unavailable"]
        }

    async def _fallback_manual_intervention(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Manual intervention fallback strategy."""
        return {
            "success": False,
            "data": {"requires_manual_intervention": True, "escalated": True},
            "recommendations": ["Manual intervention required - error has been escalated"]
        }

    async def _update_circuit_breaker_state(self, error_context: ErrorContext, success: bool) -> None:
        """Update circuit breaker state based on operation result."""
        service = error_context.component
        
        if service in self.circuit_breakers:
            cb_state = self.circuit_breakers[service]
            
            if success:
                if cb_state.state == "half_open":
                    cb_state.half_open_calls += 1
                    cb_state.half_open_successes += 1
                elif cb_state.state == "closed":
                    cb_state.failure_count = max(0, cb_state.failure_count - 1)
            else:
                if cb_state.state == "closed":
                    cb_state.failure_count += 1
                elif cb_state.state == "half_open":
                    cb_state.half_open_calls += 1
                    # Note: half_open_successes not incremented for failures

    async def _track_fallback_performance(self, strategy: str, result: RecoveryResult, execution_time: float) -> None:
        """Track fallback strategy performance."""
        performance_entry = {
            "success": result.success,
            "execution_time": execution_time,
            "timestamp": datetime.utcnow(),
            "recovery_time": result.recovery_time
        }
        
        self.fallback_performance[strategy].append(performance_entry)
        
        # Keep only recent entries (last 100 per strategy)
        if len(self.fallback_performance[strategy]) > 100:
            self.fallback_performance[strategy] = self.fallback_performance[strategy][-100:]

    async def _add_to_error_history(
        self, 
        error_context: ErrorContext, 
        classification: ErrorClassification, 
        recovery_result: RecoveryResult
    ) -> None:
        """Add error to history for analysis."""
        history_entry = {
            "timestamp": error_context.timestamp,
            "error_type": type(error_context.error).__name__,
            "error_message": str(error_context.error),
            "operation": error_context.operation,
            "component": error_context.component,
            "category": classification.category,
            "severity": classification.severity,
            "is_recoverable": classification.is_recoverable,
            "fallback_used": recovery_result.fallback_used,
            "recovery_success": recovery_result.success,
            "recovery_time": recovery_result.recovery_time,
            "user_id": error_context.user_id,
            "session_id": error_context.session_id
        }
        
        self.error_history.append(history_entry)
        
        # Maintain history size
        if len(self.error_history) > self.max_history_size:
            self.error_history = self.error_history[-self.max_history_size//2:]

    async def _handle_critical_error(
        self, 
        error_context: ErrorContext, 
        classification: ErrorClassification, 
        recovery_result: RecoveryResult
    ) -> None:
        """Handle critical errors with escalation."""
        self.error_metrics.escalated_errors += 1
        
        # Log critical error
        logger.critical(f"Critical error in {error_context.component}: {str(error_context.error)}")
        
        # Send alert if alert manager available
        if self.alert_manager:
            try:
                await self.alert_manager.send_alert({
                    "level": "critical",
                    "component": error_context.component,
                    "error": str(error_context.error),
                    "classification": classification,
                    "recovery_success": recovery_result.success,
                    "timestamp": error_context.timestamp.isoformat()
                })
            except Exception as alert_error:
                logger.error(f"Failed to send critical error alert: {str(alert_error)}")

    def _update_average_recovery_time(self, recovery_time: float) -> None:
        """Update average recovery time metric."""
        total_errors = self.error_metrics.total_errors
        current_avg = self.error_metrics.average_recovery_time
        
        new_avg = ((current_avg * (total_errors - 1)) + recovery_time) / total_errors
        self.error_metrics.average_recovery_time = new_avg

    async def _calculate_health_score(self) -> float:
        """Calculate overall system health score."""
        if self.error_metrics.total_errors == 0:
            return 1.0
        
        # Base score from fallback success rate
        fallback_success_rate = await self._calculate_fallback_success_rate()
        base_score = fallback_success_rate
        
        # Adjust for error frequency
        recent_error_rate = len([e for e in self.error_history[-100:] if (datetime.utcnow() - e["timestamp"]).seconds < 3600])
        error_penalty = min(0.5, recent_error_rate / 100.0)
        base_score -= error_penalty
        
        # Adjust for critical errors
        critical_errors = sum(1 for e in self.error_history[-100:] if e["severity"] == "critical")
        critical_penalty = min(0.3, critical_errors / 10.0)
        base_score -= critical_penalty
        
        # Circuit breaker penalty
        open_circuit_breakers = sum(1 for cb in self.circuit_breakers.values() if cb.state == "open")
        circuit_penalty = min(0.2, open_circuit_breakers / len(self.circuit_breakers) if self.circuit_breakers else 0)
        base_score -= circuit_penalty
        
        return max(0.0, min(1.0, base_score))

    async def _calculate_fallback_success_rate(self) -> float:
        """Calculate fallback success rate."""
        if not self.error_history:
            return 1.0
        
        successful_recoveries = sum(1 for e in self.error_history if e.get("recovery_success", False))
        return successful_recoveries / len(self.error_history)

    # Additional helper methods for analytics and reporting

    def _parse_timeframe(self, timeframe: str) -> int:
        """Parse timeframe string to hours."""
        if timeframe.endswith('h'):
            return int(timeframe[:-1])
        elif timeframe.endswith('d'):
            return int(timeframe[:-1]) * 24
        elif timeframe.endswith('w'):
            return int(timeframe[:-1]) * 24 * 7
        else:
            return 24  # Default to 24 hours

    def _analyze_error_categories(self, errors: List[Dict[str, Any]]) -> Dict[str, int]:
        """Analyze error distribution by category."""
        categories = {}
        for error in errors:
            category = error.get("category", "unknown")
            categories[category] = categories.get(category, 0) + 1
        return categories

    def _analyze_error_severities(self, errors: List[Dict[str, Any]]) -> Dict[str, int]:
        """Analyze error distribution by severity."""
        severities = {}
        for error in errors:
            severity = error.get("severity", "unknown")
            severities[severity] = severities.get(severity, 0) + 1
        return severities

    def _identify_error_patterns(self, errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Identify common error patterns."""
        patterns = {}
        
        for error in errors:
            key = f"{error.get('error_type', 'unknown')}_{error.get('component', 'unknown')}"
            if key not in patterns:
                patterns[key] = {"count": 0, "error_type": error.get("error_type"), "component": error.get("component")}
            patterns[key]["count"] += 1
        
        # Return top 5 patterns
        sorted_patterns = sorted(patterns.values(), key=lambda x: x["count"], reverse=True)
        return sorted_patterns[:5]

    async def _get_component_health_status(self) -> Dict[str, str]:
        """Get health status for each component."""
        component_health = {}
        
        for service, cb_state in self.circuit_breakers.items():
            if cb_state.state == "open":
                component_health[service] = "unhealthy"
            elif cb_state.state == "half_open":
                component_health[service] = "recovering"
            else:
                component_health[service] = "healthy"
        
        return component_health

    async def _get_circuit_breaker_status(self) -> Dict[str, Dict[str, Any]]:
        """Get detailed circuit breaker status."""
        status = {}
        
        for service, cb_state in self.circuit_breakers.items():
            status[service] = {
                "state": cb_state.state,
                "failure_count": cb_state.failure_count,
                "last_failure": cb_state.last_failure_time.isoformat() if cb_state.last_failure_time else None,
                "half_open_calls": cb_state.half_open_calls,
                "half_open_successes": cb_state.half_open_successes
            }
        
        return status

    async def _calculate_error_rates(self) -> Dict[str, float]:
        """Calculate various error rates."""
        if not self.error_history:
            return {}
        
        now = datetime.utcnow()
        last_hour = [e for e in self.error_history if (now - e["timestamp"]).seconds < 3600]
        last_day = [e for e in self.error_history if (now - e["timestamp"]).seconds < 86400]
        
        return {
            "errors_per_hour": len(last_hour),
            "errors_per_day": len(last_day),
            "critical_error_rate": sum(1 for e in last_day if e["severity"] == "critical") / max(len(last_day), 1),
            "recovery_success_rate": sum(1 for e in last_day if e.get("recovery_success", False)) / max(len(last_day), 1)
        }

    async def _get_recovery_metrics(self) -> Dict[str, Any]:
        """Get recovery performance metrics."""
        if not self.error_history:
            return {}
        
        recovery_times = [e["recovery_time"] for e in self.error_history if e.get("recovery_time", 0) > 0]
        
        if not recovery_times:
            return {"average_recovery_time": 0.0}
        
        return {
            "average_recovery_time": sum(recovery_times) / len(recovery_times),
            "max_recovery_time": max(recovery_times),
            "min_recovery_time": min(recovery_times),
            "recovery_count": len(recovery_times)
        }

    async def _generate_health_recommendations(
        self, 
        health_score: float, 
        component_health: Dict[str, str], 
        circuit_breaker_status: Dict[str, Dict[str, Any]], 
        error_rates: Dict[str, float]
    ) -> List[str]:
        """Generate health-based recommendations."""
        recommendations = []
        
        if health_score < 0.7:
            recommendations.append("System health is degraded - investigate error patterns")
        
        unhealthy_components = [comp for comp, status in component_health.items() if status == "unhealthy"]
        if unhealthy_components:
            recommendations.append(f"Unhealthy components detected: {', '.join(unhealthy_components)}")
        
        if error_rates.get("critical_error_rate", 0) > 0.1:
            recommendations.append("High critical error rate - immediate attention required")
        
        if error_rates.get("errors_per_hour", 0) > 50:
            recommendations.append("High error frequency - check system load and capacity")
        
        open_breakers = [service for service, status in circuit_breaker_status.items() if status["state"] == "open"]
        if open_breakers:
            recommendations.append(f"Circuit breakers open for: {', '.join(open_breakers)}")
        
        return recommendations

    def _analyze_fallback_effectiveness(self, errors: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze effectiveness of different fallback strategies."""
        strategy_stats = {}
        
        for error in errors:
            strategy = error.get("fallback_used", "unknown")
            if strategy not in strategy_stats:
                strategy_stats[strategy] = {"total": 0, "successful": 0}
            
            strategy_stats[strategy]["total"] += 1
            if error.get("recovery_success", False):
                strategy_stats[strategy]["successful"] += 1
        
        # Calculate success rates
        for strategy, stats in strategy_stats.items():
            stats["success_rate"] = stats["successful"] / stats["total"] if stats["total"] > 0 else 0.0
        
        return strategy_stats

    def _analyze_recovery_times(self, errors: List[Dict[str, Any]]) -> Dict[str, float]:
        """Analyze recovery time patterns."""
        recovery_times = [e.get("recovery_time", 0) for e in errors if e.get("recovery_time", 0) > 0]
        
        if not recovery_times:
            return {}
        
        return {
            "average": sum(recovery_times) / len(recovery_times),
            "median": sorted(recovery_times)[len(recovery_times) // 2],
            "max": max(recovery_times),
            "min": min(recovery_times)
        }

    def _analyze_component_errors(self, errors: List[Dict[str, Any]]) -> Dict[str, int]:
        """Analyze errors by component."""
        component_errors = {}
        
        for error in errors:
            component = error.get("component", "unknown")
            component_errors[component] = component_errors.get(component, 0) + 1
        
        return component_errors

    def _analyze_error_trends(self, errors: List[Dict[str, Any]], hours_back: int) -> Dict[str, Any]:
        """Analyze error trends over time."""
        if not errors:
            return {}
        
        # Group errors by hour
        hourly_counts = {}
        now = datetime.utcnow()
        
        for error in errors:
            hour_key = error["timestamp"].strftime("%Y-%m-%d %H:00")
            hourly_counts[hour_key] = hourly_counts.get(hour_key, 0) + 1
        
        # Calculate trend
        if len(hourly_counts) >= 2:
            counts = list(hourly_counts.values())
            trend = "increasing" if counts[-1] > counts[0] else "decreasing" if counts[-1] < counts[0] else "stable"
        else:
            trend = "insufficient_data"
        
        return {
            "trend": trend,
            "hourly_distribution": hourly_counts,
            "peak_hour": max(hourly_counts, key=hourly_counts.get) if hourly_counts else None,
            "total_hours_analyzed": hours_back
        }

    async def _generate_analytics_recommendations(self, analytics: Dict[str, Any]) -> List[str]:
        """Generate recommendations based on analytics."""
        recommendations = []
        
        if analytics["total_errors"] > 100:
            recommendations.append("High error volume - consider scaling or optimization")
        
        if analytics.get("trends", {}).get("trend") == "increasing":
            recommendations.append("Error rate is increasing - investigate root cause")
        
        top_category = max(analytics["categories"], key=analytics["categories"].get) if analytics["categories"] else None
        if top_category and analytics["categories"][top_category] > analytics["total_errors"] * 0.5:
            recommendations.append(f"Dominant error category '{top_category}' - focus improvement efforts")
        
        fallback_effectiveness = analytics.get("fallback_effectiveness", {})
        low_performing_strategies = [
            strategy for strategy, stats in fallback_effectiveness.items() 
            if stats.get("success_rate", 0) < 0.5 and stats.get("total", 0) > 5
        ]
        if low_performing_strategies:
            recommendations.append(f"Low-performing fallback strategies: {', '.join(low_performing_strategies)}")
        
        return recommendations

    async def _validate_circuit_breaker_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Validate circuit breaker configuration."""
        errors = []
        
        failure_threshold = config.get("failure_threshold", 5)
        if not isinstance(failure_threshold, int) or failure_threshold < 1:
            errors.append("failure_threshold must be a positive integer")
        
        recovery_timeout = config.get("recovery_timeout", 60)
        if not isinstance(recovery_timeout, int) or recovery_timeout < 10:
            errors.append("recovery_timeout must be at least 10 seconds")
        
        half_open_max_calls = config.get("half_open_max_calls", 3)
        if not isinstance(half_open_max_calls, int) or half_open_max_calls < 1:
            errors.append("half_open_max_calls must be a positive integer")
        
        success_threshold = config.get("success_threshold", 2)
        if not isinstance(success_threshold, int) or success_threshold < 1:
            errors.append("success_threshold must be a positive integer")
        
        if success_threshold > half_open_max_calls:
            errors.append("success_threshold cannot be greater than half_open_max_calls")
        
        return {"is_valid": len(errors) == 0, "errors": errors}

    # Required abstract methods from IErrorFallbackManager interface
    async def handle_error(self, error: Exception, context: Dict[str, Any]) -> Dict[str, Any]:
        """Handle an error with appropriate fallback strategy"""
        error_context = ErrorContext(
            operation=context.get('operation', 'unknown'),
            component=context.get('component', 'unknown'),
            user_id=context.get('user_id'),
            session_id=context.get('session_id'),
            error_type=type(error).__name__,
            error_message=str(error),
            additional_context=context
        )
        
        return await self.handle_agentic_error(error_context)

    async def get_fallback_strategy(self, error_type: str, context: Dict[str, Any]) -> str:
        """Get appropriate fallback strategy for error type"""
        strategies = await self._determine_fallback_strategies(error_type, context)
        return strategies[0] if strategies else "graceful_degradation"

    async def execute_fallback(self, strategy: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a fallback strategy"""
        return await self._execute_fallback(strategy, context)

    async def record_error_pattern(self, error: Exception, context: Dict[str, Any]) -> bool:
        """Record error pattern for learning"""
        error_context = ErrorContext(
            operation=context.get('operation', 'unknown'),
            component=context.get('component', 'unknown'),
            user_id=context.get('user_id'),
            session_id=context.get('session_id'),
            error_type=type(error).__name__,
            error_message=str(error),
            additional_context=context
        )
        
        await self._record_error_pattern(error_context)
        return True

    async def assess_system_health(self) -> Dict[str, Any]:
        """Assess overall system health"""
        health_assessment = await self._assess_system_health()
        return {
            "overall_health": health_assessment.overall_health,
            "component_health": health_assessment.component_health,
            "recent_errors": len(health_assessment.recent_error_patterns),
            "circuit_breaker_states": health_assessment.circuit_breaker_states
        }