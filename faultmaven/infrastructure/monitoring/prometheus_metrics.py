"""Prometheus Metrics for FaultMaven.

This module provides Prometheus-format metrics exposition for production monitoring.
Metrics are exposed via GET /metrics endpoint when ENABLE_METRICS=true.

Metrics Included:
- faultmaven_build_info: Build/deployment information (version, commit, environment)
- http_requests_total: Total HTTP requests by endpoint, method, status
- http_request_duration_seconds: HTTP request latency histogram
- llm_requests_total: Total LLM API requests by provider, model, status
- llm_request_duration_seconds: LLM request latency histogram
- ml_model_load_seconds: Time to load ML models
- ml_model_loaded: Whether ML model is loaded (gauge)

Label Constraints (to prevent cardinality explosion):
- endpoint: Route template (e.g., /cases/{id}), NOT raw URL path
- method: HTTP method (GET, POST, etc.)
- status_code: HTTP status code (bounded set)
- provider: LLM provider name (bounded set)
- model: Model name (bounded set)
- NO: case_id, user_id, org_id, session_id, user-agent, IP

Usage:
    from faultmaven.infrastructure.monitoring.prometheus_metrics import (
        get_metrics_registry,
        is_metrics_enabled,
        HTTP_REQUESTS,
        HTTP_LATENCY,
    )

    # Record a request
    HTTP_REQUESTS.labels(endpoint="/cases/{id}", method="GET", status_code="200").inc()

    # Record latency
    with HTTP_LATENCY.labels(endpoint="/cases/{id}", method="GET").time():
        response = await handle_request()

Environment Variables:
    ENABLE_METRICS: Set to 'true' to enable Prometheus metrics
"""

import os
import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# Check if prometheus_client is available
try:
    from prometheus_client import (
        Counter,
        Histogram,
        Gauge,
        Info,
        REGISTRY,
        CONTENT_TYPE_LATEST,
        generate_latest,
        CollectorRegistry,
    )
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logger.info("prometheus_client not installed, metrics will be disabled")
    # Create dummy classes for type hints
    REGISTRY = None
    CONTENT_TYPE_LATEST = "text/plain"

    def generate_latest(registry=None):
        return b""


def _is_metrics_enabled() -> bool:
    """Check if metrics are enabled via environment variable."""
    return os.getenv("ENABLE_METRICS", "false").lower() == "true"


def is_metrics_enabled() -> bool:
    """Public check for metrics enabled status."""
    return PROMETHEUS_AVAILABLE and _is_metrics_enabled()


def get_metrics_status() -> dict:
    """Get current metrics status for health checks."""
    return {
        "prometheus_available": PROMETHEUS_AVAILABLE,
        "metrics_enabled": _is_metrics_enabled(),
        "active": is_metrics_enabled(),
    }


# =============================================================================
# Build Info Metric
# =============================================================================

def _get_git_commit() -> str:
    """Get current git commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _get_environment() -> str:
    """Get current environment from settings."""
    return os.getenv("ENVIRONMENT", "development")


def _get_version() -> str:
    """Get application version."""
    return "1.0.0"  # TODO: Read from package metadata


# Only create metrics if prometheus_client is available
if PROMETHEUS_AVAILABLE:
    # Build info as a Gauge with constant labels (Prometheus best practice)
    BUILD_INFO = Gauge(
        "faultmaven_build_info",
        "FaultMaven build information",
        ["version", "commit", "environment"],
    )

    # Initialize build info with value 1 (standard for info metrics)
    _version = _get_version()
    _commit = _get_git_commit()
    _environment = _get_environment()
    BUILD_INFO.labels(
        version=_version,
        commit=_commit,
        environment=_environment,
    ).set(1)
    logger.debug(f"Build info metric set: version={_version}, commit={_commit}, env={_environment}")

    # =============================================================================
    # HTTP Request Metrics
    # =============================================================================

    HTTP_REQUESTS = Counter(
        "http_requests_total",
        "Total HTTP requests",
        ["endpoint", "method", "status_code"],
    )

    HTTP_LATENCY = Histogram(
        "http_request_duration_seconds",
        "HTTP request latency in seconds",
        ["endpoint", "method"],
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    )

    # =============================================================================
    # LLM Request Metrics
    # =============================================================================

    LLM_REQUESTS = Counter(
        "llm_requests_total",
        "Total LLM API requests",
        ["provider", "model", "status"],
    )

    LLM_LATENCY = Histogram(
        "llm_request_duration_seconds",
        "LLM request latency in seconds",
        ["provider", "model"],
        buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
    )

    # =============================================================================
    # ML Model Load Metrics
    # =============================================================================

    ML_MODEL_LOAD_TIME = Histogram(
        "faultmaven_ml_model_load_seconds",
        "Time to load ML model",
        ["model"],
        buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    )

    ML_MODEL_LOADED = Gauge(
        "faultmaven_ml_model_loaded",
        "Whether ML model is loaded (1=loaded, 0=not loaded)",
        ["model"],
    )

else:
    # Create no-op metrics when prometheus_client is not available
    class NoOpMetric:
        """No-op metric for when prometheus_client is not installed."""
        def labels(self, **kwargs):
            return self
        def inc(self, amount=1):
            pass
        def dec(self, amount=1):
            pass
        def set(self, value):
            pass
        def observe(self, value):
            pass
        def time(self):
            import contextlib
            return contextlib.nullcontext()

    BUILD_INFO = NoOpMetric()
    HTTP_REQUESTS = NoOpMetric()
    HTTP_LATENCY = NoOpMetric()
    LLM_REQUESTS = NoOpMetric()
    LLM_LATENCY = NoOpMetric()
    ML_MODEL_LOAD_TIME = NoOpMetric()
    ML_MODEL_LOADED = NoOpMetric()


def get_metrics_registry():
    """Get the Prometheus registry for metrics exposition."""
    if PROMETHEUS_AVAILABLE:
        return REGISTRY
    return None


def generate_metrics_output() -> bytes:
    """Generate Prometheus-format metrics output."""
    if not is_metrics_enabled():
        return b""
    return generate_latest(REGISTRY)


def get_content_type() -> str:
    """Get the content type for Prometheus metrics."""
    return CONTENT_TYPE_LATEST


# =============================================================================
# Helper Functions for Recording Metrics
# =============================================================================

def record_http_request(endpoint: str, method: str, status_code: int, duration_seconds: float):
    """Record an HTTP request metric.

    Args:
        endpoint: Route template (e.g., /cases/{id}), NOT raw URL
        method: HTTP method (GET, POST, etc.)
        status_code: HTTP response status code
        duration_seconds: Request duration in seconds
    """
    if not is_metrics_enabled():
        return

    HTTP_REQUESTS.labels(
        endpoint=endpoint,
        method=method,
        status_code=str(status_code),
    ).inc()

    HTTP_LATENCY.labels(
        endpoint=endpoint,
        method=method,
    ).observe(duration_seconds)


def record_llm_request(provider: str, model: str, status: str, duration_seconds: float):
    """Record an LLM request metric.

    Args:
        provider: LLM provider name (openai, anthropic, etc.)
        model: Model name (gpt-4o, claude-3, etc.)
        status: Request status (success, error, timeout)
        duration_seconds: Request duration in seconds
    """
    if not is_metrics_enabled():
        return

    LLM_REQUESTS.labels(
        provider=provider,
        model=model,
        status=status,
    ).inc()

    LLM_LATENCY.labels(
        provider=provider,
        model=model,
    ).observe(duration_seconds)


def record_ml_model_load(model: str, duration_seconds: float, loaded: bool = True):
    """Record ML model load metric.

    Args:
        model: Model name (e.g., BAAI/bge-m3)
        duration_seconds: Time to load model
        loaded: Whether model is now loaded
    """
    if not is_metrics_enabled():
        return

    ML_MODEL_LOAD_TIME.labels(model=model).observe(duration_seconds)
    ML_MODEL_LOADED.labels(model=model).set(1 if loaded else 0)


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Status checks
    "PROMETHEUS_AVAILABLE",
    "is_metrics_enabled",
    "get_metrics_status",
    # Metrics
    "BUILD_INFO",
    "HTTP_REQUESTS",
    "HTTP_LATENCY",
    "LLM_REQUESTS",
    "LLM_LATENCY",
    "ML_MODEL_LOAD_TIME",
    "ML_MODEL_LOADED",
    # Helpers
    "record_http_request",
    "record_llm_request",
    "record_ml_model_load",
    # Exposition
    "get_metrics_registry",
    "generate_metrics_output",
    "get_content_type",
]
