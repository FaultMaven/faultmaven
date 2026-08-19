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

Import cost:
    These names are resolved lazily (PEP 562). Re-exporting all three shims
    eagerly meant that importing ANY one of them paid for ALL of them: the
    optional cloud dependencies behind them are heavy, and Presidio's nlp
    engine imports torch at module scope purely to detect a device. Measured
    before this was made lazy — each figure is the cost of importing that one
    submodule, all of it the eager package ``__init__``:

        shims.metrics        17.85s   (torch resident: True)
        shims.observability  13.60s   (torch resident: True)
        shims.security       13.40s   (torch resident: True)

    Because `core.investigation` reaches `shims.metrics` via
    `lifecycle_metrics`, that cost landed on the case persistence layer. See
    #849. Nothing about the shims' own degradation behaviour changed: each
    submodule still does its own feature detection at ITS import time, so
    ``PRESIDIO_AVAILABLE`` and friends mean exactly what they did before.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # static analysis only — never executed at runtime
    from .metrics import (
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
        llm_stop_reasons,
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
    from .observability import (
        OPIK_AVAILABLE,
        get_tracing_status,
        is_tracing_active,
        track,
    )
    from .security import (
        PRESIDIO_AVAILABLE,
        PIIRedactor,
        get_pii_redaction_status,
    )

# Which submodule owns each re-exported name. Kept exhaustive against
# ``__all__`` by tests/unit/test_import_isolation.py, which fails if a name is
# added to one and not the other.
_EXPORTS_BY_SUBMODULE = {
    "metrics": (
        "PROMETHEUS_AVAILABLE",
        "Counter",
        "Gauge",
        "Histogram",
        "Info",
        "Summary",
        "active_sessions",
        "case_operations",
        "get_metrics_status",
        "is_metrics_active",
        "knowledge_queries",
        "llm_call_tokens",
        "llm_cost_usd",
        "llm_latency",
        "llm_provider_calls",
        "llm_requests",
        "llm_stop_reasons",
        "llm_tokens",
        "llm_unpriced_calls",
        "request_counter",
        "request_duration",
        "sla_active_breaches",
        "sla_availability_ratio",
        "sla_error_rate_ratio",
        "sla_response_time_p95_seconds",
        "sla_status",
    ),
    "observability": (
        "OPIK_AVAILABLE",
        "get_tracing_status",
        "is_tracing_active",
        "track",
    ),
    "security": (
        "PRESIDIO_AVAILABLE",
        "PIIRedactor",
        "get_pii_redaction_status",
    ),
}

_SUBMODULE_BY_EXPORT = {
    name: submodule
    for submodule, names in _EXPORTS_BY_SUBMODULE.items()
    for name in names
}


def __getattr__(name: str):
    """Resolve a re-exported shim name, or a submodule, on first access."""
    from importlib import import_module

    submodule = _SUBMODULE_BY_EXPORT.get(name)
    if submodule is not None:
        value = getattr(import_module(f".{submodule}", __name__), name)
        # Cache on the package so repeat access skips this path entirely.
        globals()[name] = value
        return value

    # Submodules. Importing a submodule binds it on its parent package, so the
    # old eager re-exports made `shims.metrics`, `.observability` and
    # `.security` reachable as plain attributes as a side effect. Resolving any
    # real submodule keeps that working — and only imports the one asked for,
    # so the isolation this laziness buys is preserved.
    try:
        module = import_module(f".{name}", __name__)
    except ModuleNotFoundError as exc:
        # Only translate "this package has no such submodule". An import error
        # raised from *inside* a real submodule must propagate, not be reported
        # as a missing attribute.
        if exc.name == f"{__name__}.{name}":
            raise AttributeError(
                f"module {__name__!r} has no attribute {name!r}"
            ) from None
        raise
    globals()[name] = module
    return module


def __dir__() -> list:
    from pkgutil import iter_modules

    return sorted(set(__all__) | {m.name for m in iter_modules(__path__)})


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
    "llm_stop_reasons",
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
