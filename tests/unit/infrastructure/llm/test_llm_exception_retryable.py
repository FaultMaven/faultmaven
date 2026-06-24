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

from faultmaven.exceptions import LLMException


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
