"""Integration tests for infrastructure shims.

Tests the complete shim system working together:
- Environment variable toggling
- Shim interaction patterns
- Combined usage scenarios
- Real library integration (when available)
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestShimPackageImports:
    """Tests for shim package import behavior."""

    def test_all_exports_available(self):
        """Test that all expected exports are available from package."""
        from faultmaven.infrastructure.shims import (
            OPIK_AVAILABLE,
            PRESIDIO_AVAILABLE,
            PIIRedactor,
            get_pii_redaction_status,
            get_tracing_status,
            is_tracing_active,
            track,
        )

        # Verify all exports are callable or proper types
        assert callable(track)
        assert callable(get_tracing_status)
        assert callable(is_tracing_active)
        assert isinstance(OPIK_AVAILABLE, bool)
        assert callable(PIIRedactor)
        assert callable(get_pii_redaction_status)
        assert isinstance(PRESIDIO_AVAILABLE, bool)

    def test_shims_work_in_isolation(self):
        """Test that shims work without affecting each other."""
        with patch.dict(
            os.environ, {"ENABLE_TRACING": "true", "ENABLE_PII_REDACTION": "false"}
        ):
            from faultmaven.infrastructure.shims import PIIRedactor, get_tracing_status

            tracing = get_tracing_status()
            redactor = PIIRedactor()

            # Tracing enabled (even if Opik not available)
            assert tracing["tracing_enabled"] is True

            # PII disabled
            assert redactor.pii_enabled is False
            assert redactor.active is False


class TestEnvironmentVariableToggling:
    """Tests for environment variable toggling behavior."""

    def test_tracing_responds_to_env_change(self):
        """Test that tracing status responds to environment variable."""
        # First with tracing disabled
        with patch.dict(os.environ, {"ENABLE_TRACING": "false"}):
            from faultmaven.infrastructure.shims.observability import get_tracing_status

            status1 = get_tracing_status()
            assert status1["tracing_enabled"] is False

        # Then with tracing enabled
        with patch.dict(os.environ, {"ENABLE_TRACING": "true"}):
            status2 = get_tracing_status()
            assert status2["tracing_enabled"] is True

    def test_pii_responds_to_env_change(self):
        """Test that PII redaction responds to environment variable."""
        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "false"}):
            from faultmaven.infrastructure.shims.security import PIIRedactor

            redactor1 = PIIRedactor()
            assert redactor1.pii_enabled is False

        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "true"}):
            redactor2 = PIIRedactor()
            assert redactor2.pii_enabled is True

    def test_both_shims_enabled(self):
        """Test both shims enabled simultaneously."""
        with patch.dict(
            os.environ, {"ENABLE_TRACING": "true", "ENABLE_PII_REDACTION": "true"}
        ):
            from faultmaven.infrastructure.shims import PIIRedactor, get_tracing_status

            tracing = get_tracing_status()
            redactor = PIIRedactor()

            assert tracing["tracing_enabled"] is True
            assert redactor.pii_enabled is True

    def test_both_shims_disabled(self):
        """Test both shims disabled simultaneously."""
        with patch.dict(
            os.environ, {"ENABLE_TRACING": "false", "ENABLE_PII_REDACTION": "false"}
        ):
            from faultmaven.infrastructure.shims import PIIRedactor, get_tracing_status

            tracing = get_tracing_status()
            redactor = PIIRedactor()

            assert tracing["tracing_enabled"] is False
            assert tracing["active"] is False
            assert redactor.pii_enabled is False
            assert redactor.active is False


class TestCombinedShimUsage:
    """Tests for using multiple shims together."""

    @pytest.mark.asyncio
    async def test_tracked_function_with_pii_redaction(self):
        """Test using track decorator with PII redaction in same function."""
        with patch.dict(
            os.environ, {"ENABLE_TRACING": "false", "ENABLE_PII_REDACTION": "false"}
        ):
            from faultmaven.infrastructure.shims import PIIRedactor, track

            redactor = PIIRedactor()

            @track("process_user_data")
            async def process_user_data(text: str) -> str:
                """Simulates processing user data with PII redaction."""
                return redactor.redact(text)

            # Test the combined functionality
            input_text = "User email: test@example.com"
            result = await process_user_data(input_text)

            # With both disabled, should return original text
            assert result == input_text

    def test_sync_function_with_both_shims(self):
        """Test sync function using both shims."""
        with patch.dict(
            os.environ, {"ENABLE_TRACING": "false", "ENABLE_PII_REDACTION": "false"}
        ):
            from faultmaven.infrastructure.shims import PIIRedactor, track

            redactor = PIIRedactor()

            @track("sync_processor")
            def sync_process(data: str) -> dict:
                """Sync function using both shims."""
                safe_data = redactor.redact(data)
                return {"processed": True, "data": safe_data}

            result = sync_process("Contact: 555-1234")
            assert result["processed"] is True
            assert result["data"] == "Contact: 555-1234"


class TestShimsWithDisabledMode:
    """Tests specifically for community mode (shims disabled)."""

    def test_application_runs_without_tracing(self):
        """Test application works without tracing."""
        with patch.dict(os.environ, {"ENABLE_TRACING": "false"}):
            with patch(
                "faultmaven.infrastructure.shims.observability.OPIK_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import track

                @track("operation")
                def my_operation():
                    return "completed"

                result = my_operation()
                assert result == "completed"

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

    @pytest.mark.asyncio
    async def test_async_application_runs_without_dependencies(self):
        """Test async application works without enterprise dependencies."""
        with patch.dict(
            os.environ, {"ENABLE_TRACING": "false", "ENABLE_PII_REDACTION": "false"}
        ):
            with patch(
                "faultmaven.infrastructure.shims.observability.OPIK_AVAILABLE", False
            ):
                with patch(
                    "faultmaven.infrastructure.shims.security.PRESIDIO_AVAILABLE", False
                ):
                    from faultmaven.infrastructure.shims import PIIRedactor, track

                    redactor = PIIRedactor()

                    @track("async_operation")
                    async def async_operation(data: str) -> str:
                        await asyncio.sleep(0.001)
                        return redactor.redact(data)

                    result = await async_operation("test data")
                    assert result == "test data"


class TestShimStatusDiagnostics:
    """Tests for shim diagnostic/status functions."""

    def test_combined_status_check(self):
        """Test checking status of all shims at once."""
        with patch.dict(
            os.environ, {"ENABLE_TRACING": "true", "ENABLE_PII_REDACTION": "false"}
        ):
            from faultmaven.infrastructure.shims import (
                get_pii_redaction_status,
                get_tracing_status,
            )

            tracing = get_tracing_status()
            pii = get_pii_redaction_status()

            # Can use these for diagnostics/health checks
            diagnostics = {
                "tracing": tracing,
                "pii_redaction": pii,
            }

            assert "tracing" in diagnostics
            assert "pii_redaction" in diagnostics
            assert diagnostics["tracing"]["tracing_enabled"] is True
            assert diagnostics["pii_redaction"]["pii_redaction_enabled"] is False

    def test_is_tracing_active_function(self):
        """Test is_tracing_active utility function."""
        with patch.dict(os.environ, {"ENABLE_TRACING": "false"}):
            from faultmaven.infrastructure.shims import is_tracing_active

            assert is_tracing_active() is False

        with patch.dict(os.environ, {"ENABLE_TRACING": "true"}):
            # Will still be False if Opik not available
            from faultmaven.infrastructure.shims.observability import (
                OPIK_AVAILABLE,
                is_tracing_active,
            )

            result = is_tracing_active()
            if OPIK_AVAILABLE:
                assert result is True
            else:
                assert result is False


class TestShimPerformance:
    """Tests for shim performance characteristics."""

    def test_noop_decorator_minimal_overhead(self):
        """Test that no-op decorator has minimal overhead."""
        import time

        with patch.dict(os.environ, {"ENABLE_TRACING": "false"}):
            from faultmaven.infrastructure.shims import track

            @track("fast_operation")
            def fast_function():
                return 42

            # Warm up
            fast_function()

            # Measure overhead
            start = time.perf_counter()
            for _ in range(1000):
                fast_function()
            elapsed = time.perf_counter() - start

            # Should complete 1000 iterations in under 100ms
            assert elapsed < 0.1, f"No-op decorator too slow: {elapsed}s for 1000 calls"

    def test_pii_redactor_passthrough_minimal_overhead(self):
        """Test that PIIRedactor passthrough has minimal overhead."""
        import time

        with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "false"}):
            from faultmaven.infrastructure.shims import PIIRedactor

            redactor = PIIRedactor()
            text = "Some text to process"

            # Warm up
            redactor.redact(text)

            # Measure overhead
            start = time.perf_counter()
            for _ in range(1000):
                redactor.redact(text)
            elapsed = time.perf_counter() - start

            # Should complete 1000 iterations in under 50ms
            assert elapsed < 0.05, f"Passthrough too slow: {elapsed}s for 1000 calls"


class TestShimGracefulDegradation:
    """Tests for graceful degradation scenarios."""

    def test_track_graceful_with_missing_opik(self):
        """Test track decorator gracefully handles missing Opik."""
        with patch.dict(os.environ, {"ENABLE_TRACING": "true"}):
            with patch(
                "faultmaven.infrastructure.shims.observability.OPIK_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import observability

                @observability.track("test")
                def my_func():
                    return "works"

                # Should work without crashing
                assert my_func() == "works"

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
        from faultmaven.infrastructure.shims import OPIK_AVAILABLE, PRESIDIO_AVAILABLE

        # These should be booleans, not raise errors
        assert isinstance(OPIK_AVAILABLE, bool)
        assert isinstance(PRESIDIO_AVAILABLE, bool)


class TestRealWorldScenarios:
    """Tests simulating real-world usage scenarios."""

    @pytest.mark.asyncio
    async def test_case_creation_workflow(self):
        """Simulate case creation workflow with shims."""
        with patch.dict(
            os.environ, {"ENABLE_TRACING": "false", "ENABLE_PII_REDACTION": "false"}
        ):
            from faultmaven.infrastructure.shims import PIIRedactor, track

            redactor = PIIRedactor()

            @track("create_case")
            async def create_case(title: str, description: str) -> dict:
                """Simulated case creation."""
                safe_description = redactor.redact(description)
                return {
                    "id": "case-123",
                    "title": title,
                    "description": safe_description,
                }

            case = await create_case(
                title="Database Issue", description="Contact admin@example.com for help"
            )

            assert case["id"] == "case-123"
            assert case["title"] == "Database Issue"
            # With PII disabled, description unchanged
            assert case["description"] == "Contact admin@example.com for help"

    def test_health_check_endpoint_simulation(self):
        """Simulate health check endpoint using shim status."""
        with patch.dict(
            os.environ, {"ENABLE_TRACING": "false", "ENABLE_PII_REDACTION": "false"}
        ):
            from faultmaven.infrastructure.shims import (
                get_pii_redaction_status,
                get_tracing_status,
            )

            def health_check() -> dict:
                """Simulated health check endpoint."""
                return {
                    "status": "healthy",
                    "features": {
                        "tracing": get_tracing_status(),
                        "pii_redaction": get_pii_redaction_status(),
                    },
                }

            health = health_check()

            assert health["status"] == "healthy"
            assert "tracing" in health["features"]
            assert "pii_redaction" in health["features"]
