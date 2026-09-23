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


# Stable error_code for "the turn ran out of budget part-way through the LLM
# retry ladder". Transient, and it maps to the same 503 + Retry-After as
# ``RETRY_EXHAUSTED`` because the caller's next move is identical — but it names
# a DIFFERENT condition and that distinction is the point.
#
# ``RETRY_EXHAUSTED`` says the provider was given every attempt the retry
# configuration allows and failed all of them. This says the ladder stopped
# EARLY, because the next attempt could not have finished inside the turn-wide
# deadline (``AGENT_REQUEST_TIMEOUT``) that would otherwise have cancelled it
# mid-attempt and turned an honest 503 into an opaque 504 (#1278, #1292). One
# is a verdict about the provider; the other is a verdict about the
# configuration, and an operator seeing this one should look at
# ``LLM_REQUEST_TIMEOUT`` against ``AGENT_REQUEST_TIMEOUT`` — which
# ``GET /admin/config/status`` reports directly.
TURN_BUDGET_EXHAUSTED = "TURN_BUDGET_EXHAUSTED"


# Stable error_code for "the LLM layer is not configured": no provider in the
# fallback chain, or the registry cannot build one. Permanent until an operator
# edits the environment, so it is TERMINAL — the opposite of the transient codes
# above, and the reason it needs a name rather than falling into UNKNOWN_ERROR
# (which is mapped as retryable and hands the user a Retry-After for a condition
# that will never clear on its own). The registry already set this string on the
# exceptions it raises; naming it here is what let the engine and the HTTP
# boundary read it instead of the message.
LLM_CONFIG_ERROR = "LLM_CONFIG_ERROR"


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


# ---------------------------------------------------------------------------
# Typed LLM failure categories (#509)
# ---------------------------------------------------------------------------
#
# WHAT KIND of failure the provider reported, decided ONCE at the provider
# boundary and carried on the exception. Before this, every downstream
# consumer re-derived the answer by matching substrings of provider prose:
# ``CONTEXT_OVERFLOW_PHRASES`` in the engine's error handler, the same tuple
# again in ``milestone_engine``, and ``RetryConfig.retryable_patterns`` for
# retryability. Nine providers word errors differently and may reword them at
# any release, and — worse — those global lists were applied to EVERY message
# that reached the engine, including messages the engine itself composed and
# messages from non-LLM dependencies. That is how a ``host:port`` containing
# "404" became MODEL_NOT_FOUND and how an OpenAI "Unsupported parameter:
# 'max_tokens'" 400 became "Context too large".
#
# The category is the contract those consumers read instead. It is derived
# where the provider's own response is in hand and is authoritative there;
# downstream code asks WHAT the failure was, never HOW it was worded.


class LLMErrorCategory(str, Enum):
    """The recovery-selecting fact about an LLM failure.

    Deliberately about the FAILURE, not the remedy: the engine decides
    COMPRESS_MEMORY / FAIL / RETRY from this, and a different consumer may
    decide differently, but neither has to read provider prose to do it.
    """

    #: The prompt did not fit the model's context window. Retrying the
    #: identical request cannot help; shrinking the INPUT is the only remedy
    #: (the minimal-prompt degrade, #662).
    CONTEXT_OVERFLOW = "context_overflow"

    #: The provider cut the ANSWER at the generation cap. The prompt fit; the
    #: response did not. First remedy is a larger cap, not a smaller prompt.
    OUTPUT_TRUNCATION = "output_truncation"

    #: The request itself was refused as malformed/unsupported — a wrong
    #: parameter, an unsupported value, a schema the model will not compile.
    #: Permanent for THIS request shape and for it alone, so it must never be
    #: read as an overflow (which would loop on futile compression) nor as a
    #: transient (which would loop on an identical request).
    REQUEST_REJECTED = "request_rejected"

    #: The provider failed in a way that may not recur: 5xx, rate limiting,
    #: overload, a transport fault.
    TRANSIENT = "transient"

    #: Nothing authoritative said which of the above it is. Never collapsed
    #: into one of them — "no signal" and "signal says no" are different
    #: answers, and a consumer that cannot tell them apart fails open.
    UNKNOWN = "unknown"


# Machine-readable error codes providers publish in their error bodies, mapped
# to what they MEAN. This is the authoritative tier: a code is part of a
# provider's API contract in a way a sentence is not.
#
# Deliberately absent are the GENERIC 400-family codes — OpenAI/Anthropic
# ``invalid_request_error``, Gemini ``INVALID_ARGUMENT``, vLLM
# ``BadRequestError``. Each of those covers BOTH an overflow and a bad
# parameter, so mapping them would assert a distinction the provider did not
# make. They fall through to the wording tier below, which is what still has
# to separate them. That is the honest limit of "classify off the error code":
# OpenAI-compatible providers publish ``context_length_exceeded``, Anthropic
# and Gemini publish nothing narrower than the 400 family.
_PROVIDER_ERROR_CODE_CATEGORIES: Dict[str, "LLMErrorCategory"] = {
    # OpenAI-compatible (OpenAI, Groq, Fireworks, OpenRouter, vLLM)
    "context_length_exceeded": LLMErrorCategory.CONTEXT_OVERFLOW,
    "string_above_max_length": LLMErrorCategory.CONTEXT_OVERFLOW,
    "unsupported_parameter": LLMErrorCategory.REQUEST_REJECTED,
    "unsupported_value": LLMErrorCategory.REQUEST_REJECTED,
    "invalid_value": LLMErrorCategory.REQUEST_REJECTED,
    "unsupported_country_region_territory": LLMErrorCategory.REQUEST_REJECTED,
    "rate_limit_exceeded": LLMErrorCategory.TRANSIENT,
    "server_error": LLMErrorCategory.TRANSIENT,
    # Anthropic error ``type`` values
    "overloaded_error": LLMErrorCategory.TRANSIENT,
    "api_error": LLMErrorCategory.TRANSIENT,
    # Google API ``status`` values (Gemini)
    "resource_exhausted": LLMErrorCategory.TRANSIENT,
    "unavailable": LLMErrorCategory.TRANSIENT,
    "deadline_exceeded": LLMErrorCategory.TRANSIENT,
    "internal": LLMErrorCategory.TRANSIENT,
}


# LAST-RESORT provider WORDING, consulted only when no machine code decided.
#
# These are two of the three tuples that used to live in the engine
# (``CONTEXT_OVERFLOW_PHRASES`` and ``_PARAM_ERROR_GUARD_PHRASES``; the third,
# ``_OUTPUT_TRUNCATION_PHRASES``, is deleted outright — see below). What
# changed is not that prose disappeared —
# Anthropic and Gemini give us nothing else — but WHERE it is read: once, at
# the provider boundary, against the provider's own response body. Downstream
# nothing matches text, so the engine's own messages, a ``host:port``, a JSON
# decoder's complaint and a ChromaDB outage can no longer be classified as
# LLM failures by accident.
#
# Length/window-specific on purpose: NO bare "token" and NO bare "too long",
# both of which fire on ordinary request-validation errors.
_CONTEXT_OVERFLOW_WORDING: tuple = (
    "context length",
    "context window",
    "maximum context",
    "context_length_exceeded",
    "too many tokens",
    "reduce the length of the messages",
    "prompt is too long",
    "input is too long",
    "maximum context length",
    "exceeds the maximum context",
    # Gemini's 400 body, whose ``status`` is the undifferentiated
    # ``INVALID_ARGUMENT``: "The input token count (1200000) exceeds the
    # maximum number of tokens allowed (1048576)." None of the phrases above
    # match it, so before #509 the shipped default provider's own overflow
    # reached the engine unclassified and HARD-FAILED the turn instead of
    # degrading. Narrow enough not to fire on a parameter-shape 400: it names
    # a maximum NUMBER OF TOKENS, not a bare "token" or "too long".
    "exceeds the maximum number of tokens",
)

# There is deliberately NO wording tier for OUTPUT_TRUNCATION. A cut answer
# arrives as an ordinary HTTP 200 with a short body, so no error body ever
# reports one — the party that notices is the party that must declare it:
# the Gemini adapter, which sees ``finishReason: MAX_TOKENS`` and passes
# ``category=OUTPUT_TRUNCATION``, and the engine's own
# ``OutputTruncationError``, which carries the same. The predecessor tuple
# (``"truncated"``, ``"finishreason=max_tokens"``) existed only to recognise
# that one Gemini raise by its sentence; keeping it would have left a 4xx body
# that merely says "request truncated" routed to the max_tokens ladder, which
# cannot help a rejected request.

# A wrong/unsupported request parameter is a config error, NOT an overflow;
# reading it as one masks the real cause and loops on futile compression
# (e.g. OpenAI "Unsupported parameter: 'max_tokens' ... use
# 'max_completion_tokens'"). Checked FIRST so a message carrying both
# vocabularies is read as the rejection it is.
_REQUEST_REJECTED_WORDING: tuple = (
    "unsupported parameter",
    "unsupported_parameter",
    "is not supported with this model",
)


def classify_llm_error(
    status_code: Optional[int] = None,
    provider_error_code: Optional[str] = None,
    message: str = "",
) -> LLMErrorCategory:
    """Classify a provider failure, preferring the most authoritative signal.

    Four tiers, in strict precedence order:

    1. The provider's own machine-readable error code, where it publishes one
       narrow enough to mean something (``context_length_exceeded``,
       ``unsupported_parameter``, ``overloaded_error``, ``RESOURCE_EXHAUSTED``).
    2. Provider WORDING, for the providers whose 400 family is undifferentiated
       — Anthropic's "prompt is too long", Gemini's "input token count …
       exceeds". Read here and nowhere else.
    3. The HTTP status: 5xx and 429 are transient, every other 4xx is a
       rejection of this request shape.
    4. ``UNKNOWN`` — no status, no code, no recognised wording.

    ``OUTPUT_TRUNCATION`` is never derived here — it is only ever declared, by
    the party that watched the answer get cut. So a body that says "input
    truncated: context length exceeded" is read as the overflow it is
    reporting, rather than as an ambiguity to be resolved by precedence.

    The status tier is LAST, not first, on purpose: a gateway can answer 5xx
    with an overflow body, and treating that as merely transient would spend
    every retry re-sending the same oversized prompt and then fail the turn
    instead of degrading (#662's NO-COLLAPSE guarantee).
    """
    code = (provider_error_code or "").strip().lower()
    if code:
        mapped = _PROVIDER_ERROR_CODE_CATEGORIES.get(code)
        if mapped is not None:
            return mapped

    text = (message or "").lower()
    if any(p in text for p in _REQUEST_REJECTED_WORDING):
        return LLMErrorCategory.REQUEST_REJECTED
    if any(p in text for p in _CONTEXT_OVERFLOW_WORDING):
        return LLMErrorCategory.CONTEXT_OVERFLOW

    if status_code is not None:
        if status_code >= 500 or status_code == 429:
            return LLMErrorCategory.TRANSIENT
        return LLMErrorCategory.REQUEST_REJECTED

    return LLMErrorCategory.UNKNOWN


def declared_llm_category(error: BaseException) -> Optional[LLMErrorCategory]:
    """The first genuine ``LLMErrorCategory`` on the ``__cause__`` chain.

    ``None`` means nobody classified this failure — never ``UNKNOWN``, which is
    a provider-boundary verdict of "I looked and could not tell". A consumer
    that cannot distinguish "not an LLM failure at all" from "an LLM failure of
    unknown kind" fails open on both.

    The value must be a genuine ``LLMErrorCategory``. ``getattr`` alone would
    accept a ``Mock``'s auto-attribute (truthy, and equal to nothing) or a
    string left by some other layer, and a bare string would silently never
    match an ``is`` comparison downstream.
    """
    for cursor in walk_cause_chain(error):
        category = getattr(cursor, "category", None)
        if isinstance(category, LLMErrorCategory):
            return category
    return None


# Billing/quota-exhaustion markers found in provider error bodies. These signal
# a PERMANENT account-level condition — out of credits, billing not enabled, or a
# hard spend/quota cap — that NO amount of retrying or waiting will clear; only an
# operator action (add credits / enable billing) resolves it. This is distinct
# from transient 429 rate-limiting, which IS retryable once the window resets.
# Matched case-insensitively against the full LLMException message (every
# provider includes the upstream response body in the message it raises).
#
# This is the FALLBACK tier, not the primary one (#548): a structured billing
# code, where the provider publishes one, is read first from
# ``_BILLING_PROVIDER_ERROR_CODES`` below. The markers stay because most
# providers publish nothing structured — Cohere sends a bare message,
# HuggingFace a bare string, and Gemini's only machine signal for a
# billing-disabled 403 is the same ``PERMISSION_DENIED`` a mis-scoped key
# gets. Narrowing this list to "what the code tier misses" would shrink
# detection for seven of the nine providers.
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
    # Anthropic's COARSE billing body, whose ``error.type`` is the
    # undifferentiated ``invalid_request_error`` rather than the union member
    # below: "Your credit balance is too low to access the Anthropic API."
    # None of the markers above match it. Narrow on purpose — the whole clause,
    # not a bare "credit balance", which a non-error sentence could carry.
    "credit balance is too low",
)


# Machine-readable provider error codes that mean PERMANENT billing/quota
# exhaustion AND NOTHING ELSE (#548). This is the authoritative tier for
# billing — the counterpart, on the ``error_code`` axis, of
# ``_PROVIDER_ERROR_CODE_CATEGORIES`` above.
#
# It costs no new per-provider plumbing because the per-provider part is
# already done: every adapter reads its own body shape once through
# ``providers.base.extract_provider_error_code`` and hands the result to
# ``LLMException(provider_error_code=...)``. Until this tier existed that code
# decided the *category* and was ignored for billing, so billing classification
# depended entirely on a provider's ENGLISH — the marker list above — for every
# status but 402. A provider rewording its message, or localizing it, silently
# regressed the case_b639fac38fe0 failure.
#
# Matched by EXACT VALUE against the extracted code, never as a substring of a
# message, so an identifier or a sentence that merely contains one of these
# words cannot fire it.
#
# Admission bar: the code must mean billing exhaustion and nothing else.
# Deliberately absent, each for a reason:
#   * ``resource_exhausted`` — Gemini returns it for BOTH a transient
#     per-minute rate limit and a hard quota cap. Mapping it would make every
#     Gemini rate limit permanent, the exact inverse of the incident.
#   * ``permission_denied`` — Gemini's billing-disabled 403 carries it, but so
#     does a key that merely lacks access to the API. The unambiguous fact is
#     nested in ``error.details[].reason == "BILLING_DISABLED"``, deeper than
#     the shared extractor reaches, so Gemini stays on the marker tier where
#     "billing has not been enabled" already classifies it.
#   * ``rate_limit_exceeded``, ``invalid_request_error``, ``api_error`` — the
#     coarse families. Each covers billing AND not-billing.
_BILLING_PROVIDER_ERROR_CODES: frozenset = frozenset(
    {
        # OpenAI: both ``error.code`` and ``error.type`` on the quota 429.
        # Inherited unchanged by OpenRouter (which subclasses OpenAIProvider)
        # and by any OpenAI-compatible surface that copies the envelope.
        "insufficient_quota",
        # Anthropic: ``error.type``, a member of the error discriminated union
        # in the installed SDK — ``anthropic.types.BetaBillingError`` declares
        # ``type: Literal["billing_error"]``, distinct from
        # ``rate_limit_error``. Its prose names a credit balance, which matched
        # no marker before, so this failure was invisible to BOTH tiers.
        "billing_error",
    }
)


def is_billing_quota_error(
    message: str,
    status_code: Optional[int] = None,
    provider_error_code: Optional[str] = None,
) -> bool:
    """Detect a permanent billing/quota-exhaustion error from a provider.

    Returns True for account-level billing failures (out of credits, billing
    disabled, hard quota cap) that an operator must resolve — NOT for transient
    rate-limiting.

    Three tiers, most authoritative first (#548):

    1. The provider's own machine-readable error code, where it publishes one
       narrow enough to mean billing and nothing else
       (``_BILLING_PROVIDER_ERROR_CODES``). A code is part of a provider's API
       contract in a way a sentence is not: it survives rewording and
       localization, which the marker tier does not.
    2. HTTP 402 Payment Required, whose only meaning is this one.
    3. English markers in the body (``_BILLING_ERROR_MARKERS``) — the
       FALLBACK, and still load-bearing: of the nine providers only two publish
       a structured billing code, and Gemini — the shipped default — is one of
       the seven that do not.

    The tiers only ever ADD. Nothing here can veto a marker match, because a
    provider whose code tier says nothing is exactly the provider the markers
    exist for.
    """
    code = (provider_error_code or "").strip().lower()
    if code in _BILLING_PROVIDER_ERROR_CODES:
        return True
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
            ``QUOTA_EXHAUSTED`` for billing/quota exhaustion). Auto-detected
            from ``provider_error_code`` first, then the status, then the
            message, when not passed explicitly. ``None`` for ordinary
            transient/config errors.
        provider_error_code: The machine-readable code the provider put in its
            error body (OpenAI ``context_length_exceeded``, Anthropic
            ``overloaded_error``, Gemini ``RESOURCE_EXHAUSTED``). Providers
            extract it with
            ``infrastructure.llm.providers.base.extract_provider_error_code``.
            Read on BOTH classification axes: ``classify_llm_error`` turns it
            into a ``category``, and ``is_billing_quota_error`` reads it for
            billing exhaustion (#548).
        category: ``LLMErrorCategory`` — WHAT KIND of failure this is (#509).
            Derived from ``status_code`` + ``provider_error_code`` + the
            provider's wording unless the raiser passes one. Always set; never
            ``None``. This is what the engine keys COMPRESS_MEMORY / FAIL /
            RETRY off, in place of the substring lists it used to carry.
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
        provider_error_code: Optional[str] = None,
        category: Optional[LLMErrorCategory] = None,
        **kwargs,
    ):
        self.status_code = status_code
        self.provider_error_code = provider_error_code

        # Auto-classify permanent billing/quota exhaustion from the provider
        # body. Every provider folds the upstream error text into the message
        # and hands over the machine-readable code it extracted from that same
        # body, so this single chokepoint classifies all of them — off the code
        # where one exists, off the wording where it does not (#548).
        if error_code is None and is_billing_quota_error(
            message, status_code, provider_error_code
        ):
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

        # WHAT KIND of failure this is (#509), decided here — the one place
        # that holds the provider's status, its machine-readable error code and
        # its own wording at the same time. Every downstream consumer reads
        # this instead of re-matching the message.
        #
        # ``category`` may be passed explicitly by a provider that observed the
        # failure itself rather than reading it off an HTTP response — Gemini's
        # ``finishReason=MAX_TOKENS`` is the shipped example. An explicit value
        # always wins: the raiser knows more than any derivation can.
        #
        # Deliberately does NOT feed ``retryable``. That flag is a separate,
        # already-reviewed derivation from the HTTP contract, and the two
        # answer different questions: an overflow can arrive on a retryable
        # 5xx, and the engine's overflow branch is what must win there, not a
        # re-derived flag.
        self.category: LLMErrorCategory = (
            category
            if category is not None
            else classify_llm_error(
                status_code=status_code,
                provider_error_code=provider_error_code,
                message=message,
            )
        )
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
