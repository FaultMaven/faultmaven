"""
Logging standards and conventions for FaultMaven.

This module defines standardized error codes, log levels, and context
field standards to ensure consistent logging across the application.
"""

from enum import Enum
from typing import Dict, Any, Optional

class ErrorCode(Enum):
    """Standardized error codes for consistent error categorization."""
    
    # Session-related errors (1000-1999)
    SESSION_NOT_FOUND = "SESSION_001"
    SESSION_EXPIRED = "SESSION_002"
    SESSION_CREATION_FAILED = "SESSION_003"
    SESSION_DELETION_FAILED = "SESSION_004"
    SESSION_UPDATE_FAILED = "SESSION_005"
    
    # Data-related errors (2000-2999)
    DATA_UPLOAD_FAILED = "DATA_001"
    DATA_PROCESSING_FAILED = "DATA_002"
    DATA_VALIDATION_FAILED = "DATA_003"
    DATA_NOT_FOUND = "DATA_004"
    DATA_FORMAT_UNSUPPORTED = "DATA_005"
    
    # Agent-related errors (3000-3999)
    AGENT_INITIALIZATION_FAILED = "AGENT_001"
    AGENT_PROCESSING_FAILED = "AGENT_002"
    AGENT_STATE_INVALID = "AGENT_003"
    AGENT_TIMEOUT = "AGENT_004"
    AGENT_QUOTA_EXCEEDED = "AGENT_005"
    
    # Knowledge base errors (4000-4999)
    KB_QUERY_FAILED = "KB_001"
    KB_INDEXING_FAILED = "KB_002"
    KB_UPDATE_FAILED = "KB_003"
    KB_NOT_FOUND = "KB_004"
    
    # Infrastructure errors (5000-5999)
    REDIS_CONNECTION_FAILED = "INFRA_001"
    REDIS_OPERATION_FAILED = "INFRA_002"
    LLM_PROVIDER_ERROR = "INFRA_003"
    LLM_RATE_LIMIT = "INFRA_004"
    LLM_AUTHENTICATION_FAILED = "INFRA_005"
    
    # Security errors (6000-6999)
    AUTHENTICATION_FAILED = "SEC_001"
    AUTHORIZATION_FAILED = "SEC_002"
    INPUT_SANITIZATION_FAILED = "SEC_003"
    RATE_LIMIT_EXCEEDED = "SEC_004"
    
    # System errors (9000-9999)
    INTERNAL_SERVER_ERROR = "SYS_001"
    CONFIGURATION_ERROR = "SYS_002"
    RESOURCE_UNAVAILABLE = "SYS_003"
    TIMEOUT_ERROR = "SYS_004"

class LogLevel(Enum):
    """Standardized log levels with usage guidelines."""
    
    DEBUG = "DEBUG"      # Detailed diagnostic information
    INFO = "INFO"        # General operational messages
    WARNING = "WARNING"  # Recoverable issues
    ERROR = "ERROR"      # Unrecoverable errors
    CRITICAL = "CRITICAL" # System failures

class ContextField(Enum):
    """Standardized context field names for consistent structured logging."""
    
    # Request context
    CORRELATION_ID = "correlation_id"
    REQUEST_ID = "request_id"
    SESSION_ID = "session_id"
    USER_ID = "user_id"
    
    # Business context
    AGENT_PHASE = "agent_phase"
    INVESTIGATION_ID = "investigation_id"
    DATA_TYPE = "data_type"
    QUERY_COMPLEXITY = "query_complexity"
    
    # Technical context
    MODULE = "module"
    FUNCTION = "function"
    LINE = "line"
    DURATION_MS = "duration_ms"
    
    # Performance metrics
    TOKENS_USED = "tokens_used"
    CACHE_HIT = "cache_hit"
    RESPONSE_SIZE = "response_size"
    
    # Error context
    ERROR_CODE = "error_code"
    ERROR_TYPE = "error_type"
    ERROR_MESSAGE = "error_message"
    STACK_TRACE = "stack_trace"

class LoggingStandards:
    """Utility class for applying logging standards."""
    
    @staticmethod
    def get_error_context(error_code: ErrorCode, error_message: str, 
                         additional_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Generate standardized error context."""
        context = {
            ContextField.ERROR_CODE.value: error_code.value,
            ContextField.ERROR_MESSAGE.value: error_message,
            ContextField.ERROR_TYPE.value: error_code.name
        }
        
        if additional_context:
            context.update(additional_context)
            
        return context
    
    @staticmethod
    def get_business_context(session_id: Optional[str] = None,
                           user_id: Optional[str] = None,
                           agent_phase: Optional[str] = None,
                           investigation_id: Optional[str] = None,
                           **kwargs) -> Dict[str, Any]:
        """Generate standardized business context."""
        context = {}
        
        if session_id:
            context[ContextField.SESSION_ID.value] = session_id
        if user_id:
            context[ContextField.USER_ID.value] = user_id
        if agent_phase:
            context[ContextField.AGENT_PHASE.value] = agent_phase
        if investigation_id:
            context[ContextField.INVESTIGATION_ID.value] = investigation_id
            
        context.update(kwargs)
        return context
    
    @staticmethod
    def get_performance_context(duration_ms: float,
                              tokens_used: Optional[int] = None,
                              cache_hit: Optional[bool] = None,
                              response_size: Optional[int] = None) -> Dict[str, Any]:
        """Generate standardized performance context."""
        context = {
            ContextField.DURATION_MS.value: duration_ms
        }
        
        if tokens_used is not None:
            context[ContextField.TOKENS_USED.value] = tokens_used
        if cache_hit is not None:
            context[ContextField.CACHE_HIT.value] = cache_hit
        if response_size is not None:
            context[ContextField.RESPONSE_SIZE.value] = response_size
            
        return context

# Log level usage guidelines
LOG_LEVEL_GUIDELINES = {
    LogLevel.DEBUG: {
        "description": "Detailed diagnostic information",
        "usage": [
            "Function entry/exit points",
            "Variable values and state changes",
            "Database query details",
            "Network request details",
            "Cache hit/miss information"
        ],
        "production": "Disabled by default"
    },
    
    LogLevel.INFO: {
        "description": "General operational messages",
        "usage": [
            "Request start/completion",
            "Business operations (session creation, data upload)",
            "Service initialization",
            "Configuration changes",
            "User actions"
        ],
        "production": "Enabled"
    },
    
    LogLevel.WARNING: {
        "description": "Recoverable issues",
        "usage": [
            "Deprecated feature usage",
            "Performance degradation",
            "Resource usage approaching limits",
            "Retry attempts",
            "Fallback to alternative methods"
        ],
        "production": "Enabled"
    },
    
    LogLevel.ERROR: {
        "description": "Unrecoverable errors requiring attention",
        "usage": [
            "Failed operations",
            "Exception handling",
            "Service unavailability",
            "Data validation failures",
            "Authentication/authorization failures"
        ],
        "production": "Enabled"
    },
    
    LogLevel.CRITICAL: {
        "description": "System failures requiring immediate action",
        "usage": [
            "Service startup failures",
            "Database connection failures",
            "Critical resource exhaustion",
            "Security breaches",
            "System crashes"
        ],
        "production": "Enabled"
    }
}

# Context field standards
CONTEXT_FIELD_STANDARDS = {
    ContextField.CORRELATION_ID: {
        "description": "Unique identifier for request tracing",
        "format": "8-character hex string",
        "example": "a1b2c3d4",
        "required": True
    },
    
    ContextField.SESSION_ID: {
        "description": "User session identifier",
        "format": "UUID string",
        "example": "f6d5a493-9efa-4faa-9e1f-2d8b030fd590",
        "required": False
    },
    
    ContextField.USER_ID: {
        "description": "User identifier",
        "format": "String (user-defined)",
        "example": "user_123",
        "required": False
    },
    
    ContextField.AGENT_PHASE: {
        "description": "Current agent processing phase",
        "format": "String enum",
        "example": "validate_hypothesis",
        "required": False
    },
    
    ContextField.ERROR_CODE: {
        "description": "Standardized error code",
        "format": "String from ErrorCode enum",
        "example": "SESSION_001",
        "required": False
    },
    
    ContextField.DURATION_MS: {
        "description": "Operation duration in milliseconds",
        "format": "Float",
        "example": 1234.56,
        "required": False
    }
} 