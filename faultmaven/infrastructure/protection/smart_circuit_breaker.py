# File: faultmaven/infrastructure/protection/smart_circuit_breaker.py

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Callable, Awaitable
from enum import Enum
from dataclasses import dataclass
import statistics

from faultmaven.models.behavioral import ClientProfile, ReputationLevel, RiskLevel
from faultmaven.models.protection import SystemMetrics


class CircuitState(str, Enum):
    """Circuit breaker states"""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Circuit is open, requests blocked
    HALF_OPEN = "half_open"  # Testing if service has recovered


class CircuitDecision(str, Enum):
    """Circuit breaker decision types"""
    ALLOW = "allow"
    DENY = "deny"
    THROTTLE = "throttle"


@dataclass
class Request:
    """Request information for circuit breaker analysis"""
    session_id: str
    endpoint: str
    method: str
    timestamp: datetime
    payload_size: int = 0
    headers: Dict[str, str] = None
    client_ip: str = ""
    
    def __post_init__(self):
        if self.headers is None:
            self.headers = {}


@dataclass
class Response:
    """Response information for circuit breaker analysis"""
    status_code: int
    response_time: float  # milliseconds
    error_type: Optional[str] = None
    timestamp: datetime = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()
    
    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 400
    
    @property
    def is_failure(self) -> bool:
        return self.status_code >= 500  # Only server errors count as failures


@dataclass
class CircuitMetrics:
    """Circuit breaker metrics"""
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    avg_response_time: float = 0.0
    error_rate: float = 0.0
    last_failure_time: Optional[datetime] = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0


@dataclass
class CircuitConfig:
    """Circuit breaker configuration"""
    failure_threshold: int = 5  # Number of failures before opening
    success_threshold: int = 3  # Number of successes before closing from half-open
    timeout: timedelta = timedelta(seconds=60)  # Time to wait before half-open
    response_time_threshold: float = 5000.0  # Slow response threshold (ms)
    error_rate_threshold: float = 0.5  # Error rate threshold (50%)
    min_requests: int = 10  # Minimum requests before considering error rate


class RiskPrediction:
    """Risk prediction for circuit breaker"""
    def __init__(self, risk_score: float, predicted_failures: int, confidence: float):
        self.risk_score = risk_score  # 0.0 to 1.0
        self.predicted_failures = predicted_failures
        self.confidence = confidence  # 0.0 to 1.0
        self.timestamp = datetime.utcnow()


class Decision:
    """Circuit breaker decision"""
    def __init__(self, action: CircuitDecision, reason: str, confidence: float = 1.0, 
                 metadata: Dict[str, Any] = None):
        self.action = action
        self.reason = reason
        self.confidence = confidence
        self.metadata = metadata or {}
        self.timestamp = datetime.utcnow()


class SmartCircuitBreaker:
    """
    Intelligent circuit breaker with adaptive behavior
    
    Features:
    - Dynamic threshold adjustment based on system conditions
    - Reputation-aware decisions
    - Predictive failure prevention
    - Graceful degradation strategies
    - Multiple circuit types (service, client, endpoint)
    """

    def __init__(self, name: str, config: CircuitConfig = None):
        self.name = name
        self.config = config or CircuitConfig()
        self.logger = logging.getLogger(f"{__name__}.{name}")
        
        # Circuit state
        self.state = CircuitState.CLOSED
        self.state_changed_at = datetime.utcnow()
        self.metrics = CircuitMetrics()
        
        # Adaptive thresholds
        self.adaptive_failure_threshold = self.config.failure_threshold
        self.adaptive_response_time_threshold = self.config.response_time_threshold
        self.adaptive_error_rate_threshold = self.config.error_rate_threshold
        
        # Request tracking
        self.request_history: List[Dict[str, Any]] = []
        self.response_history: List[Response] = []
        self.max_history = 1000
        
        # Performance tracking
        self.response_times: List[float] = []
        self.error_counts: Dict[str, int] = {}
        
        # Predictive components
        self.failure_predictions: List[RiskPrediction] = []
        self.trend_window = timedelta(minutes=5)
        
        # Callbacks for events
        self.on_state_change: Optional[Callable[[CircuitState, CircuitState], Awaitable[None]]] = None
        self.on_failure: Optional[Callable[[Request, Response], Awaitable[None]]] = None
        
        self.logger.info(f"SmartCircuitBreaker '{name}' initialized in {self.state.value} state")

    async def should_allow_request(self, request: Request, client: Optional[ClientProfile] = None) -> Decision:
        """
        Determine if a request should be allowed through the circuit
        
        Args:
            request: Incoming request details
            client: Client profile for reputation-aware decisions
            
        Returns:
            Decision with action and reasoning
        """
        try:
            # Check current state
            if self.state == CircuitState.OPEN:
                # Check if timeout has elapsed for half-open transition
                if datetime.utcnow() - self.state_changed_at >= self.config.timeout:
                    await self._transition_to_half_open()
                else:
                    return Decision(
                        CircuitDecision.DENY,
                        f"Circuit is open, {self.config.timeout.total_seconds():.0f}s timeout not elapsed",
                        metadata={
                            "state": self.state.value, 
                            "time_remaining": (self.config.timeout - (datetime.utcnow() - self.state_changed_at)).total_seconds()
                        }
                    )
            
            # Reputation-aware decision making
            if client:
                reputation_decision = await self._reputation_based_decision(request, client)
                if reputation_decision.action != CircuitDecision.ALLOW:
                    return reputation_decision
            
            # Predictive failure detection
            risk_prediction = await self._predict_failure_risk()
            if risk_prediction.risk_score > 0.8 and risk_prediction.confidence > 0.7:
                return Decision(
                    CircuitDecision.THROTTLE,
                    f"High failure risk predicted: {risk_prediction.risk_score:.2f}",
                    risk_prediction.confidence,
                    {"prediction": risk_prediction.__dict__}
                )
            
            # Half-open state logic
            if self.state == CircuitState.HALF_OPEN:
                # Allow limited requests to test recovery
                recent_requests = self._get_recent_requests(timedelta(seconds=30))
                if len(recent_requests) >= 3:  # Limit test requests
                    return Decision(
                        CircuitDecision.DENY,
                        "Half-open state: limiting test requests",
                        metadata={"test_requests_sent": len(recent_requests)}
                    )
            
            # System load-based throttling
            if await self._should_throttle_for_load():
                return Decision(
                    CircuitDecision.THROTTLE,
                    "System under high load, throttling requests",
                    metadata={"load_factor": await self._calculate_load_factor()}
                )
            
            # Default: allow request
            return Decision(CircuitDecision.ALLOW, "Normal operation")
            
        except Exception as e:
            self.logger.error(f"Error in circuit breaker decision: {e}")
            return Decision(CircuitDecision.ALLOW, "Error in circuit breaker, defaulting to allow")

    async def update_metrics(self, response: Response, client: Optional[ClientProfile] = None):
        """
        Update circuit metrics based on response
        
        Args:
            response: Response details
            client: Optional client profile
        """
        try:
            # Update basic metrics
            self.metrics.total_requests += 1
            
            if response.is_success:
                self.metrics.successful_requests += 1
                self.metrics.consecutive_successes += 1
                self.metrics.consecutive_failures = 0
            elif response.is_failure:
                self.metrics.failed_requests += 1
                self.metrics.consecutive_failures += 1
                self.metrics.consecutive_successes = 0
                self.metrics.last_failure_time = response.timestamp
                
                # Track error types
                if response.error_type:
                    self.error_counts[response.error_type] = self.error_counts.get(response.error_type, 0) + 1
            
            # Update response time tracking
            self.response_times.append(response.response_time)
            if len(self.response_times) > 100:  # Keep last 100 response times
                self.response_times = self.response_times[-100:]
            
            # Calculate metrics
            if self.metrics.total_requests > 0:
                self.metrics.error_rate = self.metrics.failed_requests / self.metrics.total_requests
                self.metrics.avg_response_time = statistics.mean(self.response_times) if self.response_times else 0.0
            
            # Store response history
            self.response_history.append(response)
            if len(self.response_history) > self.max_history:
                self.response_history = self.response_history[-self.max_history:]
            
            # State transition logic
            await self._check_state_transitions(response)
            
            # Trigger callbacks
            if response.is_failure and self.on_failure:
                # Don't await here to avoid blocking
                asyncio.create_task(self.on_failure(None, response))  # Request not available in this context
            
        except Exception as e:
            self.logger.error(f"Error updating circuit metrics: {e}")

    async def adjust_thresholds(self, system_metrics: SystemMetrics):
        """
        Adjust circuit thresholds based on system conditions
        
        Args:
            system_metrics: Current system performance metrics
        """
        try:
            # Adjust failure threshold based on system health
            if system_metrics.overall_health_score < 0.5:
                # System is unhealthy, be more conservative
                self.adaptive_failure_threshold = max(2, self.config.failure_threshold // 2)
            elif system_metrics.overall_health_score > 0.8:
                # System is healthy, be more lenient
                self.adaptive_failure_threshold = self.config.failure_threshold * 2
            else:
                self.adaptive_failure_threshold = self.config.failure_threshold
            
            # Adjust response time threshold based on system load
            cpu_factor = system_metrics.cpu_usage / 100.0
            memory_factor = system_metrics.memory_usage / 100.0
            load_factor = (cpu_factor + memory_factor) / 2.0
            
            self.adaptive_response_time_threshold = self.config.response_time_threshold * (1 + load_factor)
            
            # Adjust error rate threshold based on error trends
            recent_error_rate = await self._calculate_recent_error_rate()
            if recent_error_rate > self.config.error_rate_threshold * 2:
                # High error environment, be more tolerant temporarily
                self.adaptive_error_rate_threshold = min(0.8, self.config.error_rate_threshold * 1.5)
            else:
                self.adaptive_error_rate_threshold = self.config.error_rate_threshold
            
            self.logger.debug(f"Adjusted thresholds - Failures: {self.adaptive_failure_threshold}, "
                            f"Response time: {self.adaptive_response_time_threshold:.0f}ms, "
                            f"Error rate: {self.adaptive_error_rate_threshold:.2f}")
            
        except Exception as e:
            self.logger.error(f"Error adjusting thresholds: {e}")

    async def predict_failure_risk(self, current_state: Optional[Dict[str, Any]] = None) -> RiskPrediction:
        """
        Predict the risk of future failures
        
        Args:
            current_state: Current system state information
            
        Returns:
            Risk prediction with confidence
        """
        try:
            risk_score = 0.0
            predicted_failures = 0
            confidence = 0.0
            
            # Analyze recent trends
            recent_responses = self._get_recent_responses(self.trend_window)
            
            if len(recent_responses) >= 5:
                # Calculate failure trend
                failure_rate = sum(1 for r in recent_responses if r.is_failure) / len(recent_responses)
                
                # Calculate response time trend
                recent_times = [r.response_time for r in recent_responses]
                avg_response_time = statistics.mean(recent_times)
                
                # Response time increasing trend
                if len(recent_times) >= 3:
                    time_trend = self._calculate_trend(recent_times[-3:])
                    if time_trend > 1.2:  # 20% increase trend
                        risk_score += 0.3
                
                # High error rate
                if failure_rate > self.adaptive_error_rate_threshold:
                    risk_score += 0.4
                
                # Slow responses
                if avg_response_time > self.adaptive_response_time_threshold:
                    risk_score += 0.3
                
                # Consecutive failures
                if self.metrics.consecutive_failures >= self.adaptive_failure_threshold // 2:
                    risk_score += 0.4
                
                confidence = min(len(recent_responses) / 20.0, 1.0)  # More data = higher confidence
                predicted_failures = int(failure_rate * 10)  # Predict failures in next 10 requests
            
            # Consider system state if provided
            if current_state:
                system_load = current_state.get('system_load', 0.0)
                if system_load > 0.8:
                    risk_score += 0.2
                
                memory_pressure = current_state.get('memory_pressure', 0.0)
                if memory_pressure > 0.9:
                    risk_score += 0.3
            
            risk_score = min(risk_score, 1.0)
            
            prediction = RiskPrediction(risk_score, predicted_failures, confidence)
            self.failure_predictions.append(prediction)
            
            # Keep only recent predictions
            cutoff = datetime.utcnow() - timedelta(hours=1)
            self.failure_predictions = [p for p in self.failure_predictions if p.timestamp > cutoff]
            
            return prediction
            
        except Exception as e:
            self.logger.error(f"Error predicting failure risk: {e}")
            return RiskPrediction(0.0, 0, 0.0)

    async def get_status(self) -> Dict[str, Any]:
        """Get current circuit breaker status"""
        return {
            "name": self.name,
            "state": self.state.value,
            "state_duration": (datetime.utcnow() - self.state_changed_at).total_seconds(),
            "metrics": {
                "total_requests": self.metrics.total_requests,
                "success_rate": (self.metrics.successful_requests / max(self.metrics.total_requests, 1)) * 100,
                "error_rate": self.metrics.error_rate * 100,
                "avg_response_time": self.metrics.avg_response_time,
                "consecutive_failures": self.metrics.consecutive_failures,
                "consecutive_successes": self.metrics.consecutive_successes
            },
            "thresholds": {
                "failure_threshold": self.adaptive_failure_threshold,
                "response_time_threshold": self.adaptive_response_time_threshold,
                "error_rate_threshold": self.adaptive_error_rate_threshold
            },
            "recent_predictions": len(self.failure_predictions)
        }

    async def reset(self):
        """Reset circuit breaker to initial state"""
        old_state = self.state
        self.state = CircuitState.CLOSED
        self.state_changed_at = datetime.utcnow()
        self.metrics = CircuitMetrics()
        self.response_times = []
        self.error_counts = {}
        self.failure_predictions = []
        
        self.logger.info(f"Circuit breaker '{self.name}' reset from {old_state.value} to {self.state.value}")

    async def _reputation_based_decision(self, request: Request, client: ClientProfile) -> Decision:
        """Make decision based on client reputation"""
        if not client.reputation_score:
            return Decision(CircuitDecision.ALLOW, "No reputation data available")
        
        reputation_level = client.reputation_score.reputation_level
        
        # Block requests from blocked clients
        if reputation_level == ReputationLevel.BLOCKED:
            return Decision(
                CircuitDecision.DENY,
                "Client reputation is blocked",
                1.0,
                {"reputation_score": client.reputation_score.overall_score}
            )
        
        # Throttle suspicious clients during high load
        if reputation_level == ReputationLevel.SUSPICIOUS:
            load_factor = await self._calculate_load_factor()
            if load_factor > 0.7:
                return Decision(
                    CircuitDecision.THROTTLE,
                    "Suspicious client throttled during high load",
                    0.8,
                    {"reputation_score": client.reputation_score.overall_score, "load_factor": load_factor}
                )
        
        # Give priority to trusted clients
        if reputation_level == ReputationLevel.TRUSTED:
            return Decision(
                CircuitDecision.ALLOW,
                "Trusted client allowed",
                1.0,
                {"reputation_score": client.reputation_score.overall_score}
            )
        
        return Decision(CircuitDecision.ALLOW, "Normal reputation-based processing")

    async def _check_state_transitions(self, response: Response):
        """Check if state transitions are needed based on response"""
        if self.state == CircuitState.CLOSED:
            # Check for opening conditions
            if (self.metrics.consecutive_failures >= self.adaptive_failure_threshold or
                (self.metrics.total_requests >= self.config.min_requests and 
                 self.metrics.error_rate >= self.adaptive_error_rate_threshold)):
                await self._transition_to_open()
        
        elif self.state == CircuitState.HALF_OPEN:
            if response.is_success:
                if self.metrics.consecutive_successes >= self.config.success_threshold:
                    await self._transition_to_closed()
            elif response.is_failure:
                await self._transition_to_open()

    async def _transition_to_open(self):
        """Transition circuit to open state"""
        old_state = self.state
        self.state = CircuitState.OPEN
        self.state_changed_at = datetime.utcnow()
        
        self.logger.warning(f"Circuit breaker '{self.name}' opened - failures: {self.metrics.consecutive_failures}, "
                          f"error rate: {self.metrics.error_rate:.2f}")
        
        if self.on_state_change:
            asyncio.create_task(self.on_state_change(old_state, self.state))

    async def _transition_to_half_open(self):
        """Transition circuit to half-open state"""
        old_state = self.state
        self.state = CircuitState.HALF_OPEN
        self.state_changed_at = datetime.utcnow()
        
        self.logger.info(f"Circuit breaker '{self.name}' half-opened for testing")
        
        if self.on_state_change:
            asyncio.create_task(self.on_state_change(old_state, self.state))

    async def _transition_to_closed(self):
        """Transition circuit to closed state"""
        old_state = self.state
        self.state = CircuitState.CLOSED
        self.state_changed_at = datetime.utcnow()
        
        # Reset consecutive failures when closing
        self.metrics.consecutive_failures = 0
        
        self.logger.info(f"Circuit breaker '{self.name}' closed - service recovered")
        
        if self.on_state_change:
            asyncio.create_task(self.on_state_change(old_state, self.state))

    def _get_recent_requests(self, window: timedelta) -> List[Dict[str, Any]]:
        """Get requests within the specified time window"""
        cutoff = datetime.utcnow() - window
        return [req for req in self.request_history if req.get('timestamp', datetime.min) > cutoff]

    def _get_recent_responses(self, window: timedelta) -> List[Response]:
        """Get responses within the specified time window"""
        cutoff = datetime.utcnow() - window
        return [resp for resp in self.response_history if resp.timestamp > cutoff]

    async def _calculate_recent_error_rate(self) -> float:
        """Calculate error rate for recent responses"""
        recent_responses = self._get_recent_responses(timedelta(minutes=5))
        if not recent_responses:
            return 0.0
        
        failures = sum(1 for r in recent_responses if r.is_failure)
        return failures / len(recent_responses)

    async def _calculate_load_factor(self) -> float:
        """Calculate current system load factor (0.0 to 1.0)"""
        # Simplified load calculation based on response times and request rate
        recent_responses = self._get_recent_responses(timedelta(minutes=1))
        
        if not recent_responses:
            return 0.0
        
        # Base load on request rate (requests per second)
        request_rate = len(recent_responses) / 60.0
        rate_factor = min(request_rate / 10.0, 1.0)  # Normalize to max 10 requests/second
        
        # Base load on response times
        avg_response_time = statistics.mean([r.response_time for r in recent_responses])
        time_factor = min(avg_response_time / 1000.0, 1.0)  # Normalize to 1 second
        
        return (rate_factor + time_factor) / 2.0

    async def _should_throttle_for_load(self) -> bool:
        """Determine if requests should be throttled due to system load"""
        load_factor = await self._calculate_load_factor()
        return load_factor > 0.8

    def _calculate_trend(self, values: List[float]) -> float:
        """Calculate trend factor (>1.0 = increasing, <1.0 = decreasing)"""
        if len(values) < 2:
            return 1.0
        
        first_half = values[:len(values)//2] or [values[0]]
        second_half = values[len(values)//2:] or [values[-1]]
        
        avg_first = statistics.mean(first_half)
        avg_second = statistics.mean(second_half)
        
        if avg_first == 0:
            return 1.0
        
        return avg_second / avg_first