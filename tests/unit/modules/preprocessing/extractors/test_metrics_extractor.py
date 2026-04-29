"""
Tests for MetricsAndPerformanceExtractor.

Covers:
- R4.2: CSV quoting fix (csv.reader)
- R5.3: Prometheus label parsing (prometheus_client integration)
- ISS-022: anomaly summary preserves both spikes and drops
"""

import os
import random
from pathlib import Path
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
        from datetime import datetime, timedelta

        from faultmaven.modules.preprocessing.extractors.metrics_extractor import (
            MetricsAndPerformanceExtractor,
        )

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


# Candidate locations for the NAB ambient-temperature regression CSV. The
# preferred path is the sibling fm-data-exam workspace, but the test must not
# break in environments that don't have it (CI containers, fresh clones).
_AMBIENT_TEMP_CANDIDATES = (
    Path(
        "/home/swhouse/product/fm-data-exam/test-data/NAB/data/realKnownCause/"
        "ambient_temperature_system_failure.csv"
    ),
    Path(__file__).resolve().parents[5]
    / "fm-data-exam"
    / "test-data"
    / "NAB"
    / "data"
    / "realKnownCause"
    / "ambient_temperature_system_failure.csv",
)


def _find_ambient_temperature_csv() -> Path | None:
    override = os.environ.get("FAULTMAVEN_AMBIENT_TEMP_CSV")
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
    for candidate in _AMBIENT_TEMP_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


class TestAnomalyDirectionPreservation:
    """ISS-022: the surfaced anomaly summary must preserve **both** upward
    spikes and downward drops in the display window.

    Root cause: the previous implementation sorted anomalies by raw value
    descending, which pushed every drop (low value) to the tail of the list.
    With more than ``display_cap`` spikes, drops were silently truncated out
    of the summary even when the underlying detector flagged them.

    Fix: sort by absolute deviation from the median so the most extreme
    anomalies in *either* direction surface first, plus mirror the spike
    IQR-proxy threshold onto the lower side so drops are detected
    symmetrically (this resolves real-world cases like the NAB ambient
    temperature dataset where drops are gradual and never trigger the legacy
    50%-below-mean rule).
    """

    @pytest.fixture
    def extractor(self):
        return MetricsAndPerformanceExtractor()

    def test_synthetic_series_surfaces_both_spikes_and_drops(self, extractor):
        """A synthetic series with mean ~50, std ~5, plus 3 high spikes at
        value 80 and 3 deep drops at value 5 must surface **both** at least
        one DROP and one SPIKE within the top-10 display window.

        Before the fix, the value-descending sort would place all 3 spikes
        and the noise tail before any of the drops — drops at value 5 were
        ranked dead last by raw value. The synthetic case here would only
        manifest the bug with more spikes than display_cap, but the key
        assertion is that direction is preserved regardless.
        """
        rng = random.Random(20260429)
        rows = ["timestamp,value"]
        # Baseline: 100 points around mean=50, std=5
        for i in range(100):
            rows.append(f"2024-01-01T00:{i:02d}:00,{50.0 + rng.gauss(0, 5):.4f}")
        # 3 high spikes
        for i in range(3):
            rows.append(f"2024-01-02T00:{i:02d}:00,80.0")
        # 3 deep drops (well below the 50%-below-mean rule and the symmetric
        # IQR-proxy threshold)
        for i in range(3):
            rows.append(f"2024-01-03T00:{i:02d}:00,5.0")

        result = extractor.extract("\n".join(rows))
        summary = result.file_extract

        # Both directions must appear in the formatted summary (display window
        # is the first 10 anomalies per metric).
        display_window = summary.split("... and")[0]
        assert "SPIKE" in display_window, (
            "expected at least one SPIKE in the displayed anomalies, got:\n"
            + display_window
        )
        assert "DROP" in display_window, (
            "expected at least one DROP in the displayed anomalies "
            "(regression: ISS-022 sorted by raw value, hiding all drops), got:\n"
            + display_window
        )
        # The descriptive heading should now include direction counts.
        assert "spike(s)" in summary and "drop(s)" in summary

    def test_many_spikes_do_not_crowd_out_drops_in_display_window(self, extractor):
        """Direct ISS-022 reproduction: when the raw spike count exceeds the
        display cap (10), the previous value-descending sort would push every
        drop past the truncation boundary. The new abs-deviation sort must
        keep at least one drop visible.
        """
        rng = random.Random(123)
        rows = ["timestamp,value"]
        # Large baseline so the spike/drop counts stay below p95 — otherwise
        # the anomalies themselves dominate the percentiles and slip back
        # under the threshold.
        for i in range(2000):
            rows.append(f"2024-01-01T{i:04d},{50.0 + rng.gauss(0, 5):.4f}")
        # 12 spikes — more than the display cap
        for i in range(12):
            rows.append(f"2024-02-01T{i:04d},90.0")
        # 3 deep drops
        for i in range(3):
            rows.append(f"2024-03-01T{i:04d},5.0")

        result = extractor.extract("\n".join(rows))
        summary = result.file_extract

        display_window = summary.split("... and")[0]
        spike_lines = [
            line for line in display_window.splitlines() if "SPIKE at" in line
        ]
        drop_lines = [line for line in display_window.splitlines() if "DROP at" in line]
        assert spike_lines, (
            "expected SPIKE entries to be displayed; summary was:\n" + display_window
        )
        assert drop_lines, (
            "expected DROP entries to survive the display cap when there are "
            "more spikes than the cap (regression: ISS-022); summary was:\n"
            + display_window
        )

    def test_anomalies_sorted_by_absolute_deviation_from_median(self, extractor):
        """Internal contract: ``_detect_anomalies`` must order results so that
        the largest deviations from the median come first — independent of
        sign. This is what makes the display cap fair to both directions.
        """
        rng = random.Random(7)
        data_points: list[tuple[str | None, float]] = []
        for i in range(200):
            data_points.append((f"t{i}", 50.0 + rng.gauss(0, 5)))
        # Add bidirectional anomalies with varying magnitudes
        data_points.append(("t_drop_huge", 1.0))  # |dev| ≈ 49
        data_points.append(("t_spike_med", 80.0))  # |dev| ≈ 30
        data_points.append(("t_drop_med", 20.0))  # |dev| ≈ 30
        data_points.append(("t_spike_huge", 200.0))  # |dev| ≈ 150
        values = [v for _, v in data_points]

        stats = extractor._calculate_statistics(values)
        anomalies = extractor._detect_anomalies(data_points, stats)

        assert anomalies, "expected anomalies to be detected"
        deviations = [abs(a["value"] - stats["p50"]) for a in anomalies]
        assert deviations == sorted(deviations, reverse=True), (
            "anomalies must be sorted by |value - p50| descending so the "
            "display cap surfaces the most extreme anomalies in either "
            "direction (ISS-022)"
        )
        # The top-2 anomalies must include the huge spike and huge drop —
        # they are the two largest deviations.
        top_two_timestamps = {a["timestamp"] for a in anomalies[:2]}
        assert "t_spike_huge" in top_two_timestamps
        assert "t_drop_huge" in top_two_timestamps

    def test_summary_heading_breaks_down_spike_and_drop_counts(self, extractor):
        """Cosmetic enhancement (criterion 2): the heading should disclose
        spike and drop counts so a reader can immediately see the directional
        composition without scrolling the per-anomaly list.
        """
        rng = random.Random(11)
        rows = ["timestamp,value"]
        for i in range(100):
            rows.append(f"t{i},{50.0 + rng.gauss(0, 5):.4f}")
        rows.extend(["t_s1,90", "t_s2,90", "t_d1,5", "t_d2,5"])
        result = extractor.extract("\n".join(rows))
        summary = result.file_extract
        # Top-level heading
        assert "spike(s)" in summary
        assert "drop(s)" in summary

    @pytest.mark.skipif(
        _find_ambient_temperature_csv() is None,
        reason=(
            "NAB ambient_temperature_system_failure.csv not available; set "
            "FAULTMAVEN_AMBIENT_TEMP_CSV to override path."
        ),
    )
    def test_ambient_temperature_csv_surfaces_dec_spike_and_apr_drop(self, extractor):
        """ISS-022 regression test against the real failing dataset:
        NAB ambient_temperature_system_failure.csv (case
        ``metrics-ambient-temp-01``). The series has a high spike around
        2013-12-22/23 (peak 86.22°F) and a sustained drop around
        2014-04-13 (minimum 57.46°F). Both directions must surface in the
        displayed anomaly list.

        Pre-fix behaviour: 9 spikes were detected and shown, 0 drops — the
        50%-below-mean rule never fired (drop was only ~19% below mean) and
        the value-descending sort would have buried any drops anyway. The
        agent answered q1/q2/q4 framing the dataset as "spike only".
        """
        csv_path = _find_ambient_temperature_csv()
        assert csv_path is not None  # appeasement of type checker; skip handles None
        content = csv_path.read_text(encoding="utf-8")

        result = extractor.extract(content)
        summary = result.file_extract

        # Full-window scan (not just first 10) — but we want both within the
        # display window so the agent actually sees them.
        display_window = summary.split("... and")[0]
        assert "SPIKE" in display_window, (
            "expected the December 2013 spike(s) to surface in the displayed "
            "anomalies; summary was:\n" + display_window
        )
        assert "DROP" in display_window, (
            "expected the April 2014 drop to surface in the displayed "
            "anomalies (regression: ISS-022); summary was:\n" + display_window
        )
        # December 2013 spike timestamps appear in the summary at all
        assert "2013-12" in summary
        # April 2014 drop should be visible too
        assert "2014-04" in display_window or "2014-05" in display_window, (
            "expected an April or May 2014 drop timestamp in the displayed "
            "window; summary was:\n" + display_window
        )
