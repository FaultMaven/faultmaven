"""Metrics shim for Prometheus metrics collection.

Provides graceful degradation for Prometheus metrics:
- If prometheus_client installed + ENABLE_METRICS=true: Use Prometheus
- Otherwise: No-op metrics (do nothing)

Usage:
    from faultmaven.infrastructure.shims.metrics import request_counter, request_duration

    # Counter usage
    request_counter.inc()
    request_counter.labels(method="GET", endpoint="/api/cases").inc()

    # Histogram usage
    request_duration.observe(0.5)
    request_duration.labels(method="POST", endpoint="/api/cases").observe(1.2)

    # Gauge usage
    active_connections.set(42)
    active_connections.inc()
    active_connections.dec()
"""

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Feature detection
try:
    from prometheus_client import Counter as PrometheusCounter
    from prometheus_client import Gauge as PrometheusGauge
    from prometheus_client import Histogram as PrometheusHistogram
    from prometheus_client import Info as PrometheusInfo
    from prometheus_client import Summary as PrometheusSummary

    PROMETHEUS_AVAILABLE = True
    logger.info("Prometheus metrics library available")
except ImportError:
    PROMETHEUS_AVAILABLE = False
    PrometheusCounter = None  # type: ignore
    PrometheusHistogram = None  # type: ignore
    PrometheusGauge = None  # type: ignore
    PrometheusInfo = None  # type: ignore
    PrometheusSummary = None  # type: ignore
    logger.debug("Prometheus not installed - metrics disabled")


def _is_metrics_enabled() -> bool:
    """Check if metrics collection is enabled via environment variable."""
    return os.getenv("ENABLE_METRICS", "false").lower() == "true"


class NoOpMetric:
    """
    No-op metric that does nothing.

    Used when Prometheus is unavailable or disabled.
    Provides the same interface as Prometheus metrics but with no-op implementations.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize no-op metric (does nothing)."""
        pass

    def inc(self, amount: float = 1, *args: Any, **kwargs: Any) -> None:
        """No-op increment."""
        pass

    def dec(self, amount: float = 1, *args: Any, **kwargs: Any) -> None:
        """No-op decrement."""
        pass

    def set(self, value: float, *args: Any, **kwargs: Any) -> None:
        """No-op set."""
        pass

    def observe(self, amount: float, *args: Any, **kwargs: Any) -> None:
        """No-op observe."""
        pass

    def info(self, value: Dict[str, str], *args: Any, **kwargs: Any) -> None:
        """No-op info."""
        pass

    def labels(self, *args: Any, **kwargs: Any) -> "NoOpMetric":
        """No-op labels (returns self for chaining)."""
        return self

    def time(self) -> "NoOpContextManager":
        """No-op timer context manager."""
        return NoOpContextManager()


class NoOpContextManager:
    """No-op context manager for timing metrics."""

    def __enter__(self) -> "NoOpContextManager":
        """Enter context (does nothing)."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit context (does nothing)."""
        pass


def Counter(
    name: str,
    documentation: str,
    labelnames: Optional[List[str]] = None,
    namespace: str = "",
    subsystem: str = "",
    unit: str = "",
    registry: Any = None,
) -> Any:
    """
    Create a Counter metric with graceful degradation.

    If Prometheus is available and ENABLE_METRICS=true, creates a real Counter.
    Otherwise, returns a no-op metric that does nothing.

    Args:
        name: Metric name
        documentation: Metric description
        labelnames: Optional list of label names
        namespace: Optional metric namespace
        subsystem: Optional metric subsystem
        unit: Optional metric unit
        registry: Optional Prometheus registry

    Returns:
        Prometheus Counter or NoOpMetric

    Example:
        request_counter = Counter(
            'http_requests_total',
            'Total HTTP requests',
            labelnames=['method', 'endpoint']
        )
        request_counter.labels(method='GET', endpoint='/api/cases').inc()
    """
    metrics_enabled = _is_metrics_enabled()

    if PROMETHEUS_AVAILABLE and metrics_enabled:
        logger.debug(f"Creating Prometheus Counter: {name}")
        return PrometheusCounter(
            name=name,
            documentation=documentation,
            labelnames=labelnames or [],
            namespace=namespace,
            subsystem=subsystem,
            unit=unit,
            registry=registry,
        )
    else:
        logger.debug(
            f"Creating NoOp Counter: {name} "
            f"(PROMETHEUS_AVAILABLE={PROMETHEUS_AVAILABLE}, ENABLE_METRICS={metrics_enabled})"
        )
        return NoOpMetric()


def Histogram(
    name: str,
    documentation: str,
    labelnames: Optional[List[str]] = None,
    buckets: Optional[List[float]] = None,
    namespace: str = "",
    subsystem: str = "",
    unit: str = "",
    registry: Any = None,
) -> Any:
    """
    Create a Histogram metric with graceful degradation.

    If Prometheus is available and ENABLE_METRICS=true, creates a real Histogram.
    Otherwise, returns a no-op metric that does nothing.

    Args:
        name: Metric name
        documentation: Metric description
        labelnames: Optional list of label names
        buckets: Optional histogram buckets
        namespace: Optional metric namespace
        subsystem: Optional metric subsystem
        unit: Optional metric unit
        registry: Optional Prometheus registry

    Returns:
        Prometheus Histogram or NoOpMetric

    Example:
        request_duration = Histogram(
            'http_request_duration_seconds',
            'HTTP request duration',
            labelnames=['method', 'endpoint']
        )
        request_duration.labels(method='POST', endpoint='/api/cases').observe(1.5)
    """
    metrics_enabled = _is_metrics_enabled()

    if PROMETHEUS_AVAILABLE and metrics_enabled:
        logger.debug(f"Creating Prometheus Histogram: {name}")
        kwargs = {
            "name": name,
            "documentation": documentation,
            "labelnames": labelnames or [],
            "namespace": namespace,
            "subsystem": subsystem,
            "unit": unit,
            "registry": registry,
        }
        if buckets is not None:
            kwargs["buckets"] = buckets
        return PrometheusHistogram(**kwargs)
    else:
        logger.debug(
            f"Creating NoOp Histogram: {name} "
            f"(PROMETHEUS_AVAILABLE={PROMETHEUS_AVAILABLE}, ENABLE_METRICS={metrics_enabled})"
        )
        return NoOpMetric()


def Gauge(
    name: str,
    documentation: str,
    labelnames: Optional[List[str]] = None,
    namespace: str = "",
    subsystem: str = "",
    unit: str = "",
    registry: Any = None,
) -> Any:
    """
    Create a Gauge metric with graceful degradation.

    If Prometheus is available and ENABLE_METRICS=true, creates a real Gauge.
    Otherwise, returns a no-op metric that does nothing.

    Args:
        name: Metric name
        documentation: Metric description
        labelnames: Optional list of label names
        namespace: Optional metric namespace
        subsystem: Optional metric subsystem
        unit: Optional metric unit
        registry: Optional Prometheus registry

    Returns:
        Prometheus Gauge or NoOpMetric

    Example:
        active_sessions = Gauge(
            'active_sessions_total',
            'Number of active sessions',
            labelnames=['status']
        )
        active_sessions.labels(status='active').inc()
        active_sessions.labels(status='active').dec()
        active_sessions.labels(status='active').set(42)
    """
    metrics_enabled = _is_metrics_enabled()

    if PROMETHEUS_AVAILABLE and metrics_enabled:
        logger.debug(f"Creating Prometheus Gauge: {name}")
        return PrometheusGauge(
            name=name,
            documentation=documentation,
            labelnames=labelnames or [],
            namespace=namespace,
            subsystem=subsystem,
            unit=unit,
            registry=registry,
        )
    else:
        logger.debug(
            f"Creating NoOp Gauge: {name} "
            f"(PROMETHEUS_AVAILABLE={PROMETHEUS_AVAILABLE}, ENABLE_METRICS={metrics_enabled})"
        )
        return NoOpMetric()


def Summary(
    name: str,
    documentation: str,
    labelnames: Optional[List[str]] = None,
    namespace: str = "",
    subsystem: str = "",
    unit: str = "",
    registry: Any = None,
) -> Any:
    """
    Create a Summary metric with graceful degradation.

    If Prometheus is available and ENABLE_METRICS=true, creates a real Summary.
    Otherwise, returns a no-op metric that does nothing.

    Args:
        name: Metric name
        documentation: Metric description
        labelnames: Optional list of label names
        namespace: Optional metric namespace
        subsystem: Optional metric subsystem
        unit: Optional metric unit
        registry: Optional Prometheus registry

    Returns:
        Prometheus Summary or NoOpMetric

    Example:
        request_latency = Summary(
            'request_latency_seconds',
            'Request latency',
            labelnames=['endpoint']
        )
        request_latency.labels(endpoint='/api/cases').observe(0.5)
    """
    metrics_enabled = _is_metrics_enabled()

    if PROMETHEUS_AVAILABLE and metrics_enabled:
        logger.debug(f"Creating Prometheus Summary: {name}")
        return PrometheusSummary(
            name=name,
            documentation=documentation,
            labelnames=labelnames or [],
            namespace=namespace,
            subsystem=subsystem,
            unit=unit,
            registry=registry,
        )
    else:
        logger.debug(
            f"Creating NoOp Summary: {name} "
            f"(PROMETHEUS_AVAILABLE={PROMETHEUS_AVAILABLE}, ENABLE_METRICS={metrics_enabled})"
        )
        return NoOpMetric()


def Info(
    name: str,
    documentation: str,
    labelnames: Optional[List[str]] = None,
    namespace: str = "",
    subsystem: str = "",
    registry: Any = None,
) -> Any:
    """
    Create an Info metric with graceful degradation.

    If Prometheus is available and ENABLE_METRICS=true, creates a real Info.
    Otherwise, returns a no-op metric that does nothing.

    Args:
        name: Metric name
        documentation: Metric description
        labelnames: Optional list of label names
        namespace: Optional metric namespace
        subsystem: Optional metric subsystem
        registry: Optional Prometheus registry

    Returns:
        Prometheus Info or NoOpMetric

    Example:
        app_info = Info('app', 'Application information')
        app_info.info({'version': '1.0.0', 'environment': 'production'})
    """
    metrics_enabled = _is_metrics_enabled()

    if PROMETHEUS_AVAILABLE and metrics_enabled:
        logger.debug(f"Creating Prometheus Info: {name}")
        return PrometheusInfo(
            name=name,
            documentation=documentation,
            labelnames=labelnames or [],
            namespace=namespace,
            subsystem=subsystem,
            registry=registry,
        )
    else:
        logger.debug(
            f"Creating NoOp Info: {name} "
            f"(PROMETHEUS_AVAILABLE={PROMETHEUS_AVAILABLE}, ENABLE_METRICS={metrics_enabled})"
        )
        return NoOpMetric()


def get_metrics_status() -> dict:
    """
    Get current metrics collection status for diagnostics.

    Returns:
        Dictionary with metrics configuration status:
        - prometheus_available: Whether the prometheus_client library is installed
        - metrics_enabled: Whether ENABLE_METRICS env var is true
        - active: Whether metrics collection is actually active (both conditions met)
    """
    metrics_enabled = _is_metrics_enabled()
    return {
        "prometheus_available": PROMETHEUS_AVAILABLE,
        "metrics_enabled": metrics_enabled,
        "active": PROMETHEUS_AVAILABLE and metrics_enabled,
    }


def is_metrics_active() -> bool:
    """
    Check if metrics collection is currently active.

    Returns:
        True if Prometheus is available AND ENABLE_METRICS=true
    """
    return PROMETHEUS_AVAILABLE and _is_metrics_enabled()


# Pre-defined common metrics (follow evolution strategy example from lines 898-913)
request_counter = Counter(
    "http_requests_total",
    "Total HTTP requests",
    labelnames=["method", "endpoint", "status_code"],
)

request_duration = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    labelnames=["method", "endpoint"],
)

active_sessions = Gauge(
    "active_sessions_total", "Number of active sessions", labelnames=["status"]
)

case_operations = Counter(
    "case_operations_total", "Total case operations", labelnames=["operation", "status"]
)

knowledge_queries = Counter(
    "knowledge_queries_total",
    "Total knowledge base queries",
    labelnames=["query_type", "status"],
)

llm_requests = Counter(
    "llm_requests_total",
    "Total LLM requests",
    labelnames=["provider", "model", "status"],
)

llm_latency = Histogram(
    "llm_request_duration_seconds",
    "LLM request duration in seconds",
    labelnames=["provider", "model"],
)
