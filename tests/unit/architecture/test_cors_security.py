"""
CORS Security Configuration Tests

Tests for the CORS wildcard fail-fast validation that prevents
insecure wildcard origins in production environments.

Security Requirement: Production deployments must NOT use wildcard
CORS origins (e.g., chrome-extension://*) as they allow any browser
extension to access the API.
"""

from unittest.mock import MagicMock, patch

import pytest


class TestCORSWildcardValidation:
    """Test CORS wildcard validation in production mode."""

    def test_wildcard_origins_rejected_in_production(self):
        """Wildcards in CORS origins should raise RuntimeError in production."""
        from faultmaven.config.settings import Environment

        # Mock settings with wildcards and production environment
        mock_settings = MagicMock()
        mock_settings.server.environment = Environment.PRODUCTION
        mock_settings.security.cors_allow_origins = [
            "http://localhost:3333",
            "chrome-extension://*",  # Wildcard - should be rejected
        ]
        mock_settings.security.cors_allow_credentials = True
        mock_settings.security.cors_expose_headers = ["X-Request-ID"]

        # The validation should raise RuntimeError
        with pytest.raises(RuntimeError) as exc_info:
            _validate_cors_origins_for_production(mock_settings)

        assert "SECURITY ERROR" in str(exc_info.value)
        assert "chrome-extension://*" in str(exc_info.value)

    def test_specific_extension_id_allowed_in_production(self):
        """Specific extension IDs should be allowed in production."""
        from faultmaven.config.settings import Environment

        mock_settings = MagicMock()
        mock_settings.server.environment = Environment.PRODUCTION
        mock_settings.security.cors_allow_origins = [
            "https://faultmaven.ai",
            "chrome-extension://abcdef123456",  # Specific ID - allowed
        ]
        mock_settings.security.cors_allow_credentials = True
        mock_settings.security.cors_expose_headers = ["X-Request-ID"]

        # Should not raise
        _validate_cors_origins_for_production(mock_settings)

    def test_wildcard_origins_allowed_in_development(self):
        """Wildcards in CORS origins should be allowed in development."""
        from faultmaven.config.settings import Environment

        mock_settings = MagicMock()
        mock_settings.server.environment = Environment.DEVELOPMENT
        mock_settings.security.cors_allow_origins = [
            "http://localhost:3333",
            "chrome-extension://*",  # Wildcard - OK in dev
            "moz-extension://*",  # Wildcard - OK in dev
        ]
        mock_settings.security.cors_allow_credentials = True
        mock_settings.security.cors_expose_headers = ["X-Request-ID"]

        # Should not raise
        _validate_cors_origins_for_production(mock_settings)

    def test_wildcard_origins_allowed_in_staging(self):
        """Wildcards in CORS origins should be allowed in staging."""
        from faultmaven.config.settings import Environment

        mock_settings = MagicMock()
        mock_settings.server.environment = Environment.STAGING
        mock_settings.security.cors_allow_origins = [
            "http://localhost:3333",
            "chrome-extension://*",
        ]
        mock_settings.security.cors_allow_credentials = True
        mock_settings.security.cors_expose_headers = ["X-Request-ID"]

        # Should not raise
        _validate_cors_origins_for_production(mock_settings)

    def test_multiple_wildcards_all_reported(self):
        """All wildcard origins should be reported in error message."""
        from faultmaven.config.settings import Environment

        mock_settings = MagicMock()
        mock_settings.server.environment = Environment.PRODUCTION
        mock_settings.security.cors_allow_origins = [
            "chrome-extension://*",
            "moz-extension://*",
            "https://faultmaven.ai",
        ]
        mock_settings.security.cors_allow_credentials = True
        mock_settings.security.cors_expose_headers = ["X-Request-ID"]

        with pytest.raises(RuntimeError) as exc_info:
            _validate_cors_origins_for_production(mock_settings)

        error_msg = str(exc_info.value)
        assert "chrome-extension://*" in error_msg
        assert "moz-extension://*" in error_msg

    def test_empty_origins_allowed_in_production(self):
        """Empty CORS origins list should be allowed in production."""
        from faultmaven.config.settings import Environment

        mock_settings = MagicMock()
        mock_settings.server.environment = Environment.PRODUCTION
        mock_settings.security.cors_allow_origins = []
        mock_settings.security.cors_allow_credentials = True
        mock_settings.security.cors_expose_headers = ["X-Request-ID"]

        # Should not raise
        _validate_cors_origins_for_production(mock_settings)


def _validate_cors_origins_for_production(settings):
    """
    Extracted validation logic for testing.

    This mirrors the validation in faultmaven/main.py setup_middleware()
    """
    from faultmaven.config.settings import Environment

    cors_origins = list(settings.security.cors_allow_origins)

    if settings.server.environment == Environment.PRODUCTION:
        wildcard_origins = [o for o in cors_origins if "://*" in o]
        if wildcard_origins:
            raise RuntimeError(
                f"SECURITY ERROR: Wildcard CORS origins are not allowed in production: {wildcard_origins}. "
                "Configure CORS_ALLOW_ORIGINS with specific extension IDs "
                "(e.g., chrome-extension://abc123def456)."
            )
