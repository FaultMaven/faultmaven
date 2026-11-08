"""Integration tests for infrastructure shims.

Tests the complete shim system working together:
- Environment variable toggling
- Shim interaction patterns
- Combined usage scenarios
- Real library integration (when available)
"""

import asyncio
import contextlib
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The real engines load a spaCy model and, when none is installed (none is in
# any FaultMaven image), download it: a network call from a test that only
# reads ``pii_enabled``. The network guard refuses it; these doubles make it
# not happen.


def _no_presidio_engines():
    """Presidio's analyzer and anonymizer, stood in for the length of the block."""
    stack = contextlib.ExitStack()
    for name in ("AnalyzerEngine", "AnonymizerEngine"):
        stack.enter_context(
            patch(
                f"faultmaven.infrastructure.shims.security.{name}",
                MagicMock(),
                create=True,
            )
        )
    return stack


class TestShimPackageImports:
    """Tests for shim package import behavior."""

    @pytest.mark.filterwarnings(
        "ignore:Field.*has conflict with protected namespace:UserWarning"
    )
    def test_all_exports_available(self):
        """Test that all expected exports are available from package."""
        from faultmaven.infrastructure.shims import (
            PRESIDIO_AVAILABLE,
            PIIRedactor,
            get_pii_redaction_status,
        )

        # Verify all exports are callable or proper types
        assert callable(PIIRedactor)
        assert callable(get_pii_redaction_status)
        assert isinstance(PRESIDIO_AVAILABLE, bool)


class TestEnvironmentVariableToggling:
    """Tests for environment variable toggling behavior."""

    def test_pii_responds_to_env_change(self):
        """Test that PII redaction responds to environment variable."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "false"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor1 = PIIRedactor()
            assert redactor1.pii_enabled is False

        with (
            patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}),
            _no_presidio_engines(),
        ):
            redactor2 = PIIRedactor()
            assert redactor2.pii_enabled is True


class TestShimsWithDisabledMode:
    """Tests specifically for community mode (shims disabled)."""

    def test_application_runs_without_pii_redaction(self):
        """Test application works without PII redaction."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "false"}):
            with patch(
                "faultmaven.infrastructure.shims.security.PRESIDIO_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import PIIRedactor

                redactor = PIIRedactor()
                result = redactor.redact("My SSN is 123-45-6789")

                assert result == "My SSN is 123-45-6789"


# The ``PIIRedactor`` passthrough timing that lived here, 1000 calls against a
# raw wall-clock total, is ``tests/performance/test_shim_overhead.py`` now,
# behind a calibrated budget (#1579). It was the only timing in this module.


class TestShimGracefulDegradation:
    """Tests for graceful degradation scenarios."""

    def test_pii_graceful_with_missing_presidio(self):
        """Test PIIRedactor gracefully handles missing Presidio."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
            with patch(
                "faultmaven.infrastructure.shims.security.PRESIDIO_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import security

                redactor = security.PIIRedactor()
                text = "sensitive data"

                # Should work without crashing
                result = redactor.redact(text)
                assert result == text

    def test_shims_dont_crash_on_import_errors(self):
        """Test shim imports don't crash when libraries unavailable."""
        # This test verifies the import-time behavior is safe
        # The actual import happens at module load, but we can verify
        # the constants are set correctly
        from faultmaven.infrastructure.shims import PRESIDIO_AVAILABLE

        # This should be a boolean, not raise errors
        assert isinstance(PRESIDIO_AVAILABLE, bool)


class TestRealWorldScenarios:
    """Tests simulating real-world usage scenarios."""

    def test_health_check_endpoint_simulation(self):
        """Simulate health check endpoint using shim status."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "false"}):
            from faultmaven.infrastructure.shims import get_pii_redaction_status

            def health_check() -> dict:
                """Simulated health check endpoint."""
                return {
                    "status": "healthy",
                    "features": {"pii_redaction": get_pii_redaction_status()},
                }

            health = health_check()

            assert health["status"] == "healthy"
            assert "pii_redaction" in health["features"]
