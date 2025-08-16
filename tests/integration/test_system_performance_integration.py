"""System Performance Integration Tests

This module contains comprehensive performance tests for the integrated Phase 2
system, validating performance under load, concurrent operations, and stress
conditions. These tests ensure the system can handle production-level workloads
while maintaining acceptable response times and resource usage.

Key Test Areas:
- Concurrent workflow execution performance
- Memory service performance under load
- Knowledge service throughput testing
- End-to-end system performance benchmarks
- Resource utilization and memory management
- Scalability testing with increasing load
- Performance regression detection
"""

import asyncio
import gc
import time
import psutil
import os
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from unittest.mock import Mock, AsyncMock, patch
from concurrent.futures import ThreadPoolExecutor, as_completed
import pytest

from faultmaven.services.orchestration_service import OrchestrationService
from faultmaven.services.enhanced_knowledge_service import EnhancedKnowledgeService
from faultmaven.services.memory_service import MemoryService
from faultmaven.services.planning_service import PlanningService
from faultmaven.services.reasoning_service import ReasoningService
from faultmaven.core.orchestration.troubleshooting_orchestrator import WorkflowContext
from faultmaven.models.interfaces import IMemoryService, IPlanningService, ILLMProvider, IVectorStore


@pytest.fixture
async def performance_mock_vector_store():
    """High-performance mock vector store for load testing"""
    vector_store = Mock()
    
    # Simulate realistic search latency
    async def mock_search(query, k=10, **kwargs):
        # Simulate processing time (10-50ms)
        await asyncio.sleep(0.01 + (hash(query) % 40) / 1000)
        
        return [
            {
                "id": f"doc_{i}_{hash(query) % 1000}",
                "content": f"Performance test document {i} for query: {query[:50]}...",
                "metadata": {
                    "source": f"perf_doc_{i}.md",
                    "type": "performance_test",
                    "relevance": 0.9 - (i * 0.1)
                },
                "score": 0.9 - (i * 0.1)
            }
            for i in range(min(k, 5))
        ]
    
    vector_store.search = AsyncMock(side_effect=mock_search)
    return vector_store


@pytest.fixture
async def performance_mock_llm():
    """High-performance mock LLM for load testing"""
    llm = Mock()
    
    async def mock_generate(prompt, context=None, **kwargs):
        # Simulate realistic LLM response time (100-300ms)
        processing_time = 0.1 + (hash(prompt) % 200) / 1000
        await asyncio.sleep(processing_time)
        
        return {
            "response": f"AI analysis for: {prompt[:100]}...",
            "confidence": 0.8 + (hash(prompt) % 20) / 100,
            "reasoning": f"Analysis based on {len(prompt)} characters of input",
            "processing_time": processing_time
        }
    
    llm.generate = AsyncMock(side_effect=mock_generate)
    return llm


@pytest.fixture
async def performance_memory_service(performance_mock_llm):
    """Memory service optimized for performance testing"""
    return MemoryService(
        llm_provider=performance_mock_llm,
        tracer=None
    )


@pytest.fixture
async def performance_knowledge_service(performance_mock_vector_store, performance_memory_service, performance_mock_llm):
    """Knowledge service optimized for performance testing"""
    return EnhancedKnowledgeService(
        vector_store=performance_mock_vector_store,
        memory_service=performance_memory_service,
        llm_provider=performance_mock_llm,
        sanitizer=None,
        tracer=None
    )


@pytest.fixture
async def performance_orchestration_service(
    performance_memory_service,
    performance_knowledge_service,
    performance_mock_llm
):
    """Full orchestration service for performance testing"""
    planning_service = PlanningService(llm_provider=performance_mock_llm, tracer=None)
    reasoning_service = ReasoningService(llm_provider=performance_mock_llm, tracer=None)
    
    return OrchestrationService(
        memory_service=performance_memory_service,
        planning_service=planning_service,
        reasoning_service=reasoning_service,
        enhanced_knowledge_service=performance_knowledge_service,
        llm_provider=performance_mock_llm,
        tracer=None
    )


class TestConcurrentWorkflowPerformance:
    """Test performance with concurrent workflow operations"""
    
    @pytest.mark.asyncio
    @pytest.mark.performance
    async def test_concurrent_workflow_creation(self, performance_orchestration_service):
        """Test system performance with concurrent workflow creation"""
        async def create_workflow(workflow_num):
            return await performance_orchestration_service.create_troubleshooting_workflow(
                session_id=f"perf-session-{workflow_num}",
                case_id=f"perf-case-{workflow_num}",
                user_id=f"perf-user-{workflow_num}",
                problem_description=f"Performance test problem {workflow_num}",
                context={
                    "test_type": "concurrent_creation",
                    "workflow_num": workflow_num,
                    "timestamp": datetime.utcnow().isoformat()
                }
            )
        
        # Test different concurrency levels
        concurrency_levels = [5, 10, 20, 50]
        results = {}
        
        for concurrency in concurrency_levels:
            start_time = time.time()
            
            # Create concurrent workflows
            tasks = [create_workflow(i) for i in range(concurrency)]
            workflow_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            end_time = time.time()
            total_time = end_time - start_time
            
            # Validate results
            successful_workflows = [r for r in workflow_results if isinstance(r, dict) and r.get("success")]
            success_rate = len(successful_workflows) / concurrency
            avg_time = total_time / concurrency
            
            results[concurrency] = {
                "total_time": total_time,
                "avg_time": avg_time,
                "success_rate": success_rate,
                "throughput": concurrency / total_time
            }
            
            # Performance assertions
            assert success_rate >= 0.95  # At least 95% success rate
            assert avg_time < 2.0  # Average under 2 seconds per workflow
            assert total_time < 20.0  # Total time under 20 seconds
        
        # Verify scaling characteristics
        for concurrency in concurrency_levels:
            print(f"Concurrency {concurrency}: "
                  f"{results[concurrency]['avg_time']:.3f}s avg, "
                  f"{results[concurrency]['throughput']:.2f} workflows/sec, "
                  f"{results[concurrency]['success_rate']:.2%} success")
    
    @pytest.mark.asyncio
    @pytest.mark.performance
    async def test_concurrent_step_execution(self, performance_orchestration_service):
        """Test concurrent step execution performance"""
        # Pre-create workflows
        workflows = []
        for i in range(10):
            workflow_result = await performance_orchestration_service.create_troubleshooting_workflow(
                session_id=f"step-perf-session-{i}",
                case_id=f"step-perf-case-{i}",
                user_id=f"step-perf-user-{i}",
                problem_description=f"Step execution performance test {i}",
                context={"test_type": "step_execution"}
            )
            workflows.append(workflow_result["workflow_id"])
        
        async def execute_step(workflow_id, step_num):
            return await performance_orchestration_service.execute_workflow_step(
                workflow_id=workflow_id,
                step_inputs={
                    "step_number": step_num,
                    "performance_test": True,
                    "timestamp": datetime.utcnow().isoformat()
                }
            )
        
        # Execute steps concurrently across multiple workflows
        start_time = time.time()
        
        tasks = []
        for i, workflow_id in enumerate(workflows):
            tasks.append(execute_step(workflow_id, 1))
        
        step_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        end_time = time.time()
        total_time = end_time - start_time
        
        # Validate performance
        successful_steps = [r for r in step_results if isinstance(r, dict) and r.get("success")]
        success_rate = len(successful_steps) / len(workflows)
        avg_step_time = total_time / len(workflows)
        
        assert success_rate >= 0.95
        assert avg_step_time < 3.0  # Average step execution under 3 seconds
        assert total_time < 15.0  # Total concurrent execution under 15 seconds
    
    @pytest.mark.asyncio
    @pytest.mark.performance
    async def test_workflow_state_management_performance(self, performance_orchestration_service):
        """Test performance of workflow state management under load"""
        # Create multiple workflows
        workflow_ids = []
        for i in range(25):
            workflow_result = await performance_orchestration_service.create_troubleshooting_workflow(
                session_id=f"state-perf-session-{i}",
                case_id=f"state-perf-case-{i}",
                user_id=f"state-perf-user-{i}",
                problem_description=f"State management performance test {i}",
                context={"test_type": "state_management"}
            )
            workflow_ids.append(workflow_result["workflow_id"])
        
        # Perform concurrent state operations
        async def state_operations(workflow_id):
            operations = []
            
            # Get status
            start = time.time()
            status = await performance_orchestration_service.get_workflow_status(workflow_id)
            operations.append(("status", time.time() - start, status.get("success", False)))
            
            # Pause workflow
            start = time.time()
            pause = await performance_orchestration_service.pause_workflow(workflow_id)
            operations.append(("pause", time.time() - start, pause.get("success", False)))
            
            # Resume workflow
            start = time.time()
            resume = await performance_orchestration_service.resume_workflow(workflow_id)
            operations.append(("resume", time.time() - start, resume.get("success", False)))
            
            # Get recommendations
            start = time.time()
            recommendations = await performance_orchestration_service.get_workflow_recommendations(workflow_id)
            operations.append(("recommendations", time.time() - start, recommendations.get("success", False)))
            
            return operations
        
        start_time = time.time()
        
        tasks = [state_operations(workflow_id) for workflow_id in workflow_ids]
        all_operations = await asyncio.gather(*tasks, return_exceptions=True)
        
        end_time = time.time()
        total_time = end_time - start_time
        
        # Analyze operation performance
        operation_stats = {"status": [], "pause": [], "resume": [], "recommendations": []}
        total_operations = 0
        successful_operations = 0
        
        for operations in all_operations:
            if isinstance(operations, list):
                for op_type, op_time, success in operations:
                    operation_stats[op_type].append(op_time)
                    total_operations += 1
                    if success:
                        successful_operations += 1
        
        # Performance validation
        success_rate = successful_operations / total_operations if total_operations > 0 else 0
        avg_operation_time = sum(sum(times) for times in operation_stats.values()) / total_operations
        
        assert success_rate >= 0.95
        assert avg_operation_time < 1.0  # Average operation under 1 second
        assert total_time < 30.0  # All operations complete within 30 seconds
        
        # Individual operation performance
        for op_type, times in operation_stats.items():
            if times:
                avg_time = sum(times) / len(times)
                max_time = max(times)
                print(f"{op_type}: avg={avg_time:.3f}s, max={max_time:.3f}s")
                
                # Specific performance requirements per operation type
                if op_type == "status":
                    assert avg_time < 0.2  # Status should be very fast
                elif op_type in ["pause", "resume"]:
                    assert avg_time < 0.5  # State changes should be fast
                elif op_type == "recommendations":
                    assert avg_time < 1.0  # Recommendations can take a bit longer


class TestMemoryServicePerformance:
    """Test memory service performance under load"""
    
    @pytest.mark.asyncio
    @pytest.mark.performance
    async def test_concurrent_memory_operations(self, performance_memory_service):
        """Test memory service performance with concurrent operations"""
        async def memory_operation_cycle(session_num):
            session_id = f"memory-perf-session-{session_num}"
            operations_times = []
            
            # Store interaction
            start = time.time()
            await performance_memory_service.store_interaction(
                session_id=session_id,
                user_input=f"Performance test question {session_num}",
                ai_response=f"Performance test response {session_num}",
                context={
                    "session_num": session_num,
                    "test_type": "memory_performance",
                    "timestamp": datetime.utcnow().isoformat()
                }
            )
            operations_times.append(("store", time.time() - start))
            
            # Retrieve context
            start = time.time()
            context = await performance_memory_service.retrieve_context(
                session_id,
                f"performance test query {session_num}"
            )
            operations_times.append(("retrieve", time.time() - start))
            
            # Store another interaction
            start = time.time()
            await performance_memory_service.store_interaction(
                session_id=session_id,
                user_input=f"Follow-up question {session_num}",
                ai_response=f"Follow-up response {session_num}",
                context={"follow_up": True, "session_num": session_num}
            )
            operations_times.append(("store_followup", time.time() - start))
            
            return operations_times
        
        # Test with increasing load
        load_levels = [10, 25, 50, 100]
        
        for load in load_levels:
            start_time = time.time()
            
            tasks = [memory_operation_cycle(i) for i in range(load)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            end_time = time.time()
            total_time = end_time - start_time
            
            # Analyze results
            all_operations = []
            for result in results:
                if isinstance(result, list):
                    all_operations.extend(result)
            
            operation_types = {}
            for op_type, op_time in all_operations:
                if op_type not in operation_types:
                    operation_types[op_type] = []
                operation_types[op_type].append(op_time)
            
            # Performance validation
            total_operations = len(all_operations)
            avg_operation_time = sum(op_time for _, op_time in all_operations) / total_operations
            throughput = total_operations / total_time
            
            assert avg_operation_time < 0.5  # Average operation under 500ms
            assert throughput > load  # Throughput should exceed load level
            
            print(f"Load {load}: {avg_operation_time:.3f}s avg, {throughput:.1f} ops/sec")
            
            # Specific operation performance
            for op_type, times in operation_types.items():
                avg_time = sum(times) / len(times)
                max_time = max(times)
                
                if op_type in ["store", "store_followup"]:
                    assert avg_time < 0.3  # Store operations under 300ms
                elif op_type == "retrieve":
                    assert avg_time < 0.5  # Retrieve operations under 500ms
    
    @pytest.mark.asyncio
    @pytest.mark.performance
    async def test_memory_scaling_with_data_volume(self, performance_memory_service):
        """Test memory service performance as data volume increases"""
        session_id = "memory-scaling-test"
        
        # Store increasing amounts of data and measure performance
        data_volumes = [10, 50, 100, 200]
        performance_metrics = {}
        
        for volume in data_volumes:
            # Pre-populate with data
            for i in range(volume):
                await performance_memory_service.store_interaction(
                    session_id=session_id,
                    user_input=f"Scaling test question {i}",
                    ai_response=f"Scaling test response {i}",
                    context={
                        "data_volume": volume,
                        "interaction_num": i,
                        "complexity": "medium"
                    }
                )
            
            # Measure retrieval performance
            retrieval_times = []
            for test_query in range(10):  # 10 test queries
                start = time.time()
                await performance_memory_service.retrieve_context(
                    session_id,
                    f"scaling test query {test_query}"
                )
                retrieval_times.append(time.time() - start)
            
            avg_retrieval = sum(retrieval_times) / len(retrieval_times)
            max_retrieval = max(retrieval_times)
            
            performance_metrics[volume] = {
                "avg_retrieval": avg_retrieval,
                "max_retrieval": max_retrieval
            }
            
            # Performance should remain reasonable even with more data
            assert avg_retrieval < 1.0  # Under 1 second average
            assert max_retrieval < 2.0  # Under 2 seconds maximum
            
            print(f"Volume {volume}: avg={avg_retrieval:.3f}s, max={max_retrieval:.3f}s")
        
        # Verify scaling doesn't degrade too much
        first_volume = data_volumes[0]
        last_volume = data_volumes[-1]
        
        performance_degradation = (
            performance_metrics[last_volume]["avg_retrieval"] / 
            performance_metrics[first_volume]["avg_retrieval"]
        )
        
        # Performance shouldn't degrade more than 3x with 20x data increase
        assert performance_degradation < 3.0


class TestKnowledgeServicePerformance:
    """Test knowledge service performance and throughput"""
    
    @pytest.mark.asyncio
    @pytest.mark.performance
    async def test_concurrent_knowledge_searches(self, performance_knowledge_service):
        """Test knowledge service performance with concurrent searches"""
        search_queries = [
            "database performance optimization",
            "API rate limiting strategies", 
            "microservice communication patterns",
            "container orchestration best practices",
            "security vulnerability assessment",
            "monitoring and alerting setup",
            "CI/CD pipeline optimization",
            "distributed system debugging",
            "cloud infrastructure scaling",
            "data pipeline performance"
        ]
        
        async def perform_search(query, search_num):
            return await performance_knowledge_service.search_with_reasoning_context(
                query=query,
                session_id=f"search-perf-session-{search_num}",
                reasoning_type="diagnostic",
                context={"search_num": search_num, "performance_test": True}
            )
        
        # Test with different concurrency levels
        concurrency_levels = [5, 10, 20, 40]
        
        for concurrency in concurrency_levels:
            start_time = time.time()
            
            # Create concurrent search tasks
            tasks = []
            for i in range(concurrency):
                query = search_queries[i % len(search_queries)]
                tasks.append(perform_search(query, i))
            
            search_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            end_time = time.time()
            total_time = end_time - start_time
            
            # Analyze results
            successful_searches = [r for r in search_results if isinstance(r, dict) and "results" in r]
            success_rate = len(successful_searches) / concurrency
            avg_search_time = total_time / concurrency
            throughput = concurrency / total_time
            
            # Performance validation
            assert success_rate >= 0.95  # 95% success rate
            assert avg_search_time < 1.0  # Under 1 second average
            assert throughput > concurrency * 0.5  # Reasonable throughput
            
            # Check search quality
            avg_confidence = sum(r.get("confidence_score", 0) for r in successful_searches) / len(successful_searches)
            assert avg_confidence > 0.5  # Maintain search quality
            
            print(f"Concurrency {concurrency}: "
                  f"{avg_search_time:.3f}s avg, "
                  f"{throughput:.1f} searches/sec, "
                  f"{avg_confidence:.3f} avg confidence")
    
    @pytest.mark.asyncio
    @pytest.mark.performance
    async def test_knowledge_curation_performance(self, performance_knowledge_service):
        """Test knowledge curation performance"""
        reasoning_types = ["diagnostic", "analytical", "strategic", "creative"]
        topics = [
            "database optimization",
            "system monitoring", 
            "security hardening",
            "performance tuning",
            "infrastructure scaling"
        ]
        
        async def perform_curation(reasoning_type, topic, curation_num):
            return await performance_knowledge_service.curate_knowledge_for_reasoning(
                reasoning_type=reasoning_type,
                session_id=f"curation-perf-session-{curation_num}",
                topic_focus=topic,
                user_profile={"expertise": "intermediate"}
            )
        
        # Test concurrent curation operations
        start_time = time.time()
        
        tasks = []
        for i in range(20):  # 20 concurrent curations
            reasoning_type = reasoning_types[i % len(reasoning_types)]
            topic = topics[i % len(topics)]
            tasks.append(perform_curation(reasoning_type, topic, i))
        
        curation_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        end_time = time.time()
        total_time = end_time - start_time
        
        # Analyze curation performance
        successful_curations = [r for r in curation_results if isinstance(r, dict) and "curated_content" in r]
        success_rate = len(successful_curations) / 20
        avg_curation_time = total_time / 20
        
        assert success_rate >= 0.95
        assert avg_curation_time < 2.0  # Under 2 seconds average
        assert total_time < 20.0  # Total under 20 seconds
        
        # Check curation quality
        total_curated_items = sum(len(r.get("curated_content", [])) for r in successful_curations)
        avg_items_per_curation = total_curated_items / len(successful_curations)
        
        assert avg_items_per_curation > 3  # At least 3 items per curation on average
        
        print(f"Curation: {avg_curation_time:.3f}s avg, "
              f"{avg_items_per_curation:.1f} items/curation")


class TestEndToEndPerformanceBenchmarks:
    """End-to-end system performance benchmarks"""
    
    @pytest.mark.asyncio
    @pytest.mark.performance
    async def test_full_workflow_performance_benchmark(self, performance_orchestration_service):
        """Comprehensive end-to-end workflow performance benchmark"""
        workflow_scenarios = [
            {
                "name": "Database Issue",
                "problem": "Database connection timeouts in production environment",
                "context": {"service": "api", "database": "postgresql"},
                "priority": "high"
            },
            {
                "name": "Performance Degradation",
                "problem": "API response times increased 300% after deployment",
                "context": {"service": "web-api", "change": "deployment"},
                "priority": "critical"
            },
            {
                "name": "Security Issue",
                "problem": "Unauthorized access attempts detected in logs",
                "context": {"security": "breach_attempt", "scope": "authentication"},
                "priority": "critical"
            },
            {
                "name": "Infrastructure Issue",
                "problem": "Container orchestration platform experiencing instability",
                "context": {"platform": "kubernetes", "issue": "node_failures"},
                "priority": "medium"
            }
        ]
        
        async def run_full_workflow(scenario, workflow_num):
            timings = {}
            
            # 1. Create workflow
            start = time.time()
            workflow_result = await performance_orchestration_service.create_troubleshooting_workflow(
                session_id=f"benchmark-session-{workflow_num}",
                case_id=f"benchmark-case-{workflow_num}",
                user_id=f"benchmark-user-{workflow_num}",
                problem_description=scenario["problem"],
                context=scenario["context"],
                priority_level=scenario["priority"]
            )
            timings["create"] = time.time() - start
            
            if not workflow_result.get("success"):
                return None
            
            workflow_id = workflow_result["workflow_id"]
            
            # 2. Execute multiple steps
            for step_num in range(3):  # Execute 3 steps
                start = time.time()
                step_result = await performance_orchestration_service.execute_workflow_step(
                    workflow_id=workflow_id,
                    step_inputs={"step_number": step_num, "benchmark": True}
                )
                timings[f"step_{step_num}"] = time.time() - start
                
                if not step_result.get("success"):
                    break
            
            # 3. Get status
            start = time.time()
            status_result = await performance_orchestration_service.get_workflow_status(workflow_id)
            timings["status"] = time.time() - start
            
            # 4. Get recommendations
            start = time.time()
            rec_result = await performance_orchestration_service.get_workflow_recommendations(workflow_id)
            timings["recommendations"] = time.time() - start
            
            return {
                "scenario": scenario["name"],
                "timings": timings,
                "total_time": sum(timings.values()),
                "success": all([
                    workflow_result.get("success"),
                    status_result.get("success"),
                    rec_result.get("success")
                ])
            }
        
        # Run benchmarks for all scenarios
        benchmark_tasks = []
        for i, scenario in enumerate(workflow_scenarios * 5):  # 5 runs per scenario
            benchmark_tasks.append(run_full_workflow(scenario, i))
        
        start_time = time.time()
        benchmark_results = await asyncio.gather(*benchmark_tasks, return_exceptions=True)
        total_benchmark_time = time.time() - start_time
        
        # Analyze benchmark results
        successful_runs = [r for r in benchmark_results if isinstance(r, dict) and r.get("success")]
        success_rate = len(successful_runs) / len(benchmark_tasks)
        
        assert success_rate >= 0.95  # 95% success rate
        
        # Performance analysis by scenario
        scenario_stats = {}
        for result in successful_runs:
            scenario_name = result["scenario"]
            if scenario_name not in scenario_stats:
                scenario_stats[scenario_name] = []
            scenario_stats[scenario_name].append(result["total_time"])
        
        # Performance requirements per scenario type
        performance_limits = {
            "Database Issue": 15.0,      # 15 seconds max
            "Performance Degradation": 12.0,  # 12 seconds max  
            "Security Issue": 10.0,      # 10 seconds max (urgent)
            "Infrastructure Issue": 20.0  # 20 seconds max
        }
        
        for scenario_name, times in scenario_stats.items():
            avg_time = sum(times) / len(times)
            max_time = max(times)
            min_time = min(times)
            
            print(f"{scenario_name}: avg={avg_time:.2f}s, "
                  f"min={min_time:.2f}s, max={max_time:.2f}s")
            
            # Validate against performance limits
            limit = performance_limits.get(scenario_name, 20.0)
            assert avg_time < limit, f"{scenario_name} exceeded {limit}s limit"
            assert max_time < limit * 1.5, f"{scenario_name} max time too high"
        
        print(f"Total benchmark time: {total_benchmark_time:.2f}s for {len(benchmark_tasks)} workflows")
        
        # Overall system throughput
        throughput = len(successful_runs) / total_benchmark_time
        assert throughput > 0.5  # At least 0.5 workflows per second
    
    @pytest.mark.asyncio
    @pytest.mark.performance
    async def test_system_resource_utilization(self, performance_orchestration_service):
        """Test system resource utilization under load"""
        process = psutil.Process(os.getpid())
        
        # Baseline measurements
        baseline_memory = process.memory_info().rss
        baseline_cpu = process.cpu_percent()
        
        # Create sustained load
        async def sustained_load():
            tasks = []
            
            # Create multiple concurrent workflows
            for i in range(30):
                task = performance_orchestration_service.create_troubleshooting_workflow(
                    session_id=f"resource-test-session-{i}",
                    case_id=f"resource-test-case-{i}",
                    user_id=f"resource-test-user-{i}",
                    problem_description=f"Resource utilization test {i}",
                    context={"resource_test": True, "workflow_num": i}
                )
                tasks.append(task)
            
            # Execute workflows
            workflow_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Execute steps on successful workflows
            step_tasks = []
            for i, result in enumerate(workflow_results):
                if isinstance(result, dict) and result.get("success"):
                    step_task = performance_orchestration_service.execute_workflow_step(
                        workflow_id=result["workflow_id"],
                        step_inputs={"resource_test": True}
                    )
                    step_tasks.append(step_task)
            
            if step_tasks:
                await asyncio.gather(*step_tasks, return_exceptions=True)
        
        # Monitor resource usage during load
        start_time = time.time()
        
        # Run sustained load
        await sustained_load()
        
        end_time = time.time()
        load_duration = end_time - start_time
        
        # Post-load measurements
        peak_memory = process.memory_info().rss
        peak_cpu = process.cpu_percent()
        
        # Memory usage analysis
        memory_growth = peak_memory - baseline_memory
        memory_growth_mb = memory_growth / (1024 * 1024)
        
        print(f"Memory growth: {memory_growth_mb:.1f} MB")
        print(f"Peak CPU: {peak_cpu:.1f}%")
        print(f"Load duration: {load_duration:.2f}s")
        
        # Resource utilization limits
        assert memory_growth_mb < 200  # Less than 200MB growth
        assert peak_cpu < 80  # Less than 80% CPU usage
        
        # Force garbage collection and check for memory leaks
        gc.collect()
        await asyncio.sleep(2)  # Allow cleanup
        
        post_gc_memory = process.memory_info().rss
        memory_after_gc = post_gc_memory - baseline_memory
        memory_after_gc_mb = memory_after_gc / (1024 * 1024)
        
        print(f"Memory after GC: {memory_after_gc_mb:.1f} MB")
        
        # Memory should return to reasonable levels after GC
        assert memory_after_gc_mb < 100  # Less than 100MB permanent growth


class TestPerformanceRegressionDetection:
    """Test for performance regression detection"""
    
    @pytest.mark.asyncio
    @pytest.mark.performance
    async def test_performance_baseline_validation(self, performance_orchestration_service):
        """Validate performance against established baselines"""
        # Performance baselines (in seconds)
        baselines = {
            "workflow_creation": 1.0,
            "step_execution": 2.0,
            "status_retrieval": 0.2,
            "recommendations": 1.5,
            "pause_resume": 0.5
        }
        
        # Run performance tests
        performance_results = {}
        
        # Test workflow creation
        start = time.time()
        workflow_result = await performance_orchestration_service.create_troubleshooting_workflow(
            session_id="baseline-test-session",
            case_id="baseline-test-case",
            user_id="baseline-test-user",
            problem_description="Performance baseline validation test",
            context={"baseline_test": True}
        )
        performance_results["workflow_creation"] = time.time() - start
        
        if workflow_result.get("success"):
            workflow_id = workflow_result["workflow_id"]
            
            # Test step execution
            start = time.time()
            await performance_orchestration_service.execute_workflow_step(
                workflow_id=workflow_id,
                step_inputs={"baseline_test": True}
            )
            performance_results["step_execution"] = time.time() - start
            
            # Test status retrieval
            start = time.time()
            await performance_orchestration_service.get_workflow_status(workflow_id)
            performance_results["status_retrieval"] = time.time() - start
            
            # Test recommendations
            start = time.time()
            await performance_orchestration_service.get_workflow_recommendations(workflow_id)
            performance_results["recommendations"] = time.time() - start
            
            # Test pause/resume
            start = time.time()
            await performance_orchestration_service.pause_workflow(workflow_id)
            await performance_orchestration_service.resume_workflow(workflow_id)
            performance_results["pause_resume"] = time.time() - start
        
        # Validate against baselines
        regression_threshold = 1.5  # 50% slower than baseline is regression
        
        for operation, baseline in baselines.items():
            if operation in performance_results:
                actual_time = performance_results[operation]
                performance_ratio = actual_time / baseline
                
                print(f"{operation}: {actual_time:.3f}s (baseline: {baseline:.3f}s, "
                      f"ratio: {performance_ratio:.2f})")
                
                # Check for performance regression
                assert performance_ratio < regression_threshold, (
                    f"Performance regression detected in {operation}: "
                    f"{actual_time:.3f}s vs baseline {baseline:.3f}s"
                )
        
        return performance_results


@pytest.mark.performance
@pytest.mark.integration
class TestScalabilityValidation:
    """Test system scalability characteristics"""
    
    @pytest.mark.asyncio
    async def test_linear_scalability_characteristics(self, performance_orchestration_service):
        """Test that performance scales reasonably with increased load"""
        load_levels = [1, 5, 10, 20]
        scalability_results = {}
        
        for load in load_levels:
            async def load_test():
                tasks = []
                for i in range(load):
                    task = performance_orchestration_service.create_troubleshooting_workflow(
                        session_id=f"scale-session-{load}-{i}",
                        case_id=f"scale-case-{load}-{i}",
                        user_id=f"scale-user-{load}-{i}",
                        problem_description=f"Scalability test {load}-{i}",
                        context={"scalability_test": True, "load_level": load}
                    )
                    tasks.append(task)
                
                start_time = time.time()
                results = await asyncio.gather(*tasks, return_exceptions=True)
                end_time = time.time()
                
                successful_results = [r for r in results if isinstance(r, dict) and r.get("success")]
                
                return {
                    "load": load,
                    "total_time": end_time - start_time,
                    "successful_operations": len(successful_results),
                    "success_rate": len(successful_results) / load,
                    "throughput": len(successful_results) / (end_time - start_time),
                    "avg_time_per_operation": (end_time - start_time) / load
                }
            
            scalability_results[load] = await load_test()
        
        # Analyze scalability characteristics
        for load, results in scalability_results.items():
            print(f"Load {load}: {results['avg_time_per_operation']:.3f}s avg, "
                  f"{results['throughput']:.2f} ops/sec, "
                  f"{results['success_rate']:.2%} success")
            
            # Basic scalability requirements
            assert results["success_rate"] >= 0.95  # Maintain high success rate
            assert results["avg_time_per_operation"] < 5.0  # Individual operations stay reasonable
        
        # Check that scalability doesn't degrade too severely
        baseline_throughput = scalability_results[1]["throughput"]
        max_load_throughput = scalability_results[max(load_levels)]["throughput"]
        
        # Throughput should not drop below 30% of baseline at maximum load
        throughput_retention = max_load_throughput / baseline_throughput
        assert throughput_retention > 0.3, f"Throughput degraded too much: {throughput_retention:.2%}"
        
        return scalability_results