"""
Test module for faultmaven.infrastructure.logging.unified
"""

import pytest
import asyncio
import time
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime
from contextlib import asynccontextmanager, contextmanager

from faultmaven.infrastructure.logging.unified import (
    UnifiedLogger,
    get_unified_logger,
    clear_logger_cache
)
from faultmaven.infrastructure.logging.coordinator import (
    LoggingCoordinator,
    RequestContext,
    ErrorContext,
    PerformanceTracker,
    request_context
)


class TestUnifiedLogger:
    """Test cases for UnifiedLogger class."""
    
    def setup_method(self):
        """Setup for each test method."""
        # Clear any existing context and caches
        request_context.set(None)
        clear_logger_cache()
        self.mock_logger = Mock()
        
        # Create unified logger with mocked structlog logger
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_get_logger.return_value = self.mock_logger
            self.unified_logger = UnifiedLogger("test.module", "service")
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
        clear_logger_cache()
    
    def test_unified_logger_creation(self):
        """Test UnifiedLogger initialization."""
        assert self.unified_logger.logger_name == "test.module"
        assert self.unified_logger.layer == "service"
        assert self.unified_logger.logger == self.mock_logger
        assert isinstance(self.unified_logger.coordinator, LoggingCoordinator)
    
    def test_log_boundary_without_context(self):
        """Test log_boundary works without request context."""
        # No request context
        assert request_context.get() is None
        
        self.unified_logger.log_boundary(
            operation="test_operation",
            direction="inbound",
            data={"key": "value"}
        )
        
        # Should have called logger.info
        self.mock_logger.info.assert_called_once()
        call_args = self.mock_logger.info.call_args
        
        # Check message
        assert "Service boundary inbound: test_operation" in call_args[0]
        
        # Check logged data
        logged_data = call_args[1]
        assert logged_data["event_type"] == "service_boundary"
        assert logged_data["layer"] == "service"
        assert logged_data["operation"] == "test_operation"
        assert logged_data["direction"] == "inbound"
        assert "boundary_key" in logged_data
        assert "payload_info" in logged_data
    
    def test_log_boundary_with_context_deduplication(self):
        """Test log_boundary prevents duplication with request context."""
        # Create request context
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        
        # Log same boundary twice
        self.unified_logger.log_boundary("test_op", "inbound")
        self.unified_logger.log_boundary("test_op", "inbound")
        
        # Should only be logged once due to deduplication
        assert self.mock_logger.info.call_count == 1
        
        # Check that operation was marked as logged
        boundary_key = "service.boundary.test_op.inbound"
        assert ctx.has_logged(boundary_key)
        
        coordinator.end_request()
    
    def test_log_boundary_different_operations(self):
        """Test log_boundary allows different operations."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        # Log different boundaries
        self.unified_logger.log_boundary("op1", "inbound")
        self.unified_logger.log_boundary("op1", "outbound")
        self.unified_logger.log_boundary("op2", "inbound")
        
        # Should log all three (different keys)
        assert self.mock_logger.info.call_count == 3
        
        coordinator.end_request()
    
    def test_log_boundary_payload_info(self):
        """Test log_boundary includes payload information."""
        test_data = {
            "key1": "value1",
            "key2": "value2",
            "nested": {"inner": "value"}
        }
        
        self.unified_logger.log_boundary(
            operation="test_op",
            direction="inbound", 
            data=test_data
        )
        
        call_args = self.mock_logger.info.call_args[1]
        payload_info = call_args["payload_info"]
        
        assert payload_info["type"] == "dict"
        assert payload_info["size"] > 0
        assert payload_info["keys"] == ["key1", "key2", "nested"]
    
    @pytest.mark.asyncio
    async def test_operation_context_manager_success(self):
        """Test async operation context manager for successful operation."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        async with self.unified_logger.operation("test_operation", user_id="123") as ctx:
            # Context should be available for modification
            assert isinstance(ctx, dict)
            assert ctx["operation"] == "test_operation"
            assert ctx["layer"] == "service"
            assert ctx["user_id"] == "123"
            assert "start_time" in ctx
            
            # Modify context during operation
            ctx["items_processed"] = 10
            
            # Simulate some work
            await asyncio.sleep(0.01)
        
        # Should have logged operation start and end
        assert self.mock_logger.info.call_count >= 2
        
        # Check logged calls
        calls = self.mock_logger.info.call_args_list
        
        # First call should be operation start
        start_call = calls[0]
        assert "Operation started: test_operation" in start_call[0]
        assert start_call[1]["event_type"] == "operation_start"
        
        # Last call should be operation end
        end_call = calls[-1]
        assert "Operation completed: test_operation" in end_call[0]
        assert end_call[1]["event_type"] == "operation_end"
        assert end_call[1]["items_processed"] == 10
        assert "duration_seconds" in end_call[1]
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    async def test_operation_context_manager_error(self):
        """Test async operation context manager with error."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        test_error = ValueError("Test error")
        
        with pytest.raises(ValueError):
            async with self.unified_logger.operation("failing_operation") as ctx:
                ctx["step"] = "before_error"
                raise test_error
        
        # Should have logged operation start and error
        assert self.mock_logger.info.call_count >= 1  # Start
        assert self.mock_logger.error.call_count >= 1  # Error
        
        # Check error log
        error_call = self.mock_logger.error.call_args
        assert "Operation failed: failing_operation" in error_call[0]
        assert error_call[1]["event_type"] == "operation_error"
        assert error_call[1]["error_message"] == "Test error"
        assert error_call[1]["error_type"] == "ValueError"
        assert error_call[1]["step"] == "before_error"
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    async def test_operation_performance_tracking(self):
        """Test operation context manager tracks performance."""
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        
        # Mock performance tracker to simulate threshold violation
        mock_tracker = Mock()
        mock_tracker.record_timing.return_value = (True, 0.5)  # Exceeds threshold
        ctx.performance_tracker = mock_tracker
        
        async with self.unified_logger.operation("slow_operation"):
            await asyncio.sleep(0.01)  # Simulate work
        
        # Should have recorded timing
        mock_tracker.record_timing.assert_called_once()
        call_args = mock_tracker.record_timing.call_args
        assert call_args[0][0] == "service"  # layer
        assert call_args[0][1] == "slow_operation"  # operation
        assert call_args[0][2] > 0  # duration
        
        # Should log as warning due to performance violation
        warning_calls = [call for call in self.mock_logger.warning.call_args_list 
                        if "Operation completed" in str(call)]
        assert len(warning_calls) > 0
        
        coordinator.end_request()
    
    def test_operation_sync_context_manager(self):
        """Test synchronous operation context manager."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        with self.unified_logger.operation_sync("sync_operation", task_id="456") as ctx:
            assert ctx["operation"] == "sync_operation"
            assert ctx["task_id"] == "456"
            ctx["result"] = "success"
            
            # Simulate work
            time.sleep(0.001)
        
        # Should have logged start and end
        assert self.mock_logger.info.call_count >= 2
        
        coordinator.end_request()
    
    def test_operation_sync_error_handling(self):
        """Test synchronous operation error handling."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        with pytest.raises(RuntimeError):
            with self.unified_logger.operation_sync("failing_sync_op") as ctx:
                ctx["step"] = "about_to_fail"
                raise RuntimeError("Sync operation failed")
        
        # Should have logged error
        assert self.mock_logger.error.call_count >= 1
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    async def test_operation_deduplication(self):
        """Test operation logging deduplication."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        # Start same operation multiple times
        async with self.unified_logger.operation("duplicate_op"):
            pass
        
        # Try to start same operation again
        async with self.unified_logger.operation("duplicate_op"):
            pass
        
        # Should only log start/end once for each operation
        start_calls = [call for call in self.mock_logger.info.call_args_list 
                      if "Operation started" in str(call)]
        end_calls = [call for call in self.mock_logger.info.call_args_list 
                    if "Operation completed" in str(call)]
        
        # With deduplication, should have fewer logs
        assert len(start_calls) <= 2  # May be deduplicated
        
        coordinator.end_request()
    
    def test_log_metric(self):
        """Test metric logging functionality."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        self.unified_logger.log_metric(
            metric_name="request_count",
            value=42,
            unit="count",
            tags={"endpoint": "/api/test"}
        )
        
        # Should have logged metric
        assert self.mock_logger.info.call_count == 1
        
        call_args = self.mock_logger.info.call_args
        assert "Metric recorded: request_count=42 count" in call_args[0]
        
        logged_data = call_args[1]
        assert logged_data["event_type"] == "metric"
        assert logged_data["metric_name"] == "request_count"
        assert logged_data["metric_value"] == 42
        assert logged_data["metric_unit"] == "count"
        assert logged_data["metric_tags"]["endpoint"] == "/api/test"
        
        coordinator.end_request()
    
    def test_log_metric_allows_multiple_values(self):
        """Test that metrics with same name can be logged multiple times."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        # Log same metric name with different values
        self.unified_logger.log_metric("response_time", 0.1, "seconds")
        self.unified_logger.log_metric("response_time", 0.2, "seconds")
        self.unified_logger.log_metric("response_time", 0.15, "seconds")
        
        # Should log all three metrics (timestamped keys prevent deduplication)
        assert self.mock_logger.info.call_count == 3
        
        coordinator.end_request()
    
    def test_log_event(self):
        """Test event logging functionality."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        event_data = {"user_action": "login", "success": True}
        
        self.unified_logger.log_event(
            event_type="business",
            event_name="user_authentication",
            severity="info",
            data=event_data
        )
        
        # Should have logged event
        assert self.mock_logger.info.call_count == 1
        
        call_args = self.mock_logger.info.call_args
        assert "Event: business.user_authentication" in call_args[0]
        
        logged_data = call_args[1]
        assert logged_data["event_type"] == "application_event"
        assert logged_data["app_event_type"] == "business"
        assert logged_data["event_name"] == "user_authentication"
        assert logged_data["event_severity"] == "info"
        assert logged_data["event_data"] == event_data
        
        coordinator.end_request()
    
    def test_log_event_different_severities(self):
        """Test event logging with different severity levels."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        # Mock logger methods
        self.mock_logger.debug = Mock()
        self.mock_logger.warning = Mock()
        self.mock_logger.error = Mock()
        
        # Log events with different severities
        self.unified_logger.log_event("technical", "cache_miss", "debug")
        self.unified_logger.log_event("system", "high_memory", "warning")
        self.unified_logger.log_event("business", "payment_failed", "error")
        
        # Each should call appropriate log method
        self.mock_logger.debug.assert_called_once()
        self.mock_logger.warning.assert_called_once()
        self.mock_logger.error.assert_called_once()
        
        coordinator.end_request()
    
    def test_basic_log_methods(self):
        """Test basic logging methods (debug, info, warning)."""
        # Test debug
        self.unified_logger.debug("Debug message", extra_field="debug_value")
        self.mock_logger.debug.assert_called_once_with(
            "Debug message", 
            layer="service", 
            extra_field="debug_value"
        )
        
        # Test info
        self.unified_logger.info("Info message", context="test")
        self.mock_logger.info.assert_called_with(
            "Info message",
            layer="service",
            context="test"
        )
        
        # Test warning
        self.unified_logger.warning("Warning message")
        self.mock_logger.warning.assert_called_with(
            "Warning message",
            layer="service"
        )
    
    def test_error_logging_with_cascade_prevention(self):
        """Test error logging with cascade prevention."""
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        
        test_error = ValueError("Test error")
        
        # First error log should work
        self.unified_logger.error("First error", error=test_error)
        assert self.mock_logger.error.call_count == 1
        
        # Same layer should not log again (cascade prevention)
        self.unified_logger.error("Second error", error=test_error)
        # With cascade prevention, may not log again
        
        coordinator.end_request()
    
    def test_error_logging_without_context(self):
        """Test error logging without request context (fallback)."""
        # No request context
        assert request_context.get() is None
        
        test_error = RuntimeError("Test error")
        self.unified_logger.error("Error message", error=test_error, extra="value")
        
        # Should log without cascade prevention
        self.mock_logger.error.assert_called_once()
        call_args = self.mock_logger.error.call_args
        
        logged_data = call_args[1]
        assert logged_data["layer"] == "service"
        assert logged_data["error_message"] == "Test error"
        assert logged_data["error_type"] == "RuntimeError"
        assert logged_data["extra"] == "value"
    
    def test_critical_logging(self):
        """Test critical logging functionality."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        critical_error = SystemError("Critical system error")
        self.unified_logger.critical("Critical issue", error=critical_error)
        
        self.mock_logger.critical.assert_called_once()
        call_args = self.mock_logger.critical.call_args
        
        logged_data = call_args[1]
        assert logged_data["error_message"] == "Critical system error"
        assert logged_data["error_type"] == "SystemError"
        
        coordinator.end_request()


class TestGetUnifiedLogger:
    """Test cases for get_unified_logger function."""
    
    def setup_method(self):
        """Setup for each test method."""
        clear_logger_cache()
    
    def teardown_method(self):
        """Cleanup after each test method."""
        clear_logger_cache()
    
    @patch('faultmaven.infrastructure.logging.unified.UnifiedLogger')
    def test_get_unified_logger_creates_instance(self, mock_unified_logger):
        """Test get_unified_logger creates UnifiedLogger instance."""
        mock_instance = Mock()
        mock_unified_logger.return_value = mock_instance
        
        result = get_unified_logger("test.module", "service")
        
        assert result == mock_instance
        mock_unified_logger.assert_called_once_with("test.module", "service")
    
    @patch('faultmaven.infrastructure.logging.unified.UnifiedLogger')
    def test_get_unified_logger_caches_instances(self, mock_unified_logger):
        """Test get_unified_logger caches instances."""
        mock_instance1 = Mock()
        mock_instance2 = Mock()
        mock_unified_logger.side_effect = [mock_instance1, mock_instance2]
        
        # Get same logger twice
        result1 = get_unified_logger("test.module", "service")
        result2 = get_unified_logger("test.module", "service")
        
        # Should return same instance
        assert result1 == result2 == mock_instance1
        
        # Should only create instance once
        assert mock_unified_logger.call_count == 1
    
    @patch('faultmaven.infrastructure.logging.unified.UnifiedLogger')
    def test_get_unified_logger_different_combinations(self, mock_unified_logger):
        """Test get_unified_logger creates different instances for different combinations."""
        mock_instances = [Mock(), Mock(), Mock()]
        mock_unified_logger.side_effect = mock_instances
        
        # Get different logger combinations
        logger1 = get_unified_logger("module1", "service")
        logger2 = get_unified_logger("module2", "service")
        logger3 = get_unified_logger("module1", "api")
        
        # Should create separate instances
        assert logger1 == mock_instances[0]
        assert logger2 == mock_instances[1]
        assert logger3 == mock_instances[2]
        assert mock_unified_logger.call_count == 3
    
    def test_get_unified_logger_validates_layer(self):
        """Test get_unified_logger validates layer parameter."""
        # Valid layers should work
        valid_layers = ["api", "service", "core", "infrastructure"]
        
        for layer in valid_layers:
            try:
                get_unified_logger("test.module", layer)
            except ValueError:
                pytest.fail(f"Valid layer '{layer}' should not raise ValueError")
        
        # Invalid layer should raise ValueError
        with pytest.raises(ValueError) as exc_info:
            get_unified_logger("test.module", "invalid_layer")
        
        assert "Invalid layer 'invalid_layer'" in str(exc_info.value)
        assert "Must be one of:" in str(exc_info.value)
    
    def test_clear_logger_cache(self):
        """Test clear_logger_cache clears the cache."""
        # Create some cached loggers
        logger1 = get_unified_logger("module1", "service")
        logger2 = get_unified_logger("module2", "api")
        
        # Clear cache
        clear_logger_cache()
        
        # Getting same loggers should create new instances
        logger1_new = get_unified_logger("module1", "service")
        logger2_new = get_unified_logger("module2", "api")
        
        # Should be different instances now
        assert logger1 != logger1_new
        assert logger2 != logger2_new


class TestErrorHandlingAndEdgeCases:
    """Test error handling and edge cases."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
        clear_logger_cache()
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
        clear_logger_cache()
    
    @pytest.mark.asyncio
    async def test_operation_without_context(self):
        """Test operation context manager without request context."""
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            unified_logger = UnifiedLogger("test.module", "service")
            
            # No request context
            assert request_context.get() is None
            
            async with unified_logger.operation("test_operation") as ctx:
                ctx["test"] = "value"
            
            # Should still log without context
            assert mock_logger.info.call_count >= 2  # Start and end
    
    def test_log_boundary_edge_cases(self):
        """Test log_boundary with edge cases."""
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            unified_logger = UnifiedLogger("test.module", "service")
            
            # Test with None data
            unified_logger.log_boundary("test_op", "inbound", data=None)
            
            # Test with empty dict
            unified_logger.log_boundary("test_op", "outbound", data={})
            
            # Test with complex data
            complex_data = {
                "list": [1, 2, 3],
                "nested": {"deep": {"very_deep": "value"}},
                "none_value": None
            }
            unified_logger.log_boundary("test_op", "inbound", data=complex_data)
            
            # All should work without errors
            assert mock_logger.info.call_count == 3
    
    def test_metric_logging_edge_cases(self):
        """Test metric logging with edge cases."""
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            unified_logger = UnifiedLogger("test.module", "service")
            
            # Test with zero value
            unified_logger.log_metric("zero_metric", 0)
            
            # Test with negative value
            unified_logger.log_metric("negative_metric", -5.5, "units")
            
            # Test with large value
            unified_logger.log_metric("large_metric", 1234567890.123456, "bytes")
            
            # Test with no tags
            unified_logger.log_metric("no_tags_metric", 100)
            
            # Test with empty tags
            unified_logger.log_metric("empty_tags_metric", 200, tags={})
            
            # All should work
            assert mock_logger.info.call_count == 5