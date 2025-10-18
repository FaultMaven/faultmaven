"""Data Preprocessing Module

Purpose: Transform raw data insights into LLM-digestible summaries

This module takes the detailed insights from data processors (LogProcessor,
MetricsProcessor, etc.) and formats them into concise, LLM-friendly summaries
that fit within context windows while preserving critical information.

Key Functions:
- preprocess_logs(): Format log analysis insights (50K lines → 8K chars)
- preprocess_metrics(): Format metrics data insights
- preprocess_errors(): Format stack trace insights
- preprocess_config(): Format configuration file insights

Design Principles:
- Preserve critical information (errors, anomalies, patterns)
- Use structured formatting (headers, bullet points)
- Include statistical summaries
- Highlight actionable insights
- Stay within LLM context limits
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


def preprocess_logs(insights: Dict[str, Any], raw_content: str, max_chars: int = 8000) -> str:
    """
    Format log analysis insights into LLM-digestible summary.

    Takes the output from LogProcessor.process() and creates a concise,
    structured summary suitable for LLM analysis.

    Args:
        insights: Dictionary from LogProcessor.process() containing:
            - total_entries: int
            - time_range: dict with start/end/duration_hours
            - log_level_distribution: dict of level counts
            - error_summary: dict with total_errors/error_rate
            - top_errors: list of error messages
            - anomalies: list of anomaly dicts
            - performance_metrics: dict
            - unique_ips: int
        raw_content: Original log content (used for sampling)
        max_chars: Maximum characters in output (default: 8000)

    Returns:
        Formatted summary string suitable for LLM analysis
    """
    summary_parts = []

    # Header
    summary_parts.append("=" * 80)
    summary_parts.append("LOG FILE ANALYSIS SUMMARY")
    summary_parts.append("=" * 80)
    summary_parts.append("")

    # 1. Basic Statistics
    summary_parts.append("## BASIC STATISTICS")
    summary_parts.append(f"Total log entries: {insights.get('total_entries', 0):,}")

    time_range = insights.get('time_range')
    if time_range:
        summary_parts.append(f"Time range: {time_range['start']} to {time_range['end']}")
        summary_parts.append(f"Duration: {time_range['duration_hours']:.2f} hours")

    unique_ips = insights.get('unique_ips', 0)
    if unique_ips > 0:
        summary_parts.append(f"Unique IP addresses: {unique_ips}")

    summary_parts.append("")

    # 2. Log Level Distribution
    log_levels = insights.get('log_level_distribution', {})
    if log_levels:
        summary_parts.append("## LOG LEVEL DISTRIBUTION")
        total = sum(log_levels.values())
        for level in ['CRITICAL', 'FATAL', 'ERROR', 'WARN', 'WARNING', 'INFO', 'DEBUG']:
            count = log_levels.get(level, 0)
            if count > 0:
                percentage = (count / total * 100) if total > 0 else 0
                summary_parts.append(f"  {level:12s}: {count:6,} ({percentage:5.1f}%)")
        summary_parts.append("")

    # 3. Error Summary
    error_summary = insights.get('error_summary', {})
    if error_summary:
        summary_parts.append("## ERROR SUMMARY")
        total_errors = error_summary.get('total_errors', 0)
        error_rate = error_summary.get('error_rate', 0) * 100

        if total_errors > 0:
            summary_parts.append(f"Total errors: {total_errors:,}")
            summary_parts.append(f"Error rate: {error_rate:.2f}%")

            # Add critical error count if available
            critical = log_levels.get('CRITICAL', 0) + log_levels.get('FATAL', 0)
            if critical > 0:
                summary_parts.append(f"Critical/Fatal errors: {critical:,}")
        else:
            summary_parts.append("No errors detected ✓")
        summary_parts.append("")

    # 4. Top Errors
    top_errors = insights.get('top_errors', [])
    if top_errors:
        summary_parts.append("## TOP ERROR PATTERNS")
        summary_parts.append(f"(Showing top {min(10, len(top_errors))} unique errors)")
        summary_parts.append("")

        for i, error_info in enumerate(top_errors[:10], 1):
            if isinstance(error_info, dict):
                error_msg = error_info.get('message', str(error_info))
                error_count = error_info.get('count', 1)
                summary_parts.append(f"{i}. [{error_count}x] {_truncate(error_msg, 150)}")
            else:
                summary_parts.append(f"{i}. {_truncate(str(error_info), 150)}")

        summary_parts.append("")

    # 5. Anomalies
    anomalies = insights.get('anomalies', [])
    if anomalies:
        summary_parts.append("## DETECTED ANOMALIES")
        summary_parts.append(f"Total anomalies detected: {len(anomalies)}")
        summary_parts.append("")

        for i, anomaly in enumerate(anomalies[:5], 1):  # Top 5 anomalies
            if isinstance(anomaly, dict):
                anomaly_type = anomaly.get('type', 'Unknown')
                anomaly_desc = anomaly.get('description', '')
                severity = anomaly.get('severity', 'medium')
                summary_parts.append(f"{i}. [{severity.upper()}] {anomaly_type}")
                if anomaly_desc:
                    summary_parts.append(f"   {_truncate(anomaly_desc, 200)}")
            else:
                summary_parts.append(f"{i}. {_truncate(str(anomaly), 200)}")

        if len(anomalies) > 5:
            summary_parts.append(f"   ... and {len(anomalies) - 5} more anomalies")

        summary_parts.append("")

    # 6. Performance Metrics
    perf_metrics = insights.get('performance_metrics', {})
    if perf_metrics:
        summary_parts.append("## PERFORMANCE METRICS")

        for metric_name, metric_value in perf_metrics.items():
            if isinstance(metric_value, dict):
                # Complex metric (e.g., response_times with avg/p95/p99)
                summary_parts.append(f"{metric_name}:")
                for sub_key, sub_value in metric_value.items():
                    if isinstance(sub_value, float):
                        summary_parts.append(f"  {sub_key}: {sub_value:.2f}")
                    else:
                        summary_parts.append(f"  {sub_key}: {sub_value}")
            elif isinstance(metric_value, (int, float)):
                if isinstance(metric_value, float):
                    summary_parts.append(f"{metric_name}: {metric_value:.2f}")
                else:
                    summary_parts.append(f"{metric_name}: {metric_value:,}")
            else:
                summary_parts.append(f"{metric_name}: {metric_value}")

        summary_parts.append("")

    # 7. Sample Log Entries (for context)
    summary_parts.append("## SAMPLE LOG ENTRIES")
    summary_parts.append("(First and last entries for context)")
    summary_parts.append("")

    lines = raw_content.strip().split('\n')
    if lines:
        # First 2 entries
        summary_parts.append("First entries:")
        for i, line in enumerate(lines[:2], 1):
            summary_parts.append(f"  {i}. {_truncate(line, 200)}")

        summary_parts.append("")

        # Last 2 entries (if different from first)
        if len(lines) > 4:
            summary_parts.append("Last entries:")
            for i, line in enumerate(lines[-2:], len(lines) - 1):
                summary_parts.append(f"  {i}. {_truncate(line, 200)}")
            summary_parts.append("")

    # 8. Contextual Analysis (if available)
    contextual = insights.get('contextual_analysis', {})
    if contextual:
        summary_parts.append("## CONTEXTUAL INSIGHTS")
        for key, value in contextual.items():
            if isinstance(value, list):
                summary_parts.append(f"{key}: {', '.join(map(str, value[:5]))}")
            else:
                summary_parts.append(f"{key}: {value}")
        summary_parts.append("")

    # 9. Footer
    summary_parts.append("=" * 80)
    summary_parts.append("END OF LOG ANALYSIS SUMMARY")
    summary_parts.append("=" * 80)

    # Join all parts
    full_summary = "\n".join(summary_parts)

    # Truncate if exceeds max_chars
    if len(full_summary) > max_chars:
        truncation_msg = f"\n\n[TRUNCATED: Summary exceeded {max_chars} characters. Showing first {max_chars} characters.]"
        full_summary = full_summary[:max_chars - len(truncation_msg)] + truncation_msg

    logger.debug(
        f"Preprocessed log insights: {len(raw_content)} chars raw → "
        f"{len(full_summary)} chars summary"
    )

    return full_summary


def preprocess_metrics(insights: Dict[str, Any], raw_content: str, max_chars: int = 6000) -> str:
    """
    Format metrics analysis insights into LLM-digestible summary.

    Args:
        insights: Dictionary from MetricsProcessor.process()
        raw_content: Original metrics data
        max_chars: Maximum characters in output

    Returns:
        Formatted summary string
    """
    summary_parts = []

    summary_parts.append("=" * 80)
    summary_parts.append("METRICS DATA ANALYSIS SUMMARY")
    summary_parts.append("=" * 80)
    summary_parts.append("")

    # TODO: Implement metrics preprocessing
    # For now, return basic structure
    summary_parts.append("## METRICS OVERVIEW")
    summary_parts.append(f"Total data points: {insights.get('total_points', 0):,}")
    summary_parts.append("")

    summary_parts.append("⚠️  Metrics preprocessing not yet implemented")
    summary_parts.append("This feature will format time-series metrics, detect anomalies,")
    summary_parts.append("and highlight correlations for LLM analysis.")
    summary_parts.append("")

    summary_parts.append("=" * 80)

    return "\n".join(summary_parts)


def preprocess_errors(insights: Dict[str, Any], raw_content: str, max_chars: int = 5000) -> str:
    """
    Format error/stack trace analysis insights into LLM-digestible summary.

    Args:
        insights: Dictionary from ErrorProcessor.process()
        raw_content: Original error/stack trace content
        max_chars: Maximum characters in output

    Returns:
        Formatted summary string
    """
    summary_parts = []

    summary_parts.append("=" * 80)
    summary_parts.append("ERROR REPORT ANALYSIS SUMMARY")
    summary_parts.append("=" * 80)
    summary_parts.append("")

    # TODO: Implement error preprocessing
    # For now, return basic structure
    summary_parts.append("## ERROR OVERVIEW")
    summary_parts.append("")

    summary_parts.append("⚠️  Error preprocessing not yet implemented")
    summary_parts.append("This feature will parse stack traces, identify root causes,")
    summary_parts.append("and extract actionable debugging information.")
    summary_parts.append("")

    summary_parts.append("=" * 80)

    return "\n".join(summary_parts)


def preprocess_config(insights: Dict[str, Any], raw_content: str, max_chars: int = 6000) -> str:
    """
    Format configuration file analysis insights into LLM-digestible summary.

    Args:
        insights: Dictionary from ConfigProcessor.process()
        raw_content: Original config file content
        max_chars: Maximum characters in output

    Returns:
        Formatted summary string
    """
    summary_parts = []

    summary_parts.append("=" * 80)
    summary_parts.append("CONFIGURATION FILE ANALYSIS SUMMARY")
    summary_parts.append("=" * 80)
    summary_parts.append("")

    # TODO: Implement config preprocessing
    # For now, return basic structure
    summary_parts.append("## CONFIG OVERVIEW")
    summary_parts.append("")

    summary_parts.append("⚠️  Configuration preprocessing not yet implemented")
    summary_parts.append("This feature will analyze config files, detect misconfigurations,")
    summary_parts.append("and suggest improvements for LLM analysis.")
    summary_parts.append("")

    summary_parts.append("=" * 80)

    return "\n".join(summary_parts)


def _truncate(text: str, max_length: int) -> str:
    """Truncate text to max_length with ellipsis"""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."


def get_preprocessor_for_data_type(data_type: str):
    """
    Get the appropriate preprocessing function for a data type.

    Args:
        data_type: Data type enum value (e.g., "LOG_FILE", "METRICS_DATA")

    Returns:
        Preprocessing function (preprocess_logs, preprocess_metrics, etc.)
    """
    preprocessors = {
        "LOG_FILE": preprocess_logs,
        "METRICS_DATA": preprocess_metrics,
        "ERROR_REPORT": preprocess_errors,
        "CONFIG_FILE": preprocess_config,
    }

    return preprocessors.get(data_type, None)
