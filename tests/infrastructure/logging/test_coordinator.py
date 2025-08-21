"""
Test module for faultmaven.infrastructure.logging.coordinator
"""

import pytest
import uuid
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
import logging
import asyncio
from contextvars import copy_context

from faultmaven.infrastructure.logging.coordinator import (
    RequestContext,
    ErrorContext,
    PerformanceTracker,
    LoggingCoordinator,
    request_context
)


class TestRequestContext:
    """Test cases for RequestContext class."""
    
    def test_request_context_creation(self):
        """Test RequestContext creation with defaults."""
        ctx = RequestContext()
        
        # Check default values
        assert ctx.correlation_id is not None
        assert isinstance(ctx.correlation_id, str)
        assert len(ctx.correlation_id) > 0
        assert ctx.session_id is None
        assert ctx.user_id is None
        assert ctx.case_id is None
        assert ctx.agent_phase is None
        assert isinstance(ctx.start_time, datetime)
        assert isinstance(ctx.attributes, dict)
        assert len(ctx.attributes) == 0
        assert isinstance(ctx.logged_operations, set)
        assert len(ctx.logged_operations) == 0
        assert ctx.error_context is None
        assert ctx.performance_tracker is None
    
    def test_request_context_with_custom_values(self):
        """Test RequestContext creation with custom values."""
        custom_id = str(uuid.uuid4())
        custom_time = datetime.utcnow() - timedelta(minutes=1)
        custom_attrs = {"test": "value"}
        
        ctx = RequestContext(
            correlation_id=custom_id,
            session_id="session-123",
            user_id="user-456",
            case_id="case-789",
            agent_phase="define_blast_radius",
            start_time=custom_time,
            attributes=custom_attrs
        )
        
        assert ctx.correlation_id == custom_id
        assert ctx.session_id == "session-123"
        assert ctx.user_id == "user-456"
        assert ctx.case_id == "case-789"
        assert ctx.agent_phase == "define_blast_radius"
        assert ctx.start_time == custom_time
        assert ctx.attributes == custom_attrs
    
    def test_has_logged_operation(self):
        """Test operation logging tracking."""
        ctx = RequestContext()
        
        # Initially no operations logged
        assert not ctx.has_logged("test_operation")
        
        # Mark operation as logged
        ctx.mark_logged("test_operation")
        assert ctx.has_logged("test_operation")
        
        # Different operation should not be marked
        assert not ctx.has_logged("other_operation")
    
    def test_logged_operations_deduplication(self):
        """Test that logged operations track correctly and prevent duplicates."""
        ctx = RequestContext()
        
        # Mark same operation multiple times
        ctx.mark_logged("duplicate_op")
        ctx.mark_logged("duplicate_op")
        ctx.mark_logged("unique_op")
        
        # Should only contain unique operations
        assert len(ctx.logged_operations) == 2
        assert "duplicate_op" in ctx.logged_operations
        assert "unique_op" in ctx.logged_operations
        assert ctx.has_logged("duplicate_op")
        assert ctx.has_logged("unique_op")


class TestErrorContext:
    """Test cases for ErrorContext class."""
    
    def test_error_context_creation(self):
        """Test ErrorContext creation with defaults."""
        error_ctx = ErrorContext()
        
        assert error_ctx.original_error is None
        assert isinstance(error_ctx.layer_errors, dict)
        assert len(error_ctx.layer_errors) == 0
        assert error_ctx.recovery_attempts == 0
    
    def test_add_layer_error_first_error(self):
        """Test adding first error sets it as original."""
        error_ctx = ErrorContext()
        test_error = ValueError("Test error")
        
        error_ctx.add_layer_error("service", test_error)
        
        assert error_ctx.original_error == test_error
        assert "service" in error_ctx.layer_errors
        assert error_ctx.layer_errors["service"]["error"] == "Test error"
        assert error_ctx.layer_errors["service"]["type"] == "ValueError"
        assert "timestamp" in error_ctx.layer_errors["service"]
    
    def test_add_multiple_layer_errors(self):
        """Test adding multiple layer errors."""
        error_ctx = ErrorContext()
        service_error = ValueError("Service error")
        api_error = RuntimeError("API error")
        
        error_ctx.add_layer_error("service", service_error)
        error_ctx.add_layer_error("api", api_error)
        
        # Original error should be the first one
        assert error_ctx.original_error == service_error
        assert len(error_ctx.layer_errors) == 2
        
        assert error_ctx.layer_errors["service"]["error"] == "Service error"
        assert error_ctx.layer_errors["service"]["type"] == "ValueError"
        assert error_ctx.layer_errors["api"]["error"] == "API error"
        assert error_ctx.layer_errors["api"]["type"] == "RuntimeError"
    
    def test_should_log_error_cascade_prevention(self):
        """Test error cascade prevention logic."""
        error_ctx = ErrorContext()
        test_error = ValueError("Test error")
        
        # Initially, all layers should log
        assert error_ctx.should_log_error("service")
        assert error_ctx.should_log_error("api")
        assert error_ctx.should_log_error("core")
        
        # After adding error to service layer, service should not log again
        error_ctx.add_layer_error("service", test_error)
        assert not error_ctx.should_log_error("service")
        assert error_ctx.should_log_error("api")
        assert error_ctx.should_log_error("core")
    
    def test_should_log_error_during_recovery(self):
        """Test that errors can be logged during recovery attempts."""
        error_ctx = ErrorContext()
        test_error = ValueError("Test error")
        
        # Add error to service layer
        error_ctx.add_layer_error("service", test_error)
        assert not error_ctx.should_log_error("service")
        
        # During recovery, service should be able to log again
        error_ctx.recovery_attempts = 1
        assert error_ctx.should_log_error("service")


class TestPerformanceTracker:
    """Test cases for PerformanceTracker class."""
    
    def test_performance_tracker_creation(self):
        """Test PerformanceTracker initialization."""
        tracker = PerformanceTracker()
        
        assert isinstance(tracker.layer_timings, dict)
        assert len(tracker.layer_timings) == 0
        
        # Check default thresholds
        expected_thresholds = {
            'api': 0.1,
            'service': 0.5,
            'core': 0.3,
            'infrastructure': 1.0
        }
        assert tracker.thresholds == expected_thresholds
    
    def test_record_timing_within_threshold(self):
        """Test recording timing that doesn't exceed threshold."""
        tracker = PerformanceTracker()
        
        # Record API timing within threshold
        exceeds, threshold = tracker.record_timing("api", "test_operation", 0.05)
        
        assert not exceeds
        assert threshold == 0.1
        assert tracker.layer_timings["api.test_operation"] == 0.05
    
    def test_record_timing_exceeds_threshold(self):
        """Test recording timing that exceeds threshold."""
        tracker = PerformanceTracker()
        
        # Record API timing that exceeds threshold
        exceeds, threshold = tracker.record_timing("api", "slow_operation", 0.2)
        
        assert exceeds
        assert threshold == 0.1
        assert tracker.layer_timings["api.slow_operation"] == 0.2
    
    def test_record_timing_unknown_layer(self):
        """Test recording timing for unknown layer uses default threshold."""
        tracker = PerformanceTracker()
        
        # Record timing for unknown layer
        exceeds, threshold = tracker.record_timing("unknown", "test_op", 0.5)
        
        assert not exceeds  # 0.5 < 1.0 (default)
        assert threshold == 1.0
        assert tracker.layer_timings["unknown.test_op"] == 0.5
        
        # Test exceeding default threshold
        exceeds, threshold = tracker.record_timing("unknown", "slow_op", 1.5)
        assert exceeds
        assert threshold == 1.0
    
    def test_multiple_timings_same_layer(self):
        """Test recording multiple timings for same layer."""
        tracker = PerformanceTracker()
        
        tracker.record_timing("service", "op1", 0.1)
        tracker.record_timing("service", "op2", 0.3)
        tracker.record_timing("service", "op3", 0.7)  # Exceeds threshold
        
        assert len(tracker.layer_timings) == 3
        assert tracker.layer_timings["service.op1"] == 0.1
        assert tracker.layer_timings["service.op2"] == 0.3
        assert tracker.layer_timings["service.op3"] == 0.7


class TestLoggingCoordinator:
    """Test cases for LoggingCoordinator class."""
    
    def setup_method(self):
        """Setup for each test method."""
        # Clear any existing context
        request_context.set(None)
        self.coordinator = LoggingCoordinator()
    
    def teardown_method(self):
        """Cleanup after each test method."""
        # Ensure context is cleared
        request_context.set(None)
    
    def test_coordinator_creation(self):
        """Test LoggingCoordinator initialization."""
        coordinator = LoggingCoordinator()
        assert coordinator.context is None
    
    def test_start_request_creates_context(self):
        """Test start_request creates and sets context."""
        coordinator = LoggingCoordinator()
        
        ctx = coordinator.start_request(
            session_id="test-session",
            user_id="test-user"
        )
        
        assert ctx is not None
        assert isinstance(ctx, RequestContext)
        assert ctx.session_id == "test-session"
        assert ctx.user_id == "test-user"
        assert ctx.error_context is not None
        assert ctx.performance_tracker is not None
        assert coordinator.context == ctx
        assert request_context.get() == ctx
    
    def test_start_request_generates_correlation_id(self):
        """Test start_request generates unique correlation ID."""
        coordinator = LoggingCoordinator()
        
        ctx1 = coordinator.start_request()
        coordinator.end_request()  # Clean up
        
        coordinator2 = LoggingCoordinator()
        ctx2 = coordinator2.start_request()
        
        assert ctx1.correlation_id != ctx2.correlation_id
        assert len(ctx1.correlation_id) > 0
        assert len(ctx2.correlation_id) > 0
    
    def test_end_request_generates_summary(self):
        """Test end_request generates request summary."""
        coordinator = LoggingCoordinator()
        
        # Start request
        ctx = coordinator.start_request()
        ctx.attributes["user_id"] = "test-user"  # Add to attributes
        
        # Add some logged operations
        ctx.mark_logged("operation1")
        ctx.mark_logged("operation2")
        
        # Add some errors
        ctx.error_context.add_layer_error("service", ValueError("Test error"))
        
        # Add performance violations
        ctx.performance_tracker.record_timing("api", "slow_op", 0.2)  # Exceeds 0.1s threshold
        
        # End request
        summary = coordinator.end_request()
        
        assert isinstance(summary, dict)
        assert summary["correlation_id"] == ctx.correlation_id
        assert "duration_seconds" in summary
        assert summary["operations_logged"] == 2
        assert summary["errors_encountered"] == 1
        assert summary["performance_violations"] == 1
        assert summary["user_id"] == "test-user"
        
        # Context should be cleared
        assert coordinator.context is None
        assert request_context.get() is None
    
    def test_end_request_without_context(self):
        """Test end_request returns empty dict when no context."""
        coordinator = LoggingCoordinator()
        
        summary = coordinator.end_request()
        assert summary == {}
    
    def test_get_context_static_method(self):
        """Test get_context static method."""
        # Initially no context
        assert LoggingCoordinator.get_context() is None
        
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        
        # Should return current context
        assert LoggingCoordinator.get_context() == ctx
        
        coordinator.end_request()
        
        # Should return None after end
        assert LoggingCoordinator.get_context() is None
    
    @pytest.mark.asyncio
    async def test_log_once_prevents_duplicate_logging(self):
        """Test log_once prevents duplicate log entries."""
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        
        # Create mock logger
        mock_logger = Mock()
        mock_logger.info = Mock()
        
        # Log same operation multiple times
        LoggingCoordinator.log_once(
            "test_op",
            mock_logger,
            "info",
            "Test message",
            extra_field="test"
        )
        
        LoggingCoordinator.log_once(
            "test_op",
            mock_logger,
            "info",
            "Test message duplicate",
            extra_field="duplicate"
        )
        
        # Should only be called once
        assert mock_logger.info.call_count == 1
        
        # Check that operation is marked as logged
        assert ctx.has_logged("test_op")
        
        coordinator.end_request()
    
    def test_log_once_different_operations(self):
        """Test log_once allows different operations."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        # Create mock logger
        mock_logger = Mock()
        mock_logger.info = Mock()
        
        # Log different operations
        LoggingCoordinator.log_once(
            "operation1",
            mock_logger,
            "info",
            "Message 1"
        )
        
        LoggingCoordinator.log_once(
            "operation2",
            mock_logger,
            "info",
            "Message 2"
        )
        
        # Should be called twice for different operations
        assert mock_logger.info.call_count == 2
        
        coordinator.end_request()
    
    def test_log_once_without_context(self):
        """Test log_once behavior without request context."""
        # No context set
        assert request_context.get() is None
        
        # Create mock logger
        mock_logger = Mock()
        mock_logger.info = Mock()
        
        # Without context, log_once should not log anything (safety measure)
        LoggingCoordinator.log_once(
            "test_op",
            mock_logger,
            "info",
            "Test message"
        )
        
        LoggingCoordinator.log_once(
            "test_op", 
            mock_logger,
            "info",
            "Test message duplicate"
        )
        
        # Without context, nothing should be logged (no context to track deduplication)
        assert mock_logger.info.call_count == 0
    
    def test_log_once_different_log_levels(self):
        """Test log_once with different log levels."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        # Create mock logger with different level methods
        mock_logger = Mock()
        mock_logger.debug = Mock()
        mock_logger.warning = Mock()
        mock_logger.error = Mock()
        mock_logger.critical = Mock()
        
        # Test different log levels
        LoggingCoordinator.log_once("debug_op", mock_logger, "debug", "Debug message")
        LoggingCoordinator.log_once("warn_op", mock_logger, "warning", "Warning message")
        LoggingCoordinator.log_once("error_op", mock_logger, "error", "Error message")
        LoggingCoordinator.log_once("crit_op", mock_logger, "critical", "Critical message")
        
        # Each method should be called once
        mock_logger.debug.assert_called_once()
        mock_logger.warning.assert_called_once()
        mock_logger.error.assert_called_once()
        mock_logger.critical.assert_called_once()
        
        coordinator.end_request()
    
    def test_log_once_invalid_level_fallback(self):
        """Test log_once with invalid level falls back to info."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        mock_logger = Mock()
        mock_logger.info = Mock()
        # Mock invalid level method to not exist
        del mock_logger.invalid_level  # Make sure it doesn't exist
        
        # Use invalid log level - should fallback to info
        LoggingCoordinator.log_once(
            "test_op",
            mock_logger,
            "invalid_level",
            "Test message"
        )
        
        # Should fallback to info method
        mock_logger.info.assert_called_once()
        
        coordinator.end_request()


class TestContextIsolation:
    """Test context isolation in concurrent scenarios."""
    
    def teardown_method(self):
        """Cleanup after each test."""
        request_context.set(None)
    
    @pytest.mark.asyncio
    async def test_context_isolation_between_requests(self):
        """Test that contexts are isolated between concurrent requests."""
        coordinator1 = LoggingCoordinator()
        coordinator2 = LoggingCoordinator()
        
        async def request1():
            ctx1 = coordinator1.start_request()
            ctx1.attributes["user_id"] = "user1"
            ctx1.mark_logged("operation1")
            
            # Simulate some work
            await asyncio.sleep(0.01)
            
            # Check that we still have our context
            current_ctx = LoggingCoordinator.get_context()
            assert current_ctx.attributes.get("user_id") == "user1"
            assert current_ctx.has_logged("operation1")
            assert not current_ctx.has_logged("operation2")
            
            return coordinator1.end_request()
        
        async def request2():
            ctx2 = coordinator2.start_request()
            ctx2.attributes["user_id"] = "user2"
            ctx2.mark_logged("operation2")
            
            # Simulate some work
            await asyncio.sleep(0.01)
            
            # Check that we have our own context
            current_ctx = LoggingCoordinator.get_context()
            assert current_ctx.attributes.get("user_id") == "user2"
            assert current_ctx.has_logged("operation2")
            assert not current_ctx.has_logged("operation1")
            
            return coordinator2.end_request()
        
        # Run both requests concurrently
        results = await asyncio.gather(request1(), request2())
        
        # Verify results
        summary1, summary2 = results
        assert summary1["user_id"] == "user1"
        assert summary2["user_id"] == "user2"
        assert summary1["operations_logged"] == 1
        assert summary2["operations_logged"] == 1
        assert summary1["correlation_id"] != summary2["correlation_id"]
    
    def test_context_copy_behavior(self):
        """Test context behavior when copied."""
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        ctx.attributes["user_id"] = "test-user"
        ctx.mark_logged("original_operation")
        
        # Copy the context
        def check_context():
            current_ctx = LoggingCoordinator.get_context()
            if current_ctx:
                return {
                    "user_id": current_ctx.attributes.get("user_id"),
                    "has_original": current_ctx.has_logged("original_operation"),
                    "correlation_id": current_ctx.correlation_id
                }
            return None
        
        # Run in copied context
        ctx_copy = copy_context()
        result = ctx_copy.run(check_context)
        
        # Should have access to the same context
        assert result is not None
        assert result["user_id"] == "test-user"
        assert result["has_original"] == True
        assert result["correlation_id"] == ctx.correlation_id
        
        coordinator.end_request()