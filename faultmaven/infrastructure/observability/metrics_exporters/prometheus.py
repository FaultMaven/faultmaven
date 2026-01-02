"""Prometheus HTTP metrics exporter.

Provides a /metrics endpoint that returns metrics in Prometheus text format.
Only mounted when METRICS_EXPORTER=prometheus_http.

Key design principles:
- Bounded labels: Use route templates or endpoint categories, never IDs
- Forbidden labels: case_id, session_id, user_id, organization_id are prohibited
- Graceful degradation: Returns empty metrics if prometheus_client not installed

Usage:
    from faultmaven.infrastructure.observability.metrics_exporters.prometheus import (
        create_prometheus_metrics_endpoint,
    )

    # In FastAPI app setup:
    if settings.providers.metrics_exporter == MetricsExporter.PROMETHEUS_HTTP:
        app.include_router(create_prometheus_metrics_endpoint())
"""

import logging
from typing import Optional, Set

from fastapi import APIRouter, Response

logger = logging.getLogger(__name__)


# Labels that are FORBIDDEN in metrics to ensure bounded cardinality
# These would create unbounded label values and break Prometheus
FORBIDDEN_LABELS: Set[str] = {
    "case_id",
    "session_id",
    "user_id",
    "organization_id",
    "org_id",
    "request_id",
    "trace_id",
    "span_id",
    "client_ip",
    "user_agent",
}


# Allowed bounded labels for reference
ALLOWED_LABELS: Set[str] = {
    "method",           # HTTP method (GET, POST, etc.)
    "endpoint",         # Endpoint category (cases, knowledge, etc.)
    "status_code",      # HTTP status code (200, 404, etc.)
    "status_class",     # Status class (2xx, 3xx, 4xx, 5xx)
    "operation",        # Operation type (create, read, update, delete)
    "status",           # Operation status (success, failure)
    "provider",         # LLM provider name
    "model",            # LLM model name (bounded set)
    "query_type",       # Query type (search, lookup, etc.)
    "service",          # Service name
    "component",        # Component name
}


def get_prometheus_response() -> Response:
    """Generate Prometheus metrics response.

    Returns:
        Response with Prometheus text format metrics, or empty metrics
        if prometheus_client is not installed.
    """
    try:
        from prometheus_client import (
            CONTENT_TYPE_LATEST,
            generate_latest,
            REGISTRY,
        )

        # Generate metrics in Prometheus text exposition format
        metrics_output = generate_latest(REGISTRY)

        return Response(
            content=metrics_output,
            media_type=CONTENT_TYPE_LATEST,
        )

    except ImportError:
        logger.warning(
            "prometheus_client not installed. "
            "Install with: pip install prometheus-client"
        )
        # Return empty metrics (valid Prometheus format)
        return Response(
            content=b"# prometheus_client not installed\n",
            media_type="text/plain; charset=utf-8",
        )


def create_prometheus_metrics_endpoint(
    path: str = "/metrics",
    tags: Optional[list] = None,
) -> APIRouter:
    """Create a FastAPI router with Prometheus /metrics endpoint.

    Args:
        path: URL path for metrics endpoint (default: /metrics)
        tags: Optional OpenAPI tags for the endpoint

    Returns:
        FastAPI APIRouter with /metrics GET endpoint
    """
    router = APIRouter(tags=tags or ["metrics"])

    @router.get(path, include_in_schema=True)
    async def metrics() -> Response:
        """Prometheus metrics endpoint.

        Returns metrics in Prometheus text exposition format.
        Only available when METRICS_EXPORTER=prometheus_http.

        Label cardinality is bounded:
        - Uses route templates (e.g., /api/v1/cases) not actual IDs
        - No user_id, session_id, case_id, or other unbounded values
        """
        return get_prometheus_response()

    logger.info(f"Prometheus metrics endpoint created at {path}")
    return router


def validate_labels(labels: dict) -> bool:
    """Validate that labels don't contain forbidden high-cardinality values.

    Args:
        labels: Dictionary of label name -> value

    Returns:
        True if labels are valid, False if forbidden labels are present

    Raises:
        ValueError: If forbidden labels are detected
    """
    forbidden_found = set(labels.keys()) & FORBIDDEN_LABELS

    if forbidden_found:
        raise ValueError(
            f"Forbidden high-cardinality labels detected: {forbidden_found}. "
            f"These labels would create unbounded cardinality in Prometheus. "
            f"Use bounded labels like: {ALLOWED_LABELS}"
        )

    return True
