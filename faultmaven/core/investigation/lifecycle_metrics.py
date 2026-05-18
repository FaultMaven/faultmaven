"""Outcome telemetry for lifecycle invariants.

Counters defined here measure **outcomes** — what the system actually
achieved — not just rule fires. The distinction matters because the
class of regression these metrics catch is *dynamic drift*: behaviors
that look locally correct (rule fires logged, code paths exercised)
but where the system as a whole has silently stopped achieving its
design goal.

See ``docs/architecture/investigation-engine/investigation-lifecycle-
logic.md`` §1.3.1 "Dynamic drift" for the framework. See
``docs/operations/lifecycle-metrics.md`` for query and alerting
guidance.

Adding a metric here:

- Pair every "rule-fire" counter with a "rule-outcome" counter. The
  ratio is the load-bearing signal; a single counter is a leading
  indicator at best.
- Document the metric inline so future maintainers know what it
  proves and what shape of regression it surfaces.
- Counters are no-ops unless ``ENABLE_METRICS=true`` and
  ``prometheus_client`` is installed — see ``shims/metrics.py`` for
  the graceful-degradation policy.
"""

from faultmaven.infrastructure.shims.metrics import Counter

# INV-01 outcome telemetry (composition seam between the same-turn-
# confirmation guard and the recovery-turn affordances). The ratio
# ``handshake_recovered_total / handshake_deferred_total`` measures
# whether deferred handshakes are actually recovering in production.
# A ratio that drops far below 1.0 (e.g., < 0.7) signals dynamic drift
# — most likely the recovery prompt or the deterministic suggestion
# emission has weakened without anyone noticing.
#
# Captures the failure shape observed on case_bb917dcd5bb2: guard fires,
# case persists, no transition ever happens. In a healthy system the
# two counters should track each other closely (recovery may lag by a
# turn or two but should eventually catch up).
inquiry_handshake_deferred_total = Counter(
    "faultmaven_inquiry_handshake_deferred_total",
    "INV-01: same-turn-confirmation guard fires (LLM attempted to "
    "collapse INQUIRY→INVESTIGATING handshake into one turn).",
)

inquiry_handshake_recovered_total = Counter(
    "faultmaven_inquiry_handshake_recovered_total",
    "INV-01: cases that transitioned INQUIRY→INVESTIGATING after a "
    "prior same-turn-confirmation guard fire. Divide by "
    "faultmaven_inquiry_handshake_deferred_total to get the recovery "
    "ratio; sustained ratio drops indicate the recovery path is "
    "broken even though the guard is firing as expected.",
)
