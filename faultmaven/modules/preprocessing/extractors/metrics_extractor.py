"""
METRICS_AND_PERFORMANCE Extractor

Analyzes quantitative performance data (CSV, JSON time-series) and detects
anomalies using statistical methods. No LLM calls required.

Uses prometheus_client for Prometheus text format parsing when available,
falling back to regex-based parsing.
"""

import csv
import io
import json
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult
from faultmaven.modules.preprocessing.extractors.utils import (
    EMPTY_CONTENT_RESPONSE,
    has_content,
    truncate_output,
)

if TYPE_CHECKING:
    from faultmaven.models.interfaces import ISanitizer, ITracer, IVectorStore

try:
    from prometheus_client.parser import text_string_to_metric_families

    PROMETHEUS_CLIENT_AVAILABLE = True
except ImportError:
    PROMETHEUS_CLIENT_AVAILABLE = False


class MetricsAndPerformanceExtractor:
    """Statistical analysis of performance metrics (0 LLM calls)"""

    # Anomaly detection thresholds
    DROP_PERCENT_THRESHOLD = 0.50  # 50% drop from baseline
    MAX_ANOMALIES_REPORTED = 20  # Safety limit
    # Spike detection uses an IQR proxy (p95 - p50). When the data is nearly
    # constant the proxy collapses to ~0 and every tiny upward fluctuation is
    # flagged as a spike. Require a minimum spread relative to the median
    # before reporting any spikes.
    MIN_IQR_PROXY_FRACTION = 0.05
    # Extreme-outlier fallback: when the IQR proxy is too small to distinguish
    # spikes from noise (flat-data guard fires), still flag values that exceed
    # p99 by this factor. Ratio of 3 avoids false positives on normally
    # distributed data (max ≈ 1.5× p99) while catching genuine outliers like
    # a single CPU spike that is 10×+ the 99th percentile.
    EXTREME_OUTLIER_P99_RATIO = 3.0

    @property
    def strategy_name(self) -> str:
        return "statistical"

    @property
    def llm_calls_used(self) -> int:
        return 0

    def extract(self, content: str) -> ExtractResult:
        """
        Extract and analyze metrics data

        Steps:
        1. Detect format (CSV, JSON time-series, Prometheus)
        2. Parse data into time-series
        3. Calculate statistics (min, max, mean, p95, p99)
        4. Detect anomalies (spikes, drops)
        5. Generate natural language summary
        """
        content = content.lstrip("\ufeff")
        if len(content) > 50_000_000:
            return ExtractResult(
                file_extract="[File exceeds 50MB maximum size limit for extraction]"
            )

        self.csv_skipped_rows = 0
        if not has_content(content):
            return ExtractResult(file_extract=EMPTY_CONTENT_RESPONSE)

        # Try to parse as different formats
        time_series = self._parse_metrics(content)
        csv_summary = self._summarize_csv_structure(content)

        if not time_series:
            # Valid CSV with no numeric columns — provide structural summary
            if csv_summary:
                return ExtractResult(
                    file_extract=csv_summary,
                    file_meta={
                        "format": "csv (non-numeric)",
                        "size_bytes": len(content.encode("utf-8", errors="replace")),
                    },
                )
            return ExtractResult(
                file_extract="[Failed to parse metrics data - unsupported format]"
            )

        # Detect format for coverage
        format_detected = self._detect_format_name(content)

        # Analyze each metric
        summaries = []
        total_data_points = 0
        min_ts = None
        max_ts = None

        for metric_name, data_points in time_series.items():
            total_data_points += len(data_points)
            for ts, _ in data_points:
                if ts:
                    if not min_ts or ts < min_ts:
                        min_ts = ts
                    if not max_ts or ts > max_ts:
                        max_ts = ts

            summary = self._analyze_metric(metric_name, data_points)
            summaries.append(summary)

        total_anomalies = sum(len(s.get("anomalies", [])) for s in summaries)

        # Combine summaries
        output = self._format_summary(summaries)

        # Append categorical distributions for non-numeric columns if available
        if csv_summary:
            output += f"\n\n{csv_summary}"

        if getattr(self, "csv_skipped_rows", 0) > 0:
            output += f"\n\n[Warning: {self.csv_skipped_rows} malformed CSV rows were skipped during parsing]"

        output = truncate_output(output)

        time_span = f"{min_ts} to {max_ts}" if min_ts and max_ts else None

        metric_names = list(time_series.keys())
        return ExtractResult(
            file_extract=output,
            file_meta={
                "format": format_detected,
                "metrics": len(metric_names),
                "total_data_points": total_data_points,
                "time_span": time_span,
                "metric_names": ", ".join(metric_names[:10]),
                "anomalies_found": total_anomalies,
                "size_bytes": len(content.encode("utf-8", errors="replace")),
            },
        )

    def _detect_format_name(self, content: str) -> str:
        """Detect which format was successfully parsed."""
        if self._parse_json_metrics(content):
            return "json"
        if self._parse_csv_metrics(content):
            return "csv"
        if self._parse_prometheus_metrics(content):
            return "prometheus"
        return "unknown"

    def _parse_metrics(
        self, content: str
    ) -> dict[str, list[tuple[str | None, float]]] | None:
        """
        Parse metrics data from various formats

        Returns:
            Dict mapping metric_name -> [(timestamp, value), ...]
            Returns None if parsing fails
        """
        # Try JSON first (time-series format)
        json_result = self._parse_json_metrics(content)
        if json_result:
            return json_result

        # Try CSV format
        csv_result = self._parse_csv_metrics(content)
        if csv_result:
            return csv_result

        # Try Prometheus text format
        prom_result = self._parse_prometheus_metrics(content)
        if prom_result:
            return prom_result

        return None

    def _parse_json_metrics(
        self, content: str
    ) -> dict[str, list[tuple[str | None, float]]] | None:
        """Parse JSON time-series data"""
        try:
            data = json.loads(content)

            # Handle different JSON structures
            if isinstance(data, list):
                # Array of {timestamp, metric1, metric2, ...}
                return self._parse_json_array(data)
            elif isinstance(data, dict):
                # {metric_name: [{timestamp, value}, ...]}
                return self._parse_json_dict(data)

            return None
        except (json.JSONDecodeError, ValueError, KeyError):
            return None

    def _parse_json_array(
        self, data: list[dict]
    ) -> dict[str, list[tuple[str | None, float]]]:
        """Parse JSON array format: [{timestamp, cpu, memory, ...}, ...]"""
        result = {}

        for entry in data:
            timestamp = entry.get("timestamp") or entry.get("time") or entry.get("ts")

            for key, value in entry.items():
                if key in ("timestamp", "time", "ts"):
                    continue

                if isinstance(value, (int, float)):
                    if key not in result:
                        result[key] = []
                    result[key].append((timestamp, float(value)))

        return result if result else None

    def _parse_json_dict(self, data: dict) -> dict[str, list[tuple[str | None, float]]]:
        """Parse JSON dict format: {metric: [{timestamp, value}, ...]}"""
        result = {}

        for metric_name, entries in data.items():
            if not isinstance(entries, list):
                continue

            values = []
            for entry in entries:
                if isinstance(entry, dict):
                    timestamp = (
                        entry.get("timestamp") or entry.get("time") or entry.get("ts")
                    )
                    value = entry.get("value") or entry.get("val")
                    if value is not None and isinstance(value, (int, float)):
                        values.append((timestamp, float(value)))

            if values:
                result[metric_name] = values

        return result if result else None

    def _parse_csv_rows(self, content: str) -> list[list[str]] | None:
        """Parse CSV content using stdlib csv.reader (handles quoting correctly)."""
        try:
            reader = csv.reader(io.StringIO(content))
            return [row for row in reader if row]
        except csv.Error:
            return None

    def _parse_csv_metrics(
        self, content: str
    ) -> dict[str, list[tuple[str | None, float]]] | None:
        """Parse CSV format with header row.

        Auto-detects which columns are numeric by sampling data rows,
        rather than assuming all non-first columns are numeric.
        Uses csv.reader to correctly handle quoted fields.
        """
        rows = self._parse_csv_rows(content)
        if not rows or len(rows) < 2:
            return None

        # Parse header
        header = [col.strip() for col in rows[0]]
        if not header:
            return None

        # Sample up to 10 data rows to detect column types
        sample_rows = []
        for row in rows[1 : min(len(rows), 12)]:
            parts = [p.strip() for p in row]
            if len(parts) == len(header):
                sample_rows.append(parts)

        if not sample_rows:
            return None

        # Detect which columns are numeric (>50% of sampled values parse as float)
        numeric_cols = set()
        timestamp_col = None
        for col_idx in range(len(header)):
            numeric_count = 0
            for row in sample_rows:
                try:
                    float(row[col_idx])
                    numeric_count += 1
                except ValueError:
                    pass

            if numeric_count > len(sample_rows) * 0.5:
                numeric_cols.add(col_idx)

        # Find timestamp column: first non-numeric column with time-like name
        time_names = {
            "time",
            "timestamp",
            "ts",
            "date",
            "datetime",
            "created_at",
            "updated_at",
        }
        for col_idx, col_name in enumerate(header):
            if col_idx not in numeric_cols and col_name.lower() in time_names:
                timestamp_col = col_idx
                break

        # If no named timestamp column, use the first non-numeric column
        if timestamp_col is None:
            for col_idx in range(len(header)):
                if col_idx not in numeric_cols:
                    timestamp_col = col_idx
                    break

        # Metric columns = numeric columns that aren't the timestamp
        metric_cols = numeric_cols - (
            {timestamp_col} if timestamp_col is not None else set()
        )
        if not metric_cols:
            return None

        # Parse all data rows
        result: dict[str, list[tuple[str | None, float]]] = {
            header[i]: [] for i in metric_cols
        }

        for row in rows[1:]:
            parts = [p.strip() for p in row]
            if len(parts) != len(header):
                if hasattr(self, "csv_skipped_rows"):
                    self.csv_skipped_rows += 1
                continue

            ts = parts[timestamp_col] if timestamp_col is not None else None

            for col_idx in metric_cols:
                try:
                    value = float(parts[col_idx])
                    result[header[col_idx]].append((ts, value))
                except ValueError:
                    continue

        # Remove empty metrics
        result = {k: v for k, v in result.items() if v}

        return result if result else None

    def _parse_prometheus_metrics(
        self, content: str
    ) -> dict[str, list[tuple[str | None, float]]] | None:
        """Parse Prometheus text exposition format.

        Uses prometheus_client library when available for label-preserving parsing,
        falling back to regex-based parsing otherwise.
        """
        if PROMETHEUS_CLIENT_AVAILABLE:
            result = self._parse_prometheus_with_library(content)
            if result:
                return result

        return self._parse_prometheus_regex(content)

    def _parse_prometheus_with_library(
        self, content: str
    ) -> dict[str, list[tuple[str | None, float]]] | None:
        """Parse Prometheus metrics using prometheus_client library."""
        try:
            result: dict[str, list[tuple[str | None, float]]] = {}
            for family in text_string_to_metric_families(content):
                for sample in family.samples:
                    # Include labels in metric name for differentiation
                    if sample.labels:
                        label_str = ",".join(
                            f"{k}={v}" for k, v in sorted(sample.labels.items())
                        )
                        metric_key = f"{sample.name}{{{label_str}}}"
                    else:
                        metric_key = sample.name

                    if metric_key not in result:
                        result[metric_key] = []
                    result[metric_key].append((None, float(sample.value)))

            return result if result else None
        except Exception:
            return None

    def _parse_prometheus_regex(
        self, content: str
    ) -> dict[str, list[tuple[str | None, float]]] | None:
        """Parse Prometheus metrics using regex (fallback).

        Label-aware: a labeled metric like ``http_requests{method="GET"} 42``
        is kept as a distinct series keyed by ``name{labels}``. Without label
        awareness this fallback would silently skip every labeled line,
        collapsing multi-dimensional metrics into nothing.

        Value-aware: the Prometheus exposition format permits ``NaN``,
        ``+Inf`` and ``-Inf`` (OpenMetrics §5.1). A purely-digit value class
        silently drops those lines, which matters for histograms /
        summaries whose edge buckets legitimately report ``+Inf`` and for
        quantile readings that have no samples yet (``NaN``). Accept them
        explicitly — ``float()`` converts them natively.
        """
        result: dict[str, list[tuple[str | None, float]]] = {}

        # name, optional {labels}, whitespace, value (numeric or NaN/±Inf).
        # Sign only applies to ``Inf`` per OpenMetrics §5.1 — NaN is
        # unsigned. The numeric alternative carries its own sign in the
        # character class.
        pattern = re.compile(
            r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+"
            r"((?:[+-]?Inf)|NaN|[\d.eE+-]+)"
        )

        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            match = pattern.match(line)
            if not match:
                continue

            name = match.group(1)
            labels = match.group(2) or ""
            try:
                value = float(match.group(3))
            except ValueError:
                continue

            metric_key = f"{name}{labels}" if labels else name
            result.setdefault(metric_key, []).append((None, value))

        return result if result else None

    def _summarize_csv_structure(self, content: str) -> str | None:
        """Produce a structural summary for valid CSVs with no numeric metrics columns."""
        rows = self._parse_csv_rows(content)
        if not rows or len(rows) < 2:
            return None

        header = [col.strip() for col in rows[0]]
        if len(header) < 2:
            return None

        # Verify it's a valid CSV by checking at least some rows match the header count
        valid_rows = 0
        for row in rows[1:]:
            if len(row) == len(header):
                valid_rows += 1

        if valid_rows == 0:
            return None

        total_rows = len(rows) - 1

        # Collect value distributions for categorical columns (sample first 200 rows)
        col_values: dict[str, dict[str, int]] = {col: {} for col in header}
        for row in rows[1 : min(len(rows), 202)]:
            parts = [p.strip() for p in row]
            if len(parts) != len(header):
                continue
            for i, val in enumerate(parts):
                counts = col_values[header[i]]
                counts[val] = counts.get(val, 0) + 1

        out = [f"=== CSV STRUCTURE SUMMARY ===\n"]
        out.append(f"Rows: {total_rows} (valid: {valid_rows})")
        out.append(f"Columns ({len(header)}): {', '.join(header)}\n")

        is_small_ref_table = total_rows < 100 and len(header) <= 3

        for col_name in header:
            counts = col_values[col_name]
            unique = len(counts)
            out.append(f"  {col_name}: {unique} unique value(s)")
            # Show top 5 values for columns with low cardinality, or all for small reference tables
            if is_small_ref_table:
                sorted_vals = sorted(counts.items(), key=lambda x: -x[1])
                for val, cnt in sorted_vals:
                    out.append(f"    - {val} ({cnt}x)")
            elif unique <= 20:
                sorted_vals = sorted(counts.items(), key=lambda x: -x[1])[:5]
                for val, cnt in sorted_vals:
                    display = val[:60] + "..." if len(val) > 60 else val
                    out.append(f"    - {display} ({cnt}x)")

        return "\n".join(out)

    def _analyze_metric(
        self, metric_name: str, data_points: list[tuple[str | None, float]]
    ) -> dict[str, Any]:
        """
        Analyze single metric time-series

        Returns summary dict with stats and anomalies.

        Non-finite values (``NaN`` / ``±Inf``) are excluded from statistics
        because ``sum`` / ``min`` / ``sorted`` would silently propagate
        them and corrupt percentiles. They are retained in the raw count
        and reported separately so they remain visible to the LLM.
        """
        import math

        finite_points = [(t, v) for (t, v) in data_points if math.isfinite(v)]
        finite_values = [v for _, v in finite_points]
        non_finite_count = len(data_points) - len(finite_points)

        if not finite_values:
            result: dict[str, Any] = {
                "metric": metric_name,
                "count": len(data_points),
                "error": "No finite data points",
            }
            if non_finite_count:
                result["non_finite"] = non_finite_count
            return result

        # Calculate statistics over finite values only.
        stats = self._calculate_statistics(finite_values)

        # Detect anomalies only on finite data points.
        anomalies = self._detect_anomalies(finite_points, stats)

        summary = {
            "metric": metric_name,
            "count": len(data_points),
            "stats": stats,
            "anomalies": anomalies[: self.MAX_ANOMALIES_REPORTED],
        }
        if non_finite_count:
            summary["non_finite"] = non_finite_count
        return summary

    def _calculate_statistics(self, values: list[float]) -> dict[str, float]:
        """Calculate statistical measures"""
        n = len(values)
        sorted_values = sorted(values)

        mean = sum(values) / n
        variance = sum((x - mean) ** 2 for x in values) / n
        std_dev = variance**0.5

        return {
            "min": min(values),
            "max": max(values),
            "mean": mean,
            "std_dev": std_dev,
            "p50": sorted_values[n // 2],
            "p95": sorted_values[int(n * 0.95)] if n > 20 else sorted_values[-1],
            "p99": sorted_values[int(n * 0.99)] if n > 100 else sorted_values[-1],
        }

    def _detect_anomalies(
        self, data_points: list[tuple[str | None, float]], stats: dict[str, float]
    ) -> list[dict[str, Any]]:
        """
        Detect anomalies using statistical methods

        Detects:
        - Spikes: IQR-proxy threshold (p95 + 1.5 × (p95 - p50)) on the upper
          side, with a minimum-spread guard so nearly constant data doesn't
          flag every trivial fluctuation.
        - Drops: Symmetric IQR-proxy threshold on the lower side
          (p50 - 1.5 × (p95 - p50)) **plus** the legacy ≥50%-below-baseline
          rule. The two rules are unioned so we capture both gradual sustained
          dips (caught by IQR proxy) and catastrophic single-point drops
          (caught by the percent rule). The same flat-data guard suppresses
          spurious drops on nearly constant data.

        Anomalies are returned sorted by absolute deviation from the median
        (largest first) so that the display cap surfaces the most extreme
        anomalies in **either** direction. Sorting by raw value would push
        every drop to the tail of the list and hide downward anomalies.
        """
        anomalies = []
        mean = stats["mean"]
        p50 = stats["p50"]
        p95 = stats["p95"]

        iqr_proxy = p95 - p50
        p99 = stats.get("p99", p95)
        # Guard: suppress spike detection when the data is effectively flat.
        # Absent this, p95 == p50 would collapse spike_threshold onto p95 and
        # any value slightly above p95 would be flagged as a "spike".
        # Exception: when the max value exceeds p99 by EXTREME_OUTLIER_P99_RATIO,
        # the data has a genuine extreme outlier that the flat-data guard would
        # otherwise miss (e.g. a single CPU spike at 18× the baseline).
        min_meaningful_spread = max(abs(p50) * self.MIN_IQR_PROXY_FRACTION, 1e-9)
        flat_data = iqr_proxy < min_meaningful_spread
        if flat_data:
            max_value = stats.get("max", 0)
            if p99 > 0 and max_value > p99 * self.EXTREME_OUTLIER_P99_RATIO:
                spike_threshold = p99 * self.EXTREME_OUTLIER_P99_RATIO
            else:
                spike_threshold = float("inf")
        else:
            spike_threshold = p95 + (1.5 * iqr_proxy)
            if spike_threshold <= p50:
                spike_threshold = p50 * 1.5 if p50 > 0 else 1.0

        # Symmetric drop detection: mirror the IQR-proxy logic onto the lower
        # side. A value that is as far below the median as the spike threshold
        # is above is just as anomalous, regardless of direction. The flat-data
        # guard applies symmetrically — nearly constant data should not flag
        # tiny downward fluctuations any more than tiny upward ones.
        if flat_data:
            iqr_drop_threshold = float("-inf")
        else:
            iqr_drop_threshold = p50 - (1.5 * iqr_proxy)

        # Legacy percent-based drop rule retained for catastrophic drops on
        # series where the IQR proxy is dominated by noise (e.g. a single
        # service that flatlines from 100% to 0%). Union with the IQR rule.
        percent_drop_threshold = (
            mean * (1 - self.DROP_PERCENT_THRESHOLD) if mean > 0 else None
        )

        for timestamp, value in data_points:
            # Detect spikes
            if value > spike_threshold and value > p50:
                ratio = (value / p50) if p50 > 0 else value
                anomalies.append(
                    {
                        "type": "spike",
                        "timestamp": timestamp,
                        "value": value,
                        "ratio": round(ratio, 1),
                        "threshold": round(spike_threshold, 2),
                    }
                )

            # Detect drops (IQR-proxy OR legacy 50%-below rule).
            elif value < iqr_drop_threshold or (
                percent_drop_threshold is not None and value < percent_drop_threshold
            ):
                if mean > 0:
                    drop_percent = ((mean - value) / mean) * 100
                else:
                    drop_percent = 0.0
                anomalies.append(
                    {
                        "type": "drop",
                        "timestamp": timestamp,
                        "value": value,
                        "drop_percent": round(drop_percent, 1),
                        "baseline": round(mean, 2),
                    }
                )

        # Sort by absolute deviation from the median (descending) so the most
        # extreme anomalies surface first in the display cap, regardless of
        # direction. The previous value-descending sort buried every drop at
        # the tail of the list — when more than `display_cap` spikes existed
        # the agent never saw any drops at all.
        anomalies.sort(key=lambda a: abs(a["value"] - p50), reverse=True)
        return anomalies

    def _fmt_val(self, value: float) -> str:
        """Format metric values to avoid scientific notation for typical magnitudes."""
        if value == 0:
            return "0"
        abs_val = abs(value)
        if abs_val >= 1e9 or abs_val < 1e-4:
            return f"{value:.4g}"

        formatted = f"{value:.4f}"
        if "." in formatted:
            formatted = formatted.rstrip("0").rstrip(".")
        return formatted or "0"

    def _format_summary(self, summaries: list[dict[str, Any]]) -> str:
        """Format analysis results as natural language summary"""
        lines = ["=== METRICS ANALYSIS SUMMARY ===\n"]

        total_metrics = len(summaries)
        all_anomalies = [a for s in summaries for a in s.get("anomalies", [])]
        total_anomalies = len(all_anomalies)
        total_spikes = sum(1 for a in all_anomalies if a.get("type") == "spike")
        total_drops = sum(1 for a in all_anomalies if a.get("type") == "drop")

        lines.append(f"Analyzed {total_metrics} metric(s)")
        if total_anomalies:
            lines.append(
                f"Detected {total_anomalies} anomaly/anomalies "
                f"({total_spikes} spike(s), {total_drops} drop(s))\n"
            )
        else:
            lines.append(f"Detected {total_anomalies} anomaly/anomalies\n")

        for summary in summaries:
            metric_name = summary["metric"]
            count = summary["count"]

            if "error" in summary:
                lines.append(f"❌ {metric_name}: {summary['error']}")
                non_finite = summary.get("non_finite", 0)
                if non_finite:
                    lines.append(f"   Non-finite values: {non_finite} (NaN/±Inf)")
                continue

            stats = summary["stats"]
            anomalies = summary["anomalies"]

            lines.append(f"📊 {metric_name} ({count} data points):")
            lines.append(
                f"   Range: {self._fmt_val(stats['min'])} - {self._fmt_val(stats['max'])}"
            )
            lines.append(
                f"   Mean: {self._fmt_val(stats['mean'])} (±{self._fmt_val(stats['std_dev'])})"
            )
            lines.append(
                f"   Percentiles: p50={self._fmt_val(stats['p50'])}, p95={self._fmt_val(stats['p95'])}, p99={self._fmt_val(stats['p99'])}"
            )
            non_finite = summary.get("non_finite", 0)
            if non_finite:
                lines.append(
                    f"   Non-finite values: {non_finite} (NaN/±Inf, excluded from statistics)"
                )

            if anomalies:
                metric_spikes = sum(1 for a in anomalies if a.get("type") == "spike")
                metric_drops = sum(1 for a in anomalies if a.get("type") == "drop")
                lines.append(
                    f"   ⚠️  {len(anomalies)} anomaly/anomalies detected "
                    f"({metric_spikes} spike(s), {metric_drops} drop(s)):"
                )

                for anomaly in anomalies[:10]:  # Show first 10
                    anom_type = anomaly["type"]
                    timestamp = anomaly.get("timestamp", "unknown")
                    value = anomaly["value"]

                    if anom_type == "spike":
                        ratio = anomaly.get("ratio", anomaly.get("sigma", 0))
                        lines.append(
                            f"      • SPIKE at {timestamp}: {self._fmt_val(value)} ({ratio}x median)"
                        )
                    elif anom_type == "drop":
                        drop_pct = anomaly["drop_percent"]
                        lines.append(
                            f"      • DROP at {timestamp}: {self._fmt_val(value)} ({drop_pct}% below baseline)"
                        )

                if len(anomalies) > 10:
                    lines.append(f"      ... and {len(anomalies) - 10} more")
            else:
                lines.append("   ✓ No anomalies detected")

            lines.append("")

        return "\n".join(lines)
