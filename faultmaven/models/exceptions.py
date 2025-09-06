"""
Service-specific exception classes for FaultMaven system.

This module defines a hierarchy of exceptions that provide specific error context
and error codes for different types of failures in the system.
"""

from typing import Any, Dict, Optional


class FaultMavenError(Exception):
    """Base exception for FaultMaven system"""
    
    def __init__(self, message: str, error_code: str = None, context: Dict[str, Any] = None):
        super().__init__(message)
        self.message = message
        self.error_code = error_code or "FAULTMAVEN_ERROR"
        self.context = context or {}

    def __str__(self):
        if self.error_code:
            return f"[{self.error_code}] {self.message}"
        return self.message


class ConfigurationError(FaultMavenError):
    """Configuration-related errors"""
    
    def __init__(self, message: str, error_code: str = None, context: Dict[str, Any] = None):
        super().__init__(message, error_code or "CONFIG_ERROR", context)


class ServiceConnectionError(FaultMavenError):
    """External service connection errors"""
    
    def __init__(self, message: str, error_code: str = None, context: Dict[str, Any] = None):
        super().__init__(message, error_code or "SERVICE_CONNECTION_ERROR", context)


class AgentProcessingError(FaultMavenError):
    """Agent processing and reasoning errors"""
    
    def __init__(self, message: str, error_code: str = None, context: Dict[str, Any] = None):
        super().__init__(message, error_code or "AGENT_PROCESSING_ERROR", context)


class LLMProviderError(FaultMavenError):
    """LLM provider specific errors"""
    
    def __init__(self, message: str, error_code: str = None, context: Dict[str, Any] = None):
        super().__init__(message, error_code or "LLM_PROVIDER_ERROR", context)


class DataValidationError(FaultMavenError):
    """Data validation and schema errors"""
    
    def __init__(self, message: str, error_code: str = None, context: Dict[str, Any] = None):
        super().__init__(message, error_code or "DATA_VALIDATION_ERROR", context)


class ProtectionSystemError(FaultMavenError):
    """Protection system failures"""
    
    def __init__(self, message: str, error_code: str = None, context: Dict[str, Any] = None):
        super().__init__(message, error_code or "PROTECTION_SYSTEM_ERROR", context)


class KnowledgeBaseError(FaultMavenError):
    """Knowledge base and RAG system errors"""
    
    def __init__(self, message: str, error_code: str = None, context: Dict[str, Any] = None):
        super().__init__(message, error_code or "KNOWLEDGE_BASE_ERROR", context)


class SessionManagementError(FaultMavenError):
    """Session and case management errors"""
    
    def __init__(self, message: str, error_code: str = None, context: Dict[str, Any] = None):
        super().__init__(message, error_code or "SESSION_MANAGEMENT_ERROR", context)


class RedisConnectionError(ServiceConnectionError):
    """Redis-specific connection errors"""
    
    def __init__(self, message: str, error_code: str = None, context: Dict[str, Any] = None):
        super().__init__(message, error_code or "REDIS_CONNECTION_ERROR", context)


class ChromaDBError(ServiceConnectionError):
    """ChromaDB-specific errors"""
    
    def __init__(self, message: str, error_code: str = None, context: Dict[str, Any] = None):
        super().__init__(message, error_code or "CHROMADB_ERROR", context)


class PresidioError(ServiceConnectionError):
    """Presidio PII protection service errors"""
    
    def __init__(self, message: str, error_code: str = None, context: Dict[str, Any] = None):
        super().__init__(message, error_code or "PRESIDIO_ERROR", context)