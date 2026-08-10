from unittest.mock import MagicMock, patch

import pytest

from faultmaven.infrastructure.security.redaction import DataSanitizer


def test_sanitizer_handles_k8s_service_unavailable():
    """
    Test that if the K8s Presidio services are unavailable, the sanitizer
    falls back to regex-only sanitization and returns processed text.
    """
    sanitizer = DataSanitizer()

    # When K8s services are unavailable (which they are in test environment),
    # the sanitizer should still work with regex patterns
    original_text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE and other text"
    sanitized_text = sanitizer.sanitize(original_text)

    # Should redact the AWS key even without Presidio service
    assert "AKIAIOSFODNN7EXAMPLE" not in sanitized_text
    assert "<AWS_ACCESS_KEY_" in sanitized_text


def _fail_open_settings():
    """Settings with an explicit fail-OPEN posture (graceful fallback on a
    runtime Presidio error). Under the fail-CLOSED default (#654) a runtime
    timeout raises instead — that path is covered in
    test_redaction.py::TestFailClosedPosture."""
    settings = MagicMock()
    settings.protection.entities_to_protect = ["EMAIL_ADDRESS"]
    settings.protection.min_score_threshold = 0.9
    settings.protection.presidio_analyzer_url = "http://fake:8080"
    settings.protection.presidio_anonymizer_url = "http://fake:8081"
    settings.protection.sanitize_pii = True
    settings.protection.fail_open = True
    # Real str, not the MagicMock attribute: the pseudonym key is fed to hmac,
    # which rejects anything not bytes-like.
    settings.protection.pseudonym_key = "test-pseudonym-key"
    settings.server.skip_service_checks = True
    return settings


def test_sanitizer_handles_network_timeout():
    """
    Test that under a fail-OPEN posture, if the K8s Presidio service times out,
    the sanitizer gracefully falls back and returns original text (since regex
    doesn't apply to this pattern).
    """
    with patch("requests.Session.post") as mock_post:
        # Simulate network timeout
        import requests

        mock_post.side_effect = requests.exceptions.Timeout("Service timeout")

        sanitizer = DataSanitizer(settings=_fail_open_settings())

        # Force services to appear available for this test
        sanitizer.analyzer_available = True
        sanitizer.anonymizer_available = True

        original_text = "JWT: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 and other data"
        sanitized_text = sanitizer.sanitize(original_text)

        # When service times out, it should return original text
        # (regex patterns don't match this specific JWT format)
        assert sanitized_text == original_text

        # Test with a pattern that WOULD be caught by regex even on timeout
        aws_text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE and other data"
        sanitized_aws = sanitizer.sanitize(aws_text)

        # This should be redacted by regex patterns even when Presidio times out
        assert "AKIAIOSFODNN7EXAMPLE" not in sanitized_aws
        assert "<AWS_ACCESS_KEY_" in sanitized_aws
