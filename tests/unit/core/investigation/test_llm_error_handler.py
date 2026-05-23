"""Tests for LLMErrorHandler

Tests retry logic and error recovery for LLM API calls.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from faultmaven.core.investigation.llm_error_handler import (
    ErrorAction,
    ErrorResult,
    LLMErrorHandler,
    RetryConfig,
)


@pytest.fixture
def handler():
    return LLMErrorHandler()


@pytest.fixture
def fast_handler():
    """Handler with fast delays for testing."""
    config = RetryConfig(
        max_retries=3,
        base_delay_seconds=0.01,  # Fast for testing
        max_delay_seconds=0.1,
    )
    return LLMErrorHandler(config)


class TestErrorClassification:
    """Test error type classification."""

    def test_retryable_rate_limit_error(self, handler):
        """Rate limit errors should be retryable."""
        error = Exception("Rate limit exceeded, please slow down")
        assert handler.is_retryable_error(error) is True

    def test_retryable_timeout_error(self, handler):
        """Timeout errors should be retryable."""
        error = Exception("Connection timeout after 30s")
        assert handler.is_retryable_error(error) is True

    def test_retryable_503_error(self, handler):
        """503 errors should be retryable."""
        error = Exception("Service returned 503 status code")
        assert handler.is_retryable_error(error) is True

    def test_non_retryable_error(self, handler):
        """Regular errors should not be retryable."""
        error = Exception("Invalid JSON format in response")
        assert handler.is_retryable_error(error) is False

    def test_retryable_502_bad_gateway(self, handler):
        """502 Bad Gateway (string-pattern path) should be retryable."""
        error = Exception("Upstream returned 502 Bad Gateway")
        assert handler.is_retryable_error(error) is True

    def test_llm_exception_5xx_is_retryable_authoritative(self, handler):
        """LLMException with 5xx status must be retryable via typed metadata,
        not the string-pattern fallback."""
        from faultmaven.exceptions import LLMException

        error = LLMException("opaque body", status_code=502)
        assert handler.is_retryable_error(error) is True

    def test_llm_exception_4xx_is_not_retryable_authoritative(self, handler):
        """LLMException with 4xx status must be non-retryable even if its
        message happens to contain a retryable-looking word."""
        from faultmaven.exceptions import LLMException

        # Message includes "timeout" — string-pattern would say retryable.
        # The typed flag must win.
        error = LLMException("400 bad request: timeout field invalid", status_code=400)
        assert handler.is_retryable_error(error) is False

    def test_retryable_walks_cause_chain(self, handler):
        """When a generic wrapper exception has a typed LLMException cause,
        retryability is taken from the cause."""
        from faultmaven.exceptions import LLMException

        try:
            try:
                raise LLMException("upstream 502", status_code=502)
            except LLMException as inner:
                raise RuntimeError("outer wrapper") from inner
        except RuntimeError as outer:
            assert handler.is_retryable_error(outer) is True

    def test_auth_error_detection(self, handler):
        """Auth errors should be detected."""
        error = Exception("API key is invalid or expired")
        assert handler.is_auth_error(error) is True

    def test_auth_401_error(self, handler):
        """401 errors should be auth errors."""
        error = Exception("HTTP 401 Unauthorized")
        assert handler.is_auth_error(error) is True

    def test_token_limit_error(self, handler):
        """Token limit errors should be detected."""
        error = Exception("Request exceeds maximum context length")
        assert handler.is_token_limit_error(error) is True


class TestDelayCalculation:
    """Test exponential backoff delay calculation."""

    def test_first_retry_delay(self, handler):
        """First retry should use base delay."""
        delay = handler.calculate_delay(0)
        assert delay == 2.0  # Base delay

    def test_exponential_backoff(self, handler):
        """Delay should increase exponentially."""
        delay_0 = handler.calculate_delay(0)
        delay_1 = handler.calculate_delay(1)
        delay_2 = handler.calculate_delay(2)

        assert delay_1 == delay_0 * 2
        assert delay_2 == delay_0 * 4

    def test_max_delay_cap(self, handler):
        """Delay should be capped at max_delay_seconds."""
        delay = handler.calculate_delay(10)  # Would be 2048s without cap
        assert delay == 30.0  # Max delay


class TestErrorHandling:
    """Test error handling actions."""

    @pytest.mark.asyncio
    async def test_auth_error_escalates(self, handler):
        """Auth errors should escalate immediately."""
        error = Exception("API key invalid")
        result = await handler.handle_error(error)

        assert result.action == ErrorAction.ESCALATE
        assert result.error_code == "AUTH_FAILED"

    @pytest.mark.asyncio
    async def test_token_limit_triggers_compress(self, handler):
        """Token limit errors should trigger memory compression."""
        error = Exception("Context length exceeded")
        result = await handler.handle_error(error)

        assert result.action == ErrorAction.COMPRESS_MEMORY
        assert result.error_code == "TOKEN_LIMIT"

    @pytest.mark.asyncio
    async def test_retryable_error_retries(self, fast_handler):
        """Retryable errors should trigger retry."""
        error = Exception("Rate limit exceeded")
        result = await fast_handler.handle_error(error, retry_count=0)

        assert result.action == ErrorAction.RETRY
        assert result.retry_count == 1

    @pytest.mark.asyncio
    async def test_retry_exhausted_fails(self, handler):
        """Exhausted retries should fail."""
        error = Exception("Rate limit exceeded")
        result = await handler.handle_error(error, retry_count=3)

        assert result.action == ErrorAction.FAIL
        assert result.error_code == "RETRY_EXHAUSTED"


class TestWithRetry:
    """Test with_retry execution wrapper."""

    @pytest.mark.asyncio
    async def test_success_on_first_try(self, handler):
        """Successful operation should return immediately."""
        operation = AsyncMock(return_value="success")

        result, error = await handler.with_retry(operation)

        assert result == "success"
        assert error is None
        operation.assert_called_once()

    @pytest.mark.asyncio
    async def test_retry_on_transient_error(self, fast_handler):
        """Transient errors should trigger retries."""
        call_count = 0

        async def failing_then_success():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("Rate limit exceeded")
            return "success"

        result, error = await fast_handler.with_retry(failing_then_success)

        assert result == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_unknown_error_fails_fast(self, fast_handler):
        """Unknown (non-retryable) errors should fail immediately, not retry."""
        main_op = AsyncMock(side_effect=Exception("Unknown error"))

        result, error = await fast_handler.with_retry(operation=main_op)

        assert result is None
        assert error is not None
        assert error.action == ErrorAction.FAIL
        assert error.error_code == "UNKNOWN_ERROR"
        # Must not retry — unknown errors are surfaced immediately.
        main_op.assert_called_once()

    @pytest.mark.asyncio
    async def test_max_retries_exhausted(self, fast_handler):
        """All retries exhausted should return None."""
        operation = AsyncMock(side_effect=Exception("Rate limit exceeded"))

        result, error = await fast_handler.with_retry(operation)

        assert result is None
        assert error is not None
        assert error.action == ErrorAction.FAIL


class TestErrorTracking:
    """Test error counting and summary."""

    @pytest.mark.asyncio
    async def test_error_counting(self, handler):
        """Errors should be counted by type."""
        error1 = ValueError("value error")
        error2 = ValueError("another value error")
        error3 = TypeError("type error")

        await handler.handle_error(error1)
        await handler.handle_error(error2)
        await handler.handle_error(error3)

        summary = handler.get_error_summary()

        assert summary["error_counts"]["ValueError"] == 2
        assert summary["error_counts"]["TypeError"] == 1
        assert summary["total_errors"] == 3
