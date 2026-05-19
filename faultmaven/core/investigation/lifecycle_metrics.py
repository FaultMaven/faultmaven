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


# Gate 2 outcome telemetry — measures whether the router's
# recommendation is empirically right. High override rate (alternate
# path chosen over recommended) signals the Urgency × Temporal matrix
# is mis-classifying cases; the user is bringing out-of-band context
# the data can't see. See investigation-gates design (slice 2) and
# INV-19 in investigation-lifecycle-logic.md.
inquiry_gate2_confirmed_total = Counter(
    "faultmaven_inquiry_gate2_confirmed_total",
    "INV-19: Gate 2 confirmations. Labels: outcome (recommended | override).",
    ["outcome"],
)


# Gate 3 outcome telemetry — measures the mitigation → RCA seam. The
# three signals are paired:
#   reached_total            — gate fires (mitigation_verified completed)
#   resolved_total{outcome}  — gate resolved by user click
#                              (continued_to_rca | closed_mitigation_sufficient)
#   stalled                  — gauge of cases sitting at Gate 3 with no
#                              resolution (computed by a periodic sweep)
# The ratio resolved_total / reached_total should be close to 1.0 in a
# healthy system. A growing gap surfaces stranded post-mitigation cases
# — the failure mode the gate exists to prevent.
inquiry_gate3_reached_total = Counter(
    "faultmaven_inquiry_gate3_reached_total",
    "INV-21: mitigation_verified completed on a mitigation-first case "
    "(Gate 3 opens). Pairs with faultmaven_inquiry_gate3_resolved_total.",
)

inquiry_gate3_resolved_total = Counter(
    "faultmaven_inquiry_gate3_resolved_total",
    "INV-21: Gate 3 resolved by user click. Labels: outcome "
    "(continued_to_rca | closed_mitigation_sufficient). Compare against "
    "faultmaven_inquiry_gate3_reached_total to detect stranded cases.",
    ["outcome"],
)
