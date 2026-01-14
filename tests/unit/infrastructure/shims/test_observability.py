"""Unit tests for observability shim.

Tests the graceful degradation behavior of the Opik tracing shim.
Verifies that the shim works correctly:
- When Opik is installed and enabled
- When Opik is installed but disabled
- When Opik is not installed (simulated)
- With both sync and async functions
"""

import asyncio
import os
from unittest.mock import patch, MagicMock
import pytest


class TestTrackDecorator:
    """Tests for the @track decorator."""

    def test_track_decorator_disabled_by_default(self):
        """Test track decorator is a no-op when ENABLE_TRACING is not set."""
        # Clear any existing env var
        with patch.dict(os.environ, {}, clear=True):
            # Need to reload to pick up env change
            from faultmaven.infrastructure.shims import observability

            # Force re-evaluation of tracing status
            original_enabled = observability._is_tracing_enabled()
            assert not original_enabled, "Tracing should be disabled by default"

    def test_track_decorator_disabled_when_false(self):
        """Test track decorator when ENABLE_TRACING=false."""
        with patch.dict(os.environ, {"ENABLE_TRACING": "false"}):
            from faultmaven.infrastructure.shims.observability import track

            @track("test_operation")
            def my_function():
                return "result"

            result = my_function()
            assert result == "result", "Function should execute normally"

    def test_track_decorator_disabled_when_empty(self):
        """Test track decorator when ENABLE_TRACING is empty string."""
        with patch.dict(os.environ, {"ENABLE_TRACING": ""}):
            from faultmaven.infrastructure.shims.observability import track

            @track("test_operation")
            def my_function():
                return "result"

            result = my_function()
            assert result == "result", "Function should execute normally"

    @pytest.mark.asyncio
    async def test_track_decorator_preserves_async_function_behavior(self):
        """Test decorated async function still works correctly."""
        with patch.dict(os.environ, {"ENABLE_TRACING": "false"}):
            from faultmaven.infrastructure.shims.observability import track

            @track("async_operation")
            async def my_async_function(x: int, y: int) -> int:
                await asyncio.sleep(0.001)  # Simulate async work
                return x + y

            result = await my_async_function(2, 3)
            assert result == 5, "Async function should return correct result"

    def test_track_decorator_preserves_sync_function_behavior(self):
        """Test decorated sync function still works correctly."""
        with patch.dict(os.environ, {"ENABLE_TRACING": "false"}):
            from faultmaven.infrastructure.shims.observability import track

            @track("sync_operation")
            def my_sync_function(x: int, y: int) -> int:
                return x * y

            result = my_sync_function(4, 5)
            assert result == 20, "Sync function should return correct result"

    def test_track_decorator_preserves_function_metadata(self):
        """Test that decorated function preserves name and docstring."""
        with patch.dict(os.environ, {"ENABLE_TRACING": "false"}):
            from faultmaven.infrastructure.shims.observability import track

            @track("documented_operation")
            def my_documented_function():
                """This is my docstring."""
                pass

            assert my_documented_function.__name__ == "my_documented_function"
            assert "This is my docstring" in (my_documented_function.__doc__ or "")

    def test_track_decorator_handles_exceptions(self):
        """Test that exceptions propagate correctly through decorated functions."""
        with patch.dict(os.environ, {"ENABLE_TRACING": "false"}):
            from faultmaven.infrastructure.shims.observability import track

            @track("failing_operation")
            def my_failing_function():
                raise ValueError("Test error")

            with pytest.raises(ValueError, match="Test error"):
                my_failing_function()

    @pytest.mark.asyncio
    async def test_track_decorator_handles_async_exceptions(self):
        """Test that exceptions propagate correctly through decorated async functions."""
        with patch.dict(os.environ, {"ENABLE_TRACING": "false"}):
            from faultmaven.infrastructure.shims.observability import track

            @track("async_failing_operation")
            async def my_async_failing_function():
                raise RuntimeError("Async test error")

            with pytest.raises(RuntimeError, match="Async test error"):
                await my_async_failing_function()

    def test_track_decorator_with_args_and_kwargs(self):
        """Test that args and kwargs are passed correctly."""
        with patch.dict(os.environ, {"ENABLE_TRACING": "false"}):
            from faultmaven.infrastructure.shims.observability import track

            @track("args_kwargs_operation")
            def my_function(*args, **kwargs):
                return {"args": args, "kwargs": kwargs}

            result = my_function(1, 2, 3, name="test", value=42)
            assert result["args"] == (1, 2, 3)
            assert result["kwargs"] == {"name": "test", "value": 42}


class TestTracingStatus:
    """Tests for tracing status functions."""

    def test_get_tracing_status_disabled(self):
        """Test get_tracing_status when tracing is disabled."""
        with patch.dict(os.environ, {"ENABLE_TRACING": "false"}):
            from faultmaven.infrastructure.shims.observability import get_tracing_status

            status = get_tracing_status()

            assert "opik_available" in status
            assert "tracing_enabled" in status
            assert "active" in status
            assert status["tracing_enabled"] is False
            # active depends on both opik_available AND tracing_enabled
            assert status["active"] is False

    def test_get_tracing_status_enabled_without_opik(self):
        """Test get_tracing_status when enabled but Opik not available."""
        with patch.dict(os.environ, {"ENABLE_TRACING": "true"}):
            # Mock Opik as unavailable
            with patch(
                "faultmaven.infrastructure.shims.observability.OPIK_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import observability

                status = observability.get_tracing_status()

                assert status["tracing_enabled"] is True
                # Can't be active without Opik
                assert status["active"] is False

    def test_is_tracing_active_false_by_default(self):
        """Test is_tracing_active returns False by default."""
        with patch.dict(os.environ, {}, clear=True):
            from faultmaven.infrastructure.shims.observability import is_tracing_active

            # Should be False because ENABLE_TRACING is not set
            assert is_tracing_active() is False

    def test_is_tracing_active_when_enabled_without_opik(self):
        """Test is_tracing_active when enabled but Opik not installed."""
        with patch.dict(os.environ, {"ENABLE_TRACING": "true"}):
            with patch(
                "faultmaven.infrastructure.shims.observability.OPIK_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import observability

                result = observability.is_tracing_active()
                assert result is False


class TestOpikAvailability:
    """Tests for Opik availability detection."""

    def test_opik_available_constant_exists(self):
        """Test that OPIK_AVAILABLE constant is exported."""
        from faultmaven.infrastructure.shims.observability import OPIK_AVAILABLE

        # Should be a boolean
        assert isinstance(OPIK_AVAILABLE, bool)

    def test_track_with_opik_unavailable(self):
        """Test track decorator when Opik is simulated as unavailable."""
        with patch.dict(os.environ, {"ENABLE_TRACING": "true"}):
            with patch(
                "faultmaven.infrastructure.shims.observability.OPIK_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import observability

                # Create a fresh decorator
                decorator = observability.track("test_op")

                def test_func():
                    return "test"

                decorated = decorator(test_func)
                result = decorated()
                assert result == "test"


class TestEnvironmentVariableHandling:
    """Tests for environment variable edge cases."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("false", False),
            ("False", False),
            ("FALSE", False),
            ("0", False),
            ("1", False),  # Only "true" (case-insensitive) enables
            ("yes", False),  # Only "true" (case-insensitive) enables
            ("", False),
        ],
    )
    def test_enable_tracing_values(self, value: str, expected: bool):
        """Test various ENABLE_TRACING values."""
        with patch.dict(os.environ, {"ENABLE_TRACING": value}):
            from faultmaven.infrastructure.shims.observability import (
                _is_tracing_enabled,
            )

            result = _is_tracing_enabled()
            assert result is expected, f"ENABLE_TRACING={value} should be {expected}"


class TestIntegrationWithOpik:
    """Tests for integration with real Opik library if available."""

    def test_track_with_opik_enabled(self):
        """Test track decorator with Opik enabled (if available)."""
        from faultmaven.infrastructure.shims.observability import OPIK_AVAILABLE

        if not OPIK_AVAILABLE:
            pytest.skip("Opik not installed - skipping integration test")

        with patch.dict(os.environ, {"ENABLE_TRACING": "true"}):
            from faultmaven.infrastructure.shims.observability import track

            @track("integration_test")
            def my_function():
                return "traced_result"

            # Function should still work even with real Opik
            result = my_function()
            assert result == "traced_result"

    def test_get_tracing_status_with_opik(self):
        """Test status when Opik is actually available."""
        from faultmaven.infrastructure.shims.observability import (
            OPIK_AVAILABLE,
            get_tracing_status,
        )

        if not OPIK_AVAILABLE:
            pytest.skip("Opik not installed - skipping integration test")

        with patch.dict(os.environ, {"ENABLE_TRACING": "true"}):
            status = get_tracing_status()

            assert status["opik_available"] is True
            assert status["tracing_enabled"] is True
            assert status["active"] is True
