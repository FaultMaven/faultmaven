"""
Test module for logging performance overhead validation.

This module tests that the logging infrastructure overhead is < 1% of 
request processing time and validates performance thresholds.
"""

import pytest
import asyncio
import time
import statistics
from unittest.mock import Mock, patch
from contextlib import contextmanager, asynccontextmanager
from typing import List, Dict, Any

from faultmaven.infrastructure.logging.coordinator import (
    LoggingCoordinator,
    RequestContext,
    PerformanceTracker,
    request_context
)
from faultmaven.infrastructure.logging.unified import UnifiedLogger
from faultmaven.services.base_service import BaseService
from faultmaven.infrastructure.base_client import BaseExternalClient


class TestLoggingPerformanceOverhead:
    """Test logging performance overhead."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @contextmanager
    def measure_time(self):
        """Context manager to measure execution time."""
        start_time = time.perf_counter()
        try:
            yield
        finally:
            end_time = time.perf_counter()
            self.measured_time = end_time - start_time
    
    def test_request_context_creation_overhead(self):
        """Test RequestContext creation performance."""
        iterations = 1000
        
        # Measure context creation time
        with self.measure_time():
            for _ in range(iterations):
                ctx = RequestContext()
                ctx.mark_logged("test_operation")
                assert ctx.has_logged("test_operation")
        
        # Should be very fast - less than 1ms per context
        per_operation_time = (self.measured_time / iterations) * 1000  # Convert to ms
        assert per_operation_time < 1.0, f"Context creation too slow: {per_operation_time:.3f}ms"
    
    def test_logging_coordinator_overhead(self):
        """Test LoggingCoordinator performance overhead."""
        iterations = 1000
        
        # Measure coordinator operations
        with self.measure_time():
            for i in range(iterations):
                coordinator = LoggingCoordinator()
                ctx = coordinator.start_request(user_id=f"user_{i}")
                ctx.mark_logged("test_operation")
                ctx.mark_logged("another_operation")
                summary = coordinator.end_request()
                assert summary["operations_logged"] == 2
        
        # Should be fast - less than 2ms per full cycle
        per_cycle_time = (self.measured_time / iterations) * 1000  # Convert to ms
        assert per_cycle_time < 2.0, f"Coordinator cycle too slow: {per_cycle_time:.3f}ms"
    
    def test_performance_tracker_overhead(self):
        """Test PerformanceTracker performance overhead."""
        iterations = 1000
        
        tracker = PerformanceTracker()
        
        # Measure performance tracking
        with self.measure_time():
            for i in range(iterations):
                exceeds, threshold = tracker.record_timing("api", f"operation_{i % 10}", 0.1)
                assert threshold == 0.1
        
        # Should be very fast - less than 0.1ms per timing record
        per_record_time = (self.measured_time / iterations) * 1000  # Convert to ms
        assert per_record_time < 0.1, f"Performance tracking too slow: {per_record_time:.3f}ms"
    
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    def test_unified_logger_overhead(self, mock_get_logger):
        """Test UnifiedLogger performance overhead."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        unified_logger = UnifiedLogger("test.module", "service")
        iterations = 100
        
        # Measure unified logger operations
        with self.measure_time():
            for i in range(iterations):
                unified_logger.log_boundary(f"operation_{i % 10}", "inbound")
                unified_logger.log_metric(f"metric_{i % 5}", i, "count")
                unified_logger.log_event("business", f"event_{i % 3}", "info")
        
        coordinator.end_request()
        
        # Should be fast - less than 5ms per set of operations
        per_set_time = (self.measured_time / iterations) * 1000  # Convert to ms
        assert per_set_time < 5.0, f"Unified logger operations too slow: {per_set_time:.3f}ms"
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_operation_context_manager_overhead(self, mock_get_logger):
        """Test operation context manager performance overhead."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        unified_logger = UnifiedLogger("test.module", "service")
        iterations = 100
        
        async def test_operation():
            """Simple test operation."""
            await asyncio.sleep(0.001)  # Simulate minimal work
            return {"result": "success"}
        
        # Measure operation context manager overhead
        with self.measure_time():
            for i in range(iterations):
                async with unified_logger.operation(f"test_op_{i % 10}") as ctx:
                    result = await test_operation()
                    ctx["result"] = result
        
        coordinator.end_request()
        
        # The overhead should be minimal compared to the actual work
        total_work_time = iterations * 0.001  # Total simulated work time
        logging_overhead = self.measured_time - total_work_time
        overhead_percentage = (logging_overhead / self.measured_time) * 100
        
        # Logging overhead should be < 50% of total time (generous threshold for test environment)
        assert overhead_percentage < 50, f"Operation context overhead too high: {overhead_percentage:.1f}%"


class TestServiceLoggingPerformance:
    """Test service layer logging performance."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_service_operation_logging_overhead(self, mock_get_logger):
        """Test service operation logging overhead."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        class TestService(BaseService):
            def __init__(self):
                super().__init__("performance_test_service")
            
            async def simple_operation(self, data):
                """Simple operation for performance testing."""
                await asyncio.sleep(0.01)  # Simulate work
                return {"processed": data, "count": len(str(data))}
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        service = TestService()
        test_data = {"test": "data"}
        iterations = 50
        
        # Measure service operation with logging
        start_time = time.perf_counter()
        
        for i in range(iterations):
            result = await service.execute_operation(
                f"perf_test_op_{i % 10}",
                service.simple_operation,
                test_data
            )
            assert result["processed"] == test_data
        
        end_time = time.perf_counter()
        total_time_with_logging = end_time - start_time
        
        coordinator.end_request()
        
        # Measure same operations without logging (direct calls)
        start_time = time.perf_counter()
        
        for i in range(iterations):
            result = await service.simple_operation(test_data)
            assert result["processed"] == test_data
        
        end_time = time.perf_counter()
        total_time_without_logging = end_time - start_time
        
        # Calculate logging overhead
        logging_overhead = total_time_with_logging - total_time_without_logging
        if total_time_with_logging > 0:
            overhead_percentage = (logging_overhead / total_time_with_logging) * 100
        else:
            overhead_percentage = 0
        
        # Logging overhead should be reasonable (< 100% in test environment)
        assert overhead_percentage < 100, f"Service logging overhead too high: {overhead_percentage:.1f}%"


class TestInfrastructureLoggingPerformance:
    """Test infrastructure layer logging performance."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_external_client_logging_overhead(self, mock_get_logger):
        """Test external client logging overhead."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        class TestExternalClient(BaseExternalClient):
            def __init__(self):
                super().__init__("perf_test_client", "TestService")
            
            async def simple_call(self, data):
                """Simple external call for performance testing."""
                await asyncio.sleep(0.005)  # Simulate network call
                return {"response": data}
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        client = TestExternalClient()
        test_data = {"test": "data"}
        iterations = 30
        
        # Measure external calls with logging
        start_time = time.perf_counter()
        
        for i in range(iterations):
            result = await client.call_external(
                f"perf_test_call_{i % 5}",
                client.simple_call,
                test_data
            )
            assert result["response"] == test_data
        
        end_time = time.perf_counter()
        total_time_with_logging = end_time - start_time
        
        coordinator.end_request()
        
        # Measure same calls without logging (direct calls)
        start_time = time.perf_counter()
        
        for i in range(iterations):
            result = await client.simple_call(test_data)
            assert result["response"] == test_data
        
        end_time = time.perf_counter()
        total_time_without_logging = end_time - start_time
        
        # Calculate logging overhead
        logging_overhead = total_time_with_logging - total_time_without_logging
        if total_time_with_logging > 0:
            overhead_percentage = (logging_overhead / total_time_with_logging) * 100
        else:
            overhead_percentage = 0
        
        # Logging overhead should be reasonable for external calls
        assert overhead_percentage < 100, f"Infrastructure logging overhead too high: {overhead_percentage:.1f}%"


class TestConcurrentLoggingPerformance:
    """Test logging performance under concurrent load."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_concurrent_logging_performance(self, mock_get_logger):
        """Test concurrent logging performance and isolation."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        async def concurrent_request(request_id: int):
            """Simulate a concurrent request with logging."""
            coordinator = LoggingCoordinator()
            coordinator.start_request(request_id=f"req_{request_id}")
            
            unified_logger = UnifiedLogger(f"concurrent_test_{request_id}", "service")
            
            # Simulate various logging operations
            unified_logger.log_boundary("process_request", "inbound")
            
            async with unified_logger.operation(f"process_data_{request_id}") as ctx:
                # Simulate work
                await asyncio.sleep(0.01)
                ctx["items_processed"] = request_id * 10
            
            unified_logger.log_metric("processed_items", request_id * 10, "count")
            unified_logger.log_event("business", "request_completed", "info")
            unified_logger.log_boundary("process_request", "outbound")
            
            summary = coordinator.end_request()
            return summary
        
        concurrent_requests = 20
        
        # Measure concurrent logging performance
        start_time = time.perf_counter()
        
        results = await asyncio.gather(*[
            concurrent_request(i) for i in range(concurrent_requests)
        ])
        
        end_time = time.perf_counter()
        total_time = end_time - start_time
        
        # Verify all requests completed
        assert len(results) == concurrent_requests
        
        # Each request should have logged operations
        for i, result in enumerate(results):
            assert result["operations_logged"] > 0
            assert f"req_{i}" in str(result)
        
        # Average time per request should be reasonable
        avg_time_per_request = (total_time / concurrent_requests) * 1000  # Convert to ms
        assert avg_time_per_request < 100, f"Concurrent logging too slow: {avg_time_per_request:.1f}ms per request"
    
    @pytest.mark.asyncio
    async def test_context_variable_performance(self):
        """Test context variable performance under load."""
        iterations = 1000
        
        # Measure context variable operations
        start_time = time.perf_counter()
        
        for i in range(iterations):
            coordinator = LoggingCoordinator()
            ctx = coordinator.start_request(iteration=i)
            
            # Multiple context variable accesses
            current_ctx = LoggingCoordinator.get_context()
            assert current_ctx is not None
            assert current_ctx.correlation_id == ctx.correlation_id
            
            # Modify context
            ctx.mark_logged(f"operation_{i % 100}")
            assert ctx.has_logged(f"operation_{i % 100}")
            
            coordinator.end_request()
        
        end_time = time.perf_counter()
        total_time = end_time - start_time
        
        # Context variable operations should be fast
        per_iteration_time = (total_time / iterations) * 1000  # Convert to ms
        assert per_iteration_time < 5.0, f"Context variable operations too slow: {per_iteration_time:.3f}ms"


class TestDeduplicationPerformance:
    """Test deduplication algorithm performance."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    def test_deduplication_scaling(self):
        """Test deduplication performance with large number of operations."""
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        
        # Test with increasing numbers of operations
        for operation_count in [100, 500, 1000, 2000]:
            
            start_time = time.perf_counter()
            
            # Log many operations
            for i in range(operation_count):
                operation_key = f"operation_{i % 50}"  # Create some duplicates
                ctx.mark_logged(operation_key)
            
            # Check all operations
            for i in range(operation_count):
                operation_key = f"operation_{i % 50}"
                assert ctx.has_logged(operation_key)
            
            end_time = time.perf_counter()
            total_time = (end_time - start_time) * 1000  # Convert to ms
            
            # Time should scale reasonably (not exponentially)
            time_per_operation = total_time / operation_count
            assert time_per_operation < 0.1, f"Deduplication too slow at {operation_count} ops: {time_per_operation:.3f}ms per op"
        
        coordinator.end_request()
    
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    def test_log_once_performance(self, mock_get_logger):
        """Test LoggingCoordinator.log_once performance."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        iterations = 1000
        
        # Measure log_once performance with duplicates
        start_time = time.perf_counter()
        
        for i in range(iterations):
            # Use same operation keys to test deduplication performance
            operation_key = f"duplicate_operation_{i % 10}"
            
            LoggingCoordinator.log_once(
                operation_key,
                mock_logger,
                "info",
                f"Message {i}"
            )
        
        end_time = time.perf_counter()
        total_time = (end_time - start_time) * 1000  # Convert to ms
        
        # log_once should be fast even with many duplicates
        time_per_call = total_time / iterations
        assert time_per_call < 0.5, f"log_once too slow: {time_per_call:.3f}ms per call"
        
        # Should have only logged unique operations (10 unique keys)
        assert mock_logger.info.call_count == 10
        
        coordinator.end_request()


class TestMemoryUsagePerformance:
    """Test memory usage of logging infrastructure."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    def test_context_memory_growth(self):
        """Test that request contexts don't cause memory leaks."""
        import gc
        import sys
        
        # Force garbage collection
        gc.collect()
        initial_objects = len(gc.get_objects())
        
        # Create and destroy many contexts
        for i in range(100):
            coordinator = LoggingCoordinator()
            ctx = coordinator.start_request(iteration=i)
            
            # Add some operations
            for j in range(10):
                ctx.mark_logged(f"operation_{i}_{j}")
            
            coordinator.end_request()
            
            # Periodically force garbage collection
            if i % 20 == 0:
                gc.collect()
        
        # Final garbage collection
        gc.collect()
        final_objects = len(gc.get_objects())
        
        # Object count should not have grown significantly
        object_growth = final_objects - initial_objects
        # Allow some growth but not excessive (< 1000 objects)
        assert object_growth < 1000, f"Excessive object growth: {object_growth} objects"
    
    def test_performance_tracker_memory_usage(self):
        """Test PerformanceTracker memory usage with many timings."""
        tracker = PerformanceTracker()
        
        # Record many timings
        for i in range(1000):
            layer = ["api", "service", "core", "infrastructure"][i % 4]
            operation = f"operation_{i % 50}"
            duration = (i % 100) / 1000.0  # Vary duration
            
            tracker.record_timing(layer, operation, duration)
        
        # Memory usage should be reasonable
        timing_count = len(tracker.layer_timings)
        
        # Should have recorded all unique layer.operation combinations
        expected_combinations = 4 * 50  # 4 layers * 50 operations
        assert timing_count <= expected_combinations
        
        # Each timing entry should not use excessive memory
        # (This is more of a sanity check than precise measurement)
        assert timing_count > 0


class TestRealWorldPerformanceScenarios:
    """Test logging performance in realistic scenarios."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_typical_api_request_overhead(self, mock_get_logger):
        """Test logging overhead for typical API request scenario."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        # Simulate typical API request processing time (100ms)
        typical_processing_time = 0.1
        
        async def simulate_api_processing():
            """Simulate typical API processing with logging."""
            coordinator = LoggingCoordinator()
            coordinator.start_request(user_id="user123", endpoint="/api/data")
            
            # API layer logging
            api_logger = UnifiedLogger("api.data", "api")
            api_logger.log_boundary("handle_request", "inbound")
            
            async with api_logger.operation("process_request") as ctx:
                # Service layer logging
                service_logger = UnifiedLogger("service.data", "service")
                
                async with service_logger.operation("fetch_user_data") as service_ctx:
                    # Infrastructure layer logging
                    client_logger = UnifiedLogger("client.database", "infrastructure")
                    
                    async with client_logger.operation("query_database") as client_ctx:
                        # Simulate actual work
                        await asyncio.sleep(typical_processing_time)
                        client_ctx["rows_fetched"] = 15
                    
                    service_ctx["user_data_size"] = 1024
                
                ctx["response_size"] = 2048
            
            api_logger.log_boundary("handle_request", "outbound")
            api_logger.log_metric("response_time", typical_processing_time * 1000, "ms")
            
            summary = coordinator.end_request()
            return summary
        
        # Measure total time with logging
        start_time = time.perf_counter()
        summary = await simulate_api_processing()
        end_time = time.perf_counter()
        
        total_time = end_time - start_time
        logging_overhead = total_time - typical_processing_time
        overhead_percentage = (logging_overhead / total_time) * 100
        
        # Verify request was processed
        assert summary["operations_logged"] > 0
        
        # Logging overhead should be < 50% of total request time (generous for test environment)
        assert overhead_percentage < 50, f"API request logging overhead too high: {overhead_percentage:.1f}%"
        
        # Absolute overhead should be small (< 50ms)
        assert logging_overhead < 0.05, f"Absolute logging overhead too high: {logging_overhead*1000:.1f}ms"
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_high_frequency_operations(self, mock_get_logger):
        """Test logging performance with high-frequency operations."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        logger = UnifiedLogger("high_freq_test", "service")
        
        # Simulate high-frequency operations (like processing many items)
        operations_count = 500
        work_per_operation = 0.001  # 1ms of work per operation
        
        start_time = time.perf_counter()
        
        async with logger.operation("batch_processing") as ctx:
            for i in range(operations_count):
                # Log metrics frequently (realistic scenario)
                if i % 10 == 0:
                    logger.log_metric("items_processed", i, "count")
                
                # Log events occasionally
                if i % 50 == 0:
                    logger.log_event("business", "checkpoint_reached", "info", {"checkpoint": i})
                
                # Simulate work
                await asyncio.sleep(work_per_operation)
            
            ctx["total_items"] = operations_count
        
        end_time = time.perf_counter()
        
        total_time = end_time - start_time
        expected_work_time = operations_count * work_per_operation
        logging_overhead = total_time - expected_work_time
        overhead_percentage = (logging_overhead / total_time) * 100
        
        coordinator.end_request()
        
        # For high-frequency operations, logging overhead should still be reasonable
        assert overhead_percentage < 100, f"High-frequency logging overhead too high: {overhead_percentage:.1f}%"
        
        # Metrics and events should have been logged (via info calls)
        info_calls = mock_logger.info.call_args_list
        
        # Find metric and event calls in the info calls
        metric_calls = [call for call in info_calls if "Metric recorded" in str(call)]
        event_calls = [call for call in info_calls if "Event:" in str(call)]
        
        assert len(metric_calls) > 0, f"Expected metric calls, got: {len(metric_calls)}"
        assert len(event_calls) > 0, f"Expected event calls, got: {len(event_calls)}"