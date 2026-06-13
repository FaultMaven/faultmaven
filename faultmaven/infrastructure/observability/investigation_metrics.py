"""Investigation throughput metrics — the case funnel as Prometheus series.

These measure the FLOW of investigations (the "throughput" view):

- ``faultmaven_case_transitions_total`` — case state transitions, i.e. the
  funnel inquiry → investigating → resolved/closed. The rate is throughput;
  the ``to_state`` breakdown is the resolved-vs-closed-unresolved mix.
- ``faultmaven_case_resolution_turns`` — conversation turns from case creation
  to a terminal state; p50/p95 are the investigation-effort SLA. Turns, not
  wall-clock: wall-clock is dominated by human think/idle time (a case left
  open overnight reads as "hours" for a two-turn investigation), so it
  measures user availability, not the copilot's effectiveness. Turns measure
  the effort the agent actually took to drive to resolution.
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

# Investigations run a handful to a few dozen turns — Fibonacci-ish buckets
# give resolution where most cases live (the low single/double digits).
case_resolution_turns = Histogram(
    "faultmaven_case_resolution_turns",
    "Conversation turns from case creation to a terminal state "
    "(resolved/closed). p50/p95 are the investigation-effort SLA. Turns, not "
    "wall-clock — wall-clock is dominated by human idle time.",
    labelnames=["to_state"],
    buckets=[1, 2, 3, 5, 8, 13, 21, 34, 55],
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


def record_resolution_turns(to_state: str, turns: int) -> None:
    """Record turns-to-terminal for a resolved/closed case (clamped at >=0)."""
    case_resolution_turns.labels(to_state=to_state).observe(max(turns, 0))
