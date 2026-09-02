"""LLM Error Handler with Retry and Recovery

Handles LLM API errors with automatic retry, exponential backoff,
and recovery strategies.

Design Reference:
- docs/architecture/investigation-engine/error-handling-and-recovery.md Section 2

Usage:
    handler = LLMErrorHandler()
    result, error = await handler.with_retry(llm_call_coroutine)
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Tuple, TypeVar

from faultmaven.core.investigation.turn_budget import (
    can_afford_next_attempt,
    spendable_turn_budget,
)
from faultmaven.exceptions import (
    LLM_CONFIG_ERROR,
    PROVIDER_AUTH_FAILED,
    PROVIDER_CIRCUIT_OPEN,
    QUOTA_EXHAUSTED,
    TOKEN_LIMIT,
    TURN_BUDGET_EXHAUSTED,
    is_billing_error,
    walk_cause_chain,
)
from faultmaven.infrastructure.base_client import CircuitBreakerError

logger = logging.getLogger(__name__)

# ``service_name`` the LLM router gives its ``BaseExternalClient`` (router.py).
# The open-breaker branch in ``handle_error`` keys on it so an outage of some
# OTHER dependency that happens to reach this handler is not reported to the
# user as an AI-provider outage.
LLM_BREAKER_SERVICE = "LLM_Providers"

T = TypeVar("T")

# Context-window overflow phrases (the prompt is too large for the window).
# SHARED with milestone_engine._is_context_length_error (which imports this
# tuple) so the two overflow classifiers cannot drift — a provider's overflow
# wording (e.g. Cohere's "too many tokens") must route the same way through
# either path. Deliberately length/window-specific: NO bare "token" or bare
# "too long" (those fire on ordinary request-validation errors).
CONTEXT_OVERFLOW_PHRASES: Tuple[str, ...] = (
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
)

# Output truncated at the generation cap: the response body is cut off, so the
# JSON fails to parse. Distinct from input overflow — the prompt fit, the
# ANSWER did not — and it has its own recovery ladder (raise max_tokens, then
# degrade the prompt), driven by the typed ``OutputTruncationError`` below.
#
# These phrases are for *reporting* (``classify_token_limit_reason``) and for
# the one site where wording is the only evidence available
# (``is_output_truncation_error``: a provider that reports the cut before there
# is any body to inspect). They are NOT how the parse site decides — see
# ``is_truncated_json_error``, which tests position instead of text.
_OUTPUT_TRUNCATION_PHRASES: Tuple[str, ...] = (
    "truncated",
    "unterminated",  # truncated JSON string
    "eof while parsing",  # Pydantic/JSON parse of a cut-off body
    "finishreason=max_tokens",  # Gemini surfaces the output-cap reason
)

# A wrong/unsupported request parameter is a config error, NOT a token limit;
# matching it as one masks the real cause and loops on futile compression
# (e.g. OpenAI "Unsupported parameter: 'max_tokens' ... use
# 'max_completion_tokens'").
_PARAM_ERROR_GUARD_PHRASES: Tuple[str, ...] = (
    "unsupported parameter",
    "unsupported_parameter",
    "is not supported with this model",
)


class OutputTruncationError(Exception):
    """The model's response body was cut off at the generation cap (#513).

    Raised by the caller that OWNS the cap — the engine's structured-output
    loop — because only that caller knows whether raising it is still an option.

    Typed rather than string-matched, because the two sites that observe
    truncation word it in completely unrelated ways: the provider says
    ``finishReason=MAX_TOKENS``, while CPython's JSON decoder says ``Expecting
    ',' delimiter`` or ``Unterminated string starting at``. Neither vocabulary
    covers the other, and matching on either alone is what let the failure
    retry forever without ever raising the cap.

    ``cap_reached`` names the lever that is left:

    * ``False`` — the cap was raised, so an identical retry now has room to
      finish. RETRY.
    * ``True`` — the cap is already at its ceiling; raising it again is a
      no-op. The only remaining lever is shrinking the INPUT, which is what the
      minimal-prompt degrade does (#662), so this routes to COMPRESS_MEMORY.
      Without that hand-off the turn spends its remaining attempts on identical
      full-size calls and then fails, breaking the NO-COLLAPSE guarantee.
    """

    def __init__(self, message: str, cap_reached: bool = False):
        super().__init__(message)
        self.cap_reached = cap_reached


def is_output_truncation_error(error: BaseException) -> bool:
    """True when a *provider* reports it cut the response at the generation cap.

    Text-based by necessity: this is the one site where the provider's own
    wording is the only evidence there is. Gemini raises on
    ``finishReason=MAX_TOKENS`` from inside ``generate()``, before any body
    exists to inspect. The parse-time site has the body and uses the sharper
    positional test in ``is_truncated_json_error`` instead.

    Input overflow wins when a message carries both vocabularies — a gateway
    that says "input truncated: context length exceeded" is reporting that the
    PROMPT did not fit, and raising the generation cap cannot help. Same
    precedence as ``classify_token_limit_reason``, so the two never disagree
    about which failure a single message is.
    """
    msg = str(getattr(error, "message", "") or error).lower()
    # A wrong/unsupported request parameter is a config error, not a cut body.
    if any(guard in msg for guard in _PARAM_ERROR_GUARD_PHRASES):
        return False
    if any(p in msg for p in CONTEXT_OVERFLOW_PHRASES):
        return False
    return any(p in msg for p in _OUTPUT_TRUNCATION_PHRASES)


def is_truncated_json_error(error: BaseException, content: Any) -> bool:
    """True when *error* is a JSON parse failure caused by a body that ran out.

    Truncation is a POSITION, not a phrase. CPython's decoder never emits the
    words this code used to look for: a body cut mid-document raises
    ``Expecting ',' delimiter`` / ``Expecting ':' delimiter`` / ``Expecting
    value`` with ``pos`` at the end of the input, or ``Unterminated string
    starting at``, which by construction means the input ended inside a string
    literal. Neither ``truncated`` nor ``EOF while parsing`` ever appears, so
    the phrase test that guarded this never fired and the max_tokens ladder
    never engaged (#513).

    A document malformed in the MIDDLE — model prose, a stray comma, a bad
    literal — stops with ``pos`` well before the end. That is deliberately NOT
    truncation: a bigger generation cap cannot fix it, and letting it consume
    the truncation ladder would burn the turn's attempts on a retry that has no
    reason to differ.
    """
    if not isinstance(error, json.JSONDecodeError) or not isinstance(content, str):
        return False
    if error.msg.startswith("Unterminated string"):
        return True
    return error.pos >= len(content.rstrip())


# Reason labels for the degrade-recovery metric. Both classes reach the
# minimal-prompt recovery as TOKEN_LIMIT, but by different routes and for
# different reasons. INPUT_OVERFLOW is what that recovery targets directly.
# OUTPUT_TRUNCATION arrives only once the max_tokens ladder is exhausted
# (#513) — raising the cap is the targeted fix, and shrinking the prompt is the
# fallback when the ceiling could not buy enough room. So a rising
# OUTPUT_TRUNCATION share means the ceiling is too low for the schemas in play,
# not that prompts are too large.
RECOVERY_REASON_INPUT_OVERFLOW = "input_overflow"
RECOVERY_REASON_OUTPUT_TRUNCATION = "output_truncation"
RECOVERY_REASON_UNCLASSIFIED = "unclassified"


def classify_token_limit_reason(error: BaseException) -> str:
    """Which class of token failure *error* is, for metric labeling.

    Lives here so the phrase lists stay encapsulated with the classifier that
    owns them (same reason ``CONTEXT_OVERFLOW_PHRASES`` is shared rather than
    copied). Input overflow is checked FIRST: a message can carry both kinds of
    wording once the engine folds the provider text into its own message, and
    the input-overflow reading is the one the recovery is designed for.
    ``unclassified`` covers a pure engine ``TOKEN_LIMIT`` signal whose provider
    wording did not survive — reportable, not an error.
    """
    msg = str(getattr(error, "message", "") or error).lower()
    if any(p in msg for p in CONTEXT_OVERFLOW_PHRASES):
        return RECOVERY_REASON_INPUT_OVERFLOW
    if any(p in msg for p in _OUTPUT_TRUNCATION_PHRASES):
        return RECOVERY_REASON_OUTPUT_TRUNCATION
    return RECOVERY_REASON_UNCLASSIFIED


class ErrorAction(str, Enum):
    """Actions to take after error handling."""

    RETRY = "retry"
    COMPRESS_MEMORY = "compress_memory"
    ESCALATE = "escalate"
    FAIL = "fail"


@dataclass
class RetryConfig:
    """Configuration for LLM retry behavior."""

    max_retries: int = 3
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 30.0
    exponential_base: float = 2.0

    # LAST-RESORT error message patterns. Reached only for an exception that
    # declares no ``retryable`` flag anywhere on its ``__cause__`` chain and is
    # not a typed engine or timeout signal — i.e. an untyped third-party
    # exception whose wording is genuinely all there is. All 5xx codes are
    # listed explicitly because partial matching ("50") would catch unrelated
    # numbers.
    #
    # Nothing FaultMaven raises should need this list, and a fix that ADDS a
    # phrase here is almost always the wrong one: prose is not a contract, the
    # raising code is free to reword it, and nothing couples the two. #1287 is
    # the worked example — "timed out after 30.0s" against a list containing
    # ``"timeout"``, which is not a substring of ``"timed out"``. The fix was to
    # make the raising site declare ``retryable`` (``ExternalCallTimeout``) and
    # to dispatch ``TimeoutError`` on type. What it must NOT be is a second
    # spelling here: ``"timed out"`` is deliberately absent, and adding it would
    # move the decision back into prose for every future reword.
    #
    # ``"timeout"`` itself STAYS, and deleting it was a regression caught in
    # review. The type rule above only covers exceptions that inherit from the
    # builtin ``TimeoutError``; several clients this process talks to do not.
    # Measured: ``aiohttp.ServerTimeoutError`` does inherit from it, but
    # ``httpx.TimeoutException`` and ``redis.exceptions.TimeoutError`` do not,
    # and they declare no ``retryable`` either — so with the phrase gone,
    # ``redis.exceptions.TimeoutError("Timeout reading from socket")`` and
    # ``httpx.ReadTimeout("Read timeout")`` went from retryable to PERMANENT.
    # A last-resort phrase for third-party wording is exactly what this list is
    # for; the mistake in #1287 was relying on it for FaultMaven's OWN raise
    # sites, not having it at all.
    #
    # Truncation is deliberately NOT extended here either. The engine raises
    # ``OutputTruncationError`` for it, which is dispatched on type before any
    # of this runs; adding JSON-decoder phrasing would only add entries that
    # never decide anything (and, as #513 showed, entries that look like
    # coverage while matching nothing real). The two provider-worded phrases
    # below predate that signal and stay as a floor for a truncation that
    # somehow surfaces outside the structured-output loop.
    retryable_patterns: Tuple[str, ...] = (
        "rate limit",
        "over capacity",
        "500",
        "502",
        "503",
        "504",
        "429",
        "bad gateway",
        "gateway timeout",
        "timeout",
        "connection",
        "temporary",
        "overloaded",
        "truncated",
        "finishreason=max_tokens",
    )


@dataclass
class ErrorResult:
    """Result of error handling."""

    action: ErrorAction
    message: str
    error_code: Optional[str] = None
    retry_count: int = 0
    # The exception that produced this result, preserved so the caller can
    # surface its real message. Losing it turned an informative provider
    # overflow ("prompt is too long: 250000 > 200000") into an opaque engine
    # message. Callers fold this into their raised message text for
    # diagnostics; they do NOT chain it via ``raise ... from`` when a semantic
    # error_code is the authoritative signal, because a typed LLMException on
    # the __cause__ chain would override that code at the HTTP boundary. Set in
    # ``with_retry``.
    original_exception: Optional[BaseException] = None


class LLMErrorHandler:
    """
    Handles LLM API errors with automatic recovery.

    Features:
    - Exponential backoff for transient errors
    - Error classification (retryable vs non-retryable)
    - Fallback prompt support
    - Error tracking for patterns
    """

    def __init__(self, config: Optional[RetryConfig] = None):
        self.config = config or RetryConfig()
        self._error_counts: dict[str, int] = {}

    def is_retryable_error(self, error: Exception) -> bool:
        """Check if error is retryable.

        Four tiers, in strict precedence order:

        1. The engine's own typed truncation signal.
        2. **A DECLARED ``retryable`` flag** on any exception in the ``__cause__``
           chain. The raising code states what it knows; nothing downstream
           re-derives it. ``LLMException`` sets it from ``status_code`` per the
           4xx/5xx contract in ``faultmaven.exceptions``, and
           ``ExternalCallTimeout`` declares it directly.
        3. ``TimeoutError`` by TYPE — a deadline expiring means the call did not
           finish, which no message needs to say. This catches a bare
           ``asyncio.TimeoutError`` from any ``wait_for`` that is not wrapped
           (``str()`` on one is the EMPTY STRING, so the phrase fallback below
           can never see a timeout at all).
        4. Only then, phrase matching — a last resort for untyped third-party
           exceptions whose wording is all there is.

        Tier 2 replaced an ``isinstance(cursor, LLMException)`` test. Keying on
        one concrete class meant every other typed exception — including the
        timeout ``BaseExternalClient`` raises for the LLM router — fell to tier
        4 and was classified by prose. That prose said "timed out"; the phrase
        list said "timeout"; a hung provider got zero retries while a provider's
        own 504 timeout got three (#1287).

        The flag must be a genuine ``bool``. ``getattr`` alone would accept a
        ``Mock``'s auto-attribute (truthy — every mocked error retryable
        forever) or an unset/``None`` marker (falsy — a permanent verdict from a
        class that never made one). Anything that is not ``True``/``False`` is
        no declaration, so the walk continues past it.
        """
        # The engine's typed truncation signal: retryable exactly while raising
        # the generation cap is still an option. Checked before the declared-flag
        # walk, because the provider exception it was built from may be on the
        # chain and would answer for the wrong question.
        if isinstance(error, OutputTruncationError):
            return not error.cap_reached

        declared = self._declares_retryable(error)
        if declared is not None:
            return declared

        # A declaration outranks the type rule, so this runs only when nothing
        # on the chain declared anything.
        if any(isinstance(c, TimeoutError) for c in walk_cause_chain(error)):
            return True

        error_str = str(error).lower()
        return any(pattern in error_str for pattern in self.config.retryable_patterns)

    @staticmethod
    def _declares_retryable(error: BaseException) -> Optional[bool]:
        """The retryability the raising code DECLARED, or ``None`` if nobody did.

        Same rule as tier 2 of :meth:`is_retryable_error`: the first genuine
        ``bool`` on the ``__cause__`` chain. ``None`` means no declaration —
        never "False" — so a caller can tell "said it is permanent" from "said
        nothing", which is exactly the distinction ``handle_error`` needs in
        order to know whether prose is still allowed to decide.
        """
        for cursor in walk_cause_chain(error):
            declared = getattr(cursor, "retryable", None)
            if isinstance(declared, bool):
                return declared
        return None

    @staticmethod
    def _first_error_code(error: BaseException) -> Optional[str]:
        """The first ``error_code`` on the ``__cause__`` chain, if any."""
        for cursor in walk_cause_chain(error):
            code = getattr(cursor, "error_code", None)
            if code:
                return code
        return None

    def is_billing_error(self, error: Exception) -> bool:
        """Detect a permanent billing/quota-exhaustion failure.

        Thin wrapper over the shared ``exceptions.is_billing_error`` so the
        engine handler and other layers classify identically.
        """
        return is_billing_error(error)

    def is_auth_error(self, error: Exception) -> bool:
        """Check if error is authentication-related."""
        error_str = str(error).lower()
        return any(
            pattern in error_str
            for pattern in ("auth", "api key", "unauthorized", "401", "403")
        )

    def is_model_not_found_error(self, error: Exception) -> bool:
        """Check if error is related to model not found (404)."""
        error_str = str(error).lower()
        return any(
            pattern in error_str
            for pattern in (
                "model not found",
                "not_found",
                "404",
                "inaccessible",
                "not deployed",
            )
        )

    def is_token_limit_error(self, error: Exception) -> bool:
        """Check if an error is a context-window overflow.

        Input overflow only: the prompt is too large for the context window, so
        COMPRESS_MEMORY is the direct remedy. Output truncation is a different
        failure with a different first remedy (raise the generation cap) and
        reaches COMPRESS_MEMORY only after that ladder is spent — it travels as
        ``OutputTruncationError`` and is dispatched on type, never matched here.

        A request-shape 400 that merely *names* a token parameter — e.g.
        OpenAI's "Unsupported parameter: 'max_tokens' is not supported with this
        model. Use 'max_completion_tokens' instead." — is a non-retryable config
        error, NOT a context overflow. Matching it here masked the real cause as
        "Context too large" and sent the engine into a futile compression loop,
        so we key on specific overflow signatures and never on the bare word
        "token"/"max_tokens", and bail early on unsupported-parameter errors.
        """
        error_str = str(error).lower()
        # A wrong/unsupported request parameter is a config error, not a limit.
        if any(guard in error_str for guard in _PARAM_ERROR_GUARD_PHRASES):
            return False
        return any(p in error_str for p in CONTEXT_OVERFLOW_PHRASES)

    def calculate_delay(self, retry_count: int) -> float:
        """Calculate delay for next retry using exponential backoff."""
        delay = self.config.base_delay_seconds * (
            self.config.exponential_base**retry_count
        )
        return min(delay, self.config.max_delay_seconds)

    async def handle_error(
        self,
        error: Exception,
        retry_count: int = 0,
        next_attempt_seconds: float = 0.0,
    ) -> ErrorResult:
        """
        Handle LLM API error with appropriate recovery.

        Args:
            error: The exception that occurred
            retry_count: Number of retries attempted
            next_attempt_seconds: What one more attempt is expected to cost, in
                seconds. Only consulted inside a turn with a bound deadline (see
                ``turn_budget``), where it decides whether the backoff and the
                attempt after it still fit. Defaults to 0.0, which makes the
                budget check guard the BACKOFF alone — the honest reading for a
                caller that has no cost estimate to offer.

        Returns:
            ErrorResult with recovery action and message
        """
        # Track error for pattern detection
        error_type = type(error).__name__
        self._error_counts[error_type] = self._error_counts.get(error_type, 0) + 1

        # Output truncation is a typed signal the engine raised about its OWN
        # generation cap, so it is dispatched first — before any of the
        # phrase-matching classifiers below get to read a message that belongs
        # to the provider or to the JSON decoder. (A provider's truncation text
        # names its model; letting `is_model_not_found_error` see it would
        # escalate a recoverable cut as a configuration failure.)
        if isinstance(error, OutputTruncationError):
            if error.cap_reached:
                # Nothing left to raise. Hand it to the same COMPRESS_MEMORY
                # recovery an input overflow uses: shrinking the prompt is now
                # the only way to make room for the answer (#662).
                return ErrorResult(
                    action=ErrorAction.COMPRESS_MEMORY,
                    message=(
                        "Response truncated at the maximum generation cap. "
                        "Reducing the prompt to make room for the answer..."
                    ),
                    error_code=TOKEN_LIMIT,
                )
            # The cap was raised; the identical call now has room to finish.
            return await self._retry_or_exhaust(retry_count, next_attempt_seconds)

        # Permanent billing / quota exhaustion (non-retryable). The provider
        # account is out of credits or quota; retrying or waiting cannot help —
        # an operator must add credits / enable billing. Surface a clear,
        # operator-actionable message and a stable error_code so the API and UI
        # can tell the user to top up rather than uselessly retry. Checked first
        # because it must not be mistaken for a transient 429/5xx.
        if self.is_billing_error(error):
            logger.error(f"LLM provider billing/quota exhausted: {error}")
            return ErrorResult(
                action=ErrorAction.ESCALATE,
                message=(
                    "FaultMaven's AI provider is out of quota or credits, so the "
                    "investigation can't continue right now. An administrator "
                    "needs to add credits or update the provider's billing plan. "
                    "Once that's done, resend your message to continue."
                ),
                error_code=QUOTA_EXHAUSTED,
            )

        # The LLM circuit breaker is open: this request never reached a
        # provider, so there is no provider status and no provider wording to
        # classify from. Dispatched on TYPE, before the prose-matching
        # classifiers below — its message ("Circuit breaker is open for
        # LLM_Providers") matches no retry phrase, so it used to fall all the
        # way through to the UNKNOWN_ERROR tail and report the one failure the
        # system understands completely as unclassified (#1287).
        #
        # Placed AFTER the billing check on purpose: the breaker latches the
        # error_code of the failure that opened it, and a quota-latched breaker
        # must keep escalating as QUOTA_EXHAUSTED (the case_b639fac38fe0 chain).
        # A latched credential rejection is the same shape — permanent until an
        # operator rotates the key — so it is routed to the same terminal
        # AUTH_FAILED an unwrapped 401 gets, rather than to a transient code
        # that invites the user to resend.
        #
        # NOT retryable: the breaker's recovery window (30s) outlasts the whole
        # backoff ladder (2+4+8 = 14s), so every retry would be spent on a
        # breaker guaranteed to still be open.
        #
        # Tested on ``error`` itself rather than walked down ``__cause__``,
        # because on this path it arrives unwrapped and that is a property of
        # the path, not an accident: the router re-raises it bare (``except
        # Exception as e: ... raise``) and ``milestone_engine.llm_operation``
        # re-raises it bare too (its only wrap is the truncation branch, which
        # this message cannot enter). If a future wrapper is introduced between
        # them, this must become a chain walk — a wrapped breaker error would
        # otherwise silently fall back to the UNKNOWN_ERROR tail this branch
        # exists to prevent.
        # Scoped to the LLM breaker by ``service``. ``CircuitBreakerError`` is
        # raised by EVERY ``BaseExternalClient`` — ChromaDB, Redis, Presidio and
        # the runbook KB included — so an unscoped ``isinstance`` would answer a
        # ChromaDB outage with "The AI provider failed repeatedly", which is
        # both wrong and unactionable. A breaker that carries no service name
        # (an older raiser, or a hand-built one in a test) still reaches the
        # generic branch below rather than being silently dropped.
        if isinstance(error, CircuitBreakerError) and getattr(
            error, "service", None
        ) in (None, LLM_BREAKER_SERVICE):
            latched = getattr(error, "error_code", None)
            if latched == PROVIDER_AUTH_FAILED:
                logger.error(
                    f"LLM circuit breaker open on rejected credential: {error}"
                )
                return ErrorResult(
                    action=ErrorAction.ESCALATE,
                    message="System configuration error. Please contact support.",
                    error_code="AUTH_FAILED",
                    retry_count=retry_count,
                )
            logger.error(f"LLM circuit breaker open: {error}")
            return ErrorResult(
                action=ErrorAction.FAIL,
                message=(
                    "The AI provider failed repeatedly, so calls to it are "
                    "paused briefly to let it recover. Please try again in a "
                    "minute."
                ),
                error_code=PROVIDER_CIRCUIT_OPEN,
                retry_count=retry_count,
            )

        # Context-window overflow, ahead of the declaration gate below.
        #
        # COMPRESS_MEMORY is not a permanence claim, it is a DIFFERENT RECOVERY:
        # the prompt did not fit, so shrinking it is the only thing that helps,
        # and an identical retry cannot. That holds even when the raising code
        # declared the failure retryable — a gateway can answer 5xx with
        # "context length exceeded" in the body — so the gate must not let a
        # declaration divert an overflow onto the ladder, where it would spend
        # every attempt re-sending the same oversized prompt.
        #
        # Safe to put ahead of the gate, unlike the classifiers below it: this
        # one keys on multi-word overflow SIGNATURES ("context length",
        # "prompt is too long", …), never on a bare number, so no ``host:port``
        # can reach it. That difference is the whole reason the ordering splits
        # here rather than moving every classifier to one side.
        if self.is_token_limit_error(error):
            return ErrorResult(
                action=ErrorAction.COMPRESS_MEMORY,
                message="Context too large. Compressing conversation history...",
                error_code=TOKEN_LIMIT,
            )

        # A DECLARED transient failure goes straight to the ladder, without
        # passing under the prose classifiers below (#1287 follow-up).
        #
        # This is where "a declaration outranks prose" actually has to hold.
        # Putting that rule only inside ``is_retryable_error`` was not enough:
        # ``handle_error`` reaches THREE substring classifiers first, and each
        # matches bare numbers anywhere in the message — ``is_auth_error`` on
        # "401"/"403", ``is_model_not_found_error`` on "404". Typing the
        # transport raise sites made every provider fold aiohttp's raw text
        # into the message, and that text carries ``host:port``. Measured
        # before this gate existed:
        #
        #   "…Cannot connect to host localhost:4040…"  -> ESCALATE MODEL_NOT_FOUND
        #   "…Cannot connect to host 10.0.0.5:8404…"   -> ESCALATE MODEL_NOT_FOUND
        #   "…Cannot connect to host proxy:8401…"      -> ESCALATE AUTH_FAILED
        #
        # all with ``retryable=True`` declared and discarded. Any local model
        # server or proxy on a port containing 404/401/403 became permanently
        # unavailable on its first connect failure. Sanitising the message
        # would only trade one prose dependency for another; the ordering is
        # the fix.
        #
        # Only ``True`` short-circuits. A declared NON-retryable error still
        # falls through, because the classifiers below carry SEMANTICS the flag
        # does not — a 404 is MODEL_NOT_FOUND, a 401 is AUTH_FAILED, an
        # overflow is TOKEN_LIMIT — and all three are already non-retryable.
        #
        # Billing stays ahead of this: quota exhaustion is permanent whatever
        # the transport said, and ``is_billing_error`` walks the same chain.
        if self._declares_retryable(error) is True:
            return await self._retry_or_exhaust(retry_count, next_attempt_seconds)

        # A configuration failure the LLM layer diagnosed for itself: no
        # provider is configured, or the registry cannot build one. Permanent
        # until an operator edits the environment, and the message already says
        # what to set — but it is not an ``LLMException``, declares no
        # ``retryable``, and matches no phrase, so it used to reach the
        # UNKNOWN_ERROR tail and surface as "LLM error (LLMProviderError): …"
        # with a Retry-After. Read the ``error_code`` its raiser already sets
        # rather than adding a fourth substring test.
        if self._first_error_code(error) == LLM_CONFIG_ERROR:
            logger.error(f"LLM configuration error: {error}")
            return ErrorResult(
                action=ErrorAction.ESCALATE,
                message=f"FaultMaven's AI provider is not configured: {error}",
                error_code=LLM_CONFIG_ERROR,
                retry_count=retry_count,
            )

        # Check for auth errors (non-retryable)
        if self.is_auth_error(error):
            logger.error(f"Authentication error: {error}")
            return ErrorResult(
                action=ErrorAction.ESCALATE,
                message="System configuration error. Please contact support.",
                error_code="AUTH_FAILED",
            )

        # Check for model not found errors (non-retryable configuration issue)
        if self.is_model_not_found_error(error):
            logger.error(f"Model not found error: {error}")
            # Extract more details from the error for better diagnostics
            error_details = str(error)[:300]  # First 300 chars for context
            return ErrorResult(
                action=ErrorAction.ESCALATE,
                message=f"503 LLM service unavailable: Model not found or inaccessible. Please check LLM provider configuration. Details: {error_details}",
                error_code="MODEL_NOT_FOUND",
            )

        # Check for retryable errors
        if self.is_retryable_error(error):
            return await self._retry_or_exhaust(retry_count, next_attempt_seconds)

        # Unknown error — fail fast and surface details for diagnostics.
        logger.error(
            f"Unknown LLM error (retry {retry_count}): {type(error).__name__}: {str(error)}",
            exc_info=True,
        )

        error_preview = str(error)[:200]
        error_type = type(error).__name__

        return ErrorResult(
            action=ErrorAction.FAIL,
            message=f"LLM error ({error_type}): {error_preview}",
            error_code="UNKNOWN_ERROR",
            retry_count=retry_count,
        )

    async def _retry_or_exhaust(
        self, retry_count: int, next_attempt_seconds: float = 0.0
    ) -> ErrorResult:
        """Back off and retry, or report the attempts spent.

        Shared by the string-classified retryable branch and the typed
        output-truncation signal, so a truncation retry honours the same
        ceiling and the same backoff as every other retry rather than getting
        its own budget.

        Two ceilings apply, and BOTH are checked before the backoff is slept:
        the configured attempt count, and — inside a turn that bound a deadline
        — what is left of the turn budget. The second one has to be checked
        *here*, before ``asyncio.sleep``, because the backoff is itself part of
        what the ladder spends: deciding after the sleep would already have
        burnt 2, 4 or 8 seconds of a budget that could not afford them.
        """
        if retry_count >= self.config.max_retries:
            return ErrorResult(
                action=ErrorAction.FAIL,
                message="LLM service temporarily unavailable. Please try again in a few minutes.",
                error_code="RETRY_EXHAUSTED",
                retry_count=retry_count,
            )

        delay = self.calculate_delay(retry_count)

        # A bounded operation must not begin a step it cannot finish inside its
        # own bound. Being cancelled mid-attempt by the turn-wide wait_for
        # discards the classification this handler exists to produce, and the
        # caller gets an opaque 504 instead of the honest 503 + Retry-After
        # (#1278, #1292).
        spendable = spendable_turn_budget()
        if not can_afford_next_attempt(spendable, delay, next_attempt_seconds):
            logger.warning(
                "Turn budget cannot afford retry %d/%d: %.1fs spendable, "
                "%.1fs backoff + %.1fs estimated attempt. Reporting the "
                "provider failure now rather than being cancelled mid-attempt.",
                retry_count + 1,
                self.config.max_retries,
                spendable if spendable is not None else float("inf"),
                delay,
                next_attempt_seconds,
            )
            return ErrorResult(
                action=ErrorAction.FAIL,
                message=(
                    "LLM service temporarily unavailable and the request budget "
                    "is spent. Please try again in a few minutes."
                ),
                error_code=TURN_BUDGET_EXHAUSTED,
                retry_count=retry_count,
            )

        logger.info(
            f"Retryable error, waiting {delay:.1f}s before retry {retry_count + 1}/{self.config.max_retries}"
        )
        await asyncio.sleep(delay)

        return ErrorResult(
            action=ErrorAction.RETRY,
            message=f"Transient error. Retrying ({retry_count + 1}/{self.config.max_retries})...",
            retry_count=retry_count + 1,
        )

    async def with_retry(
        self,
        operation: Callable[[], Awaitable[T]],
    ) -> Tuple[Optional[T], Optional[ErrorResult]]:
        """
        Execute operation with automatic retry on transient errors.

        Inside a turn that bound a deadline (``turn_budget.bind_turn_deadline``,
        applied by the route that also applies the turn-wide ``asyncio.wait_for``)
        the ladder is budgeted against it two ways:

        * **Every attempt is CLAMPED** to what is left of the turn. This is what
          makes the invariant unconditional — the ladder physically cannot run
          past the deadline and be cancelled mid-attempt, whatever the provider
          timeout is configured to and however wrong the estimate below is.
        * **A retry is REFUSED** when the backoff plus another attempt of the
          worst cost observed so far would not fit. This is the efficiency half:
          without it the ladder would keep starting ever-shorter clamped attempts
          and still spend the entire turn budget before answering, which is the
          two-minute wait #1278 reported.

        The worst OBSERVED attempt cost is used as the estimate rather than the
        configured LLM request timeout, because the handler would otherwise have
        to re-derive the router's per-provider timeout resolution and could drift
        from it. It is also self-calibrating in the right direction: a provider
        failing fast (a 503 in 200ms) keeps every retry affordable, while a
        provider that hangs is estimated at the full hang and stops the ladder.

        Outside a bound turn every one of these checks is inert — the budget
        reads ``None``, nothing is clamped and nothing is refused — which is the
        behaviour direct-call tests, background jobs and the CLI already had.

        Args:
            operation: Async operation to execute

        Returns:
            Tuple of (result, error_result) where result is None if all attempts failed
        """
        retry_count = 0
        last_error_result: Optional[ErrorResult] = None
        # Worst attempt cost seen in THIS ladder. Local, not instance state: one
        # handler serves every turn in the process, so an instance attribute
        # would leak one case's hung provider into another case's budgeting.
        worst_attempt_seconds = 0.0

        while retry_count <= self.config.max_retries:
            spendable = spendable_turn_budget()
            if spendable is not None and spendable <= 0:
                # No room even to start. Reached when an earlier ladder in the
                # same turn has already spent the budget — #1278's "each
                # subsequent call in the turn starts its own retry ladder".
                logger.warning(
                    "Turn budget spent (%.1fs); not starting an LLM attempt.",
                    spendable,
                )
                exhausted = ErrorResult(
                    action=ErrorAction.FAIL,
                    message=(
                        "The request budget is spent. Please try again in a "
                        "few minutes."
                    ),
                    error_code=TURN_BUDGET_EXHAUSTED,
                    retry_count=retry_count,
                )
                # A FRESH result, never ``last_error_result``. Reaching here
                # after an attempt means the last result was a RETRY, and a
                # RETRY carries no ``error_code`` — returning it would hand the
                # HTTP boundary a ``None`` code, which is not in
                # ``_RETRYABLE_ENGINE_CODES`` and falls through to a bare 500.
                # The provider's own wording is kept for diagnostics, which is
                # the only part of the earlier result worth carrying.
                if last_error_result is not None:
                    exhausted.original_exception = last_error_result.original_exception
                return None, exhausted

            started = time.monotonic()
            try:
                if spendable is None:
                    result = await operation()
                else:
                    result = await asyncio.wait_for(operation(), timeout=spendable)
                return result, None
            except Exception as e:
                worst_attempt_seconds = max(
                    worst_attempt_seconds, time.monotonic() - started
                )
                error_result = await self.handle_error(
                    e, retry_count, worst_attempt_seconds
                )
                # Preserve the triggering exception so the caller can surface its
                # real message (see ErrorResult.original_exception — callers fold
                # it into their message text, deliberately NOT onto __cause__).
                error_result.original_exception = e
                last_error_result = error_result

                if error_result.action == ErrorAction.RETRY:
                    retry_count = error_result.retry_count
                    continue

                else:
                    # Non-retryable error
                    return None, error_result

        return None, last_error_result

    def get_error_summary(self) -> dict:
        """Get summary of errors encountered."""
        return {
            "error_counts": dict(self._error_counts),
            "total_errors": sum(self._error_counts.values()),
        }
