"""Infrastructure shims for graceful degradation.

This package provides shim layers for optional enterprise dependencies:
- Observability (Opik): Distributed tracing
- Security (Presidio): PII redaction
- Metrics (Prometheus): Metrics collection

Shims enable FaultMaven to run in both:
- Community mode: Zero external dependencies
- Enterprise mode: Full observability, security, and metrics features

Environment Variables:
    ENABLE_TRACING: Set to 'true' to enable Opik distributed tracing
    ENABLE_PII_REDACTION: Set to 'true' to enable Presidio PII redaction
    ENABLE_METRICS: Set to 'true' to enable Prometheus metrics collection

Usage:
    from faultmaven.infrastructure.shims import track, PIIRedactor
    from faultmaven.infrastructure.shims import Counter, Histogram, Gauge

    # Tracing (no-op if Opik not installed or disabled)
    @track("my_operation")
    async def my_function():
        pass

    # PII redaction (pass-through if Presidio not installed or disabled)
    redactor = PIIRedactor()
    safe_text = redactor.redact(user_input)

    # Metrics (no-op if Prometheus not installed or disabled)
    my_counter = Counter('my_metric', 'My metric description')
    my_counter.inc()

    # Check status of shims
    from faultmaven.infrastructure.shims import (
        get_tracing_status,
        get_pii_redaction_status,
        get_metrics_status,
    )

    tracing = get_tracing_status()
    print(f"Tracing active: {tracing['active']}")

    pii = get_pii_redaction_status()
    print(f"PII redaction available: {pii['would_be_active']}")

    metrics = get_metrics_status()
    print(f"Metrics active: {metrics['active']}")
"""

from .metrics import (  # Pre-defined common metrics
    PROMETHEUS_AVAILABLE,
    Counter,
    Gauge,
    Histogram,
    Info,
    Summary,
    active_sessions,
    case_operations,
    get_metrics_status,
    is_metrics_active,
    knowledge_queries,
    llm_call_tokens,
    llm_cost_usd,
    llm_latency,
    llm_provider_calls,
    llm_requests,
    llm_tokens,
    llm_unpriced_calls,
    request_counter,
    request_duration,
    sla_active_breaches,
    sla_availability_ratio,
    sla_error_rate_ratio,
    sla_response_time_p95_seconds,
    sla_status,
)
from .observability import OPIK_AVAILABLE, get_tracing_status, is_tracing_active, track
from .security import PRESIDIO_AVAILABLE, PIIRedactor, get_pii_redaction_status

__all__ = [
    # Observability exports
    "track",
    "get_tracing_status",
    "is_tracing_active",
    "OPIK_AVAILABLE",
    # Security exports
    "PIIRedactor",
    "get_pii_redaction_status",
    "PRESIDIO_AVAILABLE",
    # Metrics exports
    "Counter",
    "Histogram",
    "Gauge",
    "Summary",
    "Info",
    "get_metrics_status",
    "is_metrics_active",
    "PROMETHEUS_AVAILABLE",
    # Pre-defined metrics
    "request_counter",
    "request_duration",
    "active_sessions",
    "case_operations",
    "knowledge_queries",
    "llm_requests",
    "llm_latency",
    "llm_tokens",
    "llm_call_tokens",
    "llm_cost_usd",
    "llm_provider_calls",
    "llm_unpriced_calls",
    "sla_status",
    "sla_availability_ratio",
    "sla_response_time_p95_seconds",
    "sla_error_rate_ratio",
    "sla_active_breaches",
]
