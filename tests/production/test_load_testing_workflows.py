#!/usr/bin/env python3
"""
Load Testing Suite for FaultMaven Phase 2 Intelligent Troubleshooting Workflows

This module provides comprehensive load testing for intelligent troubleshooting workflows
including multi-step orchestration, memory service cross-session scenarios, and
high-volume knowledge base operations.

Test Categories:
- Concurrent user simulation for troubleshooting workflows  
- Multi-step orchestration workflow stress testing
- Memory service load testing with cross-session scenarios
- Knowledge base performance under high query volume
- System resource usage under production load scenarios
- API endpoint performance and stability testing
"""

import pytest
import asyncio
import time
import statistics
import aiohttp
import psutil
import json
import random
import string
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor
import threading


@dataclass
class LoadTestMetrics:
    """Metrics collected during load testing."""
    test_name: str
    duration_seconds: float
    total_requests: int
    successful_requests: int
    failed_requests: int
    average_response_time_ms: float
    min_response_time_ms: float
    max_response_time_ms: float
    p95_response_time_ms: float
    p99_response_time_ms: float
    throughput_rps: float
    error_rate_percent: float
    concurrent_users: int
    memory_usage_mb: float
    cpu_usage_percent: float
    errors: List[str]


@dataclass
class WorkflowScenario:
    """Defines a load testing scenario."""
    name: str
    endpoint: str
    method: str
    payload_template: Dict[str, Any]
    concurrent_users: int
    duration_seconds: int
    expected_success_rate: float
    expected_max_response_time_ms: float


class LoadTestRunner:
    """Comprehensive load testing runner for FaultMaven workflows."""
    
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url
        self.session = None
        self.start_time = None
        self.process = psutil.Process()
        
        # Define load testing scenarios
        self.scenarios = [
            # Basic troubleshooting queries
            WorkflowScenario(
                name="basic_troubleshooting_query",
                endpoint="/api/v1/query",
                method="POST",
                payload_template={
                    "query": "Application {app_name} is showing {error_type} errors",
                    "session_id": "load_test_{session_id}",
                    "context": {
                        "environment": "production",
                        "urgency": "high"
                    }
                },
                concurrent_users=20,
                duration_seconds=60,
                expected_success_rate=0.95,
                expected_max_response_time_ms=2000
            ),
            
            # Enhanced agent workflows  
            WorkflowScenario(
                name="enhanced_agent_analysis",
                endpoint="/api/v1/enhanced/analyze",
                method="POST",
                payload_template={
                    "query": "Database performance degradation in {service_name}",
                    "session_id": "load_test_{session_id}",
                    "analysis_depth": "detailed",
                    "context": {
                        "service_type": "database",
                        "affected_users": random.randint(100, 1000)
                    }
                },
                concurrent_users=10,
                duration_seconds=120,
                expected_success_rate=0.90,
                expected_max_response_time_ms=5000
            ),
            
            # Multi-step orchestration workflows
            WorkflowScenario(
                name="orchestration_troubleshoot",
                endpoint="/api/v1/orchestration/troubleshoot",
                method="POST",
                payload_template={
                    "problem_description": "Critical system failure in {component}",
                    "session_id": "load_test_{session_id}",
                    "priority": "critical",
                    "affected_systems": ["{system1}", "{system2}"],
                    "context": {
                        "incident_start": "2025-01-15T10:00:00Z",
                        "business_impact": "high"
                    }
                },
                concurrent_users=5,
                duration_seconds=180,
                expected_success_rate=0.85,
                expected_max_response_time_ms=10000
            ),
            
            # Memory service cross-session workflows
            WorkflowScenario(
                name="memory_cross_session",
                endpoint="/api/v1/enhanced/memory/correlate",
                method="POST",
                payload_template={
                    "current_issue": "Service {service_name} timeout",
                    "session_id": "load_test_{session_id}",
                    "correlation_depth": "deep",
                    "time_range_hours": 24
                },
                concurrent_users=8,
                duration_seconds=90,
                expected_success_rate=0.88,
                expected_max_response_time_ms=3000
            ),
            
            # Knowledge base high-volume queries
            WorkflowScenario(
                name="knowledge_base_search",
                endpoint="/api/v1/knowledge/search",
                method="POST",
                payload_template={
                    "query": "How to fix {error_type} in {technology}",
                    "limit": 10,
                    "include_context": True,
                    "session_id": "load_test_{session_id}"
                },
                concurrent_users=25,
                duration_seconds=45,
                expected_success_rate=0.98,
                expected_max_response_time_ms=1500
            ),
            
            # Planning engine stress testing
            WorkflowScenario(
                name="planning_engine_decomposition",
                endpoint="/api/v1/enhanced/planning/decompose",
                method="POST",
                payload_template={
                    "complex_problem": "Multi-tier application failure with {complexity} components",
                    "session_id": "load_test_{session_id}",
                    "decomposition_strategy": "hierarchical",
                    "max_depth": 3
                },
                concurrent_users=6,
                duration_seconds=150,
                expected_success_rate=0.80,
                expected_max_response_time_ms=8000
            ),
            
            # Reasoning engine complex workflows
            WorkflowScenario(
                name="reasoning_engine_workflow",
                endpoint="/api/v1/enhanced/reasoning/analyze",
                method="POST",
                payload_template={
                    "problem_data": {
                        "symptoms": ["{symptom1}", "{symptom2}", "{symptom3}"],
                        "context": "{context_data}",
                        "constraints": {"time_sensitive": True}
                    },
                    "session_id": "load_test_{session_id}",
                    "reasoning_depth": "comprehensive"
                },
                concurrent_users=4,
                duration_seconds=200,
                expected_success_rate=0.75,
                expected_max_response_time_ms=12000
            )
        ]
        
        # Test data templates for payload generation
        self.test_data = {
            "app_names": ["web-api", "database", "cache-service", "auth-service", "payment-api"],
            "error_types": ["timeout", "connection refused", "memory leak", "deadlock", "high latency"],
            "service_names": ["postgres", "redis", "elasticsearch", "kafka", "nginx"],
            "technologies": ["kubernetes", "docker", "python", "nodejs", "java"],
            "components": ["load balancer", "database cluster", "message queue", "cache layer"],
            "systems": ["auth-system", "payment-system", "inventory-system", "notification-system"],
            "symptoms": ["high CPU usage", "memory leaks", "slow queries", "connection timeouts"],
            "contexts": ["peak traffic", "deployment rollout", "configuration change", "infrastructure update"],
            "complexities": ["high", "medium", "critical", "severe"]
        }


@pytest.fixture
async def load_test_runner():
    """Fixture providing configured load test runner."""
    runner = LoadTestRunner()
    runner.session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30),
        connector=aiohttp.TCPConnector(limit=100, limit_per_host=50)
    )
    yield runner
    await runner.session.close()


class TestBasicLoadScenarios:
    """Test basic load scenarios for core functionality."""
    
    @pytest.mark.asyncio
    @pytest.mark.load
    async def test_basic_troubleshooting_load(self, load_test_runner):
        """Test basic troubleshooting query under load."""
        scenario = next(s for s in load_test_runner.scenarios if s.name == "basic_troubleshooting_query")
        
        metrics = await load_test_runner.run_load_test(scenario)
        
        # Validate performance requirements
        assert metrics.error_rate_percent <= (100 - scenario.expected_success_rate * 100), \
            f"Error rate {metrics.error_rate_percent:.1f}% exceeds threshold"
        
        assert metrics.p95_response_time_ms <= scenario.expected_max_response_time_ms, \
            f"P95 response time {metrics.p95_response_time_ms:.0f}ms exceeds {scenario.expected_max_response_time_ms}ms"
        
        assert metrics.throughput_rps >= scenario.concurrent_users * 0.5, \
            f"Throughput {metrics.throughput_rps:.1f} RPS too low for {scenario.concurrent_users} users"
    
    @pytest.mark.asyncio
    @pytest.mark.load
    async def test_knowledge_base_search_load(self, load_test_runner):
        """Test knowledge base search under high volume."""
        scenario = next(s for s in load_test_runner.scenarios if s.name == "knowledge_base_search")
        
        metrics = await load_test_runner.run_load_test(scenario)
        
        # Knowledge base should handle high volume efficiently
        assert metrics.error_rate_percent <= 5.0, \
            f"Knowledge base error rate {metrics.error_rate_percent:.1f}% too high"
        
        assert metrics.average_response_time_ms <= scenario.expected_max_response_time_ms, \
            f"Average response time {metrics.average_response_time_ms:.0f}ms exceeds threshold"
        
        # Should maintain good throughput
        assert metrics.throughput_rps >= 15.0, \
            f"Knowledge base throughput {metrics.throughput_rps:.1f} RPS insufficient"


class TestAdvancedWorkflowLoad:
    """Test advanced workflow load scenarios."""
    
    @pytest.mark.asyncio
    @pytest.mark.load 
    @pytest.mark.slow
    async def test_enhanced_agent_load(self, load_test_runner):
        """Test enhanced agent analysis under load."""
        scenario = next(s for s in load_test_runner.scenarios if s.name == "enhanced_agent_analysis")
        
        metrics = await load_test_runner.run_load_test(scenario)
        
        # Enhanced agent may have higher latency but should be reliable
        assert metrics.error_rate_percent <= 15.0, \
            f"Enhanced agent error rate {metrics.error_rate_percent:.1f}% too high"
        
        assert metrics.p99_response_time_ms <= scenario.expected_max_response_time_ms * 1.5, \
            f"P99 response time {metrics.p99_response_time_ms:.0f}ms exceeds tolerance"
        
        # Check for reasonable resource usage
        assert metrics.memory_usage_mb <= 1024, \
            f"Memory usage {metrics.memory_usage_mb:.0f}MB too high during enhanced agent load"
    
    @pytest.mark.asyncio
    @pytest.mark.load
    @pytest.mark.slow
    async def test_orchestration_workflow_load(self, load_test_runner):
        """Test multi-step orchestration workflow under load."""
        scenario = next(s for s in load_test_runner.scenarios if s.name == "orchestration_troubleshoot")
        
        metrics = await load_test_runner.run_load_test(scenario)
        
        # Orchestration workflows are complex, allow higher error rates
        assert metrics.error_rate_percent <= 20.0, \
            f"Orchestration error rate {metrics.error_rate_percent:.1f}% too high"
        
        assert metrics.successful_requests >= scenario.concurrent_users, \
            f"Only {metrics.successful_requests} successful requests for {scenario.concurrent_users} users"
        
        # Ensure at least some throughput
        assert metrics.throughput_rps >= 0.5, \
            f"Orchestration throughput {metrics.throughput_rps:.1f} RPS too low"


class TestMemoryServiceLoad:
    """Test memory service cross-session scenarios under load."""
    
    @pytest.mark.asyncio
    @pytest.mark.load
    async def test_memory_cross_session_load(self, load_test_runner):
        """Test memory service cross-session correlation under load."""
        scenario = next(s for s in load_test_runner.scenarios if s.name == "memory_cross_session")
        
        metrics = await load_test_runner.run_load_test(scenario)
        
        # Memory service should handle cross-session queries efficiently
        assert metrics.error_rate_percent <= 15.0, \
            f"Memory service error rate {metrics.error_rate_percent:.1f}% too high"
        
        assert metrics.average_response_time_ms <= scenario.expected_max_response_time_ms, \
            f"Memory service average response time {metrics.average_response_time_ms:.0f}ms too high"
        
        # Check for session isolation under load
        await self._validate_session_isolation(load_test_runner, scenario)
    
    async def _validate_session_isolation(self, runner, scenario):
        """Validate that sessions remain isolated under load."""
        # Create multiple concurrent sessions with different data
        session_ids = [f"isolation_test_{i}" for i in range(5)]
        
        tasks = []
        for session_id in session_ids:
            payload = runner._generate_payload(scenario.payload_template, session_id)
            payload["current_issue"] = f"Unique issue for {session_id}"
            
            task = asyncio.create_task(
                runner._make_request(scenario.endpoint, scenario.method, payload)
            )
            tasks.append(task)
        
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Verify responses are session-specific
        successful_responses = [r for r in responses if not isinstance(r, Exception)]
        assert len(successful_responses) >= 3, "Insufficient successful responses for session isolation test"


class TestResourceUsageUnderLoad:
    """Test system resource usage under production load scenarios."""
    
    @pytest.mark.asyncio
    @pytest.mark.load
    async def test_system_resource_usage(self, load_test_runner):
        """Test system resource usage under combined load."""
        # Run multiple scenarios concurrently to simulate production load
        scenarios = [
            s for s in load_test_runner.scenarios 
            if s.name in ["basic_troubleshooting_query", "knowledge_base_search", "memory_cross_session"]
        ]
        
        # Monitor resource usage
        initial_memory = load_test_runner.process.memory_info().rss / 1024 / 1024
        initial_cpu = load_test_runner.process.cpu_percent()
        
        # Run concurrent load tests
        tasks = []
        for scenario in scenarios:
            # Reduce duration for concurrent testing
            modified_scenario = WorkflowScenario(
                name=scenario.name,
                endpoint=scenario.endpoint,
                method=scenario.method,
                payload_template=scenario.payload_template,
                concurrent_users=max(1, scenario.concurrent_users // 3),  # Reduce load
                duration_seconds=30,  # Shorter duration
                expected_success_rate=scenario.expected_success_rate,
                expected_max_response_time_ms=scenario.expected_max_response_time_ms
            )
            
            task = asyncio.create_task(
                load_test_runner.run_load_test(modified_scenario)
            )
            tasks.append(task)
        
        metrics_list = await asyncio.gather(*tasks)
        
        # Check final resource usage
        final_memory = load_test_runner.process.memory_info().rss / 1024 / 1024
        final_cpu = load_test_runner.process.cpu_percent()
        
        memory_increase = final_memory - initial_memory
        
        # Validate resource usage is reasonable
        assert memory_increase <= 500, \
            f"Memory increased by {memory_increase:.0f}MB during load test"
        
        assert final_cpu <= 90.0, \
            f"CPU usage {final_cpu:.1f}% too high during load test"
        
        # Validate overall performance
        total_requests = sum(m.total_requests for m in metrics_list)
        total_successful = sum(m.successful_requests for m in metrics_list)
        overall_success_rate = total_successful / total_requests if total_requests > 0 else 0
        
        assert overall_success_rate >= 0.80, \
            f"Overall success rate {overall_success_rate:.1%} too low under combined load"
    
    @pytest.mark.asyncio
    @pytest.mark.load
    @pytest.mark.slow
    async def test_extended_load_stability(self, load_test_runner):
        """Test system stability under extended load."""
        # Long-running load test to check for memory leaks and stability issues
        scenario = WorkflowScenario(
            name="extended_stability_test",
            endpoint="/api/v1/query",
            method="POST",
            payload_template={
                "query": "Extended load test query {counter}",
                "session_id": "stability_test_{session_id}"
            },
            concurrent_users=5,
            duration_seconds=300,  # 5 minutes
            expected_success_rate=0.95,
            expected_max_response_time_ms=2000
        )
        
        # Monitor memory usage over time
        memory_samples = []
        
        async def memory_monitor():
            for _ in range(30):  # Sample every 10 seconds for 5 minutes
                memory_mb = load_test_runner.process.memory_info().rss / 1024 / 1024
                memory_samples.append(memory_mb)
                await asyncio.sleep(10)
        
        # Start memory monitoring and load test concurrently
        monitor_task = asyncio.create_task(memory_monitor())
        metrics = await load_test_runner.run_load_test(scenario)
        
        # Wait for memory monitoring to complete
        await monitor_task
        
        # Analyze memory stability
        if len(memory_samples) >= 10:
            early_avg = statistics.mean(memory_samples[:10])
            late_avg = statistics.mean(memory_samples[-10:])
            memory_growth_rate = (late_avg - early_avg) / early_avg
            
            assert memory_growth_rate <= 0.20, \
                f"Memory growth rate {memory_growth_rate:.1%} indicates potential leak"
        
        # Validate sustained performance
        assert metrics.error_rate_percent <= 10.0, \
            f"Extended load test error rate {metrics.error_rate_percent:.1f}% too high"
        
        assert metrics.throughput_rps >= 0.5, \
            f"Extended load test throughput {metrics.throughput_rps:.1f} RPS too low"


# Add methods to LoadTestRunner class
async def run_load_test(self, scenario: WorkflowScenario) -> LoadTestMetrics:
    """Run a load test scenario and collect metrics."""
    print(f"Running load test: {scenario.name}")
    print(f"  Users: {scenario.concurrent_users}, Duration: {scenario.duration_seconds}s")
    
    # Initialize metrics collection
    response_times = []
    successful_requests = 0
    failed_requests = 0
    errors = []
    
    start_time = time.time()
    initial_memory = self.process.memory_info().rss / 1024 / 1024
    
    # Create semaphore to limit concurrent requests
    semaphore = asyncio.Semaphore(scenario.concurrent_users)
    
    # Generate session IDs for users
    session_ids = [f"user_{i}_{int(time.time())}" for i in range(scenario.concurrent_users)]
    
    async def user_session(session_id: str):
        """Simulate a single user session."""
        session_start = time.time()
        user_requests = 0
        
        while time.time() - start_time < scenario.duration_seconds:
            async with semaphore:
                try:
                    payload = self._generate_payload(scenario.payload_template, session_id)
                    request_start = time.time()
                    
                    response = await self._make_request(
                        scenario.endpoint, scenario.method, payload
                    )
                    
                    request_time = (time.time() - request_start) * 1000
                    response_times.append(request_time)
                    
                    if response.get("success", True):  # Assume success if not specified
                        nonlocal successful_requests
                        successful_requests += 1
                    else:
                        nonlocal failed_requests
                        failed_requests += 1
                        errors.append(f"Session {session_id}: {response.get('error', 'Unknown error')}")
                    
                    user_requests += 1
                    
                except Exception as e:
                    failed_requests += 1
                    errors.append(f"Session {session_id}: {str(e)}")
                
                # Brief pause to prevent overwhelming
                await asyncio.sleep(0.1)
    
    # Run all user sessions concurrently
    tasks = [asyncio.create_task(user_session(session_id)) for session_id in session_ids]
    
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=scenario.duration_seconds + 30
        )
    except asyncio.TimeoutError:
        # Cancel remaining tasks
        for task in tasks:
            task.cancel()
    
    # Calculate final metrics
    end_time = time.time()
    actual_duration = end_time - start_time
    final_memory = self.process.memory_info().rss / 1024 / 1024
    
    total_requests = successful_requests + failed_requests
    
    if response_times:
        response_times.sort()
        avg_response_time = statistics.mean(response_times)
        min_response_time = min(response_times)
        max_response_time = max(response_times)
        p95_index = int(0.95 * len(response_times))
        p99_index = int(0.99 * len(response_times))
        p95_response_time = response_times[p95_index] if p95_index < len(response_times) else max_response_time
        p99_response_time = response_times[p99_index] if p99_index < len(response_times) else max_response_time
    else:
        avg_response_time = min_response_time = max_response_time = 0
        p95_response_time = p99_response_time = 0
    
    throughput = total_requests / actual_duration if actual_duration > 0 else 0
    error_rate = (failed_requests / total_requests * 100) if total_requests > 0 else 0
    
    metrics = LoadTestMetrics(
        test_name=scenario.name,
        duration_seconds=actual_duration,
        total_requests=total_requests,
        successful_requests=successful_requests,
        failed_requests=failed_requests,
        average_response_time_ms=avg_response_time,
        min_response_time_ms=min_response_time,
        max_response_time_ms=max_response_time,
        p95_response_time_ms=p95_response_time,
        p99_response_time_ms=p99_response_time,
        throughput_rps=throughput,
        error_rate_percent=error_rate,
        concurrent_users=scenario.concurrent_users,
        memory_usage_mb=final_memory - initial_memory,
        cpu_usage_percent=self.process.cpu_percent(),
        errors=errors[:10]  # Keep first 10 errors
    )
    
    print(f"  Completed: {total_requests} requests, {error_rate:.1f}% error rate")
    print(f"  Throughput: {throughput:.1f} RPS, P95: {p95_response_time:.0f}ms")
    
    return metrics

def _generate_payload(self, template: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    """Generate test payload from template."""
    payload = json.loads(json.dumps(template))  # Deep copy
    
    # Replace placeholders
    payload_str = json.dumps(payload)
    
    # Replace session_id
    payload_str = payload_str.replace("{session_id}", session_id)
    
    # Replace random data
    for key, values in self.test_data.items():
        placeholder = "{" + key[:-1] + "}"  # Remove 's' from key (e.g., 'app_names' -> 'app_name')
        if placeholder in payload_str:
            payload_str = payload_str.replace(placeholder, random.choice(values))
    
    # Replace numbered placeholders
    for i in range(1, 4):
        for key, values in self.test_data.items():
            placeholder = "{" + key[:-1] + str(i) + "}"
            if placeholder in payload_str:
                payload_str = payload_str.replace(placeholder, random.choice(values))
    
    # Replace counter with random number
    payload_str = payload_str.replace("{counter}", str(random.randint(1000, 9999)))
    
    return json.loads(payload_str)

async def _make_request(self, endpoint: str, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Make HTTP request to API endpoint."""
    url = f"{self.base_url}{endpoint}"
    
    try:
        if method.upper() == "GET":
            async with self.session.get(url, params=payload) as response:
                if response.status < 400:
                    return {"success": True, "data": await response.json()}
                else:
                    return {"success": False, "error": f"HTTP {response.status}"}
        else:
            async with self.session.post(url, json=payload) as response:
                if response.status < 400:
                    return {"success": True, "data": await response.json()}
                else:
                    return {"success": False, "error": f"HTTP {response.status}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

# Monkey patch methods to LoadTestRunner
LoadTestRunner.run_load_test = run_load_test
LoadTestRunner._generate_payload = _generate_payload  
LoadTestRunner._make_request = _make_request


@pytest.mark.load
@pytest.mark.slow
class TestProductionLoadSimulation:
    """Simulate realistic production load patterns."""
    
    @pytest.mark.asyncio
    async def test_mixed_workload_simulation(self, load_test_runner):
        """Simulate mixed production workload with varying intensities."""
        # Define production-like load pattern: 80% basic queries, 15% enhanced, 5% orchestration
        workload_distribution = [
            ("basic_troubleshooting_query", 0.80, 15),
            ("enhanced_agent_analysis", 0.15, 3), 
            ("orchestration_troubleshoot", 0.05, 1)
        ]
        
        metrics_list = []
        
        for scenario_name, weight, users in workload_distribution:
            scenario = next(s for s in load_test_runner.scenarios if s.name == scenario_name)
            
            # Adjust scenario for production simulation
            prod_scenario = WorkflowScenario(
                name=f"prod_sim_{scenario_name}",
                endpoint=scenario.endpoint,
                method=scenario.method,
                payload_template=scenario.payload_template,
                concurrent_users=users,
                duration_seconds=90,
                expected_success_rate=scenario.expected_success_rate * 0.9,  # Slightly lower expectations
                expected_max_response_time_ms=scenario.expected_max_response_time_ms * 1.2
            )
            
            metrics = await load_test_runner.run_load_test(prod_scenario)
            metrics_list.append((metrics, weight))
        
        # Calculate weighted performance metrics
        total_weighted_requests = sum(m.total_requests * w for m, w in metrics_list)
        total_weighted_successful = sum(m.successful_requests * w for m, w in metrics_list)
        
        overall_success_rate = total_weighted_successful / total_weighted_requests if total_weighted_requests > 0 else 0
        
        # Production simulation should maintain good overall performance
        assert overall_success_rate >= 0.85, \
            f"Mixed workload success rate {overall_success_rate:.1%} below production threshold"
        
        # Check individual scenario performance
        for metrics, weight in metrics_list:
            assert metrics.error_rate_percent <= 20.0, \
                f"Scenario {metrics.test_name} error rate {metrics.error_rate_percent:.1f}% too high in mixed workload"


if __name__ == "__main__":
    import sys
    
    # Allow running this module directly for debugging
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        pytest.main([__file__, "-v", "-m", "load"])
    else:
        print("FaultMaven Load Testing Suite")
        print("Usage: python test_load_testing_workflows.py --test")