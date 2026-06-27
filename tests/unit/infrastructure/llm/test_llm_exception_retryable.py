"""Unit tests for LLMException.retryable derivation.

The provider layer raises ``LLMException`` with a ``status_code`` and relies on
the exception to classify retryability. This is load-bearing: the resilient
client and the registry fallback chain both branch on ``.retryable``. The
2026-06 provider-uniformity audit found that 429 (rate limited) was treated as
non-retryable everywhere except Groq/Anthropic, and that Groq/Anthropic's
explicit ``retryable=status==429`` override silently forced 5xx to
non-retryable. These tests lock the corrected derivation in place.
"""

import pytest

from faultmaven.exceptions import (
    QUOTA_EXHAUSTED,
    LLMException,
    is_billing_quota_error,
)


class TestRetryableDerivation:
    """status_code → retryable classification (no explicit override)."""

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
    def test_5xx_is_retryable(self, status):
        assert LLMException("server error", status_code=status).retryable is True

    def test_429_is_retryable(self):
        """Rate limiting is transient — the one 4xx that should be retried."""
        assert LLMException("rate limited", status_code=429).retryable is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_other_4xx_is_non_retryable(self, status):
        assert LLMException("client error", status_code=status).retryable is False

    def test_no_status_code_defaults_non_retryable(self):
        """Callers must opt in to retry when there is no status code."""
        assert LLMException("opaque failure").retryable is False


class TestExplicitOverride:
    """An explicit retryable= wins over derivation (used for timeouts /
    connection errors that carry no status_code)."""

    def test_explicit_true_without_status(self):
        assert LLMException("conn reset", retryable=True).retryable is True

    def test_explicit_false_beats_5xx(self):
        # Possible but discouraged — documents that explicit wins.
        assert (
            LLMException("503 but give up", status_code=503, retryable=False).retryable
            is False
        )


class TestBillingQuotaClassification:
    """Permanent billing/quota exhaustion is auto-detected from the provider
    body and is ALWAYS non-retryable with a stable QUOTA_EXHAUSTED error_code.
    Regression for case_b639fac38fe0, where a billing 429 was treated as a
    transient rate-limit, retried, tripped the circuit breaker, and surfaced
    as an opaque 500 with 'please try again'."""

    @pytest.mark.parametrize(
        "body",
        [
            "OpenAI API error 429: insufficient_quota",
            "You exceeded your current quota, please check your plan and billing details",
            "Gemini: billing account not configured",
            "Error: out of credits",
            "insufficient_funds for this request",
        ],
    )
    def test_billing_body_is_quota_exhausted_and_non_retryable(self, body):
        e = LLMException(body, status_code=429)
        assert e.error_code == QUOTA_EXHAUSTED
        assert e.retryable is False

    def test_402_is_always_billing(self):
        e = LLMException("Payment required", status_code=402)
        assert e.error_code == QUOTA_EXHAUSTED
        assert e.retryable is False

    def test_plain_rate_limit_429_is_not_billing(self):
        """A transient 429 with no billing markers stays retryable — we must
        not mislabel ordinary rate-limiting as a permanent billing failure."""
        e = LLMException(
            "Groq API error 429: rate limit reached, retry soon", status_code=429
        )
        assert e.error_code is None
        assert e.retryable is True

    def test_plain_5xx_is_not_billing(self):
        e = LLMException("Internal server error", status_code=503)
        assert e.error_code is None
        assert e.retryable is True

    def test_billing_beats_explicit_retryable_true(self):
        """Even if a caller naively passes retryable=True, a detected billing
        condition forces non-retryable — waiting cannot add credits."""
        e = LLMException("insufficient_quota", status_code=429, retryable=True)
        assert e.error_code == QUOTA_EXHAUSTED
        assert e.retryable is False

    def test_is_billing_quota_error_helper(self):
        assert is_billing_quota_error("insufficient_quota") is True
        assert is_billing_quota_error("anything", status_code=402) is True
        assert is_billing_quota_error("rate limit reached", status_code=429) is False
        assert is_billing_quota_error("") is False
