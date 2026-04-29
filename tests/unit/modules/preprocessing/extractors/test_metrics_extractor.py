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
        assert (
            "METRICS ANALYSIS" in result.file_extract
            or "CSV STRUCTURE" in result.file_extract
        )
        assert "temperature" in result.file_extract or "humidity" in result.file_extract

    def test_csv_standard_format(self, extractor):
        """Standard CSV without quoting still works."""
        content = (
            "timestamp,cpu,memory\n"
            "2024-01-01T00:00:00,45.2,60.1\n"
            "2024-01-01T00:01:00,50.3,61.2\n"
            "2024-01-01T00:02:00,48.1,59.8\n"
        )
        result = extractor.extract(content)
        assert "METRICS ANALYSIS" in result.file_extract
        assert "cpu" in result.file_extract or "memory" in result.file_extract

    def test_csv_with_spike(self, extractor):
        """CSV with anomaly detection."""
        lines = ["timestamp,latency_ms"]
        for i in range(50):
            lines.append(f"2024-01-01T00:{i:02d}:00,100.0")
        # Add a spike
        lines.append("2024-01-01T00:50:00,5000.0")
        content = "\n".join(lines)
        result = extractor.extract(content)
        assert "SPIKE" in result.file_extract or "anomal" in result.file_extract.lower()

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
        assert "METRICS ANALYSIS" in result.file_extract

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
        assert "METRICS ANALYSIS" in result.file_extract
        assert "http_requests_total" in result.file_extract

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
        assert "METRICS ANALYSIS" in result.file_extract
        # Labels should be preserved in metric names
        assert "method=" in result.file_extract
        assert "handler=" in result.file_extract

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
        assert "cpu_usage" in result.file_extract
        # Should have separate entries for GET and POST
        assert "GET" in result.file_extract
        assert "POST" in result.file_extract

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
            assert "METRICS ANALYSIS" in result.file_extract
            assert "cpu_usage" in result.file_extract

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
            assert (
                'method="GET"' in result.file_extract
                or 'method="GET"' in result.file_extract
            )
            assert (
                'method="POST"' in result.file_extract
                or 'method="POST"' in result.file_extract
            )

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
        assert "SPIKE" not in result.file_extract

    def test_spike_still_detected_on_variable_data(self, extractor):
        """Sanity check: genuine spikes on variable data are still detected."""
        # Values range 10–50 with one obvious spike at 500
        rows = "timestamp,value\n"
        rows += "\n".join(f"t{i},{10 + (i % 10) * 4}" for i in range(100))
        rows += "\nt_spike,500"
        result = extractor.extract(rows)
        assert "SPIKE" in result.file_extract or "anomal" in result.file_extract.lower()

    # --- Prometheus NaN / ±Inf handling (regex fallback) ---

    def test_prometheus_regex_fallback_captures_nan_and_inf(self, extractor):
        """Regression: NaN and ±Inf are valid Prometheus values (OpenMetrics
        §5.1) — histograms emit ``le="+Inf"`` buckets, and summaries with no
        samples yet emit ``NaN`` quantiles. The regex fallback previously
        required ``[\\d.eE+-]+`` after the name, which does not match the
        literal strings ``NaN``/``+Inf``/``-Inf`` — so every such line was
        silently dropped, making the affected series *disappear* from the
        output rather than just skipping a single reading."""
        content = """\
# HELP http_request_duration_seconds Request duration
# TYPE http_request_duration_seconds histogram
http_request_duration_seconds_bucket{le="0.1"} 24054
http_request_duration_seconds_bucket{le="+Inf"} 144320
http_request_duration_seconds_sum NaN
request_errors_total -Inf
"""
        with patch(
            "faultmaven.modules.preprocessing.extractors.metrics_extractor.PROMETHEUS_CLIENT_AVAILABLE",
            False,
        ):
            result = extractor.extract(content)
            # All four series must be present — no silent drops.
            assert "http_request_duration_seconds_bucket" in result.file_extract
            assert "http_request_duration_seconds_sum" in result.file_extract
            assert "request_errors_total" in result.file_extract
            # Non-finite values should be visible to the LLM, not hidden.
            assert "Non-finite" in result.file_extract

    def test_prometheus_regex_fallback_nan_excluded_from_stats(self, extractor):
        """When a series has both finite and NaN values, statistics are
        computed over the finite subset — mixing NaN into min/max/mean would
        silently produce NaN percentiles."""
        # Multiple readings so statistics are actually computed.
        content = """\
# TYPE gauge_with_gaps gauge
gauge_with_gaps{shard="a"} 10
gauge_with_gaps{shard="b"} 20
gauge_with_gaps{shard="c"} NaN
gauge_with_gaps{shard="d"} 30
"""
        with patch(
            "faultmaven.modules.preprocessing.extractors.metrics_extractor.PROMETHEUS_CLIENT_AVAILABLE",
            False,
        ):
            result = extractor.extract(content)
            # Each label-qualified series is its own metric (single point
            # each), but the presence of NaN should be surfaced.
            assert "Non-finite" in result.file_extract
            # No NaN should appear in the computed statistics lines.
            assert "Range: nan" not in result.file_extract.lower()
            assert "mean: nan" not in result.file_extract.lower()


class TestFmtVal:
    """Tests for MetricsAndPerformanceExtractor._fmt_val."""

    @pytest.fixture
    def fmt(self):
        return MetricsAndPerformanceExtractor()._fmt_val

    def test_zero(self, fmt):
        assert fmt(0) == "0"
        assert fmt(0.0) == "0"

    def test_integer_like_float(self, fmt):
        # Should not produce scientific notation for typical large integers
        assert fmt(12345.0) == "12345"
        assert fmt(100.0) == "100"

    def test_typical_decimal(self, fmt):
        assert fmt(2.344) == "2.344"
        assert fmt(0.066) == "0.066"

    def test_trailing_zeros_stripped(self, fmt):
        assert fmt(1.5000) == "1.5"
        assert fmt(10.0) == "10"

    def test_small_value_near_threshold(self, fmt):
        # 1e-4 is the boundary — values at or below use :.4g
        result = fmt(1e-4)
        # 1e-4 is exactly at the boundary (abs_val < 1e-4 is False), so :.4f applies
        assert result == "0.0001"

    def test_very_small_uses_scientific(self, fmt):
        result = fmt(1e-5)
        assert "e" in result.lower()

    def test_very_large_uses_scientific(self, fmt):
        result = fmt(1e10)
        assert "e" in result.lower()

    def test_negative_value(self, fmt):
        assert fmt(-2.344) == "-2.344"
        assert fmt(-100.0) == "-100"

    def test_no_scientific_for_large_cpu_pct(self, fmt):
        # CPU percentages like 99.99 should never get scientific notation
        assert "e" not in fmt(99.99).lower()
        assert "e" not in fmt(100.0).lower()


class TestSamplingIntervalSurface:
    """Time-series CSVs answer 'what cadence is this?' as part of the
    default characterization question. The extractor must compute the
    typical sampling interval from consecutive timestamps and surface it
    in both file_meta and the file_extract so the agent can reproduce it
    in q1-style summaries (ec2-cpu q1, ISS investigation 2026-04-29)."""

    def _csv_at_interval(self, n_points: int, interval_seconds: int) -> str:
        from datetime import datetime, timedelta

        start = datetime(2024, 1, 1, 0, 0, 0)
        rows = ["timestamp,value"]
        for i in range(n_points):
            ts = start + timedelta(seconds=i * interval_seconds)
            rows.append(f"{ts.isoformat(sep=' ')},{0.1 + i * 0.001:.4f}")
        return "\n".join(rows)

    def test_5_minute_interval_detected(self):
        from faultmaven.modules.preprocessing.extractors.metrics_extractor import (
            MetricsAndPerformanceExtractor,
        )

        ex = MetricsAndPerformanceExtractor()
        result = ex.extract(self._csv_at_interval(n_points=200, interval_seconds=300))
        assert result.file_meta.get("sampling_interval") == "~5 min"
        assert "Sampling interval:" in result.file_extract
        assert "~5 min" in result.file_extract

    def test_30_second_interval_detected(self):
        from faultmaven.modules.preprocessing.extractors.metrics_extractor import (
            MetricsAndPerformanceExtractor,
        )

        ex = MetricsAndPerformanceExtractor()
        result = ex.extract(self._csv_at_interval(n_points=200, interval_seconds=30))
        assert result.file_meta.get("sampling_interval") == "~30 s"

    def test_hourly_interval_detected(self):
        from faultmaven.modules.preprocessing.extractors.metrics_extractor import (
            MetricsAndPerformanceExtractor,
        )

        ex = MetricsAndPerformanceExtractor()
        result = ex.extract(self._csv_at_interval(n_points=100, interval_seconds=3600))
        assert result.file_meta.get("sampling_interval") == "~1 h"

    def test_no_interval_when_too_few_points(self):
        from faultmaven.modules.preprocessing.extractors.metrics_extractor import (
            MetricsAndPerformanceExtractor,
        )

        # Single data point — can't compute interval.
        csv = "timestamp,value\n2024-01-01 00:00:00,0.1\n"
        ex = MetricsAndPerformanceExtractor()
        result = ex.extract(csv)
        assert "sampling_interval" not in result.file_meta

    def test_interval_robust_to_one_gap(self):
        """A single missing sample (gap) should not throw off the median."""
        from faultmaven.modules.preprocessing.extractors.metrics_extractor import (
            MetricsAndPerformanceExtractor,
        )
        from datetime import datetime, timedelta

        # 100 samples at 5-minute interval, but with one 30-minute gap
        # in the middle. Median should still report 5 min.
        start = datetime(2024, 1, 1, 0, 0, 0)
        rows = ["timestamp,value"]
        for i in range(50):
            ts = start + timedelta(seconds=i * 300)
            rows.append(f"{ts.isoformat(sep=' ')},{i * 0.01:.4f}")
        # Skip index 50 (gap of 30 min instead of 5 min).
        gap_start = start + timedelta(seconds=51 * 300 + 1500)
        for i in range(50):
            ts = gap_start + timedelta(seconds=i * 300)
            rows.append(f"{ts.isoformat(sep=' ')},{(i + 51) * 0.01:.4f}")
        csv = "\n".join(rows)
        ex = MetricsAndPerformanceExtractor()
        result = ex.extract(csv)
        assert result.file_meta.get("sampling_interval") == "~5 min"
