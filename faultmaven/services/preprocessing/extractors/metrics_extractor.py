"""
METRICS_AND_PERFORMANCE Extractor

Analyzes quantitative performance data (CSV, JSON time-series) and detects
anomalies using statistical methods. No LLM calls required.
"""

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

# Interface imports for clean architecture compliance
if TYPE_CHECKING:
    from faultmaven.models.interfaces import ISanitizer, ITracer, IVectorStore


class MetricsAndPerformanceExtractor:
    """Statistical analysis of performance metrics (0 LLM calls)"""

    # Anomaly detection thresholds
    SPIKE_SIGMA_THRESHOLD = 3.0  # Standard deviations for spike detection
    DROP_PERCENT_THRESHOLD = 0.50  # 50% drop from baseline
    MAX_ANOMALIES_REPORTED = 20  # Safety limit
    MAX_OUTPUT_LENGTH = 5000  # Character limit for output

    @property
    def strategy_name(self) -> str:
        return "statistical"

    @property
    def llm_calls_used(self) -> int:
        return 0

    def extract(self, content: str) -> str:
        """
        Extract and analyze metrics data

        Steps:
        1. Detect format (CSV, JSON time-series, Prometheus)
        2. Parse data into time-series
        3. Calculate statistics (min, max, mean, p95, p99)
        4. Detect anomalies (spikes, drops)
        5. Generate natural language summary
        """
        # Try to parse as different formats
        time_series = self._parse_metrics(content)

        if not time_series:
            # Valid CSV with no numeric columns — provide structural summary
            csv_summary = self._summarize_csv_structure(content)
            if csv_summary:
                return csv_summary
            return "[Failed to parse metrics data - unsupported format]"

        # Analyze each metric
        summaries = []
        for metric_name, data_points in time_series.items():
            summary = self._analyze_metric(metric_name, data_points)
            summaries.append(summary)

        # Combine summaries
        output = self._format_summary(summaries)

        # Safety truncation
        if len(output) > self.MAX_OUTPUT_LENGTH:
            output = output[: self.MAX_OUTPUT_LENGTH] + "\n\n... [Truncated for length]"

        return output

    def _parse_metrics(
        self, content: str
    ) -> Optional[Dict[str, List[Tuple[Optional[str], float]]]]:
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
    ) -> Optional[Dict[str, List[Tuple[Optional[str], float]]]]:
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
        self, data: List[Dict]
    ) -> Dict[str, List[Tuple[Optional[str], float]]]:
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

    def _parse_json_dict(
        self, data: Dict
    ) -> Dict[str, List[Tuple[Optional[str], float]]]:
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

    def _parse_csv_metrics(
        self, content: str
    ) -> Optional[Dict[str, List[Tuple[Optional[str], float]]]]:
        """Parse CSV format with header row.

        Auto-detects which columns are numeric by sampling data rows,
        rather than assuming all non-first columns are numeric.
        """
        lines = content.strip().split("\n")
        if len(lines) < 2:
            return None

        # Parse header
        header = [col.strip() for col in lines[0].split(",")]
        if not header:
            return None

        # Sample up to 10 data rows to detect column types
        sample_rows = []
        for line in lines[1 : min(len(lines), 12)]:
            parts = [p.strip() for p in line.split(",")]
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
        result: Dict[str, List[Tuple[Optional[str], float]]] = {
            header[i]: [] for i in metric_cols
        }

        for line in lines[1:]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != len(header):
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
    ) -> Optional[Dict[str, List[Tuple[Optional[str], float]]]]:
        """Parse Prometheus text exposition format"""
        result = {}

        # Match lines like: metric_name{labels} value timestamp
        pattern = r"^([a-zA-Z_:][a-zA-Z0-9_:]*)\s+([\d.eE+-]+)"

        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            match = re.match(pattern, line)
            if match:
                metric_name = match.group(1)
                value = float(match.group(2))

                if metric_name not in result:
                    result[metric_name] = []

                result[metric_name].append((None, value))

        return result if result else None

    def _summarize_csv_structure(self, content: str) -> Optional[str]:
        """Produce a structural summary for valid CSVs with no numeric metrics columns."""
        lines = content.strip().split("\n")
        if len(lines) < 2:
            return None

        header = [col.strip() for col in lines[0].split(",")]
        if len(header) < 2:
            return None

        # Verify it's a valid CSV by checking at least some rows match the header count
        valid_rows = 0
        for line in lines[1:]:
            if len(line.split(",")) == len(header):
                valid_rows += 1

        if valid_rows == 0:
            return None

        total_rows = len(lines) - 1

        # Collect value distributions for categorical columns (sample first 200 rows)
        col_values: Dict[str, Dict[str, int]] = {col: {} for col in header}
        for line in lines[1 : min(len(lines), 202)]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != len(header):
                continue
            for i, val in enumerate(parts):
                counts = col_values[header[i]]
                counts[val] = counts.get(val, 0) + 1

        out = [f"=== CSV STRUCTURE SUMMARY ===\n"]
        out.append(f"Rows: {total_rows} (valid: {valid_rows})")
        out.append(f"Columns ({len(header)}): {', '.join(header)}\n")

        for col_name in header:
            counts = col_values[col_name]
            unique = len(counts)
            out.append(f"  {col_name}: {unique} unique value(s)")
            # Show top 5 values for columns with low cardinality
            if unique <= 20:
                sorted_vals = sorted(counts.items(), key=lambda x: -x[1])[:5]
                for val, cnt in sorted_vals:
                    display = val[:60] + "..." if len(val) > 60 else val
                    out.append(f"    - {display} ({cnt}x)")

        return "\n".join(out)

    def _analyze_metric(
        self, metric_name: str, data_points: List[Tuple[Optional[str], float]]
    ) -> Dict[str, Any]:
        """
        Analyze single metric time-series

        Returns summary dict with stats and anomalies
        """
        values = [v for _, v in data_points]
        timestamps = [t for t, _ in data_points]

        if not values:
            return {"metric": metric_name, "count": 0, "error": "No data points"}

        # Calculate statistics
        stats = self._calculate_statistics(values)

        # Detect anomalies
        anomalies = self._detect_anomalies(data_points, stats)

        return {
            "metric": metric_name,
            "count": len(values),
            "stats": stats,
            "anomalies": anomalies[: self.MAX_ANOMALIES_REPORTED],
        }

    def _calculate_statistics(self, values: List[float]) -> Dict[str, float]:
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
        self, data_points: List[Tuple[Optional[str], float]], stats: Dict[str, float]
    ) -> List[Dict[str, Any]]:
        """
        Detect anomalies using statistical methods

        Detects:
        - Spikes: Values >3σ above mean
        - Drops: Values <50% of baseline (non-zero mean)
        """
        anomalies = []
        mean = stats["mean"]
        std_dev = stats["std_dev"]

        spike_threshold = mean + (self.SPIKE_SIGMA_THRESHOLD * std_dev)
        drop_threshold = mean * (1 - self.DROP_PERCENT_THRESHOLD) if mean > 0 else None

        for timestamp, value in data_points:
            # Detect spikes
            if value > spike_threshold and std_dev > 0:
                sigma = (value - mean) / std_dev
                anomalies.append(
                    {
                        "type": "spike",
                        "timestamp": timestamp,
                        "value": value,
                        "sigma": round(sigma, 2),
                        "threshold": round(spike_threshold, 2),
                    }
                )

            # Detect drops
            elif drop_threshold is not None and value < drop_threshold:
                drop_percent = ((mean - value) / mean) * 100
                anomalies.append(
                    {
                        "type": "drop",
                        "timestamp": timestamp,
                        "value": value,
                        "drop_percent": round(drop_percent, 1),
                        "baseline": round(mean, 2),
                    }
                )

        return anomalies

    def _format_summary(self, summaries: List[Dict[str, Any]]) -> str:
        """Format analysis results as natural language summary"""
        lines = ["=== METRICS ANALYSIS SUMMARY ===\n"]

        total_metrics = len(summaries)
        total_anomalies = sum(len(s.get("anomalies", [])) for s in summaries)

        lines.append(f"Analyzed {total_metrics} metric(s)")
        lines.append(f"Detected {total_anomalies} anomaly/anomalies\n")

        for summary in summaries:
            metric_name = summary["metric"]
            count = summary["count"]

            if "error" in summary:
                lines.append(f"❌ {metric_name}: {summary['error']}")
                continue

            stats = summary["stats"]
            anomalies = summary["anomalies"]

            lines.append(f"📊 {metric_name} ({count} data points):")
            lines.append(f"   Range: {stats['min']:.2f} - {stats['max']:.2f}")
            lines.append(f"   Mean: {stats['mean']:.2f} (±{stats['std_dev']:.2f})")
            lines.append(
                f"   Percentiles: p50={stats['p50']:.2f}, p95={stats['p95']:.2f}, p99={stats['p99']:.2f}"
            )

            if anomalies:
                lines.append(f"   ⚠️  {len(anomalies)} anomaly/anomalies detected:")

                for anomaly in anomalies[:10]:  # Show first 10
                    anom_type = anomaly["type"]
                    timestamp = anomaly.get("timestamp", "unknown")
                    value = anomaly["value"]

                    if anom_type == "spike":
                        sigma = anomaly["sigma"]
                        lines.append(
                            f"      • SPIKE at {timestamp}: {value:.2f} ({sigma}σ above mean)"
                        )
                    elif anom_type == "drop":
                        drop_pct = anomaly["drop_percent"]
                        lines.append(
                            f"      • DROP at {timestamp}: {value:.2f} ({drop_pct}% below baseline)"
                        )

                if len(anomalies) > 10:
                    lines.append(f"      ... and {len(anomalies) - 10} more")
            else:
                lines.append("   ✓ No anomalies detected")

            lines.append("")

        return "\n".join(lines)
