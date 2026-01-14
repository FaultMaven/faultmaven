"""Unit tests for metrics shim.

Tests the graceful degradation behavior of the Prometheus metrics shim.
Verifies that the shim works correctly:
- When Prometheus is installed and enabled
- When Prometheus is installed but disabled
- When Prometheus is not installed (simulated)
- With Counter, Histogram, Gauge, Summary, and Info metrics
"""

import os
from unittest.mock import patch, MagicMock
import pytest


class TestMetricCreation:
    """Tests for metric creation functions."""

    def test_counter_creation_disabled_by_default(self):
        """Test Counter creation when ENABLE_METRICS is not set."""
        with patch.dict(os.environ, {}, clear=True):
            from faultmaven.infrastructure.shims.metrics import Counter

            counter = Counter("test_counter", "Test counter description")

            # Should be a NoOpMetric
            counter.inc()  # Should not raise
            counter.inc(5)
            counter.labels(method="GET").inc()

    def test_counter_creation_when_disabled(self):
        """Test Counter creation when ENABLE_METRICS=false."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.shims.metrics import Counter

            counter = Counter("test_counter", "Test counter")
            counter.inc()  # Should not raise

    def test_histogram_creation_disabled_by_default(self):
        """Test Histogram creation when ENABLE_METRICS is not set."""
        with patch.dict(os.environ, {}, clear=True):
            from faultmaven.infrastructure.shims.metrics import Histogram

            histogram = Histogram("test_histogram", "Test histogram")
            histogram.observe(1.5)  # Should not raise
            histogram.labels(method="POST").observe(2.5)

    def test_gauge_creation_disabled_by_default(self):
        """Test Gauge creation when ENABLE_METRICS is not set."""
        with patch.dict(os.environ, {}, clear=True):
            from faultmaven.infrastructure.shims.metrics import Gauge

            gauge = Gauge("test_gauge", "Test gauge")
            gauge.set(42)  # Should not raise
            gauge.inc()
            gauge.dec()
            gauge.labels(status="active").set(10)

    def test_summary_creation_disabled_by_default(self):
        """Test Summary creation when ENABLE_METRICS is not set."""
        with patch.dict(os.environ, {}, clear=True):
            from faultmaven.infrastructure.shims.metrics import Summary

            summary = Summary("test_summary", "Test summary")
            summary.observe(3.14)  # Should not raise

    def test_info_creation_disabled_by_default(self):
        """Test Info creation when ENABLE_METRICS is not set."""
        with patch.dict(os.environ, {}, clear=True):
            from faultmaven.infrastructure.shims.metrics import Info

            info = Info("test_info", "Test info")
            info.info({"version": "1.0.0"})  # Should not raise


class TestNoOpMetricBehavior:
    """Tests for NoOpMetric behavior when metrics are disabled."""

    def test_noop_counter_all_methods(self):
        """Test all Counter methods work as no-ops."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.shims.metrics import Counter

            counter = Counter("noop_counter", "Test")

            # All methods should work without raising
            counter.inc()
            counter.inc(5)
            counter.inc(amount=10)

            # Labels should return self for chaining
            labeled = counter.labels(method="GET", endpoint="/api")
            labeled.inc()
            labeled.inc(2)

    def test_noop_histogram_all_methods(self):
        """Test all Histogram methods work as no-ops."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.shims.metrics import Histogram

            histogram = Histogram("noop_histogram", "Test")

            # All methods should work
            histogram.observe(1.5)
            histogram.observe(amount=2.5)

            # Labels and chaining
            labeled = histogram.labels(method="POST")
            labeled.observe(3.5)

            # Time context manager
            with histogram.time():
                pass  # Should work as no-op

    def test_noop_gauge_all_methods(self):
        """Test all Gauge methods work as no-ops."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.shims.metrics import Gauge

            gauge = Gauge("noop_gauge", "Test")

            # All methods should work
            gauge.set(42)
            gauge.inc()
            gauge.inc(5)
            gauge.dec()
            gauge.dec(3)

            # Labels and chaining
            labeled = gauge.labels(status="active")
            labeled.set(10)
            labeled.inc()
            labeled.dec()

    def test_noop_summary_all_methods(self):
        """Test all Summary methods work as no-ops."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.shims.metrics import Summary

            summary = Summary("noop_summary", "Test")

            summary.observe(1.23)
            summary.labels(endpoint="/api").observe(4.56)

    def test_noop_info_all_methods(self):
        """Test all Info methods work as no-ops."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.shims.metrics import Info

            info = Info("noop_info", "Test")

            info.info({"version": "1.0.0", "environment": "test"})
            info.labels(service="api").info({"status": "running"})


class TestMetricParameters:
    """Tests for metric creation with various parameters."""

    def test_counter_with_labelnames(self):
        """Test Counter creation with label names."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.shims.metrics import Counter

            counter = Counter(
                "labeled_counter",
                "Counter with labels",
                labelnames=["method", "endpoint", "status"],
            )

            counter.labels(method="GET", endpoint="/api", status="200").inc()

    def test_histogram_with_buckets(self):
        """Test Histogram creation with custom buckets."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.shims.metrics import Histogram

            histogram = Histogram(
                "bucketed_histogram",
                "Histogram with custom buckets",
                buckets=[0.1, 0.5, 1.0, 2.5, 5.0],
            )

            histogram.observe(0.75)

    def test_metric_with_namespace_and_subsystem(self):
        """Test metric creation with namespace and subsystem."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.shims.metrics import Counter

            counter = Counter(
                "namespaced_counter",
                "Counter with namespace",
                namespace="faultmaven",
                subsystem="api",
            )

            counter.inc()

    def test_metric_with_unit(self):
        """Test metric creation with unit specification."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.shims.metrics import Histogram

            histogram = Histogram("duration", "Request duration", unit="seconds")

            histogram.observe(1.5)


class TestMetricsStatus:
    """Tests for metrics status functions."""

    def test_get_metrics_status_disabled(self):
        """Test get_metrics_status when metrics are disabled."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.shims.metrics import get_metrics_status

            status = get_metrics_status()

            assert "prometheus_available" in status
            assert "metrics_enabled" in status
            assert "active" in status
            assert status["metrics_enabled"] is False
            assert status["active"] is False

    def test_get_metrics_status_enabled_without_prometheus(self):
        """Test get_metrics_status when enabled but Prometheus not available."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "true"}):
            with patch(
                "faultmaven.infrastructure.shims.metrics.PROMETHEUS_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import metrics

                status = metrics.get_metrics_status()

                assert status["metrics_enabled"] is True
                assert status["active"] is False  # Can't be active without Prometheus

    def test_is_metrics_active_false_by_default(self):
        """Test is_metrics_active returns False by default."""
        with patch.dict(os.environ, {}, clear=True):
            from faultmaven.infrastructure.shims.metrics import is_metrics_active

            assert is_metrics_active() is False

    def test_is_metrics_active_when_enabled_without_prometheus(self):
        """Test is_metrics_active when enabled but Prometheus not installed."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "true"}):
            with patch(
                "faultmaven.infrastructure.shims.metrics.PROMETHEUS_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import metrics

                result = metrics.is_metrics_active()
                assert result is False


class TestPrometheusAvailability:
    """Tests for Prometheus availability detection."""

    def test_prometheus_available_constant_exists(self):
        """Test that PROMETHEUS_AVAILABLE constant is exported."""
        from faultmaven.infrastructure.shims.metrics import PROMETHEUS_AVAILABLE

        assert isinstance(PROMETHEUS_AVAILABLE, bool)

    def test_counter_with_prometheus_unavailable(self):
        """Test Counter when Prometheus is simulated as unavailable."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "true"}):
            with patch(
                "faultmaven.infrastructure.shims.metrics.PROMETHEUS_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import metrics

                counter = metrics.Counter("test_counter", "Test")
                counter.inc()  # Should work as no-op

    def test_histogram_with_prometheus_unavailable(self):
        """Test Histogram when Prometheus is simulated as unavailable."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "true"}):
            with patch(
                "faultmaven.infrastructure.shims.metrics.PROMETHEUS_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import metrics

                histogram = metrics.Histogram("test_histogram", "Test")
                histogram.observe(1.5)  # Should work as no-op

    def test_gauge_with_prometheus_unavailable(self):
        """Test Gauge when Prometheus is simulated as unavailable."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "true"}):
            with patch(
                "faultmaven.infrastructure.shims.metrics.PROMETHEUS_AVAILABLE", False
            ):
                from faultmaven.infrastructure.shims import metrics

                gauge = metrics.Gauge("test_gauge", "Test")
                gauge.set(42)  # Should work as no-op


class TestEnvironmentVariableHandling:
    """Tests for environment variable edge cases."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("false", False),
            ("False", False),
            ("FALSE", False),
            ("0", False),
            ("1", False),  # Only "true" (case-insensitive) enables
            ("yes", False),
            ("", False),
        ],
    )
    def test_enable_metrics_values(self, value: str, expected: bool):
        """Test various ENABLE_METRICS values."""
        with patch.dict(os.environ, {"ENABLE_METRICS": value}):
            from faultmaven.infrastructure.shims.metrics import _is_metrics_enabled

            result = _is_metrics_enabled()
            assert result is expected, f"ENABLE_METRICS={value} should be {expected}"


class TestPredefinedMetrics:
    """Tests for pre-defined common metrics."""

    def test_request_counter_exists(self):
        """Test that request_counter is exported and works."""
        from faultmaven.infrastructure.shims.metrics import request_counter

        # Should work regardless of metrics being enabled
        request_counter.labels(method="GET", endpoint="/api", status_code="200").inc()

    def test_request_duration_exists(self):
        """Test that request_duration is exported and works."""
        from faultmaven.infrastructure.shims.metrics import request_duration

        request_duration.labels(method="POST", endpoint="/api").observe(1.5)

    def test_active_sessions_exists(self):
        """Test that active_sessions is exported and works."""
        from faultmaven.infrastructure.shims.metrics import active_sessions

        active_sessions.labels(status="active").set(10)
        active_sessions.labels(status="active").inc()
        active_sessions.labels(status="active").dec()

    def test_case_operations_exists(self):
        """Test that case_operations is exported and works."""
        from faultmaven.infrastructure.shims.metrics import case_operations

        case_operations.labels(operation="create", status="success").inc()

    def test_knowledge_queries_exists(self):
        """Test that knowledge_queries is exported and works."""
        from faultmaven.infrastructure.shims.metrics import knowledge_queries

        knowledge_queries.labels(query_type="vector", status="success").inc()

    def test_llm_requests_exists(self):
        """Test that llm_requests is exported and works."""
        from faultmaven.infrastructure.shims.metrics import llm_requests

        llm_requests.labels(provider="openai", model="gpt-4", status="success").inc()

    def test_llm_latency_exists(self):
        """Test that llm_latency is exported and works."""
        from faultmaven.infrastructure.shims.metrics import llm_latency

        llm_latency.labels(provider="openai", model="gpt-4").observe(2.5)


class TestIntegrationWithPrometheus:
    """Tests for integration with real Prometheus library if available."""

    def test_counter_with_prometheus_enabled(self):
        """Test Counter with Prometheus enabled (if available)."""
        from faultmaven.infrastructure.shims.metrics import PROMETHEUS_AVAILABLE

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("Prometheus not installed - skipping integration test")

        with patch.dict(os.environ, {"ENABLE_METRICS": "true"}):
            from faultmaven.infrastructure.shims.metrics import Counter

            counter = Counter("integration_test_counter", "Integration test counter")
            counter.inc()
            counter.inc(5)

    def test_histogram_with_prometheus_enabled(self):
        """Test Histogram with Prometheus enabled (if available)."""
        from faultmaven.infrastructure.shims.metrics import PROMETHEUS_AVAILABLE

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("Prometheus not installed - skipping integration test")

        with patch.dict(os.environ, {"ENABLE_METRICS": "true"}):
            from faultmaven.infrastructure.shims.metrics import Histogram

            histogram = Histogram(
                "integration_test_histogram", "Integration test histogram"
            )
            histogram.observe(1.5)

    def test_gauge_with_prometheus_enabled(self):
        """Test Gauge with Prometheus enabled (if available)."""
        from faultmaven.infrastructure.shims.metrics import PROMETHEUS_AVAILABLE

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("Prometheus not installed - skipping integration test")

        with patch.dict(os.environ, {"ENABLE_METRICS": "true"}):
            from faultmaven.infrastructure.shims.metrics import Gauge

            gauge = Gauge("integration_test_gauge", "Integration test gauge")
            gauge.set(42)
            gauge.inc()
            gauge.dec()

    def test_get_metrics_status_with_prometheus(self):
        """Test status when Prometheus is actually available."""
        from faultmaven.infrastructure.shims.metrics import (
            PROMETHEUS_AVAILABLE,
            get_metrics_status,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("Prometheus not installed - skipping integration test")

        with patch.dict(os.environ, {"ENABLE_METRICS": "true"}):
            status = get_metrics_status()

            assert status["prometheus_available"] is True
            assert status["metrics_enabled"] is True
            assert status["active"] is True


class TestHistogramTimerContextManager:
    """Tests for Histogram timer context manager."""

    def test_histogram_time_context_manager_noop(self):
        """Test Histogram.time() works as no-op when disabled."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.shims.metrics import Histogram

            histogram = Histogram("timer_test", "Test")

            with histogram.time():
                # Do some work
                total = sum(range(100))
                assert total == 4950

    def test_histogram_time_context_manager_enabled(self):
        """Test Histogram.time() when Prometheus available."""
        from faultmaven.infrastructure.shims.metrics import PROMETHEUS_AVAILABLE

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("Prometheus not installed - skipping integration test")

        with patch.dict(os.environ, {"ENABLE_METRICS": "true"}):
            from faultmaven.infrastructure.shims.metrics import Histogram

            histogram = Histogram("timer_test_enabled", "Test")

            # Should work and record timing
            with histogram.time():
                total = sum(range(100))
                assert total == 4950


class TestShimExports:
    """Tests for shim package exports."""

    def test_counter_exported_from_shims_package(self):
        """Test Counter is exported from shims package."""
        from faultmaven.infrastructure.shims import Counter

        counter = Counter("package_test", "Test")
        counter.inc()

    def test_histogram_exported_from_shims_package(self):
        """Test Histogram is exported from shims package."""
        from faultmaven.infrastructure.shims import Histogram

        histogram = Histogram("package_test", "Test")
        histogram.observe(1.0)

    def test_gauge_exported_from_shims_package(self):
        """Test Gauge is exported from shims package."""
        from faultmaven.infrastructure.shims import Gauge

        gauge = Gauge("package_test", "Test")
        gauge.set(5)

    def test_predefined_metrics_exported_from_shims_package(self):
        """Test pre-defined metrics are exported from shims package."""
        from faultmaven.infrastructure.shims import (
            request_counter,
            request_duration,
            active_sessions,
            case_operations,
            knowledge_queries,
            llm_requests,
            llm_latency,
        )

        # All should be accessible
        request_counter.inc()
        request_duration.observe(1.0)
        active_sessions.set(10)
        case_operations.inc()
        knowledge_queries.inc()
        llm_requests.inc()
        llm_latency.observe(2.0)

    def test_metrics_status_functions_exported(self):
        """Test metrics status functions are exported."""
        from faultmaven.infrastructure.shims import (
            get_metrics_status,
            is_metrics_active,
        )

        status = get_metrics_status()
        assert isinstance(status, dict)

        active = is_metrics_active()
        assert isinstance(active, bool)


class TestNoOpMetricEdgeCases:
    """Tests for edge cases in NoOpMetric implementation."""

    def test_noop_metric_accepts_arbitrary_args(self):
        """Test NoOpMetric methods accept arbitrary arguments."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.shims.metrics import Counter

            counter = Counter("edge_case_test", "Test")

            # Should handle various argument patterns
            counter.inc(1, extra_arg="ignored")
            counter.inc(amount=5, another_arg="also_ignored")

    def test_noop_metric_labels_chaining(self):
        """Test NoOpMetric labels() returns chainable object."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.shims.metrics import Counter

            counter = Counter("chain_test", "Test", labelnames=["method", "endpoint"])

            # Should support chaining
            result = counter.labels(method="GET", endpoint="/api").labels(
                extra="ignored"
            )
            result.inc()

    def test_noop_context_manager_exception_handling(self):
        """Test NoOpContextManager handles exceptions correctly."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.shims.metrics import Histogram

            histogram = Histogram("exception_test", "Test")

            # Exception should propagate through context manager
            with pytest.raises(ValueError):
                with histogram.time():
                    raise ValueError("Test exception")
