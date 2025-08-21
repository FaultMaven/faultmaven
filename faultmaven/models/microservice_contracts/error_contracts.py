"""Error Contracts for FaultMaven Microservice Architecture

This module defines standardized error contracts for consistent error handling
across microservices. These contracts support both in-process and distributed
error propagation with comprehensive metadata for debugging and monitoring.

Design Principles:
- Consistent error structure across all services
- Rich error context for debugging and observability
- Error categorization for appropriate handling
- Support for error chaining and correlation
- Standardized retry and recovery guidance
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from enum import Enum
from pydantic import BaseModel, Field
from uuid import uuid4


class ErrorType(str, Enum):
    """Categories of errors for appropriate handling."""
    VALIDATION = "validation"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    CIRCUIT_BREAKER = "circuit_breaker"
    BUDGET_EXCEEDED = "budget_exceeded"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INTERNAL = "internal"
    EXTERNAL_API = "external_api"
    CONFIGURATION = "configuration"


class ErrorSeverity(str, Enum):
    """Error severity levels for alerting and response."""
    LOW = "low"          # Informational, self-recovering
    MEDIUM = "medium"    # Requires attention, may impact users
    HIGH = "high"        # Significant impact, immediate attention needed
    CRITICAL = "critical" # System failure, emergency response required


class RetryPolicy(str, Enum):
    """Retry policy recommendations."""
    NO_RETRY = "no_retry"              # Don't retry (4xx errors, permanent failures)
    IMMEDIATE = "immediate"            # Retry immediately (rare cases)
    EXPONENTIAL_BACKOFF = "exponential_backoff"  # Standard exponential backoff
    LINEAR_BACKOFF = "linear_backoff"   # Linear delay between retries
    CUSTOM = "custom"                  # Custom retry logic required


class ServiceError(BaseModel):
    """Base error contract for all microservice errors."""
    error_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique error identifier for tracking"
    )
    error_type: ErrorType = Field(description="Error category")
    error_code: str = Field(description="Service-specific error code")
    message: str = Field(description="Human-readable error message")
    
    # Service context
    service_name: str = Field(description="Service that generated the error")
    service_version: str = Field(default="1.0", description="Service version")
    operation: str = Field(description="Operation that failed")
    
    # Request context
    request_id: Optional[str] = Field(
        default=None,
        description="Request identifier for correlation"
    )
    session_id: Optional[str] = Field(
        default=None,
        description="Session identifier"
    )
    user_id: Optional[str] = Field(
        default=None,
        description="User identifier (if applicable)"
    )
    
    # Error details
    details: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional error details and context"
    )
    severity: ErrorSeverity = Field(
        default=ErrorSeverity.MEDIUM,
        description="Error severity level"
    )
    
    # Timing information
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="Error occurrence timestamp"
    )
    
    # Error chaining
    caused_by: Optional["ServiceError"] = Field(
        default=None,
        description="Underlying error that caused this error"
    )
    correlation_id: Optional[str] = Field(
        default=None,
        description="Correlation ID for distributed tracing"
    )
    
    # Recovery guidance
    retry_policy: RetryPolicy = Field(
        default=RetryPolicy.NO_RETRY,
        description="Recommended retry policy"
    )
    retry_after_ms: Optional[int] = Field(
        default=None,
        description="Suggested retry delay in milliseconds"
    )
    max_retries: Optional[int] = Field(
        default=None,
        description="Maximum recommended retries"
    )
    
    # User guidance
    user_message: Optional[str] = Field(
        default=None,
        description="User-friendly error message"
    )
    resolution_steps: List[str] = Field(
        default_factory=list,
        description="Steps user can take to resolve the error"
    )
    
    # Technical details
    stack_trace: Optional[str] = Field(
        default=None,
        description="Stack trace (for internal errors)"
    )
    debug_info: Dict[str, Any] = Field(
        default_factory=dict,
        description="Debug information for troubleshooting"
    )

    class Config:
        """Pydantic configuration."""
        # Allow self-referencing models
        use_enum_values = True


class ValidationError(ServiceError):
    """Error for input validation failures."""
    error_type: ErrorType = Field(default=ErrorType.VALIDATION, const=True)
    
    # Validation-specific fields
    field_errors: List[Dict[str, str]] = Field(
        default_factory=list,
        description="Per-field validation errors"
    )
    schema_violations: List[str] = Field(
        default_factory=list,
        description="Schema validation violations"
    )
    
    def __init__(self, **data):
        """Initialize validation error with appropriate defaults."""
        data.setdefault('error_code', 'VALIDATION_FAILED')
        data.setdefault('severity', ErrorSeverity.LOW)
        data.setdefault('retry_policy', RetryPolicy.NO_RETRY)
        data.setdefault('user_message', 'Please check your input and try again.')
        super().__init__(**data)


class BudgetExceededError(ServiceError):
    """Error for budget/resource limit violations."""
    error_type: ErrorType = Field(default=ErrorType.BUDGET_EXCEEDED, const=True)
    
    # Budget-specific fields
    budget_type: str = Field(description="Type of budget exceeded (time/tokens/calls)")
    limit: int = Field(description="Budget limit that was exceeded")
    consumed: int = Field(description="Amount actually consumed")
    remaining_budget: Dict[str, int] = Field(
        default_factory=dict,
        description="Remaining budget for other resource types"
    )
    
    def __init__(self, **data):
        """Initialize budget exceeded error with appropriate defaults."""
        data.setdefault('error_code', 'BUDGET_EXCEEDED')
        data.setdefault('severity', ErrorSeverity.MEDIUM)
        data.setdefault('retry_policy', RetryPolicy.NO_RETRY)
        data.setdefault('user_message', 'Resource limit exceeded. Please try with a simpler request.')
        super().__init__(**data)


class CircuitBreakerError(ServiceError):
    """Error for circuit breaker activations."""
    error_type: ErrorType = Field(default=ErrorType.CIRCUIT_BREAKER, const=True)
    
    # Circuit breaker specific fields
    circuit_name: str = Field(description="Name of the circuit that is open")
    failure_count: int = Field(description="Number of consecutive failures")
    failure_threshold: int = Field(description="Threshold that triggered circuit opening")
    last_failure_time: datetime = Field(description="Timestamp of last failure")
    next_attempt_time: Optional[datetime] = Field(
        default=None,
        description="When circuit will try half-open state"
    )
    
    def __init__(self, **data):
        """Initialize circuit breaker error with appropriate defaults."""
        data.setdefault('error_code', 'CIRCUIT_BREAKER_OPEN')
        data.setdefault('severity', ErrorSeverity.HIGH)
        data.setdefault('retry_policy', RetryPolicy.EXPONENTIAL_BACKOFF)
        data.setdefault('user_message', 'Service is temporarily unavailable. Please try again later.')
        super().__init__(**data)


class TimeoutError(ServiceError):
    """Error for operation timeouts."""
    error_type: ErrorType = Field(default=ErrorType.TIMEOUT, const=True)
    
    # Timeout-specific fields
    timeout_ms: int = Field(description="Configured timeout in milliseconds")
    elapsed_ms: int = Field(description="Time elapsed before timeout")
    operation_stage: Optional[str] = Field(
        default=None,
        description="Stage of operation where timeout occurred"
    )
    partial_results: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Partial results if any were obtained"
    )
    
    def __init__(self, **data):
        """Initialize timeout error with appropriate defaults."""
        data.setdefault('error_code', 'OPERATION_TIMEOUT')
        data.setdefault('severity', ErrorSeverity.MEDIUM)
        data.setdefault('retry_policy', RetryPolicy.EXPONENTIAL_BACKOFF)
        data.setdefault('max_retries', 3)
        data.setdefault('user_message', 'Operation timed out. Please try again.')
        super().__init__(**data)


class RateLimitError(ServiceError):
    """Error for rate limiting violations."""
    error_type: ErrorType = Field(default=ErrorType.RATE_LIMITED, const=True)
    
    # Rate limiting specific fields
    limit_type: str = Field(description="Type of rate limit (requests, tokens, etc.)")
    limit: int = Field(description="Rate limit threshold")
    current_usage: int = Field(description="Current usage count")
    reset_time: Optional[datetime] = Field(
        default=None,
        description="When rate limit resets"
    )
    retry_after_seconds: Optional[int] = Field(
        default=None,
        description="Seconds to wait before retrying"
    )
    
    def __init__(self, **data):
        """Initialize rate limit error with appropriate defaults."""
        data.setdefault('error_code', 'RATE_LIMIT_EXCEEDED')
        data.setdefault('severity', ErrorSeverity.MEDIUM)
        data.setdefault('retry_policy', RetryPolicy.LINEAR_BACKOFF)
        super().__init__(**data)


class ServiceUnavailableError(ServiceError):
    """Error for service unavailability."""
    error_type: ErrorType = Field(default=ErrorType.SERVICE_UNAVAILABLE, const=True)
    
    # Service availability specific fields
    unavailable_services: List[str] = Field(
        default_factory=list,
        description="List of unavailable services"
    )
    health_check_failures: Dict[str, str] = Field(
        default_factory=dict,
        description="Health check failure details by service"
    )
    estimated_recovery_time: Optional[datetime] = Field(
        default=None,
        description="Estimated service recovery time"
    )
    fallback_available: bool = Field(
        default=False,
        description="Whether fallback service is available"
    )
    
    def __init__(self, **data):
        """Initialize service unavailable error with appropriate defaults."""
        data.setdefault('error_code', 'SERVICE_UNAVAILABLE')
        data.setdefault('severity', ErrorSeverity.HIGH)
        data.setdefault('retry_policy', RetryPolicy.EXPONENTIAL_BACKOFF)
        data.setdefault('user_message', 'Service is temporarily unavailable. Please try again later.')
        super().__init__(**data)


class AuthenticationError(ServiceError):
    """Error for authentication failures."""
    error_type: ErrorType = Field(default=ErrorType.AUTHENTICATION, const=True)
    
    # Authentication specific fields
    auth_method: str = Field(description="Authentication method that failed")
    token_expired: bool = Field(default=False, description="Whether token is expired")
    invalid_credentials: bool = Field(default=False, description="Whether credentials are invalid")
    
    def __init__(self, **data):
        """Initialize authentication error with appropriate defaults."""
        data.setdefault('error_code', 'AUTHENTICATION_FAILED')
        data.setdefault('severity', ErrorSeverity.MEDIUM)
        data.setdefault('retry_policy', RetryPolicy.NO_RETRY)
        data.setdefault('user_message', 'Authentication failed. Please check your credentials.')
        super().__init__(**data)


class AuthorizationError(ServiceError):
    """Error for authorization failures."""
    error_type: ErrorType = Field(default=ErrorType.AUTHORIZATION, const=True)
    
    # Authorization specific fields
    required_permission: str = Field(description="Permission required for operation")
    user_permissions: List[str] = Field(
        default_factory=list,
        description="User's current permissions"
    )
    resource: Optional[str] = Field(
        default=None,
        description="Resource that access was denied to"
    )
    
    def __init__(self, **data):
        """Initialize authorization error with appropriate defaults."""
        data.setdefault('error_code', 'ACCESS_DENIED')
        data.setdefault('severity', ErrorSeverity.MEDIUM)
        data.setdefault('retry_policy', RetryPolicy.NO_RETRY)
        data.setdefault('user_message', 'You do not have permission to perform this action.')
        super().__init__(**data)


class ConfigurationError(ServiceError):
    """Error for configuration issues."""
    error_type: ErrorType = Field(default=ErrorType.CONFIGURATION, const=True)
    
    # Configuration specific fields
    missing_config: List[str] = Field(
        default_factory=list,
        description="Missing configuration keys"
    )
    invalid_config: Dict[str, str] = Field(
        default_factory=dict,
        description="Invalid configuration values"
    )
    config_source: Optional[str] = Field(
        default=None,
        description="Source of configuration (file, env, etc.)"
    )
    
    def __init__(self, **data):
        """Initialize configuration error with appropriate defaults."""
        data.setdefault('error_code', 'CONFIGURATION_ERROR')
        data.setdefault('severity', ErrorSeverity.CRITICAL)
        data.setdefault('retry_policy', RetryPolicy.NO_RETRY)
        data.setdefault('user_message', 'System configuration error. Please contact support.')
        super().__init__(**data)


# Error response wrapper for HTTP APIs
class ErrorResponse(BaseModel):
    """Standardized error response for HTTP APIs."""
    success: bool = Field(default=False, const=True)
    error: ServiceError = Field(description="Error details")
    
    # Request context
    request_id: Optional[str] = Field(default=None, description="Request identifier")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="Response timestamp"
    )
    
    # API metadata
    api_version: str = Field(default="v1", description="API version")
    service_name: str = Field(description="Service that generated the response")


# Helper functions for error creation
def create_validation_error(
    message: str,
    field_errors: List[Dict[str, str]] = None,
    **kwargs
) -> ValidationError:
    """Create a validation error with standard formatting."""
    return ValidationError(
        message=message,
        field_errors=field_errors or [],
        **kwargs
    )


def create_budget_exceeded_error(
    budget_type: str,
    limit: int,
    consumed: int,
    **kwargs
) -> BudgetExceededError:
    """Create a budget exceeded error with standard formatting."""
    return BudgetExceededError(
        message=f"{budget_type.title()} budget exceeded: {consumed}/{limit}",
        budget_type=budget_type,
        limit=limit,
        consumed=consumed,
        **kwargs
    )


def create_timeout_error(
    operation: str,
    timeout_ms: int,
    elapsed_ms: int,
    **kwargs
) -> TimeoutError:
    """Create a timeout error with standard formatting."""
    return TimeoutError(
        message=f"Operation '{operation}' timed out after {elapsed_ms}ms (limit: {timeout_ms}ms)",
        operation=operation,
        timeout_ms=timeout_ms,
        elapsed_ms=elapsed_ms,
        **kwargs
    )


def create_service_unavailable_error(
    service_name: str,
    health_check_details: str = None,
    **kwargs
) -> ServiceUnavailableError:
    """Create a service unavailable error with standard formatting."""
    return ServiceUnavailableError(
        message=f"Service '{service_name}' is unavailable",
        operation=f"connect_to_{service_name}",
        unavailable_services=[service_name],
        health_check_failures={service_name: health_check_details or "Connection failed"},
        **kwargs
    )