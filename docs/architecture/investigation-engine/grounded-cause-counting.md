# Grounded-Cause Counting

**Status:** Current · **Implements:** [`grounding_metrics.py`](../../../faultmaven/core/investigation/grounding_metrics.py),
[`cause_assurance.py`](../../../faultmaven/core/investigation/cause_assurance.py)

This is the contract for *what counts as a grounded cause* across both grounding arms — the definition the
both-arms grounding baseline metric implements. It exists to make a dead mechanism **visible**: the metric
counts authority-grounded causes over the population of cases where a matching runbook was retrieved, so a
grounding arm that never fires shows up as a near-zero numerator over a real, non-zero denominator rather than
being silently masked by LLM-carried correctness.

The metric and the §7 harvest gate ([`grade_cause_assurance`](../../../faultmaven/core/investigation/cause_assurance.py))
must always agree on "grounded"; the three requirements below are what keep them from drifting apart.

## R1 — Drift-lock to the harvest grade

`grade_cause_assurance(case)` is the single source of truth for the `GROUNDED` grade. The counter
(`count_grounded_roots`) classifies grounding by **reusing the same primitives**, never by re-deriving
"grounded" from raw fields:

- **Runbook arm** — `support_is_runbook_grounded(link)`: `link.stance == SUPPORTS and link.provenance == "runbook"`.
  A `runbook`-provenance SUPPORTS is an expert-authored predicate that fired against the submitted telemetry;
  it grounds regardless of the LLM's `Evidence.category` choice on the backing datum.
- **Deductive arm** — `root.validation_method == ValidationMethod.DEDUCTIVE`: proof-by-exclusion over a
  certified-exhaustive OR-set (§7.1.1).

Because both read the shared primitives, `grade_cause_assurance(case) == GROUNDED` **iff** the case has ≥1
grounded root under this definition — by the structure of the code, for all inputs, not just empirically. A
metric that hard-codes `provenance == "runbook"` in its own body instead of calling the primitive is a defect:
it can drift out from under the gate.

## R2 — The counted unit and per-arm attribution

A **grounded root cause** is a causal node `n` that is *both*:

1. a **validated root** — `n.node_type == ROOT and n.node_state == VALIDATED` (the only unit §7 harvests), and
2. **authority-grounded by ≥1 arm** on non-dangling backing — a runbook-grounded link whose `evidence_id` is
   still present in `case.evidence` (a dangling reference to deleted evidence never counts), or
   `validation_method == DEDUCTIVE`.

Counting per validated-root (rather than per-case) recovers *how many* roots grounded and *which arm* grounded
each — the facts the metric exists to report — and rolls up losslessly to the case grade. The tally exposes:

| Field | Meaning |
|---|---|
| `grounded_roots` | validated roots grounded by **either** arm (set-union) |
| `runbook_arm` | grounded via the runbook arm — off 0 ⇒ the retrieval-seeded differential is live |
| `deductive_arm` | grounded via the deductive arm — off 0 ⇒ deductive validation is wired |
| `runbook_links_fired` | leading indicator: runbook predicates fired on **any** node (root or intermediate); diagnostic only, never a grounding count |

**The arms are not mutually exclusive** — a root can be grounded by both, so
`runbook_arm + deductive_arm ≥ grounded_roots`. Report `grounded_roots` directly; **never** sum the two arm
counts to get the total. The per-arm split is the acceptance instrument for two independent workstreams:
`runbook_arm` rising from 0 signals the retrieval-seeded differential landed; `deductive_arm` rising from 0
signals the deductive arm was wired. Collapsing them would let either mask the other's dormancy.

## R3 — Non-circular denominator

The metric is a rate over the **matching-runbook population**: cases where runbook retrieval returned ≥1
candidate on any turn, captured at retrieval time **before** the single/multiple/none verdict gate, via
`Case.runbook_retrieved`.

The denominator is **never** sourced from `differential_runbook_ids`, which is written only on a `single`
verdict — using it would gate the population by the same seeding gap the numerator measures, yielding a
meaningless `0 / ≈0`. `runbook_retrieved` is boolean by design: the matcher is one-shot on the productive path
but re-fires each turn, so a count would mislead; membership is all the denominator needs. It is set going
forward by the matcher and is not retrofitted onto historical cases.

## Projections

`compute_grounding_baseline(cases, scope=...)` reports two projections of the same tally:

- **`all`** — every matching-runbook case that reached INVESTIGATING (implied by `runbook_retrieved`, since the
  matcher only runs in INVESTIGATING). The observability signal: *does grounding ever fire?*
- **`terminal`** — restricted to RESOLVED/CLOSED cases, where `convert-from-case` actually reads the §7 grade.
  The harvest-readiness / flywheel-throughput number.

They answer different questions; quoting one for the other conflates mechanism with outcome.
