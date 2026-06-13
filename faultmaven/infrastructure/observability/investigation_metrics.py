"""Investigation throughput metrics — the case funnel as Prometheus series.

These measure the FLOW of investigations (the "throughput" view):

- ``faultmaven_case_transitions_total`` — case state transitions, i.e. the
  funnel inquiry → investigating → resolved/closed. The rate is throughput;
  the ``to_state`` breakdown is the resolved-vs-closed-unresolved mix.
- ``faultmaven_case_resolution_seconds`` — wall-clock from case creation to a
  terminal state; p50/p95 are the investigation throughput SLA.
- ``faultmaven_investigation_turns_total`` — conversation turns processed, by
  the case state at the time of the turn (activity volume + where effort goes).

Before this, the case funnel was dark: ``case_operations_total`` in the metrics
shim was defined but never recorded. This is the "most basic, high-confidence"
v1 — instrumented at the canonical transition chokepoints. Deeper diagnostic
signals (hypothesis outcomes, stage durations, cause-state progression) are a
deliberate follow-up.

Counters/histograms are no-ops unless ``ENABLE_METRICS=true`` and
``prometheus_client`` is installed — see ``shims/metrics.py`` for the
graceful-degradation policy.
"""

from faultmaven.infrastructure.shims.metrics import Counter, Histogram

case_transitions_total = Counter(
    "faultmaven_case_transitions_total",
    "Case state transitions (the investigation funnel). from_state→to_state "
    "spans inquiry→investigating→resolved/closed.",
    labelnames=["from_state", "to_state"],
)

# Investigations span minutes to days — buckets 1m,5m,15m,30m,1h,4h,12h,1d,3d,7d.
case_resolution_seconds = Histogram(
    "faultmaven_case_resolution_seconds",
    "Wall-clock seconds from case creation to a terminal state "
    "(resolved/closed). p50/p95 are the investigation throughput SLA.",
    labelnames=["to_state"],
    buckets=[60, 300, 900, 1800, 3600, 14400, 43200, 86400, 259200, 604800],
)

investigation_turns_total = Counter(
    "faultmaven_investigation_turns_total",
    "Investigation conversation turns processed, labelled by the case state at "
    "the time of the turn (inquiry / investigating / resolved / closed).",
    labelnames=["case_state"],
)


def record_transition(from_state: str, to_state: str) -> None:
    """Record one case state transition. Safe to call from any path."""
    case_transitions_total.labels(from_state=from_state, to_state=to_state).inc()


def record_resolution_seconds(to_state: str, seconds: float) -> None:
    """Record time-to-terminal for a resolved/closed case (clamped at >=0)."""
    case_resolution_seconds.labels(to_state=to_state).observe(max(seconds, 0.0))
