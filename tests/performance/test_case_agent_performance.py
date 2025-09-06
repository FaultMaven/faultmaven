"""Performance tests for case-to-agent integration.

This module tests response times, throughput, and resource usage
for the case-to-agent integration under various load conditions.
"""

import pytest
import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, AsyncMock, patch
import gc
import psutil
import os

from faultmaven.services.agent import AgentService  
from faultmaven.models import QueryRequest, AgentResponse, ResponseType
from faultmaven.exceptions import ServiceException


@pytest.fixture
def performance_agent_service():
    """AgentService configured for performance testing."""
    mock_llm = AsyncMock()
    
    # Simulate variable LLM response times
    async def mock_generate_with_delay(prompt, **kwargs):
        # Simulate realistic LLM processing time (100-500ms)
        await asyncio.sleep(0.1 + (len(prompt) % 5) * 0.08)
        return f"AI response to: {prompt[:50]}..."
    
    mock_llm.generate = mock_generate_with_delay
    
    mock_tracer = Mock()
    mock_tracer.trace = Mock()
    mock_tracer.trace.return_value.__enter__ = Mock()
    mock_tracer.trace.return_value.__exit__ = Mock(return_value=None)
    
    mock_sanitizer = Mock()
    mock_sanitizer.sanitize = Mock(side_effect=lambda x: x)  # Fast pass-through
    
    mock_session_service = AsyncMock()
    
    # Fast session operations
    async def fast_record_message(*args, **kwargs):
        await asyncio.sleep(0.01)  # 10ms simulation
    
    async def fast_format_context(session_id, case_id, limit=10):
        await asyncio.sleep(0.02)  # 20ms simulation
        return f"Context for {session_id}/{case_id}" if limit > 0 else ""
    
    mock_session_service.record_case_message = fast_record_message
    mock_session_service.format_conversation_context = fast_format_context
    
    return AgentService(
        llm_provider=mock_llm,
        tools=[],
        tracer=mock_tracer,
        sanitizer=mock_sanitizer,
        session_service=mock_session_service,
        settings=Mock()
    )


@pytest.fixture
def mock_case_service():
    """Fast mock case service for performance testing."""
    service = AsyncMock()
    
    async def fast_get_case(case_id, user_id=None):
        await asyncio.sleep(0.005)  # 5ms database simulation
        case = Mock()
        case.case_id = case_id
        case.title = f"Case {case_id}"
        case.message_count = 1
        return case
    
    async def fast_add_query(*args, **kwargs):
        await asyncio.sleep(0.003)  # 3ms update simulation
    
    service.get_case = fast_get_case
    service.add_case_query = fast_add_query
    service.add_assistant_response = AsyncMock()
    
    return service


@pytest.mark.performance
class TestCaseAgentPerformance:
    """Performance tests for case-agent integration."""
    
    @pytest.mark.asyncio
    async def test_single_query_response_time(self, performance_agent_service, mock_case_service):
        """Test response time for a single query meets SLA requirements."""
        case_id = "perf-case-001"
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            query = QueryRequest(
                query="What's causing the database connection timeouts?",
                session_id="perf-session-001",
                context={"source": "api"},
                priority="medium"
            )
            
            start_time = time.time()
            
            result = await performance_agent_service.process_query_for_case(case_id, query)
            
            end_time = time.time()
            response_time = end_time - start_time
            
            # Verify response quality
            assert isinstance(result, AgentResponse)
            assert result.response_type == ResponseType.ANSWER
            assert len(result.content) > 0
            
            # Performance requirement: < 3 seconds for standard queries
            assert response_time < 3.0, f"Response time {response_time:.2f}s exceeds 3.0s SLA"
            
            # Optimal performance: < 1 second
            if response_time < 1.0:
                print(f"✓ Excellent performance: {response_time:.3f}s")
            elif response_time < 2.0:
                print(f"✓ Good performance: {response_time:.3f}s") 
            else:
                print(f"⚠ Acceptable performance: {response_time:.3f}s")
    
    @pytest.mark.asyncio
    async def test_query_with_conversation_context_performance(self, performance_agent_service, mock_case_service):
        """Test performance impact of conversation context retrieval."""
        case_id = "perf-case-002"
        session_id = "perf-session-002"
        
        # Create long conversation context to test performance impact
        long_context = "\n".join([f"Message {i}: This is a long conversation message" for i in range(50)])
        
        performance_agent_service._session_service.format_conversation_context = AsyncMock(
            return_value=long_context
        )
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            query = QueryRequest(
                query="Continue the troubleshooting based on our previous conversation",
                session_id=session_id,
                context={"source": "api"},
                priority="medium"
            )
            
            start_time = time.time()
            
            result = await performance_agent_service.process_query_for_case(case_id, query)
            
            end_time = time.time()
            response_time = end_time - start_time
            
            assert isinstance(result, AgentResponse)
            
            # Context retrieval should not significantly impact performance
            # Allow extra time for context processing but still under 4 seconds
            assert response_time < 4.0, f"Context query time {response_time:.2f}s exceeds 4.0s limit"
    
    @pytest.mark.asyncio
    async def test_concurrent_queries_same_case_performance(self, performance_agent_service, mock_case_service):
        """Test performance under concurrent queries for the same case."""
        case_id = "perf-case-003"
        num_concurrent = 5
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            queries = [
                QueryRequest(
                    query=f"Concurrent query {i} about system performance",
                    session_id=f"perf-session-003-{i}",
                    context={"source": "api", "query_num": i},
                    priority="medium"
                )
                for i in range(num_concurrent)
            ]
            
            start_time = time.time()
            
            # Execute concurrent queries
            tasks = [
                performance_agent_service.process_query_for_case(case_id, query)
                for query in queries
            ]
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            end_time = time.time()
            total_time = end_time - start_time
            
            # Verify all queries succeeded
            successful_results = [r for r in results if isinstance(r, AgentResponse)]
            assert len(successful_results) == num_concurrent
            
            # Concurrent processing should be significantly faster than sequential
            # Sequential would take ~num_concurrent * avg_response_time
            # Concurrent should complete in roughly max(individual_response_times)
            assert total_time < num_concurrent * 2.0, f"Concurrent processing {total_time:.2f}s not efficient enough"
            
            # Calculate average response time per query
            avg_response_time = total_time / num_concurrent
            print(f"Concurrent queries average time: {avg_response_time:.3f}s per query")
    
    @pytest.mark.asyncio
    async def test_memory_usage_during_processing(self, performance_agent_service, mock_case_service):
        """Test memory usage during query processing."""
        case_id = "perf-case-004"
        
        # Force garbage collection before measuring
        gc.collect()
        
        # Get initial memory usage
        process = psutil.Process(os.getpid())
        initial_memory = process.memory_info().rss / 1024 / 1024  # MB
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            # Process multiple queries to see memory growth
            for i in range(10):
                query = QueryRequest(
                    query=f"Memory test query {i} with some content to process",
                    session_id=f"perf-session-004-{i}",
                    context={"source": "api", "iteration": i},
                    priority="medium"
                )
                
                await performance_agent_service.process_query_for_case(case_id, query)
                
                # Periodic garbage collection
                if i % 3 == 0:
                    gc.collect()
            
            # Force final garbage collection
            gc.collect()
            
            final_memory = process.memory_info().rss / 1024 / 1024  # MB
            memory_growth = final_memory - initial_memory
            
            print(f"Memory: {initial_memory:.1f}MB → {final_memory:.1f}MB (growth: {memory_growth:.1f}MB)")
            
            # Memory growth should be reasonable (< 50MB for 10 queries)
            assert memory_growth < 50, f"Memory growth {memory_growth:.1f}MB is excessive"
    
    @pytest.mark.asyncio
    async def test_large_query_processing_performance(self, performance_agent_service, mock_case_service):
        """Test performance with large query content."""
        case_id = "perf-case-005"
        
        # Create large query (simulating log dumps or large error reports)
        large_content = "ERROR: Database connection failed\n" * 1000  # ~34KB
        large_query = QueryRequest(
            query=f"Please analyze this error log:\n{large_content}",
            session_id="perf-session-005",
            context={"source": "api", "content_size": "large"},
            priority="high"
        )
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            start_time = time.time()
            
            result = await performance_agent_service.process_query_for_case(case_id, large_query)
            
            end_time = time.time()
            response_time = end_time - start_time
            
            assert isinstance(result, AgentResponse)
            
            # Large queries should still complete within reasonable time (10 seconds)
            assert response_time < 10.0, f"Large query time {response_time:.2f}s exceeds 10.0s limit"
            
            print(f"Large query ({len(large_content)} chars) processed in {response_time:.3f}s")
    
    @pytest.mark.asyncio
    async def test_error_handling_performance_impact(self, performance_agent_service, mock_case_service):
        """Test that error handling doesn't significantly impact performance."""
        case_id = "perf-case-006"
        
        # Configure service to have intermittent failures
        original_generate = performance_agent_service._llm.generate
        
        call_count = 0
        async def failing_generate(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 3 == 0:  # Fail every 3rd call
                raise Exception("Simulated LLM timeout")
            return await original_generate(prompt, **kwargs)
        
        performance_agent_service._llm.generate = failing_generate
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            results = []
            times = []
            
            # Process multiple queries, some will fail
            for i in range(9):  # Will have 3 failures
                query = QueryRequest(
                    query=f"Error handling test query {i}",
                    session_id=f"perf-session-006-{i}",
                    context={"source": "api"},
                    priority="medium"
                )
                
                start_time = time.time()
                
                try:
                    result = await performance_agent_service.process_query_for_case(case_id, query)
                    results.append(("success", result))
                except Exception as e:
                    results.append(("error", e))
                
                end_time = time.time()
                times.append(end_time - start_time)
            
            # Check that successful queries maintain good performance
            successful_times = [times[i] for i, (status, _) in enumerate(results) if status == "success"]
            error_times = [times[i] for i, (status, _) in enumerate(results) if status == "error"]
            
            if successful_times:
                avg_success_time = sum(successful_times) / len(successful_times)
                print(f"Average successful query time: {avg_success_time:.3f}s")
                assert avg_success_time < 3.0
            
            if error_times:
                avg_error_time = sum(error_times) / len(error_times)
                print(f"Average error handling time: {avg_error_time:.3f}s")
                # Error handling should be fast (not hanging)
                assert avg_error_time < 5.0
    
    @pytest.mark.asyncio
    async def test_session_service_failure_performance(self, performance_agent_service, mock_case_service):
        """Test performance when session service is slow or failing."""
        case_id = "perf-case-007"
        
        # Configure session service with delays
        async def slow_record_message(*args, **kwargs):
            await asyncio.sleep(0.5)  # 500ms delay
        
        async def slow_format_context(*args, **kwargs):
            await asyncio.sleep(0.3)  # 300ms delay
            return "Slow context"
        
        performance_agent_service._session_service.record_case_message = slow_record_message
        performance_agent_service._session_service.format_conversation_context = slow_format_context
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            query = QueryRequest(
                query="Test with slow session service",
                session_id="perf-session-007",
                context={"source": "api"},
                priority="medium"
            )
            
            start_time = time.time()
            
            result = await performance_agent_service.process_query_for_case(case_id, query)
            
            end_time = time.time()
            response_time = end_time - start_time
            
            assert isinstance(result, AgentResponse)
            
            # Even with slow session service, should complete reasonably fast
            # Session operations are not critical path for response generation
            assert response_time < 8.0, f"Query with slow session service took {response_time:.2f}s"
    
    @pytest.mark.asyncio
    async def test_throughput_capacity(self, performance_agent_service, mock_case_service):
        """Test system throughput under sustained load."""
        cases = [f"perf-case-throughput-{i}" for i in range(20)]
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            start_time = time.time()
            
            # Process queries in batches to simulate sustained load
            all_tasks = []
            for i, case_id in enumerate(cases):
                query = QueryRequest(
                    query=f"Throughput test query {i}",
                    session_id=f"throughput-session-{i}",
                    context={"source": "api", "batch": i // 5},
                    priority="medium"
                )
                
                task = performance_agent_service.process_query_for_case(case_id, query)
                all_tasks.append(task)
                
                # Add small delay between submissions to simulate realistic timing
                if i % 5 == 4:  # Every 5 queries
                    await asyncio.sleep(0.1)
            
            # Wait for all to complete
            results = await asyncio.gather(*all_tasks, return_exceptions=True)
            
            end_time = time.time()
            total_time = end_time - start_time
            
            successful_results = [r for r in results if isinstance(r, AgentResponse)]
            throughput = len(successful_results) / total_time
            
            print(f"Processed {len(successful_results)} queries in {total_time:.2f}s")
            print(f"Throughput: {throughput:.2f} queries/second")
            
            # Minimum throughput requirement (adjust based on system capacity)
            assert throughput > 2.0, f"Throughput {throughput:.2f} queries/sec is below minimum 2.0"
            
            # Verify quality wasn't sacrificed for speed
            assert len(successful_results) >= len(cases) * 0.9  # 90% success rate
    
    @pytest.mark.asyncio
    async def test_resource_cleanup_performance(self, performance_agent_service, mock_case_service):
        """Test that resources are properly cleaned up and don't accumulate."""
        case_id = "perf-case-cleanup"
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            # Track resource usage over multiple iterations
            initial_memory = psutil.Process().memory_info().rss
            memory_samples = [initial_memory]
            
            for iteration in range(50):
                query = QueryRequest(
                    query=f"Resource cleanup test iteration {iteration}",
                    session_id=f"cleanup-session-{iteration}",
                    context={"source": "api", "iteration": iteration},
                    priority="medium"
                )
                
                await performance_agent_service.process_query_for_case(case_id, query)
                
                # Sample memory every 10 iterations
                if iteration % 10 == 9:
                    gc.collect()
                    memory_samples.append(psutil.Process().memory_info().rss)
            
            # Check memory growth trend
            memory_growth = memory_samples[-1] - memory_samples[0]
            memory_growth_mb = memory_growth / 1024 / 1024
            
            print(f"Memory growth over 50 iterations: {memory_growth_mb:.1f}MB")
            
            # Should not have significant memory leak (< 20MB growth)
            assert memory_growth_mb < 20, f"Possible memory leak: {memory_growth_mb:.1f}MB growth"
            
            # Memory should stabilize (last samples shouldn't show continuous growth)
            recent_growth = memory_samples[-1] - memory_samples[-3] if len(memory_samples) >= 3 else 0
            recent_growth_mb = recent_growth / 1024 / 1024
            assert recent_growth_mb < 10, f"Memory still growing: {recent_growth_mb:.1f}MB in recent samples"