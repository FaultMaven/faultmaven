"""Custom exceptions for FaultMaven application."""

from typing import Any, Dict, Optional


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


class KnowledgeBaseException(FaultMavenException):
    """Raised when knowledge base operations fail."""
    pass


class LLMException(FaultMavenException):
    """Raised when LLM operations fail."""
    pass