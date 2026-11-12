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

import itertools
import logging
import os
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

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


# A labelled counter creates a series on its first ``.labels()`` call, so a
# series born by an increment is born AT 1 and ``increase()``/``rate()`` never
# see that first event (#1787). Every labelled counter therefore declares its
# children: ``label_values`` (closed value sets, every combination created at 0)
# or ``lazy_labels`` (why a label is open-ended and how the series is read).

#: Most children one ``label_values`` declaration may create. A counter whose
#: combinations would exceed it declares ``lazy_labels`` instead.
MAX_DECLARED_CHILDREN = 100

#: Each bounded declaration made on a real counter: (exposed metric name, label
#: names, combinations created). Read by the exposition test.
DECLARED_CHILDREN: List[Tuple[str, Tuple[str, ...], Tuple[Tuple[str, ...], ...]]] = []

#: Each lazy declaration made on a real counter: (exposed metric name, reason).
#: Read by the exposition test.
LAZY_DECLARED: List[Tuple[str, str]] = []


def _combinations(
    name: str,
    labelnames: Iterable[str],
    label_values: Mapping[str, Iterable[str]],
) -> Tuple[Tuple[str, ...], ...]:
    """Every child *label_values* declares, in *labelnames* order; ValueError
    naming *name* when the declaration does not cover exactly those labels with
    non-empty value sets, or would create more than ``MAX_DECLARED_CHILDREN``.

    Each value set is read once, so a generator works."""
    names = tuple(labelnames)
    if set(label_values) != set(names):
        raise ValueError(
            f"Counter {name!r}: label_values keys {sorted(label_values)} are not "
            f"its labelnames {sorted(names)} (#1787)"
        )
    value_sets = []
    for label in names:
        raw = label_values[label]
        values = () if isinstance(raw, str) else tuple(raw)
        if not values:
            raise ValueError(
                f"Counter {name!r}: label_values[{label!r}] is empty or a bare "
                "string, not a set of values (#1787)"
            )
        value_sets.append(values)
    combos = tuple(itertools.product(*value_sets))
    if len(combos) > MAX_DECLARED_CHILDREN:
        raise ValueError(
            f"Counter {name!r}: label_values would create {len(combos)} children, "
            f"more than {MAX_DECLARED_CHILDREN}; declare lazy_labels instead (#1787)"
        )
    return combos


def _check_reason(name: str, reason: Optional[str]) -> None:
    if reason is None or not reason.strip():
        raise ValueError(f"Counter {name!r}: lazy_labels is an empty reason (#1787)")


def _check_declaration(
    name: str,
    labelnames: List[str],
    label_values: Optional[Mapping[str, Iterable[str]]],
    lazy_labels: Optional[str],
) -> Tuple[Tuple[str, ...], ...]:
    """Refuse a counter whose children are not declared (#1787); return the
    children a ``label_values`` declaration creates (none otherwise)."""
    if not labelnames:
        if label_values is not None or lazy_labels is not None:
            raise ValueError(
                f"Counter {name!r} has no labels, so it declares neither "
                "label_values nor lazy_labels (#1787)"
            )
        return ()
    if (label_values is None) == (lazy_labels is None):
        raise ValueError(
            f"Counter {name!r} is labelled, so it declares exactly one of "
            "label_values (closed value sets, created at 0) or lazy_labels "
            "(why a label is open-ended) (#1787)"
        )
    if label_values is None:
        _check_reason(name, lazy_labels)
        return ()
    return _combinations(name, labelnames, label_values)


def _exposed_name(name: str, namespace: str, subsystem: str, unit: str) -> str:
    """The family name ``prometheus_client`` exposes a counter under."""
    full = "_".join(part for part in (namespace, subsystem, name) if part)
    if full.endswith("_total"):
        full = full[: -len("_total")]
    if unit and not full.endswith("_" + unit):
        full += "_" + unit
    return full


def _create(metric: Any, exposed: str, labelnames, combos) -> None:
    for combo in combos:
        metric.labels(*combo)
    DECLARED_CHILDREN.append((exposed, tuple(labelnames), combos))


def create_children(metric: Any, label_values: Mapping[str, Iterable[str]]) -> None:
    """Create every child *label_values* declares on *metric*, at 0, and record it.

    For a module that builds its counters straight from ``prometheus_client``,
    called after the definition (the shim's ``Counter`` takes ``label_values``).
    A no-op on a ``NoOpMetric``, without ``prometheus_client`` or with metrics
    disabled; a declaration that does not fit the counter raises ValueError
    whenever the counter is real.
    """
    if isinstance(metric, NoOpMetric) or not PROMETHEUS_AVAILABLE:
        return
    combos = _combinations(metric._name, metric._labelnames, label_values)
    if _is_metrics_enabled():
        _create(metric, metric._name, metric._labelnames, combos)


def declare_lazy(metric: Any, reason: str) -> None:
    """Record why *metric*'s labels are open-ended, for a module that builds its
    counters straight from ``prometheus_client``. ValueError on an empty
    *reason*; otherwise a no-op on a ``NoOpMetric``, without ``prometheus_client``
    or with metrics disabled."""
    _check_reason(getattr(metric, "_name", "?"), reason)
    if isinstance(metric, NoOpMetric) or not PROMETHEUS_AVAILABLE:
        return
    if _is_metrics_enabled():
        LAZY_DECLARED.append((metric._name, reason))


def Counter(
    name: str,
    documentation: str,
    labelnames: Optional[List[str]] = None,
    namespace: str = "",
    subsystem: str = "",
    unit: str = "",
    registry: Any = None,
    *,
    label_values: Optional[Mapping[str, Iterable[str]]] = None,
    lazy_labels: Optional[str] = None,
) -> Any:
    """
    Create a Counter metric with graceful degradation.

    If Prometheus is available and ENABLE_METRICS=true, creates a real Counter.
    Otherwise, returns a no-op metric that does nothing.

    A labelled counter declares exactly one of ``label_values`` or
    ``lazy_labels``, or this raises ValueError whether metrics are on or off
    (#1787).

    Args:
        name: Metric name
        documentation: Metric description
        labelnames: Optional list of label names
        namespace: Optional metric namespace
        subsystem: Optional metric subsystem
        unit: Optional metric unit
        registry: Optional Prometheus registry
        label_values: Each label name's closed set of values, read once;
            every combination is created at 0
        lazy_labels: Which label is open-ended and how its series is read

    Returns:
        Prometheus Counter or NoOpMetric

    Example:
        request_counter = Counter(
            'http_requests_total',
            'Total HTTP requests',
            labelnames=['method', 'endpoint'],
            lazy_labels='endpoint is the request path',
        )
        request_counter.labels(method='GET', endpoint='/api/cases').inc()
    """
    children = _check_declaration(name, labelnames or [], label_values, lazy_labels)
    metrics_enabled = _is_metrics_enabled()

    if PROMETHEUS_AVAILABLE and metrics_enabled:
        logger.debug(f"Creating Prometheus Counter: {name}")
        counter = PrometheusCounter(
            name=name,
            documentation=documentation,
            labelnames=labelnames or [],
            namespace=namespace,
            subsystem=subsystem,
            unit=unit,
            # registry=None must fall through to prometheus_client's default
            # REGISTRY — passing None explicitly leaves the metric unregistered
            # and therefore invisible to /metrics.
            **({"registry": registry} if registry is not None else {}),
        )
        exposed = _exposed_name(name, namespace, subsystem, unit)
        if label_values is not None:
            _create(counter, exposed, labelnames, children)
        elif lazy_labels is not None:
            LAZY_DECLARED.append((exposed, lazy_labels))
        return counter
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
        }
        # registry=None must fall through to prometheus_client's default
        # REGISTRY — an explicit None leaves the metric unregistered.
        if registry is not None:
            kwargs["registry"] = registry
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
            # registry=None must fall through to prometheus_client's default
            # REGISTRY — passing None explicitly leaves the metric unregistered
            # and therefore invisible to /metrics.
            **({"registry": registry} if registry is not None else {}),
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
            # registry=None must fall through to prometheus_client's default
            # REGISTRY — passing None explicitly leaves the metric unregistered
            # and therefore invisible to /metrics.
            **({"registry": registry} if registry is not None else {}),
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
            # registry=None must fall through to prometheus_client's default
            # REGISTRY — passing None explicitly leaves the metric unregistered
            # and therefore invisible to /metrics.
            **({"registry": registry} if registry is not None else {}),
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
    lazy_labels="endpoint is the request path, an open set; the series is "
    "per-request traffic, frequent enough that the first event is noise",
)

# Buckets spanning what this API actually serves, which prometheus_client's
# defaults do not (#1346). Those stop at a highest finite bucket of 10.0s, and
# once a quantile lands in the overflow bucket `histogram_quantile` reports the
# largest FINITE bound rather than the observation — `promql/quantile.go`
# returns `buckets[len(buckets)-2].UpperBound` for the last bucket — so every
# such request is reported as exactly 10.0s however long it really took. On the
# product's main path the instrument was blind exactly where the question is.
# Measured end to end on gemini-3.7-flash
# against a real case: healthy investigation turns 5.9s, 9.7s, 10.3s, 15.0s
# (latency rises with accumulated context), a turn against a hung provider
# 68.7s, /health 27-34ms, /readiness 16-20ms. Two of the four healthy turns
# and the pathological one all landed in the same +Inf bucket, so a 10.3s
# healthy turn and a 68.7s hung one were the same observation to every
# percentile computed from this histogram — both reported as 10.0s, the
# largest finite bound. Including `faultmaven:slo_api_latency_p95:5m`, which
# `FaultMavenAPIHighLatency` alerts on.
#
# The first fourteen are prometheus_client's defaults unchanged, so every
# existing recording rule and dashboard keeps the resolution it had below 10s
# (probes and ordinary CRUD live there). The extension is additive:
#   15.0  - just above the slowest healthy turn measured (15.0s)
#   20.0, 30.0 - resolution over the early-regression range above healthy
#   60.0  - below the hung-provider turn (68.7s), so it is separated from a
#           slow-but-healthy one rather than sharing its bucket
#   120.0 - AGENT_REQUEST_TIMEOUT's default: past this a request has outlived
#           its own declared budget, which is a different fault from latency
#   300.0, 600.0 - up to MAX_AGENT_TIMEOUT_SECONDS, the product's maximum
#           agent timeout; a local-model call under
#           LLM_PROVIDER_TIMEOUT_OVERRIDES='{"ollama": 600}' lands here
# One ladder for both request histograms (#1529). A routed LLM call's ceiling
# is max(LLM_REQUEST_TIMEOUT, LLM_PROVIDER_TIMEOUT_OVERRIDES[provider]) clamped
# to the turn's remainder (router._resolve_timeout); the request's is
# AGENT_REQUEST_TIMEOUT (default 120, max 600). `llm_request_duration_seconds`
# records routed calls only (a dedicated DA_PROVIDER sends directly and is not
# in the series). On prometheus_client's defaults a slow-but-succeeding
# provider was invisible: every call above 10s was the same +Inf observation.
_REQUEST_DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    7.5,
    10.0,
    15.0,
    20.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    float("inf"),
)

request_duration = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    labelnames=["method", "endpoint"],
    buckets=_REQUEST_DURATION_BUCKETS,
)

active_sessions = Gauge(
    "active_sessions_total", "Number of active sessions", labelnames=["status"]
)

case_operations = Counter(
    "case_operations_total",
    "Total case operations",
    labelnames=["operation", "status"],
    lazy_labels="no call site writes it, so no value vocabulary exists to declare",
)

knowledge_queries = Counter(
    "knowledge_queries_total",
    "Total knowledge base queries",
    labelnames=["query_type", "status"],
    lazy_labels="no call site writes it, so no value vocabulary exists to declare",
)

# The LLM counters below carry ``model``: the configured model id, an open set.
# They count every routed call, so a lost first event is noise against a rate
# read per provider and model (#1787).
_LAZY_MODEL = "model is the configured model id, an open set; per-call series"

llm_requests = Counter(
    "llm_requests_total",
    "Total LLM requests",
    labelnames=["provider", "model", "status"],
    lazy_labels=_LAZY_MODEL,
)

llm_latency = Histogram(
    "llm_request_duration_seconds",
    "LLM request duration in seconds",
    labelnames=["provider", "model"],
    buckets=_REQUEST_DURATION_BUCKETS,
)

llm_tokens = Counter(
    "llm_tokens_total",
    "Total LLM tokens consumed (provider-reported total per request)",
    labelnames=["provider", "model"],
    lazy_labels=_LAZY_MODEL,
)

# Why each response stopped, normalised (faultmaven...providers.StopReason).
#
# Labelled by reason rather than counting truncations alone so the UNKNOWN
# share stays legible: UNKNOWN means the provider told us nothing, not that the
# response completed, and a provider sitting at 100% UNKNOWN is a blind spot to
# fix rather than a clean bill of health. `max_tokens` is the alert-worthy one —
# every one of those is a response that was cut and may have been consumed as
# if it were whole (#1094).
llm_stop_reasons = Counter(
    "llm_stop_reasons_total",
    "LLM responses by normalised stop reason "
    "(stop|max_tokens|content_filter|tool_calls|unknown)",
    labelnames=["provider", "model", "stop_reason"],
    lazy_labels=_LAZY_MODEL,
)

# --- Fine-grained LLM spend metrics ---------------------------------------
# These are emitted at the registry chokepoint, once per REAL provider API call
# — including fallback / low-confidence attempts that the caller discards. That
# is deliberately a different basis from llm_tokens_total above (winner per
# route): sum(llm_call_tokens_total) >= llm_tokens_total whenever the fallback
# chain fans out, and the gap IS the fallback waste you want to see.
llm_call_tokens = Counter(
    "llm_call_tokens_total",
    "LLM tokens per provider API call, split by token type. Counts every "
    "billed call including discarded fallback attempts.",
    labelnames=[
        "provider",
        "model",
        "token_type",
    ],  # input|output|cache_read|cache_write
    lazy_labels=_LAZY_MODEL,
)

llm_cost_usd = Counter(
    "llm_cost_usd_total",
    "Estimated LLM spend in USD per provider API call (from the operator-"
    "maintained price table). Excludes unpriced models — see "
    "llm_unpriced_calls_total.",
    labelnames=["provider", "model"],
    lazy_labels=_LAZY_MODEL,
)

llm_provider_calls = Counter(
    "llm_provider_calls_total",
    "Provider API calls at the routing chokepoint, by disposition. "
    "outcome=kept: returned to caller; low_confidence: billed then discarded "
    "by the fallback chain (pure waste).",
    labelnames=["provider", "model", "outcome"],  # kept|low_confidence
    lazy_labels=_LAZY_MODEL,
)

llm_unpriced_calls = Counter(
    "llm_unpriced_calls_total",
    "Provider API calls whose (provider, model) had no price entry, so their "
    "spend is NOT included in llm_cost_usd_total. Non-zero => cost is under-"
    "reported; add the model to the price table or LLM_PRICING_OVERRIDES.",
    labelnames=["provider", "model"],
    lazy_labels="model is the configured model id, an open set; read as any "
    "non-zero sample, which a series born at 1 already shows",
)

# Model calls REFUSED at the model boundary (#1998): content the redaction
# module did not mint, or a keyword outside the closed set, once per distinct
# (argument, kind) per refusal. ``target`` is ``router`` (the seam and the
# router's entry) or the provider's LABEL (the dispatch). Nothing refused is
# sent; non-zero in production is a send site that does not mint what it sends.
llm_model_content_violations = Counter(
    "llm_model_content_violations_total",
    "Model calls refused before any network I/O because their model-bound "
    "content was not minted by the redaction module, by target "
    "(router|<provider label>), argument (prompt|messages.content|...|kwargs) "
    "and kind (raw|forged|not_text|bad_shape|bad_id|bad_role|unknown_key|"
    "not_model_output|unknown_kwarg).",
    labelnames=["target", "argument", "kind"],
    lazy_labels="target x argument x kind is over 100 combinations; read as any "
    "non-zero sample (zero is healthy), which a series born at 1 already shows",
)

# The gap between llm_provider_calls_total and the calls the LLM usage ledger
# persisted (#640). The ledger's writes fail OPEN — a failed write never fails a
# call — so this counter is what keeps the gap observable instead of silent.
#: ``usage_ledger.REASON_*``, which imports this module.
_UNPERSISTED_REASONS = (
    "store_error",
    "no_tenant",
    "attribution_error",
    "no_loop",
    "not_composed",
)

llm_usage_unpersisted_calls = Counter(
    "llm_usage_unpersisted_calls_total",
    "Billed LLM calls the usage ledger did not persist, by reason. "
    "store_error: the write failed or was cancelled; no_tenant: no usable "
    "enterprise under multi-tenancy; attribution_error: capturing who pays "
    "raised; no_loop: metered with no running event loop; not_composed: no "
    "ledger installed (the composition root did not run).",
    labelnames=["reason"],
    label_values={"reason": _UNPERSISTED_REASONS},
)

# Turns that ran with no per-case in-flight lease (#2096): a delete of their
# case cannot see them (``infrastructure/concurrency/inflight.py``).
case_unleased_turns = Counter(
    "case_unleased_turns_total",
    "Turns that ran without a per-case in-flight lease, so a case delete could "
    "not see them, by reason. no_store: no Redis client; store_error: the lease "
    "or tombstone command failed.",
    labelnames=["reason"],
    label_values={"reason": ("no_store", "store_error")},
)

# SLA gauges — exported from the SLA tracker at /metrics scrape time so the
# values behind /health/sla become alertable. Status mapping: 3=meeting,
# 2=at_risk, 1=breached, 0=unknown (alert on `sla_status < 3`).
sla_status = Gauge(
    "sla_status",
    "SLA compliance status per component (3=meeting, 2=at_risk, 1=breached, 0=unknown)",
    labelnames=["component"],
)

sla_availability_ratio = Gauge(
    "sla_availability_ratio",
    "Observed availability per component over the SLA window (0-1)",
    labelnames=["component"],
)

sla_response_time_p95_seconds = Gauge(
    "sla_response_time_p95_seconds",
    "Observed p95 response time per component over the SLA window",
    labelnames=["component"],
)

sla_error_rate_ratio = Gauge(
    "sla_error_rate_ratio",
    "Observed error rate per component over the SLA window (0-1)",
    labelnames=["component"],
)

sla_active_breaches = Gauge(
    "sla_active_breaches",
    "Number of currently active SLA breaches per component",
    labelnames=["component"],
)

# Component health — the only export of what `/health` grades, and the reason
# it exists (#1547). `/health` answers 200 by design (it is the liveness
# surface; see the route's docstring), so all three Kubernetes probes ignore a
# dependency outage and nothing reaches a human. This gauge is what an alert
# rule reads instead.
#
# Both classification flags are LABELS so the alerting rule can select the set
# rather than name components by hand — the fatal set is data, and a rule that
# hardcodes `database` goes stale the moment the set changes:
#
#     component_health_status{fatal="true"} == 1
#         a component the process cannot usefully serve without is down
#     component_health_status{fatal="true", fails_per_replica="false"} == 1
#         ... and no Kubernetes probe can fix it by pulling a pod, because it
#         is down for every replica at once. This is the page-a-human set.
#
# Published from `ComponentHealthMonitor.get_overall_health_status`, in the
# same loop that grades the `/health` body, so the two cannot disagree.
# Emitted for HEALTHY components too: absent series and healthy series must
# not look alike, or "not scraped" reads as "fine".
component_health_status = Gauge(
    "component_health_status",
    "Component health as /health grades it "
    "(3=healthy, 2=degraded, 1=unhealthy, 0=unknown). `fatal` is whether the "
    "process can usefully serve without the component; `fails_per_replica` is "
    "whether it can fail for one pod while its siblings keep serving.",
    labelnames=["component", "fatal", "fails_per_replica"],
)
