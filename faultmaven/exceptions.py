"""Custom exceptions for FaultMaven application."""

from enum import Enum
from typing import Any, Dict, Iterator, Optional


def walk_cause_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield ``exc`` and each ``__cause__`` ancestor, cycle-guarded.

    Service code wraps provider failures as ``raise ServiceException(...) from
    e``, so the authoritative typed metadata (``retryable``, ``error_code``,
    ``status_code``) lives on the cause rather than the wrapper. Every
    classifier that reads that metadata walks the same chain, so the walk lives
    here once.

    ``__cause__`` only — the explicit ``raise ... from`` link. Never the
    implicit ``__context__``, which would pull in any unrelated exception that
    happened to be in flight.
    """
    cursor: Optional[BaseException] = exc
    seen: set = set()
    while cursor is not None and id(cursor) not in seen:
        yield cursor
        seen.add(id(cursor))
        cursor = cursor.__cause__


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


class ExternalCallTimeout(ExternalServiceException, TimeoutError):
    """A call to an external service exceeded its CLIENT-SIDE deadline.

    Raised by ``BaseExternalClient.call_external`` when its ``asyncio.wait_for``
    expires — the dependency never answered, so there is no provider status
    code and no provider wording to classify from.

    ``retryable`` is declared here, by the code that raises, rather than
    inferred downstream from the message text. The site used to raise a bare
    ``TimeoutError("… timed out after 30.0s")`` and the engine's retry ladder
    decided retryability by substring-matching that sentence against a phrase
    list containing ``"timeout"`` — which is not a substring of ``"timed out"``.
    A hung provider therefore got ZERO retries while every provider's OWN
    timeout (an ``LLMException`` with status 504) got three (#1287). The two
    disagreed about the same condition purely because one of them was a
    sentence. Anything that reads retryability must read this attribute, never
    the message.

    Subclasses ``TimeoutError`` (which IS ``asyncio.TimeoutError`` on 3.11+, the
    project floor) so every existing ``except TimeoutError`` around
    ``call_external`` keeps catching it.

    Always ``True``: a deadline expiring says the call did not finish, not that
    it would fail again. A caller that must NOT retry bounds its own attempts;
    it cannot learn that from the timeout.
    """

    retryable = True

    def __init__(
        self,
        message: str,
        service: Optional[str] = None,
        operation: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        super().__init__(
            message,
            details={"service": service, "operation": operation, "timeout": timeout},
        )
        self.service = service
        self.operation = operation
        self.timeout = timeout


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


# Stable error_code for a rejected provider CREDENTIAL — revoked/invalid API key,
# or a key without access to the requested resource (HTTP 401/403). Like
# QUOTA_EXHAUSTED this is permanent and account-scoped: every request fails the
# same way until an operator rotates or re-provisions the key. It exists so that
# condition can (a) open a circuit breaker instead of letting every turn make a
# doomed round trip, and (b) survive on the open-breaker error as something
# actionable rather than collapsing into a generic 500.
#
# Deliberately excludes 404: a not-found is not reliably account-scoped (a wrong
# model id fails every request, but a wrong path fails only that call shape), and
# converting it into a breaker trip would replace an actionable "model not found"
# with an opaque outage.
PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"


# Stable error_code for "the LLM circuit breaker is open, so this request never
# reached a provider". Transient like ``RETRY_EXHAUSTED`` — it clears on its own
# once the breaker's recovery window elapses — and it maps to the same 503, but
# it names a DIFFERENT condition and that distinction is the whole point. A
# ``CircuitBreakerError`` carries no provider status and its message ("Circuit
# breaker is open for LLM_Providers") matches no retry phrase, so it used to
# fall through the engine's classifier to ``UNKNOWN_ERROR`` — telling an
# operator "unknown" about the one failure the system understands completely
# (#1287).
PROVIDER_CIRCUIT_OPEN = "PROVIDER_CIRCUIT_OPEN"


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
# case_b639fac38fe0 chain). A rejected credential (PROVIDER_AUTH_FAILED) is the
# same shape as quota: permanent until an operator rotates the key.
SERVICE_SCOPED_ERROR_CODES = frozenset({QUOTA_EXHAUSTED, PROVIDER_AUTH_FAILED})


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
    for cursor in walk_cause_chain(error):
        if getattr(cursor, "error_code", None) == QUOTA_EXHAUSTED:
            return True
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
        # A rejected credential is the other account-scoped permanent failure.
        # Classified from the status code alone: 401/403 mean the key is invalid,
        # revoked, or lacks access, and no provider wording is needed to know that
        # every later request fails identically until an operator rotates it.
        # Without a code here the condition is invisible to the breaker's
        # service-scope test, and every turn keeps making a doomed round trip.
        if error_code is None and status_code in (401, 403):
            error_code = PROVIDER_AUTH_FAILED
        self.error_code = error_code

        if error_code in (QUOTA_EXHAUSTED, PROVIDER_AUTH_FAILED):
            # Permanent account-level failure — retrying/waiting cannot help.
            self.retryable = False
        elif retryable is not None:
            self.retryable = retryable
        elif status_code is not None:
            self.retryable = status_code >= 500 or status_code == 429
        else:
            self.retryable = False
        super().__init__(message, **kwargs)


class LLMOutputFloorError(LLMException):
    """A response was cut at the output cap with less visible output than the
    caller's declared floor (``min_output_tokens``, #1117).

    Raised by the router instead of returning the starved body: the caller
    pre-declared the minimum visible output it can use, so handing back less
    would be handing back exactly what it said is unusable. The typical cause
    is hidden reasoning consuming the shared token budget the answer needed
    (the fm#1094 starvation shape). Only raised when a caller opted in by
    setting the floor — calls without one keep the existing behavior of
    returning the truncated response for the caller to inspect.

    Non-retryable by derivation (no status code): an identical retry starves
    identically. A caller that wants recovery should retry with a larger
    ``max_tokens`` or a lower reasoning intent — a decision this layer cannot
    make for it.
    """


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


class InactiveAccountError(AuthorizationError):
    """A deactivated account was asked to be issued a token.

    Subclasses ``AuthorizationError`` rather than defining its own handler: being
    deactivated is an authorization failure, not a server fault, and Starlette
    resolves handlers by walking the exception's MRO — so this is already
    answered as 403 with no registry entry to keep in sync. That matters here,
    because the point of raising it centrally (see
    ``jwt_token_generator._refuse_if_deactivated``) is that no caller has to know
    about it; a caller that does not translate it must still not leak a 500.

    Protocol-speaking callers translate it into their own vocabulary first — the
    OAuth legs into ``InvalidGrantError(USER_INACTIVE)``.
    """


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


class UserLookupFailed(RepositoryError):
    """A user lookup could not be completed — this is NOT "no such user" (#1043).

    The user stores previously caught every exception on the read path and
    returned ``None``, so a transient database error, an exhausted connection
    pool, or a role/permission problem all surfaced as *absent*. "No such user"
    is a **claim**, and returning it on evidence the code does not have is the
    wrong default for an auth substrate: it is worst on the operator paths, which
    run during incidents and offboarding, where it sends someone hunting for the
    right username while the real fault — an unavailable database — stays
    invisible and the cutoff has not happened.

    So a failed lookup raises this instead, and only a lookup that genuinely
    completed and matched nothing returns ``None``. Callers that legitimately
    treat absence as a normal outcome (registration uniqueness checks, the SSO
    JIT path) keep working unchanged — an absent user still returns ``None``;
    what changes is that they now fail loudly instead of proceeding on a guess.

    Over HTTP this is a ``ServiceError``, so it becomes a generic 500 and the
    identifier stays in the log (``service_error_handler``). The message is
    written for the operator reading that log, or the CLI printing it.
    """

    def __init__(self, message: str, *, lookup: str, identifier: str):
        """
        Args:
            message: What failed, in operator-facing terms.
            lookup: Which lookup it was ("user_id", "username", "email") — the
                CLIs try several in sequence, so "which one broke" is the
                difference between a typo and an outage.
            identifier: The value looked up. Echoed back to whoever supplied it,
                never to a different party: it reaches CLI output and structured
                logs, and the HTTP path replaces the whole body with a generic
                message.
        """
        super().__init__(message, details={"lookup": lookup, "identifier": identifier})
        self.lookup = lookup
        self.identifier = identifier
