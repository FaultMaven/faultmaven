"""Metrics exporters for observability providers.

This package provides deployment-neutral metrics exporters:
- prometheus: Prometheus HTTP exporter (/metrics endpoint)
- (future) otel: OpenTelemetry exporter

Usage is controlled by METRICS_EXPORTER setting:
- none: No metrics endpoint (default)
- prometheus_http: Mount /metrics with Prometheus text format
"""

from faultmaven.infrastructure.observability.metrics_exporters.prometheus import (
    create_prometheus_metrics_endpoint,
    get_prometheus_response,
    FORBIDDEN_LABELS,
)

__all__ = [
    "create_prometheus_metrics_endpoint",
    "get_prometheus_response",
    "FORBIDDEN_LABELS",
]
