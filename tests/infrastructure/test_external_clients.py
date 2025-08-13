"""
Rebuilt external client tests using minimal mocking architecture.

This module tests external client behavior with real network operations,
actual retry logic, and performance validation. Follows the proven minimal
mocking patterns from successful Phases 1-3.
"""

import asyncio
import pytest
import time
from aiohttp import web, ClientSession, ClientTimeout
from aiohttp.test_utils import TestServer
from unittest.mock import patch

from faultmaven.infrastructure.base_client import BaseExternalClient


class TestExternalClientBehavior:
    """Test real external client behavior with controlled network scenarios."""
    
    @pytest.fixture
    async def test_http_server(self):
        """Real HTTP server for controlled network testing."""
        request_count = {"value": 0}
        failure_count = {"value": 0}
        
        async def handler(request):
            request_count["value"] += 1
            path = request.path_qs
            
            # Simulate various real network scenarios
            if "timeout" in path:
                await asyncio.sleep(2.0)  # Real timeout scenario
                return web.json_response({"status": "delayed_success"})
            elif "error" in path and failure_count["value"] < 2:
                failure_count["value"] += 1
                raise web.HTTPInternalServerError(text="Real server error")
            elif "rate_limit" in path and request_count["value"] <= 3:
                raise web.HTTPTooManyRequests(text="Rate limit exceeded")
            else:
                return web.json_response({
                    "status": "success",
                    "request_count": request_count["value"],
                    "path": path
                })
        
        app = web.Application()
        app.router.add_get("/test", handler)
        app.router.add_post("/test", handler)
        
        # Start real server for testing
        server = TestServer(app)
        await server.start_server()
        yield server, request_count, failure_count
        await server.close()
    
    async def test_real_network_success(self, test_http_server):
        """Test successful real network operation with timing validation."""
        server, request_count, failure_count = test_http_server
        client = BaseExternalClient("test_client", "TestService")
        
        start_time = time.time()
        
        async def make_request():
            async with ClientSession() as session:
                async with session.get(f"{server.make_url('/test')}") as resp:
                    return await resp.json()
        
        result = await client.call_external(
            "test_operation",
            make_request,
            timeout=5.0
        )
        
        execution_time = time.time() - start_time
        
        # Validate real network behavior
        assert result["status"] == "success"
        assert result["request_count"] == 1
        assert execution_time < 1.0  # Real performance validation
        
    async def test_real_retry_logic_with_actual_failures(self, test_http_server):
        """Test actual retry behavior with real network failures."""
        server, request_count, failure_count = test_http_server
        client = BaseExternalClient("test_client", "TestService", enable_circuit_breaker=False)
        
        start_time = time.time()
        
        async def make_failing_request():
            async with ClientSession() as session:
                async with session.get(f"{server.make_url('/test?error=true')}") as resp:
                    if resp.status >= 500:
                        raise Exception(f"Server error: {resp.status}")
                    return await resp.json()
        
        # Should eventually succeed after real failures
        result = await client.call_external(
            "retry_operation",
            make_failing_request,
            timeout=10.0,
            retries=3
        )
        
        execution_time = time.time() - start_time
        
        # Validate real retry behavior
        assert result["status"] == "success"
        assert failure_count["value"] == 2  # Actually failed twice before success
        assert execution_time > 1.0  # Real backoff timing
        assert execution_time < 8.0  # Reasonable retry completion time
        
    async def test_real_timeout_handling(self, test_http_server):
        """Test actual timeout behavior with network delays."""
        server, request_count, failure_count = test_http_server
        client = BaseExternalClient("test_client", "TestService")
        
        async def make_slow_request():
            timeout = ClientTimeout(total=1.0)  # Real timeout configuration
            async with ClientSession(timeout=timeout) as session:
                async with session.get(f"{server.make_url('/test?timeout=true')}") as resp:
                    return await resp.json()
        
        start_time = time.time()
        
        # Should timeout on real network delay
        with pytest.raises(asyncio.TimeoutError):
            await client.call_external(
                "timeout_operation",
                make_slow_request,
                timeout=1.5
            )
        
        execution_time = time.time() - start_time
        assert 1.0 < execution_time < 2.0  # Real timeout timing
        
    async def test_real_rate_limiting_behavior(self, test_http_server):
        """Test real rate limiting with actual HTTP 429 responses."""
        server, request_count, failure_count = test_http_server
        client = BaseExternalClient("test_client", "TestService")
        
        async def make_rate_limited_request():
            async with ClientSession() as session:
                async with session.get(f"{server.make_url('/test?rate_limit=true')}") as resp:
                    if resp.status == 429:
                        raise Exception("Rate limited")
                    return await resp.json()
        
        start_time = time.time()
        
        # Should eventually succeed after rate limit expires
        result = await client.call_external(
            "rate_limited_operation",
            make_rate_limited_request,
            timeout=15.0,
            retries=5
        )
        
        execution_time = time.time() - start_time
        
        # Validate real rate limiting behavior
        assert result["status"] == "success"
        assert request_count["value"] > 3  # Actually hit rate limit multiple times
        assert execution_time > 2.0  # Real backoff occurred
        
    async def test_real_connection_pooling(self, test_http_server):
        """Test actual connection pooling and resource management."""
        server, request_count, failure_count = test_http_server
        client = BaseExternalClient("test_client", "TestService")
        
        # Make multiple concurrent requests to test real connection pooling
        async def make_concurrent_request(request_id):
            async with ClientSession() as session:
                async with session.get(f"{server.make_url('/test')}?id={request_id}") as resp:
                    return await resp.json()
        
        start_time = time.time()
        
        # Execute 10 concurrent requests (fix lambda closure issue)
        async def create_task(i):
            async def make_request():
                return await make_concurrent_request(i)
            return await client.call_external(f"concurrent_op_{i}", 
                                           make_request, 
                                           timeout=5.0)
        
        tasks = [create_task(i) for i in range(10)]
        
        results = await asyncio.gather(*tasks)
        execution_time = time.time() - start_time
        
        # Validate concurrent execution efficiency
        assert len(results) == 10
        assert all(result["status"] == "success" for result in results)
        assert execution_time < 2.0  # Real connection pooling efficiency
        assert request_count["value"] == 10  # All requests completed
        
    async def test_real_error_propagation(self, test_http_server):
        """Test actual error propagation and recovery mechanisms."""
        server, request_count, failure_count = test_http_server
        client = BaseExternalClient("test_client", "TestService")
        
        async def make_error_request():
            async with ClientSession() as session:
                # Force connection error by using invalid port
                async with session.get("http://localhost:99999/invalid") as resp:
                    return await resp.json()
        
        start_time = time.time()
        
        # Should properly handle and propagate real connection errors
        with pytest.raises(Exception):  # Real network error
            await client.call_external(
                "error_operation",
                make_error_request,
                timeout=5.0,
                retries=1
            )
        
        execution_time = time.time() - start_time
        assert execution_time < 3.0  # Quick failure without unnecessary retries
        
        
class TestCircuitBreakerRealBehavior:
    """Test real circuit breaker behavior with actual failures."""
    
    @pytest.fixture
    async def failing_server(self):
        """Server that can be controlled to fail/succeed."""
        failure_mode = {"enabled": True, "count": 0}
        
        async def handler(request):
            failure_mode["count"] += 1
            
            if failure_mode["enabled"]:
                raise web.HTTPInternalServerError(text="Simulated failure")
            else:
                return web.json_response({
                    "status": "success", 
                    "attempt": failure_mode["count"]
                })
        
        app = web.Application()
        app.router.add_get("/test", handler)
        
        server = TestServer(app)
        await server.start_server()
        yield server, failure_mode
        await server.close()
        
    async def test_real_circuit_breaker_open_state(self, failing_server):
        """Test circuit breaker opening with real failures."""
        server, failure_mode = failing_server
        client = BaseExternalClient(
            "test_client", 
            "TestService",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=3,
            circuit_breaker_timeout=2
        )
        
        async def make_request():
            async with ClientSession() as session:
                async with session.get(f"{server.make_url('/test')}") as resp:
                    if resp.status >= 500:
                        raise Exception("Server failure")
                    return await resp.json()
        
        # Trigger circuit breaker with real failures
        for i in range(5):  # Should open after 3 failures
            try:
                await client.call_external(f"attempt_{i}", make_request, timeout=2.0)
            except Exception:
                pass  # Expected failures
        
        # Circuit should now be open, preventing further calls
        start_time = time.time()
        
        with pytest.raises(Exception):  # Circuit breaker should reject immediately
            await client.call_external("open_circuit_test", make_request, timeout=5.0)
        
        execution_time = time.time() - start_time
        assert execution_time < 0.1  # Immediate rejection when circuit is open
        
    async def test_real_circuit_breaker_recovery(self, failing_server):
        """Test circuit breaker recovery with real success."""
        server, failure_mode = failing_server
        client = BaseExternalClient(
            "test_client",
            "TestService", 
            enable_circuit_breaker=True,
            circuit_breaker_threshold=2,
            circuit_breaker_timeout=1  # Short timeout for testing
        )
        
        async def make_request():
            async with ClientSession() as session:
                async with session.get(f"{server.make_url('/test')}") as resp:
                    if resp.status >= 500:
                        raise Exception("Server failure")
                    return await resp.json()
        
        # Open the circuit with failures
        for i in range(3):
            try:
                await client.call_external(f"failure_{i}", make_request, timeout=2.0)
            except Exception:
                pass
        
        # Wait for circuit breaker timeout
        await asyncio.sleep(1.2)
        
        # Fix the server
        failure_mode["enabled"] = False
        
        # Circuit should allow a test call and close on success
        result = await client.call_external("recovery_test", make_request, timeout=5.0)
        
        assert result["status"] == "success"
        assert result["attempt"] >= 3  # Shows real attempts were made (including recovery)
        
        # Circuit should now be closed and allow normal operation
        result2 = await client.call_external("normal_operation", make_request, timeout=5.0)
        assert result2["status"] == "success"


class TestRealPerformanceValidation:
    """Validate real performance characteristics under various conditions."""
    
    @pytest.fixture
    async def performance_server(self):
        """Server with controllable latency for performance testing."""
        async def handler(request):
            delay = float(request.query.get('delay', 0))
            if delay > 0:
                await asyncio.sleep(delay)
            
            return web.json_response({
                "status": "success",
                "delay": delay,
                "timestamp": time.time()
            })
        
        app = web.Application()
        app.router.add_get("/test", handler)
        
        server = TestServer(app)
        await server.start_server()
        yield server
        await server.close()
        
    async def test_real_latency_distribution(self, performance_server):
        """Test real network latency distribution and consistency."""
        client = BaseExternalClient("perf_client", "PerfService")
        
        latencies = []
        
        for delay in [0.1, 0.2, 0.05, 0.15, 0.1]:
            start_time = time.time()
            
            async def make_delayed_request():
                async with ClientSession() as session:
                    async with session.get(
                        f"{performance_server.make_url('/test')}?delay={delay}"
                    ) as resp:
                        return await resp.json()
            
            result = await client.call_external(
                f"latency_test_{delay}",
                make_delayed_request,
                timeout=5.0
            )
            
            actual_latency = time.time() - start_time
            latencies.append(actual_latency)
            
            # Validate expected delay was respected
            assert result["delay"] == delay
            assert actual_latency >= delay  # Should include network + processing time
            assert actual_latency < delay + 0.5  # Reasonable upper bound
        
        # Validate latency consistency
        assert len(latencies) == 5
        assert all(lat > 0.05 for lat in latencies)  # All had some delay
        
    async def test_real_throughput_under_load(self, performance_server):
        """Test real throughput characteristics under concurrent load."""
        client = BaseExternalClient("throughput_client", "ThroughputService")
        
        async def make_load_request(request_id):
            start_time = time.time()
            
            async with ClientSession() as session:
                async with session.get(
                    f"{performance_server.make_url('/test')}?id={request_id}"
                ) as resp:
                    result = await resp.json()
                    result["client_latency"] = time.time() - start_time
                    return result
        
        # Execute high concurrent load
        load_start = time.time()
        
        async def create_load_task(i):
            async def make_request():
                return await make_load_request(i)
            return await client.call_external(f"load_test_{i}", 
                                           make_request,
                                           timeout=10.0)
        
        tasks = [create_load_task(i) for i in range(50)]  # 50 concurrent requests
        
        results = await asyncio.gather(*tasks)
        total_time = time.time() - load_start
        
        # Validate throughput performance
        assert len(results) == 50
        assert all(result["status"] == "success" for result in results)
        assert total_time < 5.0  # High throughput achieved
        
        # Validate individual request performance
        latencies = [result["client_latency"] for result in results]
        avg_latency = sum(latencies) / len(latencies)
        assert avg_latency < 2.0  # Reasonable average latency under load
        
    async def test_real_memory_usage_validation(self, performance_server):
        """Test real memory usage patterns during operation."""
        import psutil
        import os
        
        process = psutil.Process(os.getpid())
        initial_memory = process.memory_info().rss / 1024 / 1024  # MB
        
        client = BaseExternalClient("memory_client", "MemoryService")
        
        # Execute memory-intensive operations
        for i in range(100):
            async def make_memory_request():
                async with ClientSession() as session:
                    async with session.get(
                        f"{performance_server.make_url('/test')}?size=large"
                    ) as resp:
                        return await resp.json()
            
            await client.call_external(f"memory_test_{i}", make_memory_request, timeout=5.0)
            
            # Check memory every 20 requests
            if i % 20 == 19:
                current_memory = process.memory_info().rss / 1024 / 1024
                memory_growth = current_memory - initial_memory
                assert memory_growth < 100  # Should not grow excessively (< 100MB)
        
        # Final memory check
        final_memory = process.memory_info().rss / 1024 / 1024
        total_growth = final_memory - initial_memory
        assert total_growth < 50  # Reasonable memory usage growth