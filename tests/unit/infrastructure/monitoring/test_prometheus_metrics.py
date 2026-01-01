"""Tests for Prometheus metrics module.

Tests cover:
- Metrics availability detection
- Build info metric initialization
- HTTP request metrics recording
- LLM request metrics recording
- ML model load metrics recording
- Metrics output generation
- Disabled state behavior
"""

import pytest
import os
from unittest.mock import patch, MagicMock


class TestPrometheusMetricsAvailability:
    """Test prometheus_client availability detection."""

    def test_prometheus_available_when_installed(self):
        """Test that PROMETHEUS_AVAILABLE is True when prometheus_client is installed."""
        from faultmaven.infrastructure.monitoring.prometheus_metrics import (
            PROMETHEUS_AVAILABLE,
        )
        # prometheus_client is installed in the test environment
        assert PROMETHEUS_AVAILABLE is True

    def test_is_metrics_enabled_false_by_default(self):
        """Test that metrics are disabled by default."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove ENABLE_METRICS if it exists
            os.environ.pop("ENABLE_METRICS", None)

            # Re-import to get fresh state
            from faultmaven.infrastructure.monitoring import prometheus_metrics

            # Check the function directly reads env
            result = prometheus_metrics._is_metrics_enabled()
            assert result is False

    def test_is_metrics_enabled_when_set_true(self):
        """Test that metrics are enabled when ENABLE_METRICS=true."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "true"}):
            from faultmaven.infrastructure.monitoring import prometheus_metrics
            result = prometheus_metrics._is_metrics_enabled()
            assert result is True

    def test_get_metrics_status(self):
        """Test get_metrics_status returns correct structure."""
        from faultmaven.infrastructure.monitoring.prometheus_metrics import (
            get_metrics_status,
            PROMETHEUS_AVAILABLE,
        )

        status = get_metrics_status()

        assert "prometheus_available" in status
        assert "metrics_enabled" in status
        assert "active" in status
        assert status["prometheus_available"] == PROMETHEUS_AVAILABLE


class TestBuildInfoMetric:
    """Test build info metric initialization."""

    def test_build_info_metric_exists(self):
        """Test that BUILD_INFO metric is created."""
        from faultmaven.infrastructure.monitoring.prometheus_metrics import (
            BUILD_INFO,
            PROMETHEUS_AVAILABLE,
        )

        if PROMETHEUS_AVAILABLE:
            # Should be a Gauge metric
            assert BUILD_INFO is not None
            # Should have labels method
            assert hasattr(BUILD_INFO, "labels")

    def test_build_info_has_correct_labels(self):
        """Test that build info has version, commit, environment labels."""
        from faultmaven.infrastructure.monitoring.prometheus_metrics import (
            BUILD_INFO,
            PROMETHEUS_AVAILABLE,
        )

        if PROMETHEUS_AVAILABLE:
            # Check that we can call labels with expected arguments
            labeled = BUILD_INFO.labels(
                version="1.0.0",
                commit="abc123",
                environment="test",
            )
            assert labeled is not None


class TestHTTPRequestMetrics:
    """Test HTTP request metrics."""

    def test_http_requests_counter_exists(self):
        """Test that HTTP_REQUESTS counter is created."""
        from faultmaven.infrastructure.monitoring.prometheus_metrics import (
            HTTP_REQUESTS,
            PROMETHEUS_AVAILABLE,
        )

        if PROMETHEUS_AVAILABLE:
            assert HTTP_REQUESTS is not None
            assert hasattr(HTTP_REQUESTS, "labels")

    def test_http_latency_histogram_exists(self):
        """Test that HTTP_LATENCY histogram is created."""
        from faultmaven.infrastructure.monitoring.prometheus_metrics import (
            HTTP_LATENCY,
            PROMETHEUS_AVAILABLE,
        )

        if PROMETHEUS_AVAILABLE:
            assert HTTP_LATENCY is not None
            assert hasattr(HTTP_LATENCY, "labels")

    def test_record_http_request_with_metrics_enabled(self):
        """Test recording HTTP request when metrics enabled."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "true"}):
            from faultmaven.infrastructure.monitoring.prometheus_metrics import (
                record_http_request,
            )

            # Should not raise
            record_http_request(
                endpoint="/cases/{id}",
                method="GET",
                status_code=200,
                duration_seconds=0.5,
            )

    def test_record_http_request_with_metrics_disabled(self):
        """Test recording HTTP request when metrics disabled (no-op)."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.monitoring.prometheus_metrics import (
                record_http_request,
            )

            # Should not raise, just no-op
            record_http_request(
                endpoint="/cases/{id}",
                method="GET",
                status_code=200,
                duration_seconds=0.5,
            )


class TestLLMRequestMetrics:
    """Test LLM request metrics."""

    def test_llm_requests_counter_exists(self):
        """Test that LLM_REQUESTS counter is created."""
        from faultmaven.infrastructure.monitoring.prometheus_metrics import (
            LLM_REQUESTS,
            PROMETHEUS_AVAILABLE,
        )

        if PROMETHEUS_AVAILABLE:
            assert LLM_REQUESTS is not None
            assert hasattr(LLM_REQUESTS, "labels")

    def test_llm_latency_histogram_exists(self):
        """Test that LLM_LATENCY histogram is created."""
        from faultmaven.infrastructure.monitoring.prometheus_metrics import (
            LLM_LATENCY,
            PROMETHEUS_AVAILABLE,
        )

        if PROMETHEUS_AVAILABLE:
            assert LLM_LATENCY is not None
            assert hasattr(LLM_LATENCY, "labels")

    def test_record_llm_request(self):
        """Test recording LLM request."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "true"}):
            from faultmaven.infrastructure.monitoring.prometheus_metrics import (
                record_llm_request,
            )

            # Should not raise
            record_llm_request(
                provider="openai",
                model="gpt-4o",
                status="success",
                duration_seconds=1.5,
            )


class TestMLModelMetrics:
    """Test ML model load metrics."""

    def test_ml_model_load_time_histogram_exists(self):
        """Test that ML_MODEL_LOAD_TIME histogram is created."""
        from faultmaven.infrastructure.monitoring.prometheus_metrics import (
            ML_MODEL_LOAD_TIME,
            PROMETHEUS_AVAILABLE,
        )

        if PROMETHEUS_AVAILABLE:
            assert ML_MODEL_LOAD_TIME is not None
            assert hasattr(ML_MODEL_LOAD_TIME, "labels")

    def test_ml_model_loaded_gauge_exists(self):
        """Test that ML_MODEL_LOADED gauge is created."""
        from faultmaven.infrastructure.monitoring.prometheus_metrics import (
            ML_MODEL_LOADED,
            PROMETHEUS_AVAILABLE,
        )

        if PROMETHEUS_AVAILABLE:
            assert ML_MODEL_LOADED is not None
            assert hasattr(ML_MODEL_LOADED, "labels")

    def test_record_ml_model_load(self):
        """Test recording ML model load."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "true"}):
            from faultmaven.infrastructure.monitoring.prometheus_metrics import (
                record_ml_model_load,
            )

            # Should not raise
            record_ml_model_load(
                model="BAAI/bge-m3",
                duration_seconds=5.0,
                loaded=True,
            )


class TestMetricsOutput:
    """Test metrics output generation."""

    def test_generate_metrics_output_when_enabled(self):
        """Test that generate_metrics_output returns bytes when enabled."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "true"}):
            from faultmaven.infrastructure.monitoring.prometheus_metrics import (
                generate_metrics_output,
                PROMETHEUS_AVAILABLE,
            )

            if PROMETHEUS_AVAILABLE:
                output = generate_metrics_output()
                assert isinstance(output, bytes)
                # Should contain build info metric
                assert b"faultmaven_build_info" in output

    def test_generate_metrics_output_when_disabled(self):
        """Test that generate_metrics_output returns empty when disabled."""
        with patch.dict(os.environ, {"ENABLE_METRICS": "false"}):
            from faultmaven.infrastructure.monitoring.prometheus_metrics import (
                generate_metrics_output,
            )

            output = generate_metrics_output()
            assert output == b""

    def test_get_content_type(self):
        """Test that get_content_type returns correct MIME type."""
        from faultmaven.infrastructure.monitoring.prometheus_metrics import (
            get_content_type,
            PROMETHEUS_AVAILABLE,
        )

        content_type = get_content_type()

        if PROMETHEUS_AVAILABLE:
            # Should be Prometheus text format
            assert "text/plain" in content_type or "text/plain" in str(content_type)


class TestNoOpMetrics:
    """Test no-op metric behavior when prometheus_client is not available."""

    def test_no_op_metric_methods_dont_raise(self):
        """Test that NoOpMetric methods don't raise exceptions."""
        from faultmaven.infrastructure.monitoring.prometheus_metrics import (
            PROMETHEUS_AVAILABLE,
        )

        if not PROMETHEUS_AVAILABLE:
            from faultmaven.infrastructure.monitoring.prometheus_metrics import (
                HTTP_REQUESTS,
                HTTP_LATENCY,
            )

            # All operations should be no-ops
            HTTP_REQUESTS.labels(endpoint="/test", method="GET", status_code="200").inc()
            HTTP_LATENCY.labels(endpoint="/test", method="GET").observe(0.5)

            # Context manager should work
            with HTTP_LATENCY.labels(endpoint="/test", method="GET").time():
                pass
