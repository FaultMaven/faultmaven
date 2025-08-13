"""Custom exceptions for FaultMaven application."""

from typing import Any, Dict, Optional
from enum import Enum


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


class FaultMavenException(Exception):
    """Base exception for all FaultMaven errors."""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.details = details or {}


class ServiceException(FaultMavenException):
    """Raised when a service operation fails."""
    pass


class AgentException(FaultMavenException):
    """Raised when agent processing fails."""
    pass


class ValidationException(FaultMavenException):
    """Raised when input validation fails."""
    pass


class ConfigurationException(FaultMavenException):
    """Raised when configuration is invalid."""
    pass


class ExternalServiceException(FaultMavenException):
    """Raised when an external service call fails."""
    pass


class SessionException(FaultMavenException):
    """Raised when session operations fail."""
    pass


class SessionStoreException(SessionException):
    """Exception raised during session store operations."""
    pass


class SessionCleanupException(SessionStoreException):
    """Exception raised during session cleanup operations."""
    pass


class KnowledgeBaseException(FaultMavenException):
    """Raised when knowledge base operations fail."""
    pass


class LLMException(FaultMavenException):
    """Raised when LLM operations fail."""
    pass