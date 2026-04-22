"""
Tests for MetricsAndPerformanceExtractor.

Covers:
- R4.2: CSV quoting fix (csv.reader)
- R5.3: Prometheus label parsing (prometheus_client integration)
"""

from unittest.mock import patch

import pytest

from faultmaven.modules.preprocessing.extractors.metrics_extractor import (
    PROMETHEUS_CLIENT_AVAILABLE,
    MetricsAndPerformanceExtractor,
)


class TestMetricsExtractor:
    @pytest.fixture
    def extractor(self):
        return MetricsAndPerformanceExtractor()

    def test_properties(self, extractor):
        assert extractor.strategy_name == "statistical"
        assert extractor.llm_calls_used == 0

    # --- R4.2: CSV quoting fix ---

    def test_csv_with_quoted_commas(self, extractor):
        """CSV fields with commas inside quotes should parse correctly."""
        content = (
            "location,temperature,humidity\n"
            '"New York, NY",42.0,18.5\n'
            '"Los Angeles, CA",72.1,55.3\n'
            '"Chicago, IL",35.2,22.0\n'
        )
        result = extractor.extract(content)
        # Should parse successfully (not return a parse failure message)
        assert "METRICS ANALYSIS" in result or "CSV STRUCTURE" in result
        assert "temperature" in result or "humidity" in result

    def test_csv_standard_format(self, extractor):
        """Standard CSV without quoting still works."""
        content = (
            "timestamp,cpu,memory\n"
            "2024-01-01T00:00:00,45.2,60.1\n"
            "2024-01-01T00:01:00,50.3,61.2\n"
            "2024-01-01T00:02:00,48.1,59.8\n"
        )
        result = extractor.extract(content)
        assert "METRICS ANALYSIS" in result
        assert "cpu" in result or "memory" in result

    def test_csv_with_spike(self, extractor):
        """CSV with anomaly detection."""
        lines = ["timestamp,latency_ms"]
        for i in range(50):
            lines.append(f"2024-01-01T00:{i:02d}:00,100.0")
        # Add a spike
        lines.append("2024-01-01T00:50:00,5000.0")
        content = "\n".join(lines)
        result = extractor.extract(content)
        assert "SPIKE" in result or "anomal" in result.lower()

    # --- JSON metrics ---

    def test_json_array_metrics(self, extractor):
        """JSON array format."""
        import json

        data = [
            {"timestamp": "2024-01-01", "cpu": 45.2, "memory": 60.1},
            {"timestamp": "2024-01-02", "cpu": 50.3, "memory": 61.2},
            {"timestamp": "2024-01-03", "cpu": 48.1, "memory": 59.8},
        ]
        result = extractor.extract(json.dumps(data))
        assert "METRICS ANALYSIS" in result

    # --- Prometheus ---

    def test_prometheus_basic(self, extractor):
        """Basic Prometheus text format."""
        content = """\
# HELP http_requests_total Total HTTP requests
# TYPE http_requests_total counter
http_requests_total 1523
http_request_duration_seconds 0.025
"""
        result = extractor.extract(content)
        assert "METRICS ANALYSIS" in result
        assert "http_requests_total" in result

    # --- R5.3: Prometheus label-preserving parsing ---

    @pytest.mark.skipif(
        not PROMETHEUS_CLIENT_AVAILABLE, reason="prometheus_client not installed"
    )
    def test_prometheus_labels_preserved(self, extractor):
        """Prometheus metrics with labels should include labels in metric name."""
        content = """\
# HELP http_requests_total Total HTTP requests
# TYPE http_requests_total counter
http_requests_total{method="GET",handler="/api"} 1027
http_requests_total{method="POST",handler="/api"} 42
"""
        result = extractor.extract(content)
        assert "METRICS ANALYSIS" in result
        # Labels should be preserved in metric names
        assert "method=" in result
        assert "handler=" in result

    @pytest.mark.skipif(
        not PROMETHEUS_CLIENT_AVAILABLE, reason="prometheus_client not installed"
    )
    def test_prometheus_mixed_labels_and_plain(self, extractor):
        """Mix of labeled and unlabeled Prometheus metrics."""
        content = """\
# HELP cpu_usage Current CPU usage
# TYPE cpu_usage gauge
cpu_usage 0.85
# HELP http_requests_total Total HTTP requests
# TYPE http_requests_total counter
http_requests_total{method="GET"} 100
http_requests_total{method="POST"} 25
"""
        result = extractor.extract(content)
        assert "cpu_usage" in result
        # Should have separate entries for GET and POST
        assert "GET" in result
        assert "POST" in result

    def test_prometheus_regex_fallback(self, extractor):
        """When prometheus_client is mocked as unavailable, regex fallback works."""
        content = """\
# HELP cpu_usage Current CPU usage
# TYPE cpu_usage gauge
cpu_usage 0.85
"""
        with patch(
            "faultmaven.modules.preprocessing.extractors.metrics_extractor.PROMETHEUS_CLIENT_AVAILABLE",
            False,
        ):
            result = extractor.extract(content)
            assert "METRICS ANALYSIS" in result
            assert "cpu_usage" in result

    def test_prometheus_regex_fallback_preserves_labels(self, extractor):
        """Regex fallback must keep labeled series distinct. Before the fix,
        the fallback regex required whitespace directly after the metric name
        and silently skipped every labeled line — collapsing multi-dimensional
        metrics into nothing."""
        content = """\
# TYPE http_requests_total counter
http_requests_total{method="GET",status="200"} 100
http_requests_total{method="POST",status="500"} 7
"""
        with patch(
            "faultmaven.modules.preprocessing.extractors.metrics_extractor.PROMETHEUS_CLIENT_AVAILABLE",
            False,
        ):
            result = extractor.extract(content)
            # Both labeled series should be represented as distinct metrics
            assert 'method="GET"' in result or 'method="GET"' in result
            assert 'method="POST"' in result or 'method="POST"' in result

    # --- Spike detection robustness ---

    def test_spike_suppressed_on_nearly_constant_data(self, extractor):
        """Regression: when data is nearly constant (p95 ≈ p50), the IQR proxy
        collapses to ~0 and every tiny upward fluctuation was previously
        flagged as a spike. The minimum-spread guard must suppress that."""
        # 100 values all within 0.1 of 50.0, with one value at 50.3 that is
        # not a genuine anomaly. IQR proxy is ~0 so spike detection should
        # be disabled.
        rows = "timestamp,value\n" + "\n".join(
            f"t{i},{50.0 + (i % 2) * 0.05}" for i in range(100)
        )
        rows += "\nt_odd,50.3"
        result = extractor.extract(rows)
        # With the constant-data guard, no spike should be reported
        assert "SPIKE" not in result

    def test_spike_still_detected_on_variable_data(self, extractor):
        """Sanity check: genuine spikes on variable data are still detected."""
        # Values range 10–50 with one obvious spike at 500
        rows = "timestamp,value\n"
        rows += "\n".join(f"t{i},{10 + (i % 10) * 4}" for i in range(100))
        rows += "\nt_spike,500"
        result = extractor.extract(rows)
        assert "SPIKE" in result or "anomal" in result.lower()
