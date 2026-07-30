"""Custom exceptions for FaultMaven application."""

from enum import Enum
from typing import Any, Dict, Optional


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


# Stable error_code identifying a permanent, operator-actionable billing /
# quota-exhaustion failure. Flows through every layer (provider → circuit
# breaker → error handler → engine → API → UI) so the user can be told to top
# up credits instead of being shown a generic "try again" 500.
QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"


# Stable error_code identifying a RECOVERABLE context-window overflow or output
# truncation. Unlike QUOTA_EXHAUSTED this is not terminal: the engine re-runs the
# turn with a minimal prompt and answers degraded (the NO-COLLAPSE guarantee).
# It is a cross-module contract with three participants — the error handler sets
# it, ``milestone_engine._is_context_length_error`` reads it to reach the degrade
# path, and the API boundary maps it to a retryable 503 — so it lives here rather
# than as a literal in each. A typo in any one of them would silently disable the
# degrade path and hard-fail the turn instead (the #662 regression).
TOKEN_LIMIT = "TOKEN_LIMIT"


# error_codes whose failure is scoped to the ACCOUNT/SERVICE rather than to the
# individual request: if this request failed for one of these reasons, so will
# every other request until an operator acts. Only these permanent failures may
# open a circuit breaker (``BaseExternalClient``), because the breaker is
# service-wide and its job is to stop pointless calls to a dependency that cannot
# currently serve anyone.
#
# The distinction that matters is SCOPE, not retryability. A rejected request
# (malformed body, unsupported feature, a response schema the model refuses to
# compile) is also permanent, but it is permanent *for that request only* —
# counting it opened the shared ``LLM_Providers`` breaker on three deterministic
# Gemini 400s and took down every other LLM call, including the fallback chain
# and smaller payloads that would have succeeded. Quota exhaustion is the
# opposite: opening the breaker is correct, and the latched ``error_code`` keeps
# the open-breaker error mapping to 402 instead of a generic 500 (the
# case_b639fac38fe0 chain).
SERVICE_SCOPED_ERROR_CODES = frozenset({QUOTA_EXHAUSTED})


# Billing/quota-exhaustion markers found in provider error bodies. These signal
# a PERMANENT account-level condition — out of credits, billing not enabled, or a
# hard spend/quota cap — that NO amount of retrying or waiting will clear; only an
# operator action (add credits / enable billing) resolves it. This is distinct
# from transient 429 rate-limiting, which IS retryable once the window resets.
# Matched case-insensitively against the full LLMException message (every
# provider includes the upstream response body in the message it raises).
_BILLING_ERROR_MARKERS: tuple = (
    "insufficient_quota",
    "exceeded your current quota",
    "check your plan and billing",
    "billing details",
    "billing account",
    "billing is not active",
    "billing has not been enabled",
    "payment required",
    "quota_exceeded",
    "out of credits",
    "insufficient credits",
    "insufficient_funds",
)


def is_billing_quota_error(message: str, status_code: Optional[int] = None) -> bool:
    """Detect a permanent billing/quota-exhaustion error from a provider.

    Returns True for account-level billing failures (out of credits, billing
    disabled, hard quota cap) that an operator must resolve — NOT for transient
    rate-limiting. HTTP 402 Payment Required is always treated as billing. For
    other statuses (notably 429, which providers reuse for both transient rate
    limits AND quota exhaustion), classification keys on explicit billing markers
    in the body so a plain rate-limit stays retryable.
    """
    if status_code == 402:
        return True
    text = (message or "").lower()
    return any(marker in text for marker in _BILLING_ERROR_MARKERS)


def is_billing_error(error: BaseException) -> bool:
    """Detect a permanent billing/quota-exhaustion failure on an exception.

    Prefers the typed ``error_code`` set on ``LLMException`` /
    ``CircuitBreakerError`` (walking ``__cause__`` so a billing error wrapped as
    the cause of a generic exception is still detected), falling back to
    marker-based body detection for plain exceptions whose typed metadata was
    lost in wrapping. Shared by the investigation error handler and any other
    layer (e.g. report generation) that must distinguish billing from transient
    failures.
    """
    cursor: Optional[BaseException] = error
    seen: set = set()  # guard against cyclic __cause__ chains
    while cursor is not None and id(cursor) not in seen:
        if getattr(cursor, "error_code", None) == QUOTA_EXHAUSTED:
            return True
        seen.add(id(cursor))
        cursor = cursor.__cause__
    return is_billing_quota_error(str(error))


class LLMException(FaultMavenException):
    """Raised when LLM operations fail.

    Attributes:
        status_code: HTTP status code from the provider API (if applicable).
        error_code: Stable classification of the failure when one applies (e.g.
            ``QUOTA_EXHAUSTED`` for billing/quota exhaustion). Auto-detected from
            the message/status when not passed explicitly. ``None`` for ordinary
            transient/config errors.
        retryable: Whether the error is worth retrying. Derived from
            status_code when provided, otherwise defaults to False (fail fast).
            - 429 → retryable (rate limited; transient, succeeds once the
              window resets — see below)
            - 4xx (other) → non-retryable (client error, same request fails again)
            - 5xx → retryable (transient server error)
            - No status code → non-retryable (callers must opt-in to retry)

            Note: 429 is the one 4xx that is retryable. Providers should pass
            ``status_code`` alone and let this derivation classify it — passing
            an explicit ``retryable=status==429`` is an anti-pattern because it
            silently forces 5xx to non-retryable. A billing/quota error is the
            exception: it is ALWAYS non-retryable regardless of status code,
            because waiting cannot add credits.
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        retryable: Optional[bool] = None,
        error_code: Optional[str] = None,
        **kwargs,
    ):
        self.status_code = status_code

        # Auto-classify permanent billing/quota exhaustion from the provider
        # body. Every provider folds the upstream error text into the message,
        # so this single chokepoint classifies all of them.
        if error_code is None and is_billing_quota_error(message, status_code):
            error_code = QUOTA_EXHAUSTED
        self.error_code = error_code

        if error_code == QUOTA_EXHAUSTED:
            # Permanent account-level failure — retrying/waiting cannot help.
            self.retryable = False
        elif retryable is not None:
            self.retryable = retryable
        elif status_code is not None:
            self.retryable = status_code >= 500 or status_code == 429
        else:
            self.retryable = False
        super().__init__(message, **kwargs)


class ModelLoadingException(LLMException):
    """Raised when an LLM model is still loading (e.g., HuggingFace 503).

    This exception signals to the orchestration layer that the model
    is temporarily unavailable due to loading, and the request should
    be retried after a delay.

    Attributes:
        retry_after: Suggested wait time in seconds before retry
        model_name: Name of the model that is loading
    """

    def __init__(
        self,
        message: str = "Model is loading",
        retry_after: int = 10,
        model_name: Optional[str] = None,
    ):
        self.retry_after = retry_after
        self.model_name = model_name
        super().__init__(
            message, details={"retry_after": retry_after, "model_name": model_name}
        )


class ToolCallingUnsupportedError(LLMException):
    """Raised when a model/provider does not support tool/function calling.

    This signals to the orchestration layer that tool calling failed due to
    model incompatibility (not a transient error), and it should fall back
    to a non-tool generation path.
    """

    def __init__(
        self,
        message: str = "Model does not support tool calling",
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.provider = provider
        self.model = model
        super().__init__(
            message,
            retryable=False,
            details={"provider": provider, "model": model},
        )


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

    This is the preferred exception class for "not found" errors in service code.
    It provides structured resource_type and resource_id fields for better error
    handling and logging.

    Attributes:
        resource_type: Type of the resource (e.g., "Case", "Session")
        resource_id: ID of the resource that was not found

    Usage:
        # Structured initialization (preferred)
        raise NotFoundError("Case", "case_123")

        # Message-only initialization (for simpler cases)
        raise NotFoundError(message="Document not found in knowledge base")
    """

    def __init__(
        self,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        message: Optional[str] = None,
    ):
        self.resource_type = resource_type
        self.resource_id = resource_id

        # Support both structured and message-only initialization
        if message:
            error_message = message
        elif resource_type and resource_id:
            error_message = f"{resource_type} not found: {resource_id}"
        elif resource_type:
            error_message = f"{resource_type} not found"
        else:
            error_message = "Resource not found"

        super().__init__(
            error_message,
            details={"resource_type": resource_type, "resource_id": resource_id},
        )


class AuthenticationError(ServiceError):
    """Authentication check failed.

    Raised when authentication fails (invalid credentials, expired tokens,
    missing authentication, etc.).

    This is re-exported from the auth module for convenience and to maintain
    a consistent exception hierarchy.
    """

    def __init__(
        self,
        message: str = "Authentication failed",
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.error_code = error_code
        super().__init__(message, details={**(details or {}), "error_code": error_code})


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
        conflict_reason: Optional[str] = None,
    ):
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.conflict_reason = conflict_reason
        super().__init__(
            message,
            details={
                "resource_type": resource_type,
                "resource_id": resource_id,
                "conflict_reason": conflict_reason,
            },
        )


class RepositoryError(ServiceError):
    """Repository operation failed.

    Raised when a repository operation (save, get, delete) fails
    due to database or storage issues.
    """

    pass
