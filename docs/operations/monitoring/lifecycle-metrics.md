# Lifecycle Outcome Metrics

Counters defined in `faultmaven/core/investigation/lifecycle_metrics.py`. They measure **outcomes** — what the system actually achieved — not just rule fires. The distinction is load-bearing: a counter that only tracks "this rule fired" is a leading indicator at best. The class of regression these metrics exist to catch is *dynamic drift* (see [Investigation Lifecycle Logic §1.3.1](../../architecture/investigation-engine/investigation-lifecycle-logic.md#131-invariant-enforcement-matrix), "Dynamic drift" paragraph): behaviors that look locally correct (rule fires logged, code paths exercised) but where the system has silently stopped achieving its design goal.

Metrics are exposed via Prometheus and gated on `ENABLE_METRICS=true` plus the `prometheus_client` library being installed (graceful no-op otherwise — see `faultmaven/infrastructure/shims/metrics.py`).

## INV-01: handshake-deferred recovery ratio

**Invariant:** INQUIRY → INVESTIGATING requires the user to confirm a `proposed_problem_statement` that was presented on a prior turn. The same-turn-confirmation guard rejects collapses and defers to a recovery turn. See [INV-01 in the matrix](../../architecture/investigation-engine/investigation-lifecycle-logic.md#131-invariant-enforcement-matrix).

**Counters:**

- `faultmaven_inquiry_handshake_deferred_total` — increments each time the same-turn-confirmation guard fires (an LLM attempted to one-shot the handshake).
- `faultmaven_inquiry_handshake_recovered_total` — increments when a case that previously had a guard fire reaches INVESTIGATING.

**Load-bearing query:**

```promql
# Recovery ratio over the last 24h.
# Healthy systems: ratio ≈ 1.0 (every deferred case eventually transitions).
sum(rate(faultmaven_inquiry_handshake_recovered_total[24h]))
  /
sum(rate(faultmaven_inquiry_handshake_deferred_total[24h]))
```

**What a dropping ratio means.** The guard is firing as expected, but the deferred cases aren't recovering. The composition seam on INV-01 has likely degraded — either the `HANDSHAKE_DEFERRED` prompt instruction is no longer eliciting re-presentation from the LLM (provider model update, prompt drift), or the engine's deterministic suggestion emission has been removed/weakened. This is the regression shape `case_bb917dcd5bb2` exhibited before the fix that introduced these metrics.

**Suggested alert (tune with production data):**

```promql
# Alert when the recovery ratio falls below 0.7 sustained over 1h
# AND there are at least 10 deferrals in the window (avoid noise on
# low-volume periods).
(
  sum(rate(faultmaven_inquiry_handshake_recovered_total[1h]))
    /
  sum(rate(faultmaven_inquiry_handshake_deferred_total[1h]))
) < 0.7
AND
sum(increase(faultmaven_inquiry_handshake_deferred_total[1h])) > 10
```

The thresholds (`< 0.7`, `> 10`) are starting points — adjust once a baseline is established.

**Lag caveat.** A deferred case typically recovers within 1–3 turns, but turn cadence is user-driven and can stretch over hours. Compute the ratio over windows that comfortably exceed expected recovery time (24h above; tighten only when you have data on how long users actually take between turns).

## Engine-owned affordance emission (INV-01 / INV-19 / INV-21)

**Invariant family:** when a state-machine gate is pending (Gate 1 — problem-statement confirmation; Gate 2 — investigation path; Gate 3 — post-mitigation continuation; or a pending_transition disposition handshake), the engine emits the canonical clickable affordance pair via `engine_owned_affordances(case, metadata)`. The LLM does not own routing intents — they're attached deterministically by the engine. This counter measures whether engine-owned emission is firing across the gate vocabulary as expected.

**Counter:**

- `faultmaven_engine_owned_affordance_served_total{gate}` — increments each turn the engine substituted the canonical affordance pair for a pending gate. Label `gate` takes one of: `gate1`, `gate2`, `gate3`, `disposition`.

**Load-bearing queries:**

```promql
# Per-gate emission rate over the last 24h. Healthy systems show non-zero
# rates on whichever gates the user traffic exercises. A sustained zero
# on a label that should be active is the regression signal.
sum by (gate) (rate(faultmaven_engine_owned_affordance_served_total[24h]))
```

```promql
# Gate-1 alarm: if INQUIRY turn volume is non-zero but gate1 emission is
# zero, the Gate-1 predicate or consolidator has regressed silently. The
# 2026-05-19 regression that prompted this counter looked exactly like
# this — LLM-emitted suggestions were going through, the engine wasn't
# substituting them, and intent metadata was missing.
sum(rate(faultmaven_engine_owned_affordance_served_total{gate="gate1"}[1h])) == 0
AND
sum(rate(faultmaven_inquiry_turn_total[1h])) > 0
```

**What a sustained zero on `gate1` means.** The consolidator (`engine_owned_affordances`) isn't returning a `gate1` tuple when it should. Likely causes: predicate logic regressed (`_gate1_is_pending` no longer detects the state), or the response builder stopped calling the consolidator, or some higher-priority branch is short-circuiting. Audit the response-builder branch in `milestone_engine.py` against the predicate definition and `engine_owned_affordances` in lockstep.

**What a sustained zero on `gate2` or `gate3` means.** Same shape, but those gates are reached only after specific case progressions (Gate 1 closed → Gate 2 fires; mitigation_verified on mitigation-first path → Gate 3 fires). Zero on gate2 or gate3 with non-zero gate1 emission could mean cases never close Gate 1 in production — which would also show up in `faultmaven_inquiry_handshake_recovered_total` lagging behind `_deferred_total`. Cross-reference.

**Enable the endpoint.** This counter only exposes via `/metrics` when `METRICS_EXPORTER=prometheus_http` is set in `.env`. With the default (`METRICS_EXPORTER=none`), the counter still records in-process — but `curl http://localhost:8090/metrics` returns 404. Set the env var and restart FM to expose.

## Conventions for adding new lifecycle metrics

Any new counter added to `lifecycle_metrics.py` should follow the same shape:

1. **Pair every rule-fire with a rule-outcome.** A guard that "fired N times" tells you nothing about whether the design goal was achieved; the ratio of fires to outcomes does.
2. **Name from the invariant.** `faultmaven_<invariant_property>_<event>_total`. Future audits should be able to map a metric back to its INV-XX row by name alone.
3. **Document in the matrix.** Reference the metric pair in the relevant INV-XX row's Enforcement column under `*Outcome telemetry:*`. Cross-reference back to this doc.
4. **Document the load-bearing query.** A counter without a documented ratio query is half-instrumentation. Future on-call needs to know what to compute, not just what to look at.

Composition seams (cross-tier dependencies in the matrix) are the natural candidates — they're where prompt-only enforcement and code-guarded enforcement cooperate, which is exactly the seam class most prone to dynamic drift.
