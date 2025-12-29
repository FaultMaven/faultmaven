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


class NotFoundException(FaultMavenException):
    """Raised when a requested resource is not found."""
    pass


class PermissionDeniedException(FaultMavenException):
    """Raised when user lacks permission for an operation."""
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


class MemoryException(FaultMavenException):
    """Raised when memory operations fail."""
    pass


class PlanningException(FaultMavenException):
    """Raised when planning operations fail."""
    pass


class ReasoningException(FaultMavenException):
    """Raised when reasoning operations fail."""
    pass


class BudgetExceededException(FaultMavenException):
    """Raised when operational budget limits are exceeded."""
    pass


class ServiceUnavailableException(FaultMavenException):
    """Raised when a required service is not available."""
    pass


class EmbeddingException(KnowledgeBaseException):
    """Base exception for embedding-related errors."""
    pass


class EmbeddingGenerationError(EmbeddingException):
    """Raised when embedding generation fails."""
    pass


class EmbeddingRateLimitError(EmbeddingException):
    """Raised when embedding API rate limit is exceeded."""
    pass


class EmbeddingInvalidInputError(EmbeddingException):
    """Raised when input text is invalid for embedding generation."""
    pass


class VectorStoreException(KnowledgeBaseException):
    """Base exception for vector store operations."""
    pass


class VectorStoreConnectionError(VectorStoreException):
    """Raised when connection to vector store fails."""
    pass


class VectorStoreOperationError(VectorStoreException):
    """Raised when a vector store operation fails."""
    pass


# ============================================================
# Service Layer Exceptions (TASK-011)
# ============================================================


class ServiceError(FaultMavenException):
    """Base exception for service layer errors.

    All service-layer exceptions inherit from this class,
    providing a consistent hierarchy for error handling.
    """
    pass


class NotFoundError(ServiceError):
    """Resource not found.

    Raised when a requested resource (case, session, etc.) does not exist.

    Attributes:
        resource_type: Type of the resource (e.g., "Case", "Session")
        resource_id: ID of the resource that was not found
    """

    def __init__(self, resource_type: str, resource_id: str):
        self.resource_type = resource_type
        self.resource_id = resource_id
        super().__init__(
            f"{resource_type} not found: {resource_id}",
            details={"resource_type": resource_type, "resource_id": resource_id}
        )


class AuthorizationError(ServiceError):
    """Authorization check failed.

    Raised when a user/organization does not have permission
    to access or modify a resource.
    """

    def __init__(self, message: str = "Not authorized"):
        super().__init__(message)


class ConflictError(ServiceError):
    """Resource conflict (duplicate, state violation, etc.).

    Raised when an operation cannot be completed due to a conflict,
    such as trying to close an already-closed case.

    Attributes:
        resource_type: Type of the resource
        resource_id: ID of the resource
        conflict_reason: Description of the conflict
    """

    def __init__(
        self,
        message: str,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        conflict_reason: Optional[str] = None
    ):
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.conflict_reason = conflict_reason
        super().__init__(
            message,
            details={
                "resource_type": resource_type,
                "resource_id": resource_id,
                "conflict_reason": conflict_reason
            }
        )


class RepositoryError(ServiceError):
    """Repository operation failed.

    Raised when a repository operation (save, get, delete) fails
    due to database or storage issues.
    """
    pass