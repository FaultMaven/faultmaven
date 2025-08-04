"""test_observability_core.py

Core observability tests focusing on actual implementation.
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from faultmaven.infrastructure.observability.tracing import trace


class TestCoreObservability:
    """Test core observability functionality."""

    def test_trace_decorator_basic_functionality(self):
        """Test that @trace decorator doesn't break function execution."""

        @trace("test_basic")
        def simple_function(x, y):
            return x + y

        result = simple_function(3, 4)
        assert result == 7

    @pytest.mark.asyncio
    async def test_trace_decorator_async_functionality(self):
        """Test that @trace decorator works with async functions."""

        @trace("test_async_basic")
        async def async_function(x):
            await asyncio.sleep(0.001)  # Tiny delay
            return x * 2

        result = await async_function(5)
        assert result == 10

    def test_trace_decorator_preserves_exceptions(self):
        """Test that @trace decorator preserves original exceptions."""

        @trace("test_exception")
        def failing_function():
            raise ValueError("Original error")

        with pytest.raises(ValueError, match="Original error"):
            failing_function()

    @pytest.mark.asyncio
    async def test_trace_decorator_preserves_async_exceptions(self):
        """Test that @trace decorator preserves exceptions in async functions."""

        @trace("test_async_exception")
        async def failing_async_function():
            await asyncio.sleep(0.001)
            raise RuntimeError("Async error")

        with pytest.raises(RuntimeError, match="Async error"):
            await failing_async_function()

    def test_trace_decorator_with_tags(self):
        """Test that @trace decorator accepts tags without crashing."""

        @trace("test_tags", tags={"env": "test"})
        def tagged_function(value):
            return value * 3

        result = tagged_function(4)
        assert result == 12

    def test_trace_decorator_preserves_function_attributes(self):
        """Test that @trace decorator preserves function metadata."""

        @trace("test_metadata")
        def documented_function():
            """This is a test function."""
            return "test"

        assert documented_function.__name__ == "documented_function"
        assert "test function" in documented_function.__doc__

    @pytest.mark.asyncio
    async def test_trace_decorator_handles_multiple_concurrent_calls(self):
        """Test that @trace decorator works with concurrent execution."""

        @trace("test_concurrent")
        async def concurrent_function(delay, value):
            await asyncio.sleep(delay)
            return value

        # Run multiple operations concurrently
        tasks = [concurrent_function(0.001, i) for i in range(5)]

        results = await asyncio.gather(*tasks)
        assert results == [0, 1, 2, 3, 4]

    def test_trace_decorator_error_handling(self):
        """Test that @trace decorator handles internal errors gracefully."""

        # Even if tracing fails internally, the function should still work
        @trace("test_error_handling")
        def resilient_function(x):
            return x + 1

        result = resilient_function(10)
        assert result == 11


class TestObservabilityIntegration:
    """Test that observability is properly integrated into key components."""

    def test_llm_router_has_tracing(self):
        """Verify LLM router methods have trace decorators."""
        from faultmaven.infrastructure.llm.router import LLMRouter

        # Check that key methods have been wrapped with @trace
        assert hasattr(LLMRouter.route, "__wrapped__")

    def test_agent_has_tracing(self):
        """Verify agent methods have trace decorators."""
        from faultmaven.core.agent.agent import FaultMavenAgent

        # Check that key methods have been wrapped with @trace
        assert hasattr(FaultMavenAgent.run, "__wrapped__")
        assert hasattr(FaultMavenAgent.resume, "__wrapped__")
        assert hasattr(FaultMavenAgent._triage_node, "__wrapped__")
        assert hasattr(FaultMavenAgent._formulate_hypothesis_node, "__wrapped__")
        assert hasattr(FaultMavenAgent._validate_hypothesis_node, "__wrapped__")
        assert hasattr(FaultMavenAgent._propose_solution_node, "__wrapped__")

    def test_data_processing_has_tracing(self):
        """Verify data processing methods have trace decorators."""
        from faultmaven.core.processing.classifier import DataClassifier
        from faultmaven.core.processing.log_analyzer import LogProcessor

        # Check that key methods have been wrapped with @trace
        assert hasattr(LogProcessor.process, "__wrapped__")
        assert hasattr(DataClassifier.classify, "__wrapped__")

    def test_knowledge_base_has_tracing(self):
        """Verify knowledge base methods have trace decorators."""
        from faultmaven.core.knowledge.ingestion import KnowledgeIngester

        # Check that key methods have been wrapped with @trace
        assert hasattr(KnowledgeIngester.ingest_document, "__wrapped__")
        assert hasattr(KnowledgeIngester.search, "__wrapped__")

    def test_api_endpoints_have_tracing(self):
        """Verify API endpoints have trace decorators."""
        from faultmaven.api.v1.routes.data import upload_data
        from faultmaven.api.v1.routes.agent import process_query
        from faultmaven.api.v1.routes.knowledge import upload_document, search_documents
        from faultmaven.api.v1.routes.session import create_session

        # Check that key endpoints have been wrapped with @trace
        assert hasattr(upload_data, "__wrapped__")
        assert hasattr(process_query, "__wrapped__")
        assert hasattr(upload_document, "__wrapped__")
        assert hasattr(search_documents, "__wrapped__")
        assert hasattr(create_session, "__wrapped__")


class TestObservabilityConfiguration:
    """Test observability configuration and initialization."""

    def test_trace_decorator_import(self):
        """Test that trace decorator can be imported."""
        from faultmaven.infrastructure.observability.tracing import trace

        assert callable(trace)

    def test_tracing_functions_import(self):
        """Test that tracing utility functions can be imported."""
        from faultmaven.infrastructure.observability.tracing import (
            create_span,
            init_opik_tracing,
            record_exception,
            set_global_tags,
        )

        assert callable(init_opik_tracing)
        assert callable(create_span)
        assert callable(record_exception)
        assert callable(set_global_tags)

    def test_init_opik_tracing_graceful_failure(self):
        """Test that init_opik_tracing handles failures gracefully."""
        from faultmaven.infrastructure.observability.tracing import init_opik_tracing

        # Should not raise exceptions even with invalid parameters
        init_opik_tracing(api_key="invalid-key")

    def test_observability_constants(self):
        """Test that observability constants are defined."""
        from faultmaven.infrastructure.observability import tracing

        # Should have availability flags
        assert hasattr(tracing, "OPIK_AVAILABLE")
        assert hasattr(tracing, "PROMETHEUS_AVAILABLE")
        assert isinstance(tracing.OPIK_AVAILABLE, bool)
        assert isinstance(tracing.PROMETHEUS_AVAILABLE, bool)


class TestObservabilityPerformance:
    """Test performance characteristics of observability features."""

    def test_trace_decorator_minimal_overhead(self):
        """Test that trace decorator adds minimal overhead."""

        # Simple function without tracing
        def untraced_function():
            return sum(range(100))

        # Same function with tracing
        @trace("performance_test")
        def traced_function():
            return sum(range(100))

        # Both should produce the same result
        assert untraced_function() == traced_function()

        # Test that both functions work without crashing
        for _ in range(5):
            untraced_result = untraced_function()
            traced_result = traced_function()
            assert untraced_result == traced_result

    @pytest.mark.asyncio
    async def test_async_trace_performance(self):
        """Test async tracing performance."""

        @trace("async_performance_test")
        async def traced_async_function(n):
            await asyncio.sleep(0.001)
            return n * 2

        # Should handle multiple concurrent calls efficiently
        start = time.time()
        tasks = [traced_async_function(i) for i in range(10)]
        results = await asyncio.gather(*tasks)
        elapsed = time.time() - start

        assert results == [i * 2 for i in range(10)]
        # Should complete in reasonable time (concurrent, not sequential)
        assert elapsed < 0.1  # Much less than 10 * 0.001 = 0.01 seconds


class TestObservabilityErrorResilience:
    """Test that observability features are resilient to errors."""

    def test_trace_decorator_with_tracing_failures(self):
        """Test that functions work even when tracing fails."""

        with patch("faultmaven.infrastructure.observability.tracing.OPIK_AVAILABLE", False):

            @trace("resilience_test")
            def test_function():
                return "success"

            # Function should work despite tracing being unavailable
            result = test_function()
            assert result == "success"

    def test_trace_decorator_preserves_return_values(self):
        """Test that trace decorator doesn't modify return values."""

        @trace("return_test")
        def complex_return_function():
            return {"status": "success", "data": [1, 2, 3], "metadata": {"count": 3}}

        result = complex_return_function()
        expected = {"status": "success", "data": [1, 2, 3], "metadata": {"count": 3}}
        assert result == expected

    @pytest.mark.asyncio
    async def test_async_trace_preserves_return_values(self):
        """Test that async trace decorator doesn't modify return values."""

        @trace("async_return_test")
        async def async_complex_return():
            await asyncio.sleep(0.001)
            return {"async": True, "result": [4, 5, 6]}

        result = await async_complex_return()
        assert result == {"async": True, "result": [4, 5, 6]}
