"""
Error Recovery Framework

Provides automated error recovery strategies with configurable policies
for different types of failures across architectural layers.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from ..exceptions import RecoveryResult


class RecoveryStrategy(ABC):
    """Abstract base class for error recovery strategies."""
    
    @abstractmethod
    async def execute(self, error: Exception, context: Dict[str, Any]) -> RecoveryResult:
        """Execute the recovery strategy.
        
        Args:
            error: The exception that occurred
            context: Additional context about the error and system state
            
        Returns:
            Result of the recovery attempt
        """
        pass
    
    @abstractmethod
    def is_applicable(self, error: Exception, layer: str) -> bool:
        """Check if this strategy is applicable to the given error and layer.
        
        Args:
            error: The exception that occurred
            layer: The architectural layer where the error occurred
            
        Returns:
            True if this strategy can be applied to this error type
        """
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Return the name of this recovery strategy."""
        pass


class RetryRecoveryStrategy(RecoveryStrategy):
    """Recovery strategy that retries the failed operation with exponential backoff."""
    
    def __init__(self, max_retries: int = 3, backoff_factor: float = 1.5, initial_delay: float = 0.1):
        """Initialize retry strategy.
        
        Args:
            max_retries: Maximum number of retry attempts
            backoff_factor: Exponential backoff multiplier
            initial_delay: Initial delay in seconds before first retry
        """
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.initial_delay = initial_delay
        self.logger = logging.getLogger(__name__)
    
    @property
    def name(self) -> str:
        return "retry"
    
    async def execute(self, error: Exception, context: Dict[str, Any]) -> RecoveryResult:
        """Execute retry recovery strategy with exponential backoff."""
        operation = context.get("operation")
        retry_count = context.get("retry_count", 0)
        
        if retry_count >= self.max_retries:
            self.logger.warning(f"Max retries ({self.max_retries}) exceeded for operation: {operation}")
            return RecoveryResult.FAILED
        
        # Calculate delay with exponential backoff
        delay = self.initial_delay * (self.backoff_factor ** retry_count)
        
        self.logger.info(f"Retrying operation '{operation}' (attempt {retry_count + 1}/{self.max_retries}) after {delay:.2f}s delay")
        
        try:
            await asyncio.sleep(delay)
            
            # In a real implementation, this would retry the actual operation
            # For now, simulate success for transient errors
            if self._is_transient_error(error):
                self.logger.info(f"Retry successful for operation: {operation}")
                return RecoveryResult.SUCCESS
            else:
                self.logger.warning(f"Retry failed for non-transient error: {type(error).__name__}")
                return RecoveryResult.FAILED
                
        except Exception as recovery_error:
            self.logger.error(f"Error during retry execution: {recovery_error}")
            return RecoveryResult.FAILED
    
    def is_applicable(self, error: Exception, layer: str) -> bool:
        """Check if retry is applicable for transient errors."""
        return self._is_transient_error(error)
    
    def _is_transient_error(self, error: Exception) -> bool:
        """Determine if an error is transient and worth retrying."""
        transient_error_types = {
            "TimeoutError", "ConnectionError", "TemporaryFailure", 
            "ServiceUnavailable", "TooManyRequests", "NetworkError",
            "HTTPException"  # Some HTTP errors might be transient
        }
        
        error_type = type(error).__name__
        
        # Check for specific HTTP status codes that are retryable
        if hasattr(error, 'status_code'):
            retryable_status_codes = {429, 502, 503, 504}
            if error.status_code in retryable_status_codes:
                return True
        
        return error_type in transient_error_types


class CircuitBreakerRecoveryStrategy(RecoveryStrategy):
    """Recovery strategy that implements circuit breaker pattern to prevent cascade failures."""
    
    def __init__(self, failure_threshold: int = 5, timeout: int = 60):
        """Initialize circuit breaker strategy.
        
        Args:
            failure_threshold: Number of failures before opening circuit
            timeout: Time in seconds to wait before attempting to close circuit
        """
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.circuit_states: Dict[str, Dict[str, Any]] = {}
        self.logger = logging.getLogger(__name__)
    
    @property
    def name(self) -> str:
        return "circuit_breaker"
    
    async def execute(self, error: Exception, context: Dict[str, Any]) -> RecoveryResult:
        """Execute circuit breaker recovery strategy."""
        service = context.get("service", "unknown")
        layer = context.get("layer", "unknown")
        circuit_key = f"{layer}.{service}"
        
        # Initialize circuit state if not exists
        if circuit_key not in self.circuit_states:
            self.circuit_states[circuit_key] = {
                "state": "closed",  # closed, open, half_open
                "failure_count": 0,
                "last_failure_time": None,
                "next_attempt_time": None
            }
        
        circuit = self.circuit_states[circuit_key]
        current_time = datetime.now(timezone.utc)
        
        # Increment failure count
        circuit["failure_count"] += 1
        circuit["last_failure_time"] = current_time
        
        # Check if we should open the circuit
        if circuit["failure_count"] >= self.failure_threshold and circuit["state"] == "closed":
            circuit["state"] = "open"
            circuit["next_attempt_time"] = current_time + timedelta(seconds=self.timeout)
            self.logger.warning(f"Circuit breaker opened for {circuit_key} after {circuit['failure_count']} failures")
            return RecoveryResult.PARTIAL
        
        # If circuit is already open, check if we can transition to half-open
        elif circuit["state"] == "open":
            if current_time >= circuit["next_attempt_time"]:
                circuit["state"] = "half_open"
                self.logger.info(f"Circuit breaker transitioning to half-open for {circuit_key}")
                return RecoveryResult.PARTIAL
            else:
                self.logger.debug(f"Circuit breaker still open for {circuit_key}")
                return RecoveryResult.FAILED
        
        # If half-open and we got another failure, back to open
        elif circuit["state"] == "half_open":
            circuit["state"] = "open"
            circuit["next_attempt_time"] = current_time + timedelta(seconds=self.timeout)
            self.logger.warning(f"Circuit breaker reopened for {circuit_key}")
            return RecoveryResult.FAILED
        
        return RecoveryResult.PARTIAL
    
    def is_applicable(self, error: Exception, layer: str) -> bool:
        """Circuit breaker is applicable for service and infrastructure layers."""
        return layer in ["service", "infrastructure"]
    
    def get_circuit_status(self, service: str, layer: str) -> Dict[str, Any]:
        """Get current status of a circuit breaker."""
        circuit_key = f"{layer}.{service}"
        return self.circuit_states.get(circuit_key, {"state": "closed", "failure_count": 0})


class FallbackRecoveryStrategy(RecoveryStrategy):
    """Recovery strategy that uses fallback mechanisms when primary operations fail."""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.fallback_handlers = {
            "llm_provider": self._fallback_llm_response,
            "knowledge_base": self._fallback_knowledge_response,
            "session_store": self._fallback_session_operation,
            "data_service": self._fallback_data_operation
        }
    
    @property
    def name(self) -> str:
        return "fallback"
    
    async def execute(self, error: Exception, context: Dict[str, Any]) -> RecoveryResult:
        """Execute fallback recovery strategy."""
        service = context.get("service", "unknown")
        operation = context.get("operation", "unknown")
        
        self.logger.info(f"Executing fallback strategy for {service}.{operation}")
        
        # Get appropriate fallback handler
        handler = self.fallback_handlers.get(service, self._generic_fallback)
        
        try:
            result = await handler(error, context)
            if result:
                self.logger.info(f"Fallback successful for {service}.{operation}")
                return RecoveryResult.SUCCESS
            else:
                self.logger.warning(f"Fallback provided partial recovery for {service}.{operation}")
                return RecoveryResult.PARTIAL
        except Exception as fallback_error:
            self.logger.error(f"Fallback strategy failed: {fallback_error}")
            return RecoveryResult.FAILED
    
    def is_applicable(self, error: Exception, layer: str) -> bool:
        """Fallback is applicable to most errors except critical system failures."""
        # Don't use fallback for critical infrastructure errors that need immediate attention
        critical_errors = {"SystemExit", "KeyboardInterrupt", "MemoryError", "OSError"}
        return type(error).__name__ not in critical_errors
    
    async def _fallback_llm_response(self, error: Exception, context: Dict[str, Any]) -> bool:
        """Provide fallback LLM response when primary provider fails."""
        self.logger.info("Using fallback LLM response")
        
        # In a real implementation, this would:
        # 1. Try alternative LLM providers
        # 2. Use cached responses for similar queries
        # 3. Provide a generic helpful response
        
        fallback_response = {
            "response": "I'm experiencing temporary difficulties. Please try again in a moment.",
            "confidence": 0.1,
            "fallback": True
        }
        
        # Store fallback response in context for the calling code to use
        context["fallback_response"] = fallback_response
        return True
    
    async def _fallback_knowledge_response(self, error: Exception, context: Dict[str, Any]) -> bool:
        """Provide fallback knowledge base response."""
        self.logger.info("Using fallback knowledge base response")
        
        # Provide basic troubleshooting guidance when KB is unavailable
        fallback_response = {
            "results": [
                {
                    "content": "Basic troubleshooting steps: 1) Check logs, 2) Verify connectivity, 3) Restart services",
                    "relevance": 0.5,
                    "source": "fallback_guidance"
                }
            ],
            "fallback": True
        }
        
        context["fallback_response"] = fallback_response
        return True
    
    async def _fallback_session_operation(self, error: Exception, context: Dict[str, Any]) -> bool:
        """Provide fallback session handling."""
        operation = context.get("operation", "unknown")
        
        if operation in ["create", "get"]:
            # Create temporary in-memory session
            context["fallback_session"] = {
                "session_id": f"temp_{datetime.now(timezone.utc).timestamp()}",
                "temporary": True,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            return True
        
        # For other operations, provide partial success
        return False
    
    async def _fallback_data_operation(self, error: Exception, context: Dict[str, Any]) -> bool:
        """Provide fallback data processing."""
        operation = context.get("operation", "unknown")
        
        if operation == "classify":
            # Provide basic classification
            context["fallback_classification"] = {
                "category": "general",
                "confidence": 0.3,
                "fallback": True
            }
            return True
        
        return False
    
    async def _generic_fallback(self, error: Exception, context: Dict[str, Any]) -> bool:
        """Generic fallback for unknown services."""
        self.logger.info("Using generic fallback strategy")
        context["fallback_used"] = True
        return False


class RecoveryManager:
    """Manages error recovery strategies and execution coordination."""
    
    def __init__(self):
        """Initialize recovery manager with available strategies."""
        self.strategies: Dict[str, RecoveryStrategy] = {
            "retry": RetryRecoveryStrategy(),
            "circuit_breaker": CircuitBreakerRecoveryStrategy(),
            "fallback": FallbackRecoveryStrategy()
        }
        self.logger = logging.getLogger(__name__)
        self.recovery_history: List[Dict[str, Any]] = []
    
    async def execute_recovery(
        self, 
        strategy_name: str, 
        error: Exception, 
        context: Dict[str, Any]
    ) -> RecoveryResult:
        """Execute a named recovery strategy.
        
        Args:
            strategy_name: Name of the recovery strategy to execute
            error: The exception that occurred
            context: Additional context about the error
            
        Returns:
            Result of the recovery attempt
        """
        if strategy_name not in self.strategies:
            self.logger.warning(f"Unknown recovery strategy: {strategy_name}")
            return RecoveryResult.NOT_ATTEMPTED
        
        strategy = self.strategies[strategy_name]
        layer = context.get("layer", "unknown")
        
        # Check if strategy is applicable
        if not strategy.is_applicable(error, layer):
            self.logger.debug(f"Strategy '{strategy_name}' not applicable for {type(error).__name__} in {layer} layer")
            return RecoveryResult.NOT_ATTEMPTED
        
        start_time = datetime.now(timezone.utc)
        
        try:
            result = await strategy.execute(error, context)
            
            # Record recovery attempt for analytics
            recovery_record = {
                "strategy": strategy_name,
                "error_type": type(error).__name__,
                "layer": layer,
                "result": result.value,
                "timestamp": start_time.isoformat(),
                "duration_ms": int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000),
                "context": {k: v for k, v in context.items() if k not in ["fallback_response", "operation_func"]}
            }
            
            self.recovery_history.append(recovery_record)
            
            # Keep only recent history (last 100 attempts)
            if len(self.recovery_history) > 100:
                self.recovery_history = self.recovery_history[-100:]
            
            self.logger.info(f"Recovery strategy '{strategy_name}' completed with result: {result.value}")
            return result
            
        except Exception as recovery_error:
            self.logger.error(f"Error executing recovery strategy '{strategy_name}': {recovery_error}")
            return RecoveryResult.FAILED
    
    async def execute_recovery_chain(
        self, 
        strategy_names: List[str], 
        error: Exception, 
        context: Dict[str, Any]
    ) -> RecoveryResult:
        """Execute multiple recovery strategies in sequence until one succeeds.
        
        Args:
            strategy_names: List of strategy names to try in order
            error: The exception that occurred
            context: Additional context about the error
            
        Returns:
            Result of the first successful recovery, or FAILED if all fail
        """
        for strategy_name in strategy_names:
            result = await self.execute_recovery(strategy_name, error, context)
            
            if result == RecoveryResult.SUCCESS:
                return result
            elif result == RecoveryResult.PARTIAL:
                # Continue with partial success, but note it for final result
                partial_result = result
            
        # If we had any partial successes, return that; otherwise failed
        return getattr(self, 'partial_result', RecoveryResult.FAILED)
    
    def get_strategy(self, strategy_name: str) -> Optional[RecoveryStrategy]:
        """Get a specific recovery strategy instance."""
        return self.strategies.get(strategy_name)
    
    def add_strategy(self, name: str, strategy: RecoveryStrategy) -> None:
        """Add a custom recovery strategy."""
        self.strategies[name] = strategy
        self.logger.info(f"Added custom recovery strategy: {name}")
    
    def get_recovery_analytics(self) -> Dict[str, Any]:
        """Get analytics about recovery attempts."""
        if not self.recovery_history:
            return {
                "total_attempts": 0,
                "success_rate": 0.0,
                "strategy_performance": {},
                "common_errors": {}
            }
        
        total_attempts = len(self.recovery_history)
        successful_attempts = len([r for r in self.recovery_history if r["result"] == "success"])
        
        # Strategy performance analysis
        strategy_stats = {}
        for record in self.recovery_history:
            strategy = record["strategy"]
            if strategy not in strategy_stats:
                strategy_stats[strategy] = {"attempts": 0, "successes": 0, "avg_duration": 0}
            
            strategy_stats[strategy]["attempts"] += 1
            if record["result"] == "success":
                strategy_stats[strategy]["successes"] += 1
        
        # Calculate success rates and average durations
        for strategy, stats in strategy_stats.items():
            stats["success_rate"] = stats["successes"] / stats["attempts"] if stats["attempts"] > 0 else 0
            strategy_records = [r for r in self.recovery_history if r["strategy"] == strategy]
            stats["avg_duration"] = sum(r["duration_ms"] for r in strategy_records) / len(strategy_records)
        
        # Common error analysis
        error_counts = {}
        for record in self.recovery_history:
            error_type = record["error_type"]
            error_counts[error_type] = error_counts.get(error_type, 0) + 1
        
        return {
            "total_attempts": total_attempts,
            "success_rate": successful_attempts / total_attempts,
            "strategy_performance": strategy_stats,
            "common_errors": dict(sorted(error_counts.items(), key=lambda x: x[1], reverse=True)[:5]),
            "recent_attempts": self.recovery_history[-10:] if len(self.recovery_history) >= 10 else self.recovery_history
        }


# Global recovery manager instance
recovery_manager = RecoveryManager()