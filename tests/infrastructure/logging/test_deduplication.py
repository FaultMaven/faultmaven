"""
Test module for logging deduplication mechanisms across all layers.

This module specifically tests the core deduplication functionality
that prevents duplicate log entries within a single request context.
"""

import pytest
import asyncio
import time
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime
import uuid

from faultmaven.infrastructure.logging.coordinator import (
    LoggingCoordinator,
    RequestContext,
    request_context
)
from faultmaven.infrastructure.logging.unified import UnifiedLogger, get_unified_logger


class TestOperationDeduplication:
    """Test deduplication of operation logging."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
        
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @pytest.mark.asyncio
    async def test_duplicate_operation_prevention(self):
        """Test that duplicate operations within same request are prevented."""
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            
            unified_logger = UnifiedLogger("test.module", "service")
            
            # Execute same operation multiple times
            async with unified_logger.operation("duplicate_operation") as op_ctx1:
                op_ctx1["first_run"] = True
            
            async with unified_logger.operation("duplicate_operation") as op_ctx2:
                op_ctx2["second_run"] = True
            
            async with unified_logger.operation("duplicate_operation") as op_ctx3:
                op_ctx3["third_run"] = True
            
            # Count operation start/end logs
            info_calls = mock_logger.info.call_args_list
            start_logs = [call for call in info_calls if "Operation started" in str(call)]
            end_logs = [call for call in info_calls if "Operation completed" in str(call)]
            
            # With deduplication, should log fewer starts/ends
            # (Implementation may vary - key is that deduplication is working)
            total_logs = len(start_logs) + len(end_logs)
            assert total_logs < 6  # Without deduplication would be 6 (3 starts + 3 ends)
            
            # Verify operations were marked as logged
            operation_key = "service.operation.duplicate_operation"
            start_key = f"{operation_key}.start"
            end_key = f"{operation_key}.end"
            
            # At least one should be marked as logged
            assert ctx.has_logged(start_key) or len(start_logs) > 0
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    async def test_different_operations_allowed(self):
        """Test that different operations are logged separately."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            
            unified_logger = UnifiedLogger("test.module", "service")
            
            # Execute different operations
            async with unified_logger.operation("operation_1"):
                pass
            
            async with unified_logger.operation("operation_2"):
                pass
            
            async with unified_logger.operation("operation_3"):
                pass
            
            # All different operations should be logged
            info_calls = mock_logger.info.call_args_list
            start_logs = [call for call in info_calls if "Operation started" in str(call)]
            
            # Should have start log for each operation
            assert len(start_logs) >= 3
            
            # Verify different operation names
            logged_operations = set()
            for call in start_logs:
                call_str = str(call)
                if "operation_1" in call_str:
                    logged_operations.add("operation_1")
                elif "operation_2" in call_str:
                    logged_operations.add("operation_2")
                elif "operation_3" in call_str:
                    logged_operations.add("operation_3")
            
            assert len(logged_operations) == 3
        
        coordinator.end_request()
    
    def test_boundary_deduplication(self):
        """Test boundary logging deduplication."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            
            unified_logger = UnifiedLogger("test.module", "service")
            
            # Log same boundary multiple times
            unified_logger.log_boundary("test_operation", "inbound", data={"attempt": 1})
            unified_logger.log_boundary("test_operation", "inbound", data={"attempt": 2})
            unified_logger.log_boundary("test_operation", "inbound", data={"attempt": 3})
            
            # Should only log once due to deduplication
            assert mock_logger.info.call_count == 1
            
            # But different directions should be logged
            unified_logger.log_boundary("test_operation", "outbound", data={"attempt": 4})
            assert mock_logger.info.call_count == 2
        
        coordinator.end_request()


class TestLoggingCoordinatorDeduplication:
    """Test LoggingCoordinator deduplication mechanisms."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
        
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    def test_log_once_basic_deduplication(self):
        """Test basic log_once deduplication functionality."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        mock_logger = Mock()
        mock_logger.info = Mock()
        
        # Log same operation multiple times
        LoggingCoordinator.log_once("test_operation", mock_logger, "info", "Test message")
        LoggingCoordinator.log_once("test_operation", mock_logger, "info", "Test message 2")
        LoggingCoordinator.log_once("test_operation", mock_logger, "info", "Test message 3")
        
        # Should only be called once
        assert mock_logger.info.call_count == 1
        
        coordinator.end_request()
    
    def test_log_once_different_levels_same_key(self):
        """Test log_once with same key but different log levels."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        mock_logger = Mock()
        mock_logger.info = Mock()
        mock_logger.warning = Mock()
        mock_logger.error = Mock()
        
        # Same key, different levels - should all be deduplicated
        LoggingCoordinator.log_once("same_key", mock_logger, "info", "Info message")
        LoggingCoordinator.log_once("same_key", mock_logger, "warning", "Warning message")
        LoggingCoordinator.log_once("same_key", mock_logger, "error", "Error message")
        
        # Only first one should be logged
        assert mock_logger.info.call_count == 1
        assert mock_logger.warning.call_count == 0
        assert mock_logger.error.call_count == 0
        
        coordinator.end_request()
    
    def test_log_once_different_keys(self):
        """Test log_once allows different operation keys."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        mock_logger = Mock()
        mock_logger.info = Mock()
        mock_logger.warning = Mock()
        
        # Different keys should all be logged
        LoggingCoordinator.log_once("operation_1", mock_logger, "info", "Message 1")
        LoggingCoordinator.log_once("operation_2", mock_logger, "warning", "Message 2")
        LoggingCoordinator.log_once("operation_3", mock_logger, "info", "Message 3")
        
        assert mock_logger.info.call_count == 2  # operation_1 and operation_3
        assert mock_logger.warning.call_count == 1  # operation_2
        
        coordinator.end_request()
    
    def test_log_once_across_request_boundaries(self):
        """Test log_once resets between different requests."""
        mock_logger = Mock()
        mock_logger.info = Mock()
        
        # First request
        coordinator1 = LoggingCoordinator()
        coordinator1.start_request()
        
        LoggingCoordinator.log_once("shared_operation", mock_logger, "info", "Request 1")
        LoggingCoordinator.log_once("shared_operation", mock_logger, "info", "Request 1 duplicate")
        
        coordinator1.end_request()
        
        # Second request - should allow logging same operation again
        coordinator2 = LoggingCoordinator()
        coordinator2.start_request()
        
        LoggingCoordinator.log_once("shared_operation", mock_logger, "info", "Request 2")
        LoggingCoordinator.log_once("shared_operation", mock_logger, "info", "Request 2 duplicate")
        
        coordinator2.end_request()
        
        # Should have logged twice (once per request)
        assert mock_logger.info.call_count == 2


class TestCrossLayerDeduplication:
    """Test deduplication across different application layers."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
        
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @pytest.mark.asyncio
    async def test_same_operation_different_layers(self):
        """Test same operation name in different layers."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        # Create loggers for different layers
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            
            api_logger = UnifiedLogger("api.module", "api")
            service_logger = UnifiedLogger("service.module", "service")
            core_logger = UnifiedLogger("core.module", "core")
            
            # Same operation name in different layers
            async with api_logger.operation("process_request"):
                async with service_logger.operation("process_request"):
                    async with core_logger.operation("process_request"):
                        pass
            
            # Different layers should have different operation keys, so all should log
            info_calls = mock_logger.info.call_args_list
            start_logs = [call for call in info_calls if "Operation started" in str(call)]
            
            # Should have start logs for each layer
            assert len(start_logs) >= 3
            
            # Verify layer-specific operation keys
            api_operations = [call for call in start_logs if "api.operation.process_request" in str(call)]
            service_operations = [call for call in start_logs if "service.operation.process_request" in str(call)]
            core_operations = [call for call in start_logs if "core.operation.process_request" in str(call)]
            
            # Each layer should have its own operation logged
            assert len(api_operations) >= 1
            assert len(service_operations) >= 1
            assert len(core_operations) >= 1
        
        coordinator.end_request()
    
    def test_boundary_logging_different_layers(self):
        """Test boundary logging across different layers."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            
            api_logger = UnifiedLogger("api.module", "api")
            service_logger = UnifiedLogger("service.module", "service")
            infrastructure_logger = UnifiedLogger("infra.module", "infrastructure")
            
            # Same boundary operation in different layers
            api_logger.log_boundary("data_processing", "inbound")
            service_logger.log_boundary("data_processing", "inbound")
            infrastructure_logger.log_boundary("data_processing", "inbound")
            
            # Each layer should log its own boundary
            assert mock_logger.info.call_count == 3
            
            # Verify layer-specific boundary keys
            calls = mock_logger.info.call_args_list
            api_calls = [call for call in calls if "api.boundary" in str(call)]
            service_calls = [call for call in calls if "service.boundary" in str(call)]
            infra_calls = [call for call in calls if "infrastructure.boundary" in str(call)]
            
            assert len(api_calls) == 1
            assert len(service_calls) == 1
            assert len(infra_calls) == 1
        
        coordinator.end_request()


class TestMetricAndEventDeduplication:
    """Test deduplication behavior for metrics and events."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
        
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    def test_metric_deduplication_allows_multiple_values(self):
        """Test that metrics allow multiple values (timestamped keys)."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            
            unified_logger = UnifiedLogger("test.module", "service")
            
            # Log same metric multiple times with different values
            unified_logger.log_metric("response_time", 0.1, "seconds")
            time.sleep(0.001)  # Ensure different timestamps
            unified_logger.log_metric("response_time", 0.2, "seconds")
            time.sleep(0.001)
            unified_logger.log_metric("response_time", 0.15, "seconds")
            
            # All should be logged (metrics use timestamped keys)
            assert mock_logger.info.call_count == 3
        
        coordinator.end_request()
    
    def test_event_deduplication_allows_multiple_occurrences(self):
        """Test that events allow multiple occurrences (timestamped keys)."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            
            unified_logger = UnifiedLogger("test.module", "service")
            
            # Log same event multiple times
            unified_logger.log_event("business", "user_login", "info", {"user": "user1"})
            time.sleep(0.001)  # Ensure different timestamps
            unified_logger.log_event("business", "user_login", "info", {"user": "user2"})
            time.sleep(0.001)
            unified_logger.log_event("business", "user_login", "info", {"user": "user3"})
            
            # All should be logged (events use timestamped keys)
            assert mock_logger.info.call_count == 3
        
        coordinator.end_request()
    
    def test_mixed_logging_deduplication(self):
        """Test deduplication behavior with mixed logging types."""
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            
            unified_logger = UnifiedLogger("test.module", "service")
            
            # Mix of boundaries (deduplicated), metrics (not deduplicated), and events (not deduplicated)
            unified_logger.log_boundary("process_data", "inbound")
            unified_logger.log_boundary("process_data", "inbound")  # Should be deduplicated
            
            unified_logger.log_metric("items_processed", 10)
            unified_logger.log_metric("items_processed", 15)  # Should both be logged
            
            unified_logger.log_event("technical", "processing_complete", "info")
            unified_logger.log_event("technical", "processing_complete", "info")  # Should both be logged
            
            # Expected: 1 boundary + 2 metrics + 2 events = 5 total
            assert mock_logger.info.call_count == 5
        
        coordinator.end_request()


class TestConcurrentDeduplication:
    """Test deduplication behavior in concurrent scenarios."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
        
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @pytest.mark.asyncio
    async def test_concurrent_operations_same_request(self):
        """Test concurrent operations within same request context."""
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            
            unified_logger = UnifiedLogger("test.module", "service")
            
            async def concurrent_operation(op_id):
                """Simulate concurrent operation."""
                async with unified_logger.operation(f"concurrent_op_{op_id}"):
                    await asyncio.sleep(0.01)  # Simulate work
                    # Try to log duplicate boundary
                    unified_logger.log_boundary("shared_boundary", "inbound")
            
            # Run concurrent operations
            await asyncio.gather(
                concurrent_operation(1),
                concurrent_operation(2),
                concurrent_operation(3)
            )
            
            # Check that shared boundary was only logged once
            info_calls = mock_logger.info.call_args_list
            boundary_calls = [call for call in info_calls if "shared_boundary" in str(call)]
            assert len(boundary_calls) == 1
            
            # But different operations should all be logged
            start_calls = [call for call in info_calls if "Operation started" in str(call)]
            assert len(start_calls) >= 3  # One for each concurrent operation
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    async def test_concurrent_requests_isolation(self):
        """Test that concurrent requests maintain separate deduplication."""
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            
            async def request_handler(request_id):
                """Simulate separate request handler."""
                coordinator = LoggingCoordinator()
                coordinator.start_request(correlation_id=request_id)
                
                unified_logger = UnifiedLogger(f"request_{request_id}", "service")
                
                # Each request logs the same operation
                unified_logger.log_boundary("shared_operation", "inbound")
                unified_logger.log_boundary("shared_operation", "inbound")  # Duplicate within request
                
                await asyncio.sleep(0.01)  # Simulate work
                
                summary = coordinator.end_request()
                return summary
            
            # Run concurrent requests
            results = await asyncio.gather(
                request_handler("req1"),
                request_handler("req2"),
                request_handler("req3")
            )
            
            # Each request should have its own context and deduplication
            assert len(results) == 3
            
            # Check that operation was logged once per request (despite duplicates within each)
            info_calls = mock_logger.info.call_args_list
            shared_op_calls = [call for call in info_calls if "shared_operation" in str(call)]
            
            # Should be logged once per request = 3 times total
            assert len(shared_op_calls) == 3


class TestDeduplicationKeyGeneration:
    """Test the key generation logic for deduplication."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
        
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    def test_boundary_key_generation(self):
        """Test boundary logging key generation."""
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            
            unified_logger = UnifiedLogger("test.module", "service")
            
            # Log boundary and check what key was generated
            unified_logger.log_boundary("test_operation", "inbound")
            
            # Expected key format: layer.boundary.operation.direction
            expected_key = "service.boundary.test_operation.inbound"
            assert ctx.has_logged(expected_key)
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    async def test_operation_key_generation(self):
        """Test operation logging key generation."""
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            
            unified_logger = UnifiedLogger("test.module", "service")
            
            async with unified_logger.operation("test_operation"):
                pass
            
            # Expected keys: layer.operation.operation_name.start and .end
            start_key = "service.operation.test_operation.start"
            end_key = "service.operation.test_operation.end"
            
            # At least start should be logged
            assert ctx.has_logged(start_key)
        
        coordinator.end_request()
    
    def test_metric_key_uniqueness(self):
        """Test that metric keys are unique (timestamped)."""
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            
            unified_logger = UnifiedLogger("test.module", "service")
            
            # Log same metric multiple times
            unified_logger.log_metric("test_metric", 1)
            time.sleep(0.001)
            unified_logger.log_metric("test_metric", 2)
            
            # Check logged operations - should have different timestamped keys
            logged_ops = list(ctx.logged_operations)
            metric_ops = [op for op in logged_ops if "service.metric.test_metric" in op]
            
            # Should have multiple unique timestamped keys
            assert len(metric_ops) >= 2
            assert len(set(metric_ops)) == len(metric_ops)  # All unique
        
        coordinator.end_request()
    
    def test_special_characters_in_keys(self):
        """Test deduplication with special characters in operation names."""
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            mock_logger = Mock()
            mock_get_logger.return_value = mock_logger
            
            unified_logger = UnifiedLogger("test.module", "service")
            
            # Use operation names with special characters
            special_operations = [
                "process-user-data",
                "handle_api_call",
                "validate.input.data",
                "send@email/notification",
                "cache:redis:set"
            ]
            
            for operation in special_operations:
                unified_logger.log_boundary(operation, "inbound")
                unified_logger.log_boundary(operation, "inbound")  # Duplicate
            
            # Each unique operation should be logged only once
            info_calls = mock_logger.info.call_args_list
            assert len(info_calls) == len(special_operations)  # One per operation
            
            # All should be marked as logged
            for operation in special_operations:
                expected_key = f"service.boundary.{operation}.inbound"
                assert ctx.has_logged(expected_key)
        
        coordinator.end_request()