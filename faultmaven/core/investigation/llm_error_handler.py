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
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Tuple, TypeVar

from faultmaven.exceptions import (
    QUOTA_EXHAUSTED,
    TOKEN_LIMIT,
    LLMException,
    is_billing_error,
)

logger = logging.getLogger(__name__)

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

    # Error message patterns that indicate retryable errors. Used as a fallback
    # when the exception is not an LLMException (which carries authoritative
    # retryable/status_code metadata) and not a typed engine signal. All 5xx
    # codes are listed explicitly because partial matching ("50") would catch
    # unrelated numbers.
    #
    # Truncation is deliberately NOT extended here. The engine raises
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

        Prefers the authoritative `LLMException.retryable` flag (set from
        `status_code` per the 4xx/5xx contract in `faultmaven.exceptions`),
        walking `__cause__` for cases where a typed exception is the cause
        of a generic wrapper. Falls back to string-pattern matching for
        non-LLM errors.
        """
        # The engine's typed truncation signal: retryable exactly while raising
        # the generation cap is still an option. Checked before the LLMException
        # walk, because the provider exception it was built from may be on the
        # chain and would answer for the wrong question.
        if isinstance(error, OutputTruncationError):
            return not error.cap_reached

        cursor: Optional[BaseException] = error
        while cursor is not None:
            if isinstance(cursor, LLMException):
                return cursor.retryable
            cursor = cursor.__cause__

        error_str = str(error).lower()
        return any(pattern in error_str for pattern in self.config.retryable_patterns)

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

    async def handle_error(self, error: Exception, retry_count: int = 0) -> ErrorResult:
        """
        Handle LLM API error with appropriate recovery.

        Args:
            error: The exception that occurred
            retry_count: Number of retries attempted

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
            return await self._retry_or_exhaust(retry_count)

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

        # Check for token limit errors
        if self.is_token_limit_error(error):
            return ErrorResult(
                action=ErrorAction.COMPRESS_MEMORY,
                message="Context too large. Compressing conversation history...",
                error_code=TOKEN_LIMIT,
            )

        # Check for retryable errors
        if self.is_retryable_error(error):
            return await self._retry_or_exhaust(retry_count)

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

    async def _retry_or_exhaust(self, retry_count: int) -> ErrorResult:
        """Back off and retry, or report the attempts spent.

        Shared by the string-classified retryable branch and the typed
        output-truncation signal, so a truncation retry honours the same
        ceiling and the same backoff as every other retry rather than getting
        its own budget.
        """
        if retry_count >= self.config.max_retries:
            return ErrorResult(
                action=ErrorAction.FAIL,
                message="LLM service temporarily unavailable. Please try again in a few minutes.",
                error_code="RETRY_EXHAUSTED",
                retry_count=retry_count,
            )

        delay = self.calculate_delay(retry_count)
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

        Args:
            operation: Async operation to execute

        Returns:
            Tuple of (result, error_result) where result is None if all attempts failed
        """
        retry_count = 0
        last_error_result: Optional[ErrorResult] = None

        while retry_count <= self.config.max_retries:
            try:
                result = await operation()
                return result, None
            except Exception as e:
                error_result = await self.handle_error(e, retry_count)
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
