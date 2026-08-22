"""
Service-specific exception classes for FaultMaven system.

This module defines a hierarchy of exceptions that provide specific error context
and error codes for different types of failures in the system.
"""

from typing import Any, Dict, Optional


class FaultMavenError(Exception):
    """Base exception for FaultMaven system"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
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

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "CONFIG_ERROR", context)


class ServiceConnectionError(FaultMavenError):
    """External service connection errors"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "SERVICE_CONNECTION_ERROR", context)


class AgentProcessingError(FaultMavenError):
    """Agent processing and reasoning errors"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "AGENT_PROCESSING_ERROR", context)


class LLMProviderError(FaultMavenError):
    """LLM provider specific errors"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "LLM_PROVIDER_ERROR", context)


class DataValidationError(FaultMavenError):
    """Data validation and schema errors"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "DATA_VALIDATION_ERROR", context)


class KnowledgeBaseError(FaultMavenError):
    """Knowledge base and RAG system errors"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "KNOWLEDGE_BASE_ERROR", context)


class SessionManagementError(FaultMavenError):
    """Session and case management errors"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "SESSION_MANAGEMENT_ERROR", context)


class RedisConnectionError(ServiceConnectionError):
    """Redis-specific connection errors"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "REDIS_CONNECTION_ERROR", context)


class ChromaDBError(ServiceConnectionError):
    """ChromaDB-specific errors"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "CHROMADB_ERROR", context)


class PresidioError(ServiceConnectionError):
    """Presidio PII protection service errors"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "PRESIDIO_ERROR", context)


# OAuth 2.0 specific exceptions
class OAuthError(FaultMavenError):
    """Base OAuth error"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "OAUTH_ERROR", context)


class OAuthProtocolError(OAuthError):
    """An RFC 6749 §5.2 error to be rendered to the client verbatim.

    Distinct from the OAuth exceptions below, which say what went wrong
    *inside* the service. This one says what the client is to be told: an
    `error` code from the RFC's registry, a description written for whoever is
    holding the failing request, and the status to answer with.

    Raised only by `POST /auth/oauth/token` and `POST /auth/oauth/revoke`, and
    rendered by ``api.exception_handlers.oauth_protocol_error_handler``. Every
    other route — `GET /auth/oauth/authorize` included — keeps FastAPI's
    ``{"detail": ...}`` shape, so this must stay a type those routes do not
    raise (#1150).

    It is an exception rather than a returned response so that the endpoints
    need no ``return`` inside an ``except`` block: that shape leaks internal
    exception text into unauthenticated bodies, and
    ``tests/unit/modules/auth/api/test_auth_error_text_not_echoed.py`` refuses
    it structurally.
    """

    def __init__(
        self,
        error: str,
        error_description: str,
        status_code: int = 400,
    ):
        super().__init__(error_description, error_code=error.upper())
        self.error = error
        self.error_description = error_description
        self.status_code = status_code


class InvalidRequestError(OAuthError):
    """Invalid OAuth request (missing or invalid parameters)"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "INVALID_REQUEST", context)


class InvalidClientError(OAuthError):
    """Invalid client authentication"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "INVALID_CLIENT", context)


class InvalidGrantError(OAuthError):
    """Invalid authorization grant (expired code, invalid verifier, etc.)"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "INVALID_GRANT", context)


class UnauthorizedClientError(OAuthError):
    """Client not authorized for this operation"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "UNAUTHORIZED_CLIENT", context)


class UnsupportedGrantTypeError(OAuthError):
    """Unsupported grant type"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "UNSUPPORTED_GRANT_TYPE", context)


class InvalidScopeError(OAuthError):
    """Invalid or unsupported scope"""

    def __init__(
        self, message: str, error_code: str = None, context: Dict[str, Any] = None
    ):
        super().__init__(message, error_code or "INVALID_SCOPE", context)
