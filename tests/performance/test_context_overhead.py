"""
Test module for context variable performance overhead.

This module tests the performance impact of context variables used
for request-scoped logging coordination.
"""

import pytest
import asyncio
import time
from contextvars import ContextVar, copy_context
from concurrent.futures import ThreadPoolExecutor
from typing import List
import threading

from faultmaven.infrastructure.logging.coordinator import (
    LoggingCoordinator,
    RequestContext,
    request_context
)


class TestContextVariablePerformance:
    """Test context variable performance overhead."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    def test_context_variable_access_speed(self):
        """Test speed of context variable access operations."""
        # Set up context
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        
        iterations = 10000
        
        # Measure context variable get operations
        start_time = time.perf_counter()
        
        for _ in range(iterations):
            current_ctx = request_context.get()
            assert current_ctx is not None
        
        end_time = time.perf_counter()
        get_time = end_time - start_time
        
        # Measure context variable set operations
        start_time = time.perf_counter()
        
        for i in range(iterations):
            # Create new context for each set
            new_ctx = RequestContext(correlation_id=f"test-{i}")
            request_context.set(new_ctx)
        
        end_time = time.perf_counter()
        set_time = end_time - start_time
        
        coordinator.end_request()
        
        # Context variable operations should be very fast
        get_time_per_op = (get_time / iterations) * 1000000  # Convert to microseconds
        set_time_per_op = (set_time / iterations) * 1000000  # Convert to microseconds
        
        assert get_time_per_op < 10, f"Context get too slow: {get_time_per_op:.1f}μs"
        assert set_time_per_op < 50, f"Context set too slow: {set_time_per_op:.1f}μs"
    
    @pytest.mark.asyncio
    async def test_async_context_propagation_overhead(self):
        """Test overhead of context propagation in async scenarios."""
        coordinator = LoggingCoordinator()
        coordinator.start_request(test_id="async_propagation")
        
        async def async_operation(depth: int):
            """Recursive async operation to test context propagation."""
            # Access context at each level
            ctx = request_context.get()
            assert ctx is not None
            assert ctx.attributes.get("test_id") == "async_propagation"
            
            if depth > 0:
                await asyncio.sleep(0.001)  # Small delay
                return await async_operation(depth - 1)
            else:
                return "completed"
        
        max_depth = 50
        iterations = 20
        
        # Measure async context propagation
        start_time = time.perf_counter()
        
        tasks = [async_operation(max_depth) for _ in range(iterations)]
        results = await asyncio.gather(*tasks)
        
        end_time = time.perf_counter()
        total_time = end_time - start_time
        
        # Verify all operations completed
        assert len(results) == iterations
        assert all(r == "completed" for r in results)
        
        # Calculate overhead per operation
        expected_work_time = iterations * (max_depth + 1) * 0.001  # Expected sleep time
        context_overhead = total_time - expected_work_time
        overhead_percentage = (context_overhead / total_time) * 100
        
        coordinator.end_request()
        
        # Context propagation overhead should be reasonable
        assert overhead_percentage < 200, f"Async context propagation overhead too high: {overhead_percentage:.1f}%"
    
    def test_context_copying_performance(self):
        """Test performance of context copying operations."""
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        
        # Add some data to context
        for i in range(10):
            ctx.mark_logged(f"operation_{i}")
        
        iterations = 1000
        
        # Measure context copying
        start_time = time.perf_counter()
        
        copied_contexts = []
        for _ in range(iterations):
            ctx_copy = copy_context()
            copied_contexts.append(ctx_copy)
        
        end_time = time.perf_counter()
        copy_time = end_time - start_time
        
        # Measure context execution in copied contexts
        start_time = time.perf_counter()
        
        def check_context():
            current_ctx = request_context.get()
            return current_ctx.correlation_id if current_ctx else None
        
        results = []
        for ctx_copy in copied_contexts[:100]:  # Test subset to avoid timeout
            result = ctx_copy.run(check_context)
            results.append(result)
        
        end_time = time.perf_counter()
        execution_time = end_time - start_time
        
        coordinator.end_request()
        
        # Verify context copying worked
        assert len(copied_contexts) == iterations
        assert len(results) == 100
        assert all(r == ctx.correlation_id for r in results)
        
        # Context operations should be reasonably fast
        copy_time_per_op = (copy_time / iterations) * 1000  # Convert to ms
        exec_time_per_op = (execution_time / 100) * 1000  # Convert to ms
        
        assert copy_time_per_op < 1.0, f"Context copying too slow: {copy_time_per_op:.3f}ms"
        assert exec_time_per_op < 1.0, f"Context execution too slow: {exec_time_per_op:.3f}ms"
    
    def test_concurrent_context_access(self):
        """Test concurrent context access performance."""
        num_threads = 10
        operations_per_thread = 100
        
        def thread_worker(thread_id: int) -> List[str]:
            """Worker function for each thread."""
            results = []
            
            # Each thread creates its own context
            coordinator = LoggingCoordinator()
            ctx = coordinator.start_request(thread_id=f"thread_{thread_id}")
            
            try:
                for i in range(operations_per_thread):
                    # Perform context operations
                    ctx.mark_logged(f"thread_{thread_id}_op_{i}")
                    
                    # Access context
                    current_ctx = request_context.get()
                    assert current_ctx is not None
                    assert current_ctx.attributes.get("thread_id") == f"thread_{thread_id}"
                    
                    results.append(current_ctx.correlation_id)
                
                # Verify all operations in this thread used same context
                assert len(set(results)) == 1  # All should have same correlation ID
                
                return results
                
            finally:
                coordinator.end_request()
        
        # Measure concurrent context operations
        start_time = time.perf_counter()
        
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(thread_worker, i) for i in range(num_threads)]
            thread_results = [future.result() for future in futures]
        
        end_time = time.perf_counter()
        total_time = end_time - start_time
        
        # Verify results
        assert len(thread_results) == num_threads
        
        # Each thread should have consistent context
        for thread_result in thread_results:
            assert len(thread_result) == operations_per_thread
            assert len(set(thread_result)) == 1  # All same correlation ID within thread
        
        # Different threads should have different contexts
        correlation_ids = [results[0] for results in thread_results]
        assert len(set(correlation_ids)) == num_threads  # All different
        
        # Performance should be reasonable
        total_operations = num_threads * operations_per_thread
        time_per_operation = (total_time / total_operations) * 1000  # Convert to ms
        
        assert time_per_operation < 1.0, f"Concurrent context access too slow: {time_per_operation:.3f}ms per op"
    
    @pytest.mark.asyncio
    async def test_context_isolation_performance(self):
        """Test performance of context isolation between async tasks."""
        num_tasks = 20
        operations_per_task = 50
        
        async def isolated_task(task_id: int) -> dict:
            """Async task with isolated context."""
            coordinator = LoggingCoordinator()
            ctx = coordinator.start_request(task_id=f"task_{task_id}")
            
            operation_times = []
            
            try:
                for i in range(operations_per_task):
                    start = time.perf_counter()
                    
                    # Context operations
                    ctx.mark_logged(f"task_{task_id}_op_{i}")
                    current_ctx = request_context.get()
                    
                    # Verify isolation
                    assert current_ctx is not None
                    assert current_ctx.attributes.get("task_id") == f"task_{task_id}"
                    
                    end = time.perf_counter()
                    operation_times.append((end - start) * 1000)  # Convert to ms
                    
                    # Small delay to allow context switching
                    await asyncio.sleep(0.001)
                
                summary = coordinator.end_request()
                
                return {
                    "task_id": task_id,
                    "correlation_id": ctx.correlation_id,
                    "operations_logged": summary["operations_logged"],
                    "avg_operation_time": sum(operation_times) / len(operation_times),
                    "max_operation_time": max(operation_times)
                }
                
            except Exception as e:
                coordinator.end_request()
                raise
        
        # Measure isolated async tasks
        start_time = time.perf_counter()
        
        results = await asyncio.gather(*[isolated_task(i) for i in range(num_tasks)])
        
        end_time = time.perf_counter()
        total_time = end_time - start_time
        
        # Verify isolation and performance
        assert len(results) == num_tasks
        
        # Each task should have unique correlation ID
        correlation_ids = [r["correlation_id"] for r in results]
        assert len(set(correlation_ids)) == num_tasks
        
        # Each task should have completed all operations
        for result in results:
            assert result["operations_logged"] == operations_per_task
        
        # Calculate performance metrics
        avg_task_time = total_time / num_tasks
        total_operations = num_tasks * operations_per_task
        avg_operation_time = (total_time / total_operations) * 1000  # Convert to ms
        
        # Performance should be reasonable
        assert avg_task_time < 1.0, f"Average task time too slow: {avg_task_time*1000:.1f}ms"
        assert avg_operation_time < 0.5, f"Average operation time too slow: {avg_operation_time:.3f}ms"
        
        # Individual operation times should be consistent
        individual_avg_times = [r["avg_operation_time"] for r in results]
        max_individual_avg = max(individual_avg_times)
        min_individual_avg = min(individual_avg_times)
        time_variance = max_individual_avg - min_individual_avg
        
        # Variance in operation times should be reasonable (not excessive)
        assert time_variance < 1.0, f"High variance in operation times: {time_variance:.3f}ms"


class TestContextMemoryEfficiency:
    """Test memory efficiency of context variables."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    def test_context_memory_usage_scaling(self):
        """Test context memory usage with increasing data."""
        import gc
        import sys
        
        # Baseline memory measurement
        gc.collect()
        
        def measure_context_memory(num_operations: int) -> int:
            """Measure memory usage with given number of operations."""
            coordinator = LoggingCoordinator()
            ctx = coordinator.start_request()
            
            # Add operations to context
            for i in range(num_operations):
                ctx.mark_logged(f"operation_{i}")
                # Add some attributes
                ctx.attributes[f"attr_{i % 100}"] = f"value_{i}"
            
            # Measure objects
            gc.collect()
            object_count = len(gc.get_objects())
            
            coordinator.end_request()
            return object_count
        
        # Test with increasing operation counts
        operation_counts = [100, 500, 1000, 2000]
        memory_measurements = []
        
        for count in operation_counts:
            memory_usage = measure_context_memory(count)
            memory_measurements.append((count, memory_usage))
            
            # Clean up between measurements
            gc.collect()
        
        # Memory should scale reasonably (not exponentially)
        for i in range(1, len(memory_measurements)):
            prev_count, prev_memory = memory_measurements[i-1]
            curr_count, curr_memory = memory_measurements[i]
            
            count_ratio = curr_count / prev_count
            memory_ratio = curr_memory / prev_memory if prev_memory > 0 else 1
            
            # Memory growth should not be excessive compared to data growth
            assert memory_ratio <= count_ratio * 2, f"Memory growth too high: {memory_ratio:.2f}x for {count_ratio:.2f}x data"
    
    def test_context_cleanup_effectiveness(self):
        """Test that context cleanup prevents memory leaks."""
        import gc
        import weakref
        
        # Collect references to contexts for cleanup testing
        context_refs = []
        
        def create_and_use_context(context_id: int):
            """Create, use, and cleanup context."""
            coordinator = LoggingCoordinator()
            ctx = coordinator.start_request(context_id=context_id)
            
            # Create weak reference to track cleanup
            ctx_ref = weakref.ref(ctx)
            context_refs.append(ctx_ref)
            
            # Use context extensively
            for i in range(100):
                ctx.mark_logged(f"context_{context_id}_op_{i}")
                ctx.attributes[f"data_{i}"] = f"value_{i}"
            
            summary = coordinator.end_request()
            return summary["operations_logged"]
        
        # Create many contexts
        num_contexts = 50
        for i in range(num_contexts):
            operations_count = create_and_use_context(i)
            assert operations_count == 100
            
            # Force garbage collection periodically
            if i % 10 == 0:
                gc.collect()
        
        # Force final garbage collection
        gc.collect()
        
        # Check that contexts were cleaned up
        active_contexts = sum(1 for ref in context_refs if ref() is not None)
        
        # Most contexts should be garbage collected
        cleanup_percentage = (num_contexts - active_contexts) / num_contexts * 100
        assert cleanup_percentage > 80, f"Poor context cleanup: only {cleanup_percentage:.1f}% cleaned up"
    
    @pytest.mark.asyncio
    async def test_async_context_memory_efficiency(self):
        """Test memory efficiency in async context scenarios."""
        import gc
        
        async def async_context_worker(worker_id: int):
            """Async worker that uses context extensively."""
            coordinator = LoggingCoordinator()
            ctx = coordinator.start_request(worker_id=worker_id)
            
            try:
                # Simulate async work with context usage
                for i in range(50):
                    ctx.mark_logged(f"worker_{worker_id}_async_op_{i}")
                    
                    # Access context frequently
                    current_ctx = request_context.get()
                    assert current_ctx is not None
                    
                    # Small async delay
                    await asyncio.sleep(0.001)
                
                summary = coordinator.end_request()
                return summary["operations_logged"]
                
            except Exception:
                coordinator.end_request()
                raise
        
        # Measure memory before async operations
        gc.collect()
        initial_objects = len(gc.get_objects())
        
        # Run many async workers
        num_workers = 30
        results = await asyncio.gather(*[
            async_context_worker(i) for i in range(num_workers)
        ])
        
        # Verify all workers completed
        assert len(results) == num_workers
        assert all(r == 50 for r in results)
        
        # Measure memory after operations
        gc.collect()
        final_objects = len(gc.get_objects())
        
        # Memory growth should be reasonable
        memory_growth = final_objects - initial_objects
        memory_per_worker = memory_growth / num_workers
        
        # Should not have excessive memory growth per worker
        assert memory_per_worker < 100, f"Excessive memory per async worker: {memory_per_worker} objects"


class TestContextVariableEdgeCases:
    """Test edge cases and stress scenarios for context variables."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    def test_rapid_context_switching(self):
        """Test performance with rapid context switching."""
        num_contexts = 100
        switches_per_context = 50
        
        contexts = []
        
        # Create multiple contexts
        for i in range(num_contexts):
            coordinator = LoggingCoordinator()
            ctx = coordinator.start_request(context_id=i)
            contexts.append((coordinator, ctx))
        
        try:
            # Measure rapid context switching
            start_time = time.perf_counter()
            
            for switch in range(switches_per_context):
                for i, (coordinator, ctx) in enumerate(contexts):
                    request_context.set(ctx)
                    
                    # Perform some context operations
                    current_ctx = request_context.get()
                    assert current_ctx.attributes.get("context_id") == i
                    
                    ctx.mark_logged(f"switch_{switch}_context_{i}")
            
            end_time = time.perf_counter()
            total_time = end_time - start_time
            
            # Calculate performance
            total_switches = num_contexts * switches_per_context
            time_per_switch = (total_time / total_switches) * 1000000  # Convert to microseconds
            
            # Rapid context switching should be fast
            assert time_per_switch < 100, f"Context switching too slow: {time_per_switch:.1f}μs per switch"
            
        finally:
            # Clean up all contexts
            for coordinator, ctx in contexts:
                coordinator.end_request()
    
    @pytest.mark.asyncio
    async def test_context_under_high_concurrency(self):
        """Test context performance under high concurrency."""
        concurrent_tasks = 100
        operations_per_task = 20
        
        async def high_concurrency_task(task_id: int):
            """Task designed for high concurrency testing."""
            coordinator = LoggingCoordinator()
            ctx = coordinator.start_request(task_id=task_id)
            
            try:
                for i in range(operations_per_task):
                    # Rapid context operations
                    ctx.mark_logged(f"task_{task_id}_op_{i}")
                    
                    # Check context consistency
                    current_ctx = request_context.get()
                    assert current_ctx.attributes.get("task_id") == task_id
                    
                    # Minimal delay to allow task switching
                    if i % 5 == 0:
                        await asyncio.sleep(0.0001)
                
                return task_id
                
            finally:
                coordinator.end_request()
        
        # Run high concurrency test
        start_time = time.perf_counter()
        
        results = await asyncio.gather(*[
            high_concurrency_task(i) for i in range(concurrent_tasks)
        ])
        
        end_time = time.perf_counter()
        total_time = end_time - start_time
        
        # Verify all tasks completed correctly
        assert len(results) == concurrent_tasks
        assert sorted(results) == list(range(concurrent_tasks))
        
        # Performance should be reasonable even under high concurrency
        avg_task_time = (total_time / concurrent_tasks) * 1000  # Convert to ms
        total_operations = concurrent_tasks * operations_per_task
        avg_operation_time = (total_time / total_operations) * 1000000  # Convert to μs
        
        assert avg_task_time < 100, f"High concurrency task time too slow: {avg_task_time:.1f}ms"
        assert avg_operation_time < 500, f"High concurrency operation time too slow: {avg_operation_time:.1f}μs"
    
    def test_context_with_large_data(self):
        """Test context performance with large data payloads."""
        coordinator = LoggingCoordinator()
        ctx = coordinator.start_request()
        
        # Add large amounts of data to context
        large_operations_count = 5000
        large_attributes_count = 1000
        
        start_time = time.perf_counter()
        
        # Add many operations
        for i in range(large_operations_count):
            ctx.mark_logged(f"large_operation_{i}")
        
        # Add many attributes
        for i in range(large_attributes_count):
            ctx.attributes[f"large_attr_{i}"] = f"large_value_{i}" * 10  # Make values larger
        
        # Test context access performance with large data
        for i in range(100):
            current_ctx = request_context.get()
            assert current_ctx is not None
            
            # Test operation checking
            assert current_ctx.has_logged(f"large_operation_{i}")
            
            # Test attribute access
            assert current_ctx.attributes.get(f"large_attr_{i % large_attributes_count}") is not None
        
        end_time = time.perf_counter()
        total_time = end_time - start_time
        
        coordinator.end_request()
        
        # Even with large data, operations should be reasonably fast
        time_per_check = (total_time / 100) * 1000  # Convert to ms
        assert time_per_check < 10, f"Context access with large data too slow: {time_per_check:.3f}ms per check"