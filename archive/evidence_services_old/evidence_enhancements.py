"""
Evidence Enhancements - Automated Evidence Processing

This module provides automated enhancements for uploaded evidence:
1. Timeline extraction from logs (Gap 1.2)
2. Hypothesis generation from anomalies (Gap 1.3)
3. Diagnostic state initialization (Gap 1.1 enhancement)

Design: Bridges preprocessing insights to diagnostic workflow automation
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from faultmaven.models.api import DataType, PreprocessedData
from faultmaven.models.evidence import EvidenceCategory
from faultmaven.models.investigation import Hypothesis, HypothesisStatus
from faultmaven.services.evidence.evidence_factory import map_datatype_to_evidence_category


# =============================================================================
# Timeline Extraction
# =============================================================================


def extract_timeline_events(preprocessed: PreprocessedData) -> List[Dict[str, any]]:
    """Extract timeline events from preprocessed data.

    For logs: Extracts key events with timestamps (errors, warnings, state changes)
    For traces: Extracts span timing information
    For metrics: Extracts spike/anomaly timestamps

    Args:
        preprocessed: PreprocessedData from preprocessing pipeline

    Returns:
        List of timeline event dictionaries:
        {
            "timestamp": datetime or str,
            "event_type": "error" | "warning" | "state_change" | "anomaly",
            "description": str,
            "severity": "critical" | "high" | "medium" | "low",
            "source": str (filename or component)
        }
    """
    events = []
    insights = preprocessed.insights

    if not insights:
        return events

    # Log file timeline extraction
    if preprocessed.metadata.data_type == DataType.LOGS_AND_ERRORS:
        # Extract error events
        if "top_errors" in insights:
            for error_info in insights.get("top_errors", [])[:5]:  # Top 5 errors
                # Try to get first occurrence timestamp
                timestamp = error_info.get("first_seen", "unknown")
                events.append({
                    "timestamp": timestamp,
                    "event_type": "error",
                    "description": error_info.get("message", "Unknown error"),
                    "severity": "high" if error_info.get("count", 0) > 10 else "medium",
                    "source": preprocessed.source_metadata.source_url if preprocessed.source_metadata else "log",
                    "count": error_info.get("count", 1)
                })

        # Extract anomaly events
        if "anomalies" in insights:
            for anomaly in insights.get("anomalies", []):
                if isinstance(anomaly, dict):
                    events.append({
                        "timestamp": anomaly.get("timestamp", "unknown"),
                        "event_type": "anomaly",
                        "description": anomaly.get("description", str(anomaly)),
                        "severity": "critical",
                        "source": preprocessed.source_metadata.source_url if preprocessed.source_metadata else "log"
                    })
                else:
                    # Anomaly is string (older format)
                    events.append({
                        "timestamp": "unknown",
                        "event_type": "anomaly",
                        "description": str(anomaly),
                        "severity": "critical",
                        "source": preprocessed.source_metadata.source_url if preprocessed.source_metadata else "log"
                    })

        # Extract time range as bounds
        if "time_range" in insights:
            time_range = insights["time_range"]
            if isinstance(time_range, dict):
                if "start" in time_range:
                    events.append({
                        "timestamp": time_range["start"],
                        "event_type": "state_change",
                        "description": "Log period start",
                        "severity": "low",
                        "source": preprocessed.source_metadata.source_url if preprocessed.source_metadata else "log"
                    })
                if "end" in time_range:
                    events.append({
                        "timestamp": time_range["end"],
                        "event_type": "state_change",
                        "description": "Log period end",
                        "severity": "low",
                        "source": preprocessed.source_metadata.source_url if preprocessed.source_metadata else "log"
                    })

    # Metrics/Performance data timeline extraction
    elif preprocessed.metadata.data_type == DataType.METRICS_AND_PERFORMANCE:
        # Extract spans with high latency or errors (trace data)
        if "slow_spans" in insights:
            for span in insights.get("slow_spans", [])[:5]:
                events.append({
                    "timestamp": span.get("start_time", "unknown"),
                    "event_type": "anomaly",
                    "description": f"Slow span: {span.get('operation', 'unknown')} ({span.get('duration_ms', 0)}ms)",
                    "severity": "high",
                    "source": span.get("service", "trace")
                })

        # Extract spike events (metrics data)
        if "spikes" in insights:
            for spike in insights.get("spikes", []):
                events.append({
                    "timestamp": spike.get("timestamp", "unknown"),
                    "event_type": "anomaly",
                    "description": f"Metric spike: {spike.get('metric_name', 'unknown')} = {spike.get('value', 'N/A')}",
                    "severity": "high",
                    "source": "metrics"
                })

    return events


def should_populate_timeline(preprocessed: PreprocessedData) -> bool:
    """Determine if uploaded data should trigger timeline population.

    Args:
        preprocessed: PreprocessedData to check

    Returns:
        True if data has timeline-relevant information
    """
    # Only populate timeline for data types that have temporal information
    category = map_datatype_to_evidence_category(preprocessed.metadata.data_type)
    return category in (
        EvidenceCategory.SYMPTOMS,  # Logs with timestamps
        EvidenceCategory.TIMELINE,  # Traces
        EvidenceCategory.METRICS,   # Time-series data
    )


# =============================================================================
# Hypothesis Generation from Anomalies
# =============================================================================


def generate_hypotheses_from_anomalies(
    preprocessed: PreprocessedData,
    current_turn: int,
    data_id: str
) -> List[Hypothesis]:
    """Generate hypothesis suggestions from detected anomalies.

    Converts anomalies in preprocessed insights into formal Hypothesis objects
    that can be added to the investigation state.

    Args:
        preprocessed: PreprocessedData with anomaly insights
        current_turn: Current conversation turn number
        data_id: Unique identifier for this preprocessed data

    Returns:
        List of Hypothesis objects generated from anomalies
    """
    hypotheses = []
    insights = preprocessed.insights

    if not insights:
        return hypotheses

    # Generate hypotheses from log/error anomalies
    if preprocessed.metadata.data_type == DataType.LOGS_AND_ERRORS:
        anomalies = insights.get("anomalies", [])

        for anomaly in anomalies[:3]:  # Limit to top 3 anomalies
            if isinstance(anomaly, dict):
                anomaly_desc = anomaly.get("description", str(anomaly))
            else:
                anomaly_desc = str(anomaly)

            # Generate hypothesis statement from anomaly
            hypothesis_statement = _generate_hypothesis_statement(
                anomaly_desc,
                preprocessed.metadata.data_type
            )

            if hypothesis_statement:
                hypotheses.append(Hypothesis(
                    hypothesis_id=f"hyp_{uuid4().hex[:8]}",
                    statement=hypothesis_statement,
                    category=_categorize_hypothesis(anomaly_desc),
                    likelihood=0.4,  # Initial moderate confidence for anomaly-based hypotheses
                    initial_likelihood=0.4,
                    status=HypothesisStatus.ACTIVE,
                    created_at_turn=current_turn,
                    last_updated_turn=current_turn,
                    supporting_evidence=[data_id],  # Link to source evidence
                ))

        # Generate hypothesis from error patterns
        top_errors = insights.get("top_errors", [])
        if top_errors:
            top_error = top_errors[0]
            error_message = top_error.get("message", "")
            error_count = top_error.get("count", 0)

            if error_count > 5:  # Significant error frequency
                hypothesis_statement = _generate_hypothesis_from_error(
                    error_message,
                    error_count
                )

                if hypothesis_statement:
                    hypotheses.append(Hypothesis(
                        hypothesis_id=f"hyp_{uuid4().hex[:8]}",
                        statement=hypothesis_statement,
                        category=_categorize_hypothesis(error_message),
                        likelihood=0.5,  # Higher confidence for frequent errors
                        initial_likelihood=0.5,
                        status=HypothesisStatus.ACTIVE,
                        created_at_turn=current_turn,
                        last_updated_turn=current_turn,
                        supporting_evidence=[data_id],
                    ))

        # Also check for error report specific insights (exception/root cause)
        # These are also LOGS_AND_ERRORS type, but with richer structured data
        exception_type = insights.get("exception_type")
        root_cause = insights.get("root_cause")

        if exception_type and root_cause:
            hypothesis_statement = f"Root cause: {root_cause}"

            hypotheses.append(Hypothesis(
                hypothesis_id=f"hyp_{uuid4().hex[:8]}",
                statement=hypothesis_statement,
                category=_categorize_hypothesis(exception_type),
                likelihood=0.6,  # High confidence for stack trace analysis
                initial_likelihood=0.6,
                status=HypothesisStatus.ACTIVE,
                created_at_turn=current_turn,
                last_updated_turn=current_turn,
                supporting_evidence=[data_id],
            ))

    return hypotheses


def _generate_hypothesis_statement(anomaly_desc: str, data_type: DataType) -> Optional[str]:
    """Generate a hypothesis statement from anomaly description.

    Args:
        anomaly_desc: Anomaly description string
        data_type: Type of data source

    Returns:
        Hypothesis statement or None if not applicable
    """
    anomaly_lower = anomaly_desc.lower()

    # Pattern matching for common anomaly types
    if "spike" in anomaly_lower or "surge" in anomaly_lower:
        return f"System experiencing resource spike: {anomaly_desc}"

    if "timeout" in anomaly_lower:
        return f"Timeout issue detected: {anomaly_desc}"

    if "connection" in anomaly_lower:
        return f"Connection problem: {anomaly_desc}"

    if "memory" in anomaly_lower or "oom" in anomaly_lower:
        return f"Memory pressure issue: {anomaly_desc}"

    if "cpu" in anomaly_lower:
        return f"CPU resource issue: {anomaly_desc}"

    # Generic hypothesis for other anomalies
    if len(anomaly_desc) > 10:
        return f"Anomaly detected: {anomaly_desc[:100]}"

    return None


def _generate_hypothesis_from_error(error_message: str, count: int) -> Optional[str]:
    """Generate hypothesis from frequent error pattern.

    Args:
        error_message: Error message text
        count: Number of occurrences

    Returns:
        Hypothesis statement
    """
    error_lower = error_message.lower()

    if "null" in error_lower or "none" in error_lower:
        return f"Null reference error occurring {count} times, likely uninitialized variable"

    if "connection" in error_lower:
        return f"Connection failures ({count} occurrences) indicating network/service issue"

    if "timeout" in error_lower:
        return f"Timeout errors ({count} times) suggesting performance degradation"

    if "not found" in error_lower or "404" in error_lower:
        return f"Resource not found ({count} times) indicating missing dependency"

    if "permission" in error_lower or "denied" in error_lower:
        return f"Permission errors ({count} times) indicating access control issue"

    # Generic
    return f"Frequent error pattern ({count} occurrences): {error_message[:80]}"


def _categorize_hypothesis(description: str) -> str:
    """Categorize hypothesis by likely root cause domain.

    Args:
        description: Hypothesis or error description

    Returns:
        Category string (infrastructure, code, config, data, external)
    """
    desc_lower = description.lower()

    # Infrastructure issues
    if any(kw in desc_lower for kw in ["memory", "cpu", "disk", "network", "timeout", "connection"]):
        return "infrastructure"

    # Code issues
    if any(kw in desc_lower for kw in ["null", "exception", "error", "bug", "crash", "assertion"]):
        return "code"

    # Configuration issues
    if any(kw in desc_lower for kw in ["config", "setting", "environment", "variable", "missing"]):
        return "config"

    # Data issues
    if any(kw in desc_lower for kw in ["data", "database", "query", "schema", "validation"]):
        return "data"

    # External dependencies
    if any(kw in desc_lower for kw in ["api", "service", "external", "third-party", "integration"]):
        return "external"

    return "unknown"


def should_generate_hypotheses(preprocessed: PreprocessedData) -> bool:
    """Determine if uploaded data should trigger hypothesis generation.

    Args:
        preprocessed: PreprocessedData to check

    Returns:
        True if data has significant anomalies or error patterns
    """
    insights = preprocessed.insights

    if not insights:
        return False

    # Check for anomalies
    if "anomalies" in insights and len(insights["anomalies"]) > 0:
        return True

    # Check for significant error patterns
    if "top_errors" in insights:
        top_errors = insights["top_errors"]
        if top_errors and top_errors[0].get("count", 0) > 5:
            return True

    # Check for stack trace analysis (part of LOGS_AND_ERRORS)
    if preprocessed.metadata.data_type == DataType.LOGS_AND_ERRORS:
        if "exception_type" in insights and "root_cause" in insights:
            return True

    return False
