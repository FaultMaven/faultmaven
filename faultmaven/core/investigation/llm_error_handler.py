"""LLM Error Handler with Retry and Recovery

Handles LLM API errors with automatic retry, exponential backoff,
and recovery strategies.

Design Reference:
- docs/architecture/investigation-engine/error-handling-and-recovery.md Section 2

Usage:
    handler = LLMErrorHandler()
    result = await handler.with_retry(
        llm_call_coroutine,
        case=case,
        on_failure=fallback_action
    )
"""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ErrorAction(str, Enum):
    """Actions to take after error handling."""

    RETRY = "retry"
    USE_FALLBACK_PROMPT = "use_fallback_prompt"
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

    # Error message patterns that indicate retryable errors
    retryable_patterns: Tuple[str, ...] = (
        "rate limit",
        "over capacity",
        "503",
        "429",
        "timeout",
        "connection",
        "temporary",
        "overloaded",
    )


@dataclass
class ErrorResult:
    """Result of error handling."""

    action: ErrorAction
    message: str
    error_code: Optional[str] = None
    retry_count: int = 0
    should_use_fallback: bool = False


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
        """Check if error is retryable based on error message patterns."""
        error_str = str(error).lower()
        return any(pattern in error_str for pattern in self.config.retryable_patterns)

    def is_auth_error(self, error: Exception) -> bool:
        """Check if error is authentication-related."""
        error_str = str(error).lower()
        return any(
            pattern in error_str
            for pattern in ("auth", "api key", "unauthorized", "401", "403")
        )

    def is_token_limit_error(self, error: Exception) -> bool:
        """Check if error is related to token limits."""
        error_str = str(error).lower()
        return any(
            pattern in error_str
            for pattern in ("token", "context length", "too long", "max_tokens")
        )

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

        # Check for auth errors (non-retryable)
        if self.is_auth_error(error):
            logger.error(f"Authentication error: {error}")
            return ErrorResult(
                action=ErrorAction.ESCALATE,
                message="System configuration error. Please contact support.",
                error_code="AUTH_FAILED",
            )

        # Check for token limit errors
        if self.is_token_limit_error(error):
            return ErrorResult(
                action=ErrorAction.COMPRESS_MEMORY,
                message="Context too large. Compressing conversation history...",
                error_code="TOKEN_LIMIT",
                should_use_fallback=True,
            )

        # Check for retryable errors
        if self.is_retryable_error(error):
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

        # Unknown error - try fallback first, then fail
        # Log full error details for debugging
        logger.error(
            f"Unknown LLM error (retry {retry_count}): {type(error).__name__}: {str(error)}",
            exc_info=True,
        )

        if retry_count == 0:
            return ErrorResult(
                action=ErrorAction.USE_FALLBACK_PROMPT,
                message="Error occurred. Trying simplified prompt...",
                error_code="UNKNOWN_ERROR",
                should_use_fallback=True,
                retry_count=1,
            )

        return ErrorResult(
            action=ErrorAction.FAIL,
            message=f"LLM error: {type(error).__name__}: {str(error)[:200]}",
            error_code="UNKNOWN_ERROR",
            retry_count=retry_count,
        )

    async def with_retry(
        self,
        operation: Callable[[], Awaitable[T]],
        on_fallback: Optional[Callable[[], Awaitable[T]]] = None,
    ) -> Tuple[Optional[T], Optional[ErrorResult]]:
        """
        Execute operation with automatic retry and fallback.

        Args:
            operation: Async operation to execute
            on_fallback: Optional fallback operation if main fails

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
                last_error_result = error_result

                if error_result.action == ErrorAction.RETRY:
                    retry_count = error_result.retry_count
                    continue

                elif (
                    error_result.action == ErrorAction.USE_FALLBACK_PROMPT
                    and on_fallback
                ):
                    logger.info("Attempting fallback operation...")
                    try:
                        result = await on_fallback()
                        return result, error_result
                    except Exception as fallback_error:
                        logger.warning(f"Fallback also failed: {fallback_error}")
                        retry_count += 1
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


# Default singleton instance
_default_handler: Optional[LLMErrorHandler] = None


def get_llm_error_handler() -> LLMErrorHandler:
    """Get default LLMErrorHandler instance."""
    global _default_handler
    if _default_handler is None:
        _default_handler = LLMErrorHandler()
    return _default_handler
