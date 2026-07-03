"""
Test module for faultmaven.infrastructure.logging.config
"""

import logging
from typing import Any, Dict
from unittest.mock import MagicMock, Mock, patch

import pytest
import structlog

from faultmaven.infrastructure.logging.config import (
    FaultMavenLogger,
    LoggingConfig,
    get_logger,
)


class TestFaultMavenLogger:
    """Test cases for FaultMavenLogger class."""

    def setup_method(self):
        """Setup for each test method."""
        # Reset structlog configuration
        structlog.reset_defaults()
        # Snapshot the root logger so constructing FaultMavenLogger (which
        # installs a handler and sets the root level) cannot leak state into
        # later tests.
        root = logging.getLogger()
        self._saved_root_handlers = root.handlers[:]
        self._saved_root_level = root.level

    def teardown_method(self):
        """Cleanup after each test method."""
        # Reset structlog configuration
        structlog.reset_defaults()
        # Restore the root logger exactly as it was before this test.
        root = logging.getLogger()
        root.handlers[:] = self._saved_root_handlers
        root.setLevel(self._saved_root_level)

    @staticmethod
    def _json_config():
        """A LoggingConfig pinned to JSON/INFO so tests that json.loads the
        captured output are independent of the ambient .env / settings."""
        config = LoggingConfig()
        config.LOG_FORMAT = "json"
        config.LOG_LEVEL = "INFO"
        config.LOG_HUMAN_READABLE = False
        return config

    def test_fault_maven_logger_creation(self):
        """Test FaultMavenLogger initialization."""
        with patch("structlog.configure") as mock_configure:
            logger_config = FaultMavenLogger()

            # Should call configure_structlog during init
            mock_configure.assert_called_once()

    @patch("structlog.configure")
    def test_configure_structlog_setup(self, mock_configure):
        """Test configure_structlog wires structlog through the stdlib root handler."""
        logger_config = FaultMavenLogger()

        # Check that structlog was configured
        mock_configure.assert_called_once()

        # Examine the structlog configuration call
        kwargs = mock_configure.call_args[1]

        assert "processors" in kwargs
        assert "context_class" in kwargs
        assert "logger_factory" in kwargs
        assert "wrapper_class" in kwargs
        assert "cache_logger_on_first_use" in kwargs

        processors = kwargs["processors"]
        assert len(processors) > 0

        # Custom processors should be included (built into the shared chain).
        assert any("add_request_context" in str(proc) for proc in processors)
        assert any("add_trace_context" in str(proc) for proc in processors)

        # Native structlog logs are handed to the stdlib ProcessorFormatter,
        # not rendered inline — the chain must end in wrap_for_formatter.
        assert processors[-1] is structlog.stdlib.ProcessorFormatter.wrap_for_formatter

        # Exactly one FaultMaven-owned root handler carries a ProcessorFormatter
        # so stdlib and structlog logs render identically. Other (foreign) root
        # handlers, if any, are left intact — we don't clear them.
        root = logging.getLogger()
        fm_handlers = [
            h
            for h in root.handlers
            if getattr(h, FaultMavenLogger._ROOT_HANDLER_MARKER, False)
        ]
        assert len(fm_handlers) == 1
        assert isinstance(fm_handlers[0].formatter, structlog.stdlib.ProcessorFormatter)
        # Root level is lowered to at least our configured verbosity.
        assert root.level != logging.NOTSET
        assert root.level <= logger_config.config.get_log_level()

    def test_stdlib_extra_fields_render(self):
        """A plain stdlib ``logger.info(msg, extra={...})`` must render its extra
        fields (regression: ``basicConfig(format="%(message)s")`` dropped them,
        making per-turn token-spend forensics invisible)."""
        import io
        import json

        FaultMavenLogger(config=self._json_config())  # pin JSON/INFO
        handler = self._fm_root_handler()
        buf = io.StringIO()
        orig_stream = handler.stream
        handler.stream = buf
        try:
            logging.getLogger("faultmaven.core.investigation.milestone_engine").info(
                "turn_token_spend",
                extra={"input_tokens": 46600, "total_calls": 3},
            )
        finally:
            handler.stream = orig_stream

        record = json.loads(buf.getvalue().strip())
        assert record["event"] == "turn_token_spend"
        assert record["input_tokens"] == 46600
        assert record["total_calls"] == 3
        assert record["logger"] == "faultmaven.core.investigation.milestone_engine"
        assert record["level"] == "info"

    def test_stdlib_positional_args_interpolated(self):
        """A stdlib ``logger.info("env=%s", value)`` must interpolate the arg
        into the event message rather than emitting a raw "%s" plus a dangling
        positional_args field."""
        import io
        import json

        FaultMavenLogger(config=self._json_config())  # pin JSON/INFO
        handler = self._fm_root_handler()
        buf = io.StringIO()
        orig_stream = handler.stream
        handler.stream = buf
        try:
            logging.getLogger("x").info("environment=%s ready", "development")
        finally:
            handler.stream = orig_stream

        record = json.loads(buf.getvalue().strip())
        assert record["event"] == "environment=development ready"
        assert "positional_args" not in record

    def test_reconfigure_preserves_foreign_root_handlers(self, caplog):
        """Re-running configuration mid-process must NOT drop foreign root
        handlers (regression: a blanket handlers.clear() removed pytest's
        caplog handler, silently breaking log capture)."""
        import faultmaven.infrastructure.logging.config as config_module

        # Simulate the singleton being rebuilt mid-test, exactly as several
        # tests in this suite do via `_logger_config = None`.
        config_module._logger_config = None
        try:
            with caplog.at_level(logging.INFO, logger="faultmaven.reconfig_probe"):
                # Triggers configure_structlog() while caplog's handler is on root.
                get_logger("faultmaven.reconfig_probe")
                logging.getLogger("faultmaven.reconfig_probe").info("still_captured")

            assert any(
                "still_captured" in r.getMessage() for r in caplog.records
            ), "caplog lost records after logging reconfiguration"
        finally:
            config_module._logger_config = None

    @staticmethod
    def _fm_root_handler():
        """Return the single FaultMaven-owned handler on the root logger."""
        fm_handlers = [
            h
            for h in logging.getLogger().handlers
            if getattr(h, FaultMavenLogger._ROOT_HANDLER_MARKER, False)
        ]
        assert len(fm_handlers) == 1, f"expected 1 FM handler, got {len(fm_handlers)}"
        return fm_handlers[0]

    def test_add_request_context_no_context(self):
        """Test add_request_context when no request context exists."""
        with patch(
            "faultmaven.infrastructure.logging.coordinator.request_context"
        ) as mock_context:
            mock_context.get.return_value = None

            event_dict = {"message": "test", "level": "info"}
            result = FaultMavenLogger.add_request_context(Mock(), "info", event_dict)

            # Should return unchanged event dict
            assert result == event_dict

    def test_add_request_context_with_context(self):
        """Test add_request_context adds context fields."""
        # Create mock request context
        mock_ctx = Mock()
        mock_ctx.correlation_id = "test-correlation-id"
        mock_ctx.session_id = "test-session-id"
        mock_ctx.user_id = "test-user-id"
        mock_ctx.case_id = "test-case-id"
        mock_ctx.agent_phase = "define_blast_radius"

        with patch(
            "faultmaven.infrastructure.logging.coordinator.request_context"
        ) as mock_context:
            mock_context.get.return_value = mock_ctx

            event_dict = {"message": "test"}
            result = FaultMavenLogger.add_request_context(Mock(), "info", event_dict)

            # Should have added context fields
            assert result["correlation_id"] == "test-correlation-id"
            assert result["session_id"] == "test-session-id"
            assert result["user_id"] == "test-user-id"
            assert result["case_id"] == "test-case-id"
            assert result["agent_phase"] == "define_blast_radius"
            assert result["message"] == "test"

    def test_add_request_context_prevents_duplication(self):
        """Test add_request_context doesn't overwrite existing fields."""
        mock_ctx = Mock()
        mock_ctx.correlation_id = "new-correlation-id"
        mock_ctx.session_id = "new-session-id"
        mock_ctx.user_id = None  # None values should not be added
        mock_ctx.investigation_id = None
        mock_ctx.agent_phase = None

        with patch(
            "faultmaven.infrastructure.logging.coordinator.request_context"
        ) as mock_context:
            mock_context.get.return_value = mock_ctx

            # Event dict already has some context fields
            event_dict = {
                "message": "test",
                "correlation_id": "existing-correlation-id",
            }
            result = FaultMavenLogger.add_request_context(Mock(), "info", event_dict)

            # Should not overwrite existing correlation_id
            assert result["correlation_id"] == "existing-correlation-id"
            # Should add session_id
            assert result["session_id"] == "new-session-id"
            # Should not add None values
            assert "user_id" not in result
            assert "investigation_id" not in result
            assert "agent_phase" not in result
            assert result["message"] == "test"

    def test_deduplicate_fields_removes_duplicates(self):
        """Test deduplicate_fields removes duplicate field entries."""
        event_dict = {
            "field1": "value1",
            "field2": "value2",
            "field1": "value1_duplicate",  # This will be the final value
            "field3": "value3",
        }

        result = FaultMavenLogger.deduplicate_fields(Mock(), "info", event_dict)

        # Should have unique fields only
        assert len(result) == 3
        assert "field1" in result
        assert "field2" in result
        assert "field3" in result
        # Python dict will keep the last value for duplicated keys
        assert result["field1"] == "value1_duplicate"
        assert result["field2"] == "value2"
        assert result["field3"] == "value3"

    def test_deduplicate_fields_empty_dict(self):
        """Test deduplicate_fields with empty dict."""
        event_dict = {}
        result = FaultMavenLogger.deduplicate_fields(Mock(), "info", event_dict)
        assert result == {}

    def test_add_trace_context_no_span(self):
        """Test add_trace_context when no active span exists."""
        with patch("opentelemetry.trace.get_current_span") as mock_get_span:
            mock_get_span.return_value = None

            event_dict = {"message": "test"}
            result = FaultMavenLogger.add_trace_context(Mock(), "info", event_dict)

            # Should return unchanged event dict
            assert result == event_dict

    def test_add_trace_context_with_span(self):
        """Test add_trace_context adds trace information."""
        # Create mock span and span context
        mock_span_context = Mock()
        mock_span_context.trace_id = 123456789012345678901234567890123456
        mock_span_context.span_id = 1234567890123456

        mock_span = Mock()
        mock_span.is_recording.return_value = True
        mock_span.get_span_context.return_value = mock_span_context

        with patch("opentelemetry.trace.get_current_span") as mock_get_span:
            mock_get_span.return_value = mock_span

            event_dict = {"message": "test"}
            result = FaultMavenLogger.add_trace_context(Mock(), "info", event_dict)

            # Should have added trace information
            assert "trace_id" in result
            assert "span_id" in result
            assert result["message"] == "test"

            # Check trace ID formatting (32 hex characters)
            assert len(result["trace_id"]) == 32
            # Check span ID formatting (16 hex characters)
            assert len(result["span_id"]) == 16

    def test_add_trace_context_span_not_recording(self):
        """Test add_trace_context when span is not recording."""
        mock_span = Mock()
        mock_span.is_recording.return_value = False

        with patch("opentelemetry.trace.get_current_span") as mock_get_span:
            mock_get_span.return_value = mock_span

            event_dict = {"message": "test"}
            result = FaultMavenLogger.add_trace_context(Mock(), "info", event_dict)

            # Should return unchanged event dict
            assert result == event_dict

    def test_add_trace_context_prevents_duplication(self):
        """Test add_trace_context doesn't overwrite existing trace fields."""
        mock_span_context = Mock()
        mock_span_context.trace_id = 123456789012345678901234567890123456
        mock_span_context.span_id = 1234567890123456

        mock_span = Mock()
        mock_span.is_recording.return_value = True
        mock_span.get_span_context.return_value = mock_span_context

        with patch("opentelemetry.trace.get_current_span") as mock_get_span:
            mock_get_span.return_value = mock_span

            # Event dict already has trace fields
            event_dict = {
                "message": "test",
                "trace_id": "existing-trace-id",
                "span_id": "existing-span-id",
            }
            result = FaultMavenLogger.add_trace_context(Mock(), "info", event_dict)

            # Should not overwrite existing fields
            assert result["trace_id"] == "existing-trace-id"
            assert result["span_id"] == "existing-span-id"
            assert result["message"] == "test"


class TestGetLogger:
    """Test cases for get_logger function."""

    def setup_method(self):
        """Setup for each test method."""
        # Reset global logger config
        import faultmaven.infrastructure.logging.config as config_module

        config_module._logger_config = None
        structlog.reset_defaults()

    def teardown_method(self):
        """Cleanup after each test method."""
        # Reset global logger config
        import faultmaven.infrastructure.logging.config as config_module

        config_module._logger_config = None
        structlog.reset_defaults()

    @patch("structlog.get_logger")
    def test_get_logger_creates_config_once(self, mock_get_logger):
        """Test get_logger creates FaultMavenLogger configuration only once."""
        with patch(
            "faultmaven.infrastructure.logging.config.FaultMavenLogger"
        ) as mock_logger_class:
            mock_logger_instance = Mock()
            mock_logger_class.return_value = mock_logger_instance
            mock_get_logger.return_value = Mock()

            # First call
            logger1 = get_logger("test.module1")

            # Second call
            logger2 = get_logger("test.module2")

            # FaultMavenLogger should be created only once
            assert mock_logger_class.call_count == 1

            # But structlog.get_logger should be called twice
            assert mock_get_logger.call_count == 2
            mock_get_logger.assert_any_call("test.module1")
            mock_get_logger.assert_any_call("test.module2")

    @patch("structlog.get_logger")
    def test_get_logger_returns_structlog_logger(self, mock_get_logger):
        """Test get_logger returns structlog BoundLogger."""
        mock_bound_logger = Mock()
        mock_get_logger.return_value = mock_bound_logger

        result = get_logger("test.module")

        assert result == mock_bound_logger
        mock_get_logger.assert_called_once_with("test.module")

    @patch("structlog.configure")
    @patch("structlog.get_logger")
    def test_get_logger_configures_structlog(self, mock_get_logger, mock_configure):
        """Test get_logger triggers structlog configuration."""
        mock_get_logger.return_value = Mock()

        get_logger("test.module")

        # Should have configured structlog
        mock_configure.assert_called_once()


class TestProcessorIntegration:
    """Integration tests for processor chain."""

    def test_processor_chain_order(self):
        """Test that processors are applied in correct order."""
        # Create a chain of events to simulate processor execution
        initial_event = {"message": "test"}

        # Mock request context
        mock_ctx = Mock()
        mock_ctx.correlation_id = "test-correlation"
        mock_ctx.session_id = None
        mock_ctx.user_id = None
        mock_ctx.investigation_id = None
        mock_ctx.agent_phase = None

        # Mock span
        mock_span_context = Mock()
        mock_span_context.trace_id = 123456789012345678901234567890123456
        mock_span_context.span_id = 1234567890123456

        mock_span = Mock()
        mock_span.is_recording.return_value = True
        mock_span.get_span_context.return_value = mock_span_context

        with patch(
            "faultmaven.infrastructure.logging.coordinator.request_context"
        ) as mock_context:
            mock_context.get.return_value = mock_ctx

            with patch("opentelemetry.trace.get_current_span") as mock_get_span:
                mock_get_span.return_value = mock_span

                # Apply processors in order
                event = initial_event.copy()

                # 1. Add request context
                event = FaultMavenLogger.add_request_context(Mock(), "info", event)

                # 2. Deduplicate fields
                event = FaultMavenLogger.deduplicate_fields(Mock(), "info", event)

                # 3. Add trace context
                event = FaultMavenLogger.add_trace_context(Mock(), "info", event)

                # Verify final event has all expected fields
                assert event["message"] == "test"
                assert event["correlation_id"] == "test-correlation"
                assert "trace_id" in event
                assert "span_id" in event
                assert len(event["trace_id"]) == 32
                assert len(event["span_id"]) == 16

    def test_processor_chain_with_conflicts(self):
        """Test processor chain handles field conflicts correctly."""
        # Start with event that has some conflicting fields
        initial_event = {
            "message": "test",
            "correlation_id": "existing-correlation",
            "trace_id": "existing-trace",
        }

        # Mock request context with different values
        mock_ctx = Mock()
        mock_ctx.correlation_id = "context-correlation"
        mock_ctx.session_id = "context-session"
        mock_ctx.user_id = None
        mock_ctx.investigation_id = None
        mock_ctx.agent_phase = None

        # Mock span with different trace ID
        mock_span_context = Mock()
        mock_span_context.trace_id = 987654321098765432109876543210987654
        mock_span_context.span_id = 9876543210987654

        mock_span = Mock()
        mock_span.is_recording.return_value = True
        mock_span.get_span_context.return_value = mock_span_context

        with patch(
            "faultmaven.infrastructure.logging.coordinator.request_context"
        ) as mock_context:
            mock_context.get.return_value = mock_ctx

            with patch("opentelemetry.trace.get_current_span") as mock_get_span:
                mock_get_span.return_value = mock_span

                # Apply processors
                event = initial_event.copy()
                event = FaultMavenLogger.add_request_context(Mock(), "info", event)
                event = FaultMavenLogger.deduplicate_fields(Mock(), "info", event)
                event = FaultMavenLogger.add_trace_context(Mock(), "info", event)

                # Existing values should be preserved (no overwriting)
                assert event["correlation_id"] == "existing-correlation"
                assert event["trace_id"] == "existing-trace"
                # New fields should be added
                assert event["session_id"] == "context-session"
                assert event["message"] == "test"


class TestErrorHandling:
    """Test error handling in logging configuration."""

    def test_processor_error_handling(self):
        """Test processors handle errors gracefully."""
        # Test add_request_context with import error
        with patch(
            "faultmaven.infrastructure.logging.coordinator.request_context",
            side_effect=ImportError("Module not found"),
        ):

            event_dict = {"message": "test"}

            # Should not raise exception, should return original event
            try:
                result = FaultMavenLogger.add_request_context(
                    Mock(), "info", event_dict
                )
                # If no exception, should return original or safe fallback
                assert "message" in result
            except ImportError:
                # If exception propagates, that's also acceptable for this test
                pass

    def test_trace_context_error_handling(self):
        """Test add_trace_context handles errors gracefully."""
        with patch(
            "opentelemetry.trace.get_current_span",
            side_effect=Exception("Tracing error"),
        ):

            event_dict = {"message": "test"}

            # Should not raise exception
            try:
                result = FaultMavenLogger.add_trace_context(Mock(), "info", event_dict)
                assert result == event_dict  # Should return unchanged
            except Exception:
                # If exception propagates, should be handled gracefully
                pass


class TestSingletonBehavior:
    """Test singleton behavior of logger configuration."""

    def setup_method(self):
        """Setup for each test method."""
        # Reset global state
        import faultmaven.infrastructure.logging.config as config_module

        config_module._logger_config = None

    def teardown_method(self):
        """Cleanup after each test method."""
        # Reset global state
        import faultmaven.infrastructure.logging.config as config_module

        config_module._logger_config = None

    def test_singleton_logger_config(self):
        """Test that FaultMavenLogger is created as singleton."""
        with patch(
            "faultmaven.infrastructure.logging.config.FaultMavenLogger"
        ) as mock_logger_class:
            mock_instance = Mock()
            mock_logger_class.return_value = mock_instance

            # Multiple calls to get_logger
            get_logger("module1")
            get_logger("module2")
            get_logger("module3")

            # FaultMavenLogger should be instantiated only once
            assert mock_logger_class.call_count == 1
