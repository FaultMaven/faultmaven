"""
FaultMaven Base Infrastructure Client

Provides a base class for all external service clients in the infrastructure
layer with unified logging, retry logic, circuit breaker patterns, and
comprehensive error handling.
"""

import asyncio
import time
from typing import Any, Callable, Dict, Optional, TypeVar, Union
from abc import ABC
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from faultmaven.infrastructure.logging.unified import get_unified_logger, UnifiedLogger


# Type variable for generic return types
T = TypeVar('T')


class CircuitBreakerError(Exception):
    """Exception raised when circuit breaker is open."""
    pass


class CircuitBreaker:
    """
    Simple circuit breaker implementation for external service calls.
    
    This class implements the circuit breaker pattern to prevent cascading
    failures when external services are unavailable or responding slowly.
    """
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        expected_exception: type = Exception
    ):
        """
        Initialize circuit breaker.
        
        Args:
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Seconds to wait before attempting to close circuit
            expected_exception: Exception type that triggers circuit breaker
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "closed"  # closed, open, half-open
    
    def can_execute(self) -> bool:
        """Check if circuit allows execution."""
        if self.state == "closed":
            return True
        
        if self.state == "open":
            # Check if we should try half-open
            if (self.last_failure_time and
                datetime.utcnow() - self.last_failure_time > timedelta(seconds=self.recovery_timeout)):
                self.state = "half-open"
                return True
            return False
        
        # half-open state
        return True
    
    def record_success(self) -> None:
        """Record successful execution."""
        self.failure_count = 0
        self.state = "closed"
    
    def record_failure(self, exception: Exception) -> None:
        """Record failed execution."""
        if isinstance(exception, self.expected_exception):
            self.failure_count += 1
            self.last_failure_time = datetime.utcnow()
            
            if self.failure_count >= self.failure_threshold:
                self.state = "open"
            elif self.state == "half-open":
                self.state = "open"


class BaseExternalClient(ABC):
    """
    Base class for all external service clients in the infrastructure layer.
    
    This class provides common functionality for infrastructure components
    that interact with external services, including unified logging, retry
    logic, circuit breaker patterns, and comprehensive error handling.
    
    All infrastructure clients should inherit from this base class to ensure
    consistent patterns for external service interaction.
    
    Attributes:
        client_name: Name of the external client
        service_name: Name of the external service being accessed
        logger: UnifiedLogger instance for infrastructure layer
        circuit_breaker: Circuit breaker for service protection
    """
    
    def __init__(
        self,
        client_name: str,
        service_name: str,
        enable_circuit_breaker: bool = True,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_timeout: int = 60
    ):
        """
        Initialize base external client with unified logging and circuit breaker.
        
        Args:
            client_name: Name of the client (e.g., "redis_client", "chromadb_client")
            service_name: Name of the external service (e.g., "Redis", "ChromaDB")
            enable_circuit_breaker: Whether to enable circuit breaker protection
            circuit_breaker_threshold: Failures before opening circuit
            circuit_breaker_timeout: Seconds to wait before retry
        """
        self.client_name = client_name
        self.service_name = service_name
        self.logger = get_unified_logger(
            f"faultmaven.infrastructure.{client_name}", 
            "infrastructure"
        )
        
        # Initialize circuit breaker if enabled
        if enable_circuit_breaker:
            self.circuit_breaker = CircuitBreaker(
                failure_threshold=circuit_breaker_threshold,
                recovery_timeout=circuit_breaker_timeout
            )
        else:
            self.circuit_breaker = None
        
        # Connection metrics
        self.connection_metrics = {
            "total_calls": 0,
            "successful_calls": 0,
            "failed_calls": 0,
            "circuit_breaker_trips": 0,
            "last_success_time": None,
            "last_failure_time": None
        }
        
        # Log client initialization
        self.logger.log_event(
            event_type="system",
            event_name="external_client_initialized",
            severity="info",
            data={
                "client_name": self.client_name,
                "service_name": self.service_name,
                "circuit_breaker_enabled": enable_circuit_breaker
            }
        )
    
    async def call_external(
        self,
        operation_name: str,
        call_func: Callable[..., Union[T, any]],
        *args,
        timeout: Optional[float] = None,
        retries: int = 0,
        retry_delay: float = 1.0,
        validate_response: Optional[Callable[[T], bool]] = None,
        transform_response: Optional[Callable[[T], T]] = None,
        **kwargs
    ) -> T:
        """
        Execute external service call with unified logging, retries, and circuit breaker.
        
        This method provides a standardized way to call external services with:
        - Unified infrastructure logging
        - Circuit breaker protection
        - Automatic retry with exponential backoff
        - Response validation and transformation
        - Comprehensive error handling and metrics
        
        Args:
            operation_name: Name of the external operation (e.g., "get", "set", "query")
            call_func: The function to call (can be sync or async)
            *args: Arguments to pass to the call function
            timeout: Optional timeout for the operation
            retries: Number of retry attempts on failure
            retry_delay: Base delay between retries (with exponential backoff)
            validate_response: Optional function to validate response
            transform_response: Optional function to transform response
            **kwargs: Keyword arguments to pass to the call function
            
        Returns:
            Result of the external call, potentially transformed
            
        Raises:
            CircuitBreakerError: If circuit breaker is open
            TimeoutError: If operation times out
            RuntimeError: If external call fails after retries
            
        Example:
            >>> async def redis_get(key: str) -> str:
            ...     return await redis_client.get(key)
            >>> 
            >>> def validate_not_none(value) -> bool:
            ...     return value is not None
            >>> 
            >>> result = await self.call_external(
            ...     "get_user_session",
            ...     redis_get,
            ...     "session:123",
            ...     timeout=5.0,
            ...     retries=2,
            ...     validate_response=validate_not_none
            ... )
        """
        # Check circuit breaker
        if self.circuit_breaker and not self.circuit_breaker.can_execute():
            self.connection_metrics["circuit_breaker_trips"] += 1
            self.logger.log_event(
                event_type="technical",
                event_name="circuit_breaker_open",
                severity="warning",
                data={
                    "client": self.client_name,
                    "service": self.service_name,
                    "operation": operation_name
                }
            )
            raise CircuitBreakerError(f"Circuit breaker is open for {self.service_name}")
        
        # Log external call boundary - inbound
        self.logger.log_boundary(
            operation=operation_name,
            direction="inbound",
            data={
                "client": self.client_name,
                "service": self.service_name,
                "args_count": len(args),
                "kwargs_keys": list(kwargs.keys()) if kwargs else [],
                "timeout": timeout,
                "retries": retries
            }
        )
        
        # Track metrics
        self.connection_metrics["total_calls"] += 1
        
        # Execute with unified operation logging
        async with self.logger.operation(operation_name, client=self.client_name, service=self.service_name) as ctx:
            last_exception = None
            
            for attempt in range(retries + 1):
                try:
                    # Log retry attempt if not first attempt
                    if attempt > 0:
                        ctx[f"retry_attempt_{attempt}"] = {
                            "delay": retry_delay * (2 ** (attempt - 1)),
                            "previous_error": str(last_exception) if last_exception else None
                        }
                        
                        self.logger.log_event(
                            event_type="technical",
                            event_name="external_call_retry",
                            severity="info",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "operation": operation_name,
                                "attempt": attempt + 1,
                                "max_attempts": retries + 1
                            }
                        )
                    
                    # Execute with timeout
                    start_time = time.time()
                    
                    if timeout:
                        if asyncio.iscoroutinefunction(call_func):
                            result = await asyncio.wait_for(call_func(*args, **kwargs), timeout=timeout)
                        else:
                            # For sync functions, we can't easily apply timeout, but we log it
                            result = call_func(*args, **kwargs)
                    else:
                        if asyncio.iscoroutinefunction(call_func):
                            result = await call_func(*args, **kwargs)
                        else:
                            result = call_func(*args, **kwargs)
                    
                    call_duration = time.time() - start_time
                    ctx["call_duration"] = call_duration
                    
                    # Validate response if validator provided
                    if validate_response:
                        try:
                            is_valid = validate_response(result)
                            if not is_valid:
                                raise ValueError("Response validation failed")
                            ctx["validation"] = "passed"
                        except Exception as validation_error:
                            ctx["validation"] = "failed"
                            ctx["validation_error"] = str(validation_error)
                            self.logger.error(
                                f"Response validation failed for {self.service_name}.{operation_name}",
                                error=validation_error,
                                client=self.client_name,
                                service=self.service_name
                            )
                            raise RuntimeError(f"Response validation failed: {str(validation_error)}") from validation_error
                    
                    # Transform response if transformer provided
                    if transform_response:
                        try:
                            if asyncio.iscoroutinefunction(transform_response):
                                result = await transform_response(result)
                            else:
                                result = transform_response(result)
                            ctx["transformation"] = "applied"
                        except Exception as transform_error:
                            ctx["transformation"] = "failed"
                            ctx["transform_error"] = str(transform_error)
                            self.logger.error(
                                f"Response transformation failed for {self.service_name}.{operation_name}",
                                error=transform_error,
                                client=self.client_name,
                                service=self.service_name
                            )
                            # Don't raise - continue with untransformed result
                            self.logger.warning(
                                f"Continuing with untransformed response for {self.service_name}.{operation_name}",
                                client=self.client_name,
                                service=self.service_name
                            )
                    
                    # Success - record metrics and circuit breaker state
                    self.connection_metrics["successful_calls"] += 1
                    self.connection_metrics["last_success_time"] = datetime.utcnow().isoformat()
                    
                    if self.circuit_breaker:
                        self.circuit_breaker.record_success()
                    
                    # Log performance metrics
                    self.logger.log_metric(
                        metric_name="external_call_duration",
                        value=call_duration,
                        unit="seconds",
                        tags={
                            "client": self.client_name,
                            "service": self.service_name,
                            "operation": operation_name,
                            "success": "true"
                        }
                    )
                    
                    # Log successful boundary - outbound
                    self.logger.log_boundary(
                        operation=operation_name,
                        direction="outbound",
                        data={
                            "client": self.client_name,
                            "service": self.service_name,
                            "success": True,
                            "duration": call_duration,
                            "attempts": attempt + 1
                        }
                    )
                    
                    # Log success event
                    self.logger.log_event(
                        event_type="technical",
                        event_name="external_call_success",
                        severity="info",
                        data={
                            "client": self.client_name,
                            "service": self.service_name,
                            "operation": operation_name,
                            "duration": call_duration,
                            "attempts": attempt + 1
                        }
                    )
                    
                    return result
                    
                except asyncio.TimeoutError as timeout_error:
                    last_exception = timeout_error
                    ctx[f"timeout_attempt_{attempt}"] = True
                    
                    # Don't retry on timeout - fail fast
                    self.connection_metrics["failed_calls"] += 1
                    self.connection_metrics["last_failure_time"] = datetime.utcnow().isoformat()
                    
                    if self.circuit_breaker:
                        self.circuit_breaker.record_failure(timeout_error)
                    
                    self.logger.error(
                        f"External call timed out: {self.service_name}.{operation_name}",
                        error=timeout_error,
                        client=self.client_name,
                        service=self.service_name,
                        timeout=timeout
                    )
                    
                    raise TimeoutError(f"External call to {self.service_name}.{operation_name} timed out after {timeout}s")
                    
                except Exception as call_error:
                    last_exception = call_error
                    ctx[f"error_attempt_{attempt}"] = {
                        "error": str(call_error),
                        "error_type": type(call_error).__name__
                    }
                    
                    # Log the error (will be retried if more attempts remain)
                    if attempt < retries:
                        self.logger.warning(
                            f"External call failed, will retry: {self.service_name}.{operation_name}",
                            error=call_error,
                            client=self.client_name,
                            service=self.service_name,
                            attempt=attempt + 1,
                            remaining_attempts=retries - attempt
                        )
                        
                        # Wait before retry with exponential backoff
                        retry_wait = retry_delay * (2 ** attempt)
                        await asyncio.sleep(retry_wait)
                    else:
                        # Final failure
                        self.connection_metrics["failed_calls"] += 1
                        self.connection_metrics["last_failure_time"] = datetime.utcnow().isoformat()
                        
                        if self.circuit_breaker:
                            self.circuit_breaker.record_failure(call_error)
                        
                        # Log failed boundary - outbound
                        self.logger.log_boundary(
                            operation=operation_name,
                            direction="outbound",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "success": False,
                                "error": str(call_error),
                                "attempts": attempt + 1
                            }
                        )
                        
                        # Log failure event
                        self.logger.log_event(
                            event_type="technical",
                            event_name="external_call_failed",
                            severity="error",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "operation": operation_name,
                                "error": str(call_error),
                                "attempts": attempt + 1
                            }
                        )
                        
                        raise RuntimeError(
                            f"External call to {self.service_name}.{operation_name} failed after {retries + 1} attempts: {str(call_error)}"
                        ) from call_error
        
        # Should not reach here, but just in case
        raise RuntimeError(f"Unexpected error in external call to {self.service_name}.{operation_name}")
    
    def call_external_sync(
        self,
        operation_name: str,
        call_func: Callable[..., T],
        *args,
        retries: int = 0,
        retry_delay: float = 1.0,
        validate_response: Optional[Callable[[T], bool]] = None,
        transform_response: Optional[Callable[[T], T]] = None,
        **kwargs
    ) -> T:
        """
        Synchronous version of call_external for non-async operations.
        
        Args:
            operation_name: Name of the external operation
            call_func: The synchronous function to call
            *args: Arguments to pass to the call function
            retries: Number of retry attempts on failure
            retry_delay: Base delay between retries
            validate_response: Optional function to validate response
            transform_response: Optional function to transform response
            **kwargs: Keyword arguments to pass to the call function
            
        Returns:
            Result of the external call, potentially transformed
        """
        # Check circuit breaker
        if self.circuit_breaker and not self.circuit_breaker.can_execute():
            self.connection_metrics["circuit_breaker_trips"] += 1
            self.logger.log_event(
                event_type="technical",
                event_name="circuit_breaker_open",
                severity="warning",
                data={
                    "client": self.client_name,
                    "service": self.service_name,
                    "operation": operation_name
                }
            )
            raise CircuitBreakerError(f"Circuit breaker is open for {self.service_name}")
        
        # Log external call boundary - inbound
        self.logger.log_boundary(
            operation=operation_name,
            direction="inbound",
            data={
                "client": self.client_name,
                "service": self.service_name,
                "args_count": len(args),
                "kwargs_keys": list(kwargs.keys()) if kwargs else [],
                "retries": retries
            }
        )
        
        # Track metrics
        self.connection_metrics["total_calls"] += 1
        
        # Execute with unified operation logging (synchronous)
        with self.logger.operation_sync(operation_name, client=self.client_name, service=self.service_name) as ctx:
            last_exception = None
            
            for attempt in range(retries + 1):
                try:
                    # Log retry attempt if not first attempt
                    if attempt > 0:
                        ctx[f"retry_attempt_{attempt}"] = {
                            "delay": retry_delay * (2 ** (attempt - 1)),
                            "previous_error": str(last_exception) if last_exception else None
                        }
                        
                        self.logger.log_event(
                            event_type="technical",
                            event_name="external_call_retry",
                            severity="info",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "operation": operation_name,
                                "attempt": attempt + 1,
                                "max_attempts": retries + 1
                            }
                        )
                    
                    # Execute call
                    start_time = time.time()
                    result = call_func(*args, **kwargs)
                    call_duration = time.time() - start_time
                    ctx["call_duration"] = call_duration
                    
                    # Validate response if validator provided
                    if validate_response:
                        try:
                            is_valid = validate_response(result)
                            if not is_valid:
                                raise ValueError("Response validation failed")
                            ctx["validation"] = "passed"
                        except Exception as validation_error:
                            ctx["validation"] = "failed"
                            ctx["validation_error"] = str(validation_error)
                            self.logger.error(
                                f"Response validation failed for {self.service_name}.{operation_name}",
                                error=validation_error,
                                client=self.client_name,
                                service=self.service_name
                            )
                            raise RuntimeError(f"Response validation failed: {str(validation_error)}") from validation_error
                    
                    # Transform response if transformer provided
                    if transform_response:
                        try:
                            result = transform_response(result)
                            ctx["transformation"] = "applied"
                        except Exception as transform_error:
                            ctx["transformation"] = "failed"
                            ctx["transform_error"] = str(transform_error)
                            self.logger.error(
                                f"Response transformation failed for {self.service_name}.{operation_name}",
                                error=transform_error,
                                client=self.client_name,
                                service=self.service_name
                            )
                            # Don't raise - continue with untransformed result
                            self.logger.warning(
                                f"Continuing with untransformed response for {self.service_name}.{operation_name}",
                                client=self.client_name,
                                service=self.service_name
                            )
                    
                    # Success - record metrics and circuit breaker state
                    self.connection_metrics["successful_calls"] += 1
                    self.connection_metrics["last_success_time"] = datetime.utcnow().isoformat()
                    
                    if self.circuit_breaker:
                        self.circuit_breaker.record_success()
                    
                    # Log performance metrics
                    self.logger.log_metric(
                        metric_name="external_call_duration",
                        value=call_duration,
                        unit="seconds",
                        tags={
                            "client": self.client_name,
                            "service": self.service_name,
                            "operation": operation_name,
                            "success": "true"
                        }
                    )
                    
                    # Log successful boundary - outbound
                    self.logger.log_boundary(
                        operation=operation_name,
                        direction="outbound",
                        data={
                            "client": self.client_name,
                            "service": self.service_name,
                            "success": True,
                            "duration": call_duration,
                            "attempts": attempt + 1
                        }
                    )
                    
                    # Log success event
                    self.logger.log_event(
                        event_type="technical",
                        event_name="external_call_success",
                        severity="info",
                        data={
                            "client": self.client_name,
                            "service": self.service_name,
                            "operation": operation_name,
                            "duration": call_duration,
                            "attempts": attempt + 1
                        }
                    )
                    
                    return result
                    
                except Exception as call_error:
                    last_exception = call_error
                    ctx[f"error_attempt_{attempt}"] = {
                        "error": str(call_error),
                        "error_type": type(call_error).__name__
                    }
                    
                    # Log the error (will be retried if more attempts remain)
                    if attempt < retries:
                        self.logger.warning(
                            f"External call failed, will retry: {self.service_name}.{operation_name}",
                            error=call_error,
                            client=self.client_name,
                            service=self.service_name,
                            attempt=attempt + 1,
                            remaining_attempts=retries - attempt
                        )
                        
                        # Wait before retry with exponential backoff
                        retry_wait = retry_delay * (2 ** attempt)
                        time.sleep(retry_wait)
                    else:
                        # Final failure
                        self.connection_metrics["failed_calls"] += 1
                        self.connection_metrics["last_failure_time"] = datetime.utcnow().isoformat()
                        
                        if self.circuit_breaker:
                            self.circuit_breaker.record_failure(call_error)
                        
                        # Log failed boundary - outbound
                        self.logger.log_boundary(
                            operation=operation_name,
                            direction="outbound",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "success": False,
                                "error": str(call_error),
                                "attempts": attempt + 1
                            }
                        )
                        
                        # Log failure event
                        self.logger.log_event(
                            event_type="technical",
                            event_name="external_call_failed",
                            severity="error",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "operation": operation_name,
                                "error": str(call_error),
                                "attempts": attempt + 1
                            }
                        )
                        
                        raise RuntimeError(
                            f"External call to {self.service_name}.{operation_name} failed after {retries + 1} attempts: {str(call_error)}"
                        ) from call_error
        
        # Should not reach here, but just in case
        raise RuntimeError(f"Unexpected error in external call to {self.service_name}.{operation_name}")
    
    async def health_check(self) -> Dict[str, Any]:
        """
        Perform health check for the external service client.
        
        This method should be overridden by subclasses to provide
        client-specific health checking logic.
        
        Returns:
            Dictionary containing health status and metrics
        """
        return {
            "client": self.client_name,
            "service": self.service_name,
            "status": "unknown",
            "timestamp": datetime.utcnow().isoformat(),
            "layer": "infrastructure",
            "metrics": self.connection_metrics.copy(),
            "circuit_breaker": {
                "enabled": self.circuit_breaker is not None,
                "state": getattr(self.circuit_breaker, 'state', 'disabled') if self.circuit_breaker else 'disabled',
                "failure_count": getattr(self.circuit_breaker, 'failure_count', 0) if self.circuit_breaker else 0
            }
        }