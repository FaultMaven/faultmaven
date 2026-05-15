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
from typing import Any, Dict, List, Optional, Tuple

from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult
from faultmaven.modules.preprocessing.extractors.utils import (
    EMPTY_CONTENT_RESPONSE,
    extract_timestamp,
    has_content,
    truncate_output,
)

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
    # ISS-039: maximum number of consecutive non-anomalous samples that can
    # sit between two outliers before they are split into separate regions.
    # 5 samples is enough to keep brief gaps (one normal reading between two
    # spikes) within a single region while preventing a daily heat spike from
    # silently merging with one a week later. Hourly data → ~5 hours of slack.
    MAX_REGION_GAP_SAMPLES = 5
    # ISS-039: cap on how many regions to surface in the formatted summary
    # block. Regions are far coarser than per-row outliers, so 10 is a
    # comfortable upper bound for almost every real-world case while still
    # bounded for output budget.
    MAX_REGIONS_REPORTED = 10
    # ISS-040: bimodal-distribution heuristic. When the inter-quartile range
    # is wider than ``BIMODAL_IQR_TO_STD_RATIO * std_dev`` the distribution
    # likely has two clusters (P25 anchored to the low cluster, P75 anchored
    # to the high cluster, median sitting in the gap). 1.5× is a conservative
    # threshold that keeps single-mode noise from triggering the flag.
    BIMODAL_IQR_TO_STD_RATIO = 1.5

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
        # ISS-039: collect clustered regions across all metrics so the agent
        # has the structured form (start/end/peak/direction/severity) in
        # file_meta. Per-row outlier counts are still surfaced in
        # `anomalies_found` for backwards compatibility.
        all_regions: list[dict[str, Any]] = []
        for s in summaries:
            metric_name = s.get("metric", "")
            for r in s.get("regions", []):
                # Stamp the metric name on each region so multi-metric
                # files (e.g., Prometheus exports) remain disambiguable.
                tagged = dict(r)
                tagged["metric"] = metric_name
                all_regions.append(tagged)
        total_regions = len(all_regions)

        # Compute typical sampling interval from the largest metric series.
        # Surfaced both in file_meta and prepended to the output so the agent
        # can answer "what is the cadence of this data?" — a default-question
        # fact for time-series CSVs that was previously missing.
        sampling_interval = self._compute_sampling_interval(time_series)

        # Combine summaries
        output = self._format_summary(summaries)

        # Prepend interval note to the analysis summary block so it is
        # visible in the FILE SUMMARY first-line orientation.
        if sampling_interval:
            output = self._inject_interval_note(output, sampling_interval)

        # Append categorical distributions for non-numeric columns if available
        if csv_summary:
            output += f"\n\n{csv_summary}"

        if getattr(self, "csv_skipped_rows", 0) > 0:
            output += f"\n\n[Warning: {self.csv_skipped_rows} malformed CSV rows were skipped during parsing]"

        output = truncate_output(output)

        time_span = f"{min_ts} to {max_ts}" if min_ts and max_ts else None

        metric_names = list(time_series.keys())
        meta = {
            "format": format_detected,
            "metrics": len(metric_names),
            "total_data_points": total_data_points,
            "time_span": time_span,
            "metric_names": ", ".join(metric_names[:10]),
            "anomalies_found": total_anomalies,
            "anomaly_count": total_regions,
            "anomalies": all_regions,
            "size_bytes": len(content.encode("utf-8", errors="replace")),
        }
        if sampling_interval:
            meta["sampling_interval"] = sampling_interval

        return ExtractResult(
            file_extract=output,
            file_meta=meta,
        )

    def _compute_sampling_interval(
        self, time_series: Dict[str, List[Tuple[Optional[str], float]]]
    ) -> Optional[str]:
        """Compute the typical interval between consecutive samples.

        Picks the largest metric series (most data points), parses its
        timestamps to datetime, and takes the median of consecutive
        differences. Median is robust to occasional gaps. Returns a
        human-readable string ("~5 min", "~1 h", "~30 s") or None when
        intervals can't be determined (e.g. <2 timestamped points,
        irregular cadence with no clear median).
        """
        if not time_series:
            return None
        # Pick the series with the most points — best sample size.
        biggest = max(time_series.values(), key=len, default=[])
        if len(biggest) < 2:
            return None

        # Parse timestamps; skip points without a parsable timestamp.
        parsed = []
        for ts, _ in biggest:
            if not ts:
                continue
            dt = extract_timestamp(ts)
            if dt is not None:
                parsed.append(dt)
        if len(parsed) < 2:
            return None
        parsed.sort()

        deltas = [
            (parsed[i + 1] - parsed[i]).total_seconds() for i in range(len(parsed) - 1)
        ]
        # Drop zero-deltas (duplicate timestamps) which would skew toward 0.
        deltas = [d for d in deltas if d > 0]
        if not deltas:
            return None
        deltas.sort()
        mid = deltas[len(deltas) // 2]

        # Format human-readable. Round to a "tidy" number where the
        # ratio is within 5% — turns 299.4 sec into "5 min" rather than
        # "4.99 min". Below that tolerance, show one decimal place.
        return self._format_interval_seconds(mid)

    @staticmethod
    def _format_interval_seconds(seconds: float) -> str:
        if seconds < 60:
            return f"~{int(round(seconds))} s" if seconds >= 1 else f"~{seconds:.2f} s"
        minutes = seconds / 60
        if minutes < 60:
            tidy = round(minutes)
            return (
                f"~{tidy} min"
                if tidy > 0 and abs(minutes - tidy) / max(minutes, 1) < 0.05
                else f"~{minutes:.1f} min"
            )
        hours = minutes / 60
        if hours < 48:
            tidy = round(hours)
            return (
                f"~{tidy} h"
                if tidy > 0 and abs(hours - tidy) / max(hours, 1) < 0.05
                else f"~{hours:.1f} h"
            )
        days = hours / 24
        return f"~{days:.1f} d"

    @staticmethod
    def _inject_interval_note(output: str, interval: str) -> str:
        """Add a sampling-interval note into the METRICS ANALYSIS SUMMARY
        header so it surfaces in the first-line orientation."""
        marker = "Analyzed "
        idx = output.find(marker)
        if idx < 0:
            # Fallback: prepend a top line.
            return f"Sampling interval: {interval} (median of consecutive timestamps)\n{output}"
        # Insert a follow-up line right after the "Analyzed N metric(s)" line.
        eol = output.find("\n", idx)
        if eol < 0:
            return output + f"\nSampling interval: {interval} (median)"
        return (
            output[: eol + 1]
            + f"Sampling interval: {interval} (median of consecutive timestamps)\n"
            + output[eol + 1 :]
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

        Returns summary dict with stats, per-row anomalies, and clustered
        anomaly regions (ISS-039).

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

        # ISS-039: cluster contiguous outlier readings into regions so a
        # short heat-event "spike" with 5 consecutive samples surfaces as
        # one event, not five. Regions are the primary unit reported in
        # file_meta and the formatted summary; per-row anomalies remain
        # available internally for callers that need raw outliers.
        regions = self._cluster_anomalies_to_regions(finite_points, anomalies, stats)

        summary = {
            "metric": metric_name,
            "count": len(data_points),
            "stats": stats,
            "anomalies": anomalies[: self.MAX_ANOMALIES_REPORTED],
            "regions": regions[: self.MAX_REGIONS_REPORTED],
        }
        if non_finite_count:
            summary["non_finite"] = non_finite_count
        return summary

    def _cluster_anomalies_to_regions(
        self,
        finite_points: list[tuple[str | None, float]],
        anomalies: list[dict[str, Any]],
        stats: dict[str, float],
    ) -> list[dict[str, Any]]:
        """Cluster per-row outliers into contiguous regions (ISS-039).

        Two outliers are placed in the same region when:
        1. They share the same direction (both spikes or both drops), and
        2. They are within ``MAX_REGION_GAP_SAMPLES`` sample positions of
           one another in the time-ordered series.

        Each region surfaces:
        - ``start_ts`` / ``end_ts``: bounds of the contiguous outlier run
        - ``peak_value`` / ``peak_ts``: the most extreme reading inside it
        - ``direction``: "spike" or "drop"
        - ``severity``: max absolute deviation in std-dev units (sigma)
        - ``sample_count``: number of outlier samples that folded into the
          region

        The output is sorted by absolute peak deviation from the median,
        descending — so the display cap surfaces the most extreme region
        in either direction first.
        """
        if not anomalies:
            return []

        # Map every anomaly back to its sample position in finite_points so
        # we can measure gaps by sample distance (not wall-clock time).
        # _detect_anomalies returns dicts keyed by (timestamp, value, type)
        # and walks finite_points in order, so we re-resolve positions by
        # scanning finite_points once and matching the anomaly key set.
        anomaly_keys = {
            (a.get("timestamp"), a.get("value"), a.get("type")) for a in anomalies
        }
        outliers: list[tuple[int, str | None, float, str]] = []
        for i, (ts, value) in enumerate(finite_points):
            # Match against either spike or drop type at this (ts, value).
            for direction in ("spike", "drop"):
                if (ts, value, direction) in anomaly_keys:
                    outliers.append((i, ts, value, direction))
                    break

        if not outliers:
            return []

        # Sort by sample position so contiguous runs are adjacent.
        outliers.sort(key=lambda x: x[0])

        p50 = stats.get("p50", 0.0)
        std_dev = stats.get("std_dev", 0.0) or 1e-9

        regions: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        last_pos = -1
        last_direction: str | None = None

        for pos, ts, value, direction in outliers:
            if (
                current is None
                or direction != last_direction
                or (pos - last_pos) > self.MAX_REGION_GAP_SAMPLES
            ):
                # Open a new region.
                if current is not None:
                    regions.append(current)
                current = {
                    "direction": direction,
                    "start_ts": ts,
                    "end_ts": ts,
                    "peak_value": value,
                    "peak_ts": ts,
                    "sample_count": 1,
                    "_peak_dev": abs(value - p50),
                }
                last_direction = direction
            else:
                # Extend current region.
                current["end_ts"] = ts
                current["sample_count"] += 1
                dev = abs(value - p50)
                if dev > current["_peak_dev"]:
                    current["_peak_dev"] = dev
                    current["peak_value"] = value
                    current["peak_ts"] = ts
            last_pos = pos

        if current is not None:
            regions.append(current)

        # Compute severity (sigma) and clean up scratch fields.
        for r in regions:
            r["severity_sigma"] = round(r.pop("_peak_dev") / std_dev, 2)

        # Sort by absolute peak deviation from the median, descending —
        # mirrors the per-row sort so the display cap is direction-fair.
        regions.sort(
            key=lambda r: abs(r["peak_value"] - p50),
            reverse=True,
        )
        return regions

    def _calculate_statistics(self, values: list[float]) -> dict[str, float]:
        """Calculate statistical measures.

        ISS-040: includes P25 / P75 / IQR so the formatted summary can
        surface a "Typical operating range (P25-P75)" line distinct from
        the existing P95-anchored outlier threshold. Using P95 alone to
        frame the baseline produces too-wide bands and too-narrow alerting
        thresholds — operators end up missing real regressions.
        """
        n = len(values)
        sorted_values = sorted(values)

        mean = sum(values) / n
        variance = sum((x - mean) ** 2 for x in values) / n
        std_dev = variance**0.5

        # P25 / P75 are calculated from the sorted index. Floor at 0 and
        # ceiling at n-1 so very small series (n < 4) don't IndexError.
        p25 = sorted_values[max(0, int(n * 0.25))]
        p75 = sorted_values[min(n - 1, int(n * 0.75))]

        return {
            "min": min(values),
            "max": max(values),
            "mean": mean,
            "std_dev": std_dev,
            "p25": p25,
            "p50": sorted_values[n // 2],
            "p75": p75,
            "iqr": p75 - p25,
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
          (p50 - 1.5 × (p95 - p50)) **unioned with** a percent-of-baseline
          rule (≥50% below mean). The two rules are complementary: IQR
          proxy catches gradual sustained dips, the percent rule catches
          catastrophic single-point drops on series where the IQR proxy
          is dominated by noise (e.g. a flatline from 100% to 0%). The
          same flat-data guard suppresses spurious drops on nearly
          constant data.

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

        # Percent-of-baseline drop rule. Catches catastrophic drops on
        # series where the IQR proxy is dominated by noise (e.g. a single
        # service that flatlines from 100% to 0%). Unioned with the IQR
        # rule — they are complementary detectors.
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

            # Detect drops (IQR-proxy OR percent-of-baseline rule).
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
        """Format analysis results as natural language summary.

        ISS-039: anomalies are reported as **regions** (contiguous outlier
        runs) rather than per-row outliers. A short heat-event spike with
        five hourly samples surfaces as one region, not five "anomalies".
        ISS-040: the stats block adds a P25-P75 typical operating range
        line (distinct from full data range and the P95 outlier
        threshold) and a bimodal-distribution note when the IQR is wide
        relative to std_dev.
        """
        lines = ["=== METRICS ANALYSIS SUMMARY ===\n"]

        total_metrics = len(summaries)
        all_regions = [r for s in summaries for r in s.get("regions", [])]
        total_regions = len(all_regions)
        total_spike_regions = sum(
            1 for r in all_regions if r.get("direction") == "spike"
        )
        total_drop_regions = sum(1 for r in all_regions if r.get("direction") == "drop")

        lines.append(f"Analyzed {total_metrics} metric(s)")
        if total_regions:
            lines.append(
                f"Detected {total_regions} anomaly region(s) "
                f"({total_spike_regions} spike(s), {total_drop_regions} drop(s))\n"
            )
        else:
            lines.append("Detected 0 anomaly region(s)\n")

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
            regions = summary.get("regions", [])

            lines.append(f"📊 {metric_name} ({count} data points):")
            lines.append(
                f"   Full data range: {self._fmt_val(stats['min'])} - "
                f"{self._fmt_val(stats['max'])}"
            )
            lines.append(
                f"   Mean: {self._fmt_val(stats['mean'])} "
                f"(±{self._fmt_val(stats['std_dev'])})"
            )
            # ISS-040: surface the P25-P75 IQR band as the typical
            # operating range. This gives the agent a tight baseline to
            # answer "what's normal?" without overweighting P95.
            if "p25" in stats and "p75" in stats:
                lines.append(
                    f"   Typical operating range (P25-P75): "
                    f"{self._fmt_val(stats['p25'])} - "
                    f"{self._fmt_val(stats['p75'])} "
                    f"(IQR={self._fmt_val(stats.get('iqr', 0.0))})"
                )
            lines.append(
                f"   Percentiles: p50={self._fmt_val(stats['p50'])}, "
                f"p95={self._fmt_val(stats['p95'])} (outlier threshold), "
                f"p99={self._fmt_val(stats['p99'])}"
            )
            # ISS-040: bimodal heuristic. If IQR is wide relative to
            # std_dev, the data likely has two clusters and the agent
            # should not frame the median as a single baseline.
            iqr = stats.get("iqr", 0.0)
            std_dev = stats.get("std_dev", 0.0)
            if std_dev > 0 and iqr > self.BIMODAL_IQR_TO_STD_RATIO * std_dev:
                lines.append(
                    "   Distribution note: IQR is wide vs std_dev — "
                    "data may be bimodal (two clusters around "
                    f"P25={self._fmt_val(stats['p25'])} and "
                    f"P75={self._fmt_val(stats['p75'])})."
                )
            non_finite = summary.get("non_finite", 0)
            if non_finite:
                lines.append(
                    f"   Non-finite values: {non_finite} "
                    "(NaN/±Inf, excluded from statistics)"
                )

            if regions:
                metric_spikes = sum(1 for r in regions if r.get("direction") == "spike")
                metric_drops = sum(1 for r in regions if r.get("direction") == "drop")
                lines.append(
                    f"   ⚠️  {len(regions)} anomaly region(s) detected "
                    f"({metric_spikes} spike(s), {metric_drops} drop(s)):"
                )

                for region in regions[:10]:
                    direction = region.get("direction", "unknown")
                    label = "SPIKE" if direction == "spike" else "DROP"
                    start_ts = region.get("start_ts") or "unknown"
                    end_ts = region.get("end_ts") or "unknown"
                    peak_ts = region.get("peak_ts") or "unknown"
                    peak_value = region.get("peak_value", 0.0)
                    samples = region.get("sample_count", 1)
                    sigma = region.get("severity_sigma", 0.0)

                    if start_ts == end_ts:
                        span = f"at {start_ts}"
                    else:
                        span = f"from {start_ts} to {end_ts}"
                    lines.append(
                        f"      • {label} REGION {span} "
                        f"({samples} sample(s), peak={self._fmt_val(peak_value)} "
                        f"at {peak_ts}, severity={sigma}σ)"
                    )

                if len(regions) > 10:
                    lines.append(f"      ... and {len(regions) - 10} more")
            else:
                lines.append("   ✓ No anomalies detected")

            lines.append("")

        return "\n".join(lines)
