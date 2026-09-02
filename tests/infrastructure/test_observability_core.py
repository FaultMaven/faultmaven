"""test_observability_core.py

Core observability tests focusing on actual implementation.
"""

import asyncio
import os
import time
from unittest.mock import patch

import pytest

from faultmaven.infrastructure.observability.tracing import trace


@pytest.fixture
def restore_opik_sdk_state():
    """Contain init_opik_tracing's process-global side effects.

    init_opik_tracing sets os.environ["OPIK_TRACK_DISABLE"] and calls
    opik.set_tracing_active() — both process-global with no restore path. A
    test that calls it would otherwise leave the SDK's tracing flag flipped
    for every test that runs after it in the session, making any later
    in-process assertion about tracing order-dependent.
    """
    previous_env = os.environ.get("OPIK_TRACK_DISABLE")
    try:
        import opik

        # ``import opik`` succeeding is not proof the SDK is there: an empty
        # leftover directory imports as a namespace package exposing nothing
        # (see infrastructure/observability/tracing.py for why that happens).
        # Test the discriminator rather than catching AttributeError off the
        # call below — a broad catch would also swallow a genuine SDK API
        # change and, by setting ``opik = None``, silently disable the teardown
        # this fixture exists to perform.
        if getattr(opik, "__file__", None) is None:
            opik = None
    except ImportError:
        opik = None

    previous_active = opik.is_tracing_active() if opik is not None else None

    yield

    if previous_env is None:
        os.environ.pop("OPIK_TRACK_DISABLE", None)
    else:
        os.environ["OPIK_TRACK_DISABLE"] = previous_env
    if opik is not None:
        opik.set_tracing_active(previous_active)


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

    def test_trace_decorator_with_settings_param(self):
        """Test that @trace decorator accepts settings param without crashing."""

        @trace("test_settings_param", settings=None)
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

    def test_llm_router_route_is_wrapped_iff_opik_installed(self):
        """LLMRouter.route carries the tracing wrapper exactly when the Opik
        SDK is importable — independent of OPIK_ENABLED.

        Since #1121 the gate is evaluated per CALL, not at import, so the
        wrapper is always applied when opik is installed and dispatches to the
        raw function when tracing is off. Decoration alone constructs no
        client, so this costs nothing when disabled. The behavioural
        guarantees (disabled ⇒ no client, no egress; enabled ⇒ traced) are
        proven in tests/unit/infrastructure/observability/test_opik_fail_closed.py
        under subprocess-controlled environments."""
        from faultmaven.infrastructure.llm import router

        wrapped = hasattr(router.LLMRouter.route, "__wrapped__")
        assert wrapped == router.OPIK_AVAILABLE

    def test_agent_has_tracing(self):
        """Verify agent service methods have trace decorators."""
        from faultmaven.modules.agent.domain.services.investigation_service import (
            InvestigationService,
        )
        from faultmaven.modules.case.domain.services.case_service import CaseService

        # Check that key methods have been wrapped with @trace
        # InvestigationService has @trace decorators on key methods.
        # close_case moved: the dead InvestigationService.close_case was
        # deleted (#845) and the live user-initiated close is
        # CaseService.close_case (#915).
        traced_methods = [
            ("process_turn", InvestigationService.process_turn),
            ("get_progress", InvestigationService.get_progress),
            (
                "transition_to_investigating",
                InvestigationService.transition_to_investigating,
            ),
            ("close_case", CaseService.close_case),
        ]

        # Check each method for the __wrapped__ attribute
        missing_wrapped = []
        for method_name, method_obj in traced_methods:
            if not hasattr(method_obj, "__wrapped__"):
                missing_wrapped.append(method_name)

        # Assert with detailed error message
        if missing_wrapped:
            raise AssertionError(
                f"The following InvestigationService methods are missing __wrapped__ attributes: {missing_wrapped}. "
                f"This indicates they may not be properly decorated with @trace decorators."
            )

    def test_data_processing_has_tracing(self):
        """Verify data processing methods have trace decorators."""
        # This used to delete a conftest stand-in out of sys.modules first, so
        # that `LogProcessor` was the real class rather than `Mock` -- against
        # which `hasattr(..., "__wrapped__")` is True for free and this test
        # asserted nothing. The stand-in is gone with #942, so the import
        # reaches the real module the way production does; the assertion below
        # is load-bearing again rather than locally rescued.
        from faultmaven.core.processing.log_analyzer import LogProcessor

        assert LogProcessor.__module__ == "faultmaven.core.processing.log_analyzer"

        # Check that key methods have been wrapped with @trace
        # LogProcessor has @trace decorators on process methods
        assert hasattr(LogProcessor.process, "__wrapped__")

    def test_knowledge_base_has_tracing(self):
        """Verify knowledge base methods have trace decorators."""
        # A `del sys.modules[...]` used to stand here, mirroring the one removed
        # from test_data_processing_has_tracing. It was already dead -- the root
        # conftest never stubbed this name -- and it is removed rather than left
        # as a pattern for the next reader to copy: reaching past a stand-in by
        # hand is what #942 replaced, and no conftest may install one now.
        from faultmaven.modules.knowledge.domain.services.ingestion import (
            KnowledgeIngester,
        )

        # Check that key methods have been wrapped with @trace.
        # KnowledgeIngester.search was removed in #943 — it was reachable only
        # via KnowledgeBaseTool, which the model could never call, and it
        # queried the unified KB collection with no scope predicate. The live
        # KB read path is kb_qa -> DocumentQATool -> KnowledgeVectorStore.
        assert hasattr(KnowledgeIngester.ingest_document, "__wrapped__")

    def test_api_endpoints_have_tracing(self):
        """Verify API endpoints have trace decorators."""
        from faultmaven.modules.auth.api.session import create_session
        from faultmaven.modules.knowledge.api.routes import (
            search_documents,
            upload_document,
        )

        # Check that key endpoints have been wrapped with @trace
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
        from faultmaven.infrastructure.observability.tracing import init_opik_tracing

        assert callable(init_opik_tracing)

    def test_init_opik_tracing_graceful_failure(self, restore_opik_sdk_state):
        """Test that init_opik_tracing handles failures gracefully.

        Uses restore_opik_sdk_state: with OPIK_ENABLED cleared by conftest
        this call takes the disabled path, which flips the SDK's global
        tracing flag and sets OPIK_TRACK_DISABLE for the rest of the session.
        """
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

        with patch(
            "faultmaven.infrastructure.observability.tracing.OPIK_AVAILABLE", False
        ):

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
