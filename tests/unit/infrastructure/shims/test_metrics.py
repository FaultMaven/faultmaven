"""Unit tests for metrics shim.

Tests the graceful degradation behavior of the Prometheus metrics shim.
Verifies that the shim works correctly:
- When Prometheus is installed and enabled
- When Prometheus is installed but disabled
- When Prometheus is not installed (simulated)
- With Counter, Histogram, Gauge, Summary, and Info metrics
"""

import os
from unittest.mock import MagicMock, patch

import pytest


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


class TestHttpDurationBucketsSpanRealTraffic:
    """#1346: the request histogram must be able to express this API's latency.

    Deleting the per-request "Slow request detected" WARNING hands latency
    alerting entirely to ``http_request_duration_seconds`` — which the
    deployment scrapes and which ``FaultMavenAPIHighLatency`` alerts on via a
    p95 recording rule. That is only an improvement if the histogram can
    actually resolve the range the product serves.

    It could not. The metric was created with no ``buckets`` argument, so it
    used prometheus_client's defaults, whose highest finite bucket is 10.0s —
    below the slowest healthy investigation turn measured. ``histogram_quantile``
    returns +Inf once the quantile lands in the overflow bucket, so a healthy
    turn and a hung-provider turn were the same observation to every percentile
    computed from it.
    """

    # Measured end to end on gemini-3.7-flash against a real case.
    HEALTH_PROBE = 0.030  # /health, 27-34ms
    ORDINARY_READ = 0.4  # a DB-backed read
    HEALTHY_TURN = 14.95  # slowest healthy turn (turn 4, deepest context)
    HUNG_PROVIDER_TURN = 68.7  # retry ladder against a hung provider -> 503

    @staticmethod
    def _bucket_reached(buckets, observation):
        """The lowest bucket boundary ``observation`` falls into.

        Two durations that reach the same boundary are indistinguishable to
        any percentile computed from the histogram.
        """
        prometheus_client = pytest.importorskip("prometheus_client")

        registry = prometheus_client.CollectorRegistry()
        histogram = prometheus_client.Histogram(
            "probe_duration_seconds",
            "Bucket-reach probe",
            buckets=buckets,
            registry=registry,
        )
        histogram.observe(observation)

        counts = [
            (float(sample.labels["le"]), sample.value)
            for metric in registry.collect()
            for sample in metric.samples
            if sample.name.endswith("_bucket")
        ]
        return min(le for le, value in counts if value > 0)

    def test_a_healthy_turn_and_a_hung_one_are_distinguishable(self):
        """The property the whole decision rests on."""
        from faultmaven.infrastructure.shims.metrics import _HTTP_DURATION_BUCKETS

        healthy = self._bucket_reached(_HTTP_DURATION_BUCKETS, self.HEALTHY_TURN)
        hung = self._bucket_reached(_HTTP_DURATION_BUCKETS, self.HUNG_PROVIDER_TURN)

        assert healthy != hung, (
            f"a {self.HEALTHY_TURN}s healthy turn and a "
            f"{self.HUNG_PROVIDER_TURN}s hung one both reach bucket le={healthy}; "
            "no percentile over this histogram can tell them apart"
        )
        assert healthy != float("inf"), (
            "the slowest healthy turn measured falls in the overflow bucket, "
            "so histogram_quantile reports +Inf for the product's main path"
        )

    def test_the_fast_end_keeps_its_resolution(self):
        """Widening the tail must not coarsen probes and ordinary reads."""
        from faultmaven.infrastructure.shims.metrics import _HTTP_DURATION_BUCKETS

        probe = self._bucket_reached(_HTTP_DURATION_BUCKETS, self.HEALTH_PROBE)
        read = self._bucket_reached(_HTTP_DURATION_BUCKETS, self.ORDINARY_READ)

        assert probe != read
        assert probe <= 0.05, "a 30ms health probe must not need a 100ms+ bucket"

    def test_the_shipped_metric_is_wired_to_these_buckets(self):
        """The call site, not just the constant.

        Run out of process: the module registers its metrics in
        prometheus_client's global REGISTRY at import, and metrics are
        disabled in-process here (so ``request_duration`` is a NoOpMetric with
        no bounds to read). A subprocess with ENABLE_METRICS=true exercises
        the real call site without contaminating this session's registry.
        """
        import json
        import os
        import subprocess
        import sys

        from faultmaven.infrastructure.shims.metrics import _HTTP_DURATION_BUCKETS

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json,sys;"
                "from faultmaven.infrastructure.shims.metrics import request_duration;"
                "sys.stderr.write('@@'+json.dumps("
                "[str(b) for b in request_duration._upper_bounds])+'@@')",
            ],
            env={**os.environ, "ENABLE_METRICS": "true"},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        marked = result.stderr.split("@@")
        assert len(marked) >= 3, f"probe produced no result: {result.stderr[-2000:]}"
        bounds = [float(b) for b in json.loads(marked[-2])]

        assert bounds == list(_HTTP_DURATION_BUCKETS), (
            "http_request_duration_seconds is not using the buckets chosen for "
            "this API's measured latency range"
        )
