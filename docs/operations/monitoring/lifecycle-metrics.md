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

## INV-29: independent-support bar + prior-cap telemetry

- `faultmaven_root_validation_blocked_support_count_total` — a ROOT with real causal-category support that cleared the generic validation bar was held at INCONCLUSIVE for lacking two independent causal supports (count, mutual-restatement collapse, or the `stance_confidence` filter). One increment per block event (state transition, never per fixpoint pass).
- `faultmaven_hypothesis_likelihood_capped_no_evidence_total` — an LLM likelihood update on a hypothesis with no confident supporting evidence links was capped at `NEW_HYPOTHESIS_MAX_PRIOR` (#573 B1).

**Healthy shape.** Transient blips on the block counter that clear within a few turns — the bar doing elicitation work (the context builder tells the model a SECOND INDEPENDENT observation is needed). The cap counter should decay to near-zero as the model learns from the `system_feedback` recovery message.

```promql
# Block events that never resolve into validations — sustained rate = bar too
# high for real traffic or the model keeps re-recording one datum. Judge
# against grounded-resolution outcomes, not this counter alone
# (cost-per-grounded-resolution is the metric that matters).
rate(faultmaven_root_validation_blocked_support_count_total[24h])

# Fiat-cap pressure: sustained non-zero = the model is asserting belief
# instead of linking evidence (prompt/elicitation drift).
rate(faultmaven_hypothesis_likelihood_capped_no_evidence_total[24h])
```

Matrix row: INV-29 in `investigation-invariants.md`; methodology §7.1 (*Independent-support bar*).

## INV-30: absence-trust discipline telemetry

- `faultmaven_counterfactual_refute_hedged_total` — a REFUTES link on a `causal_absence` row arrived below the stance-confidence bar at chain-emission ingest. The link is kept as ordinary refuting evidence but denied decisive (§7.2) force: it cannot single-handedly refute a node, zero a sibling's belief for proof-by-exclusion, or demote the identified cause. One increment per link created (absence-row links are never overwritten, so creation is the one stable event).
- `faultmaven_absence_confirmation_bearing_rejected_total` — at RESOLVED execution, a metadata-qualified `causal_absence` row was refused as the confirmation citation because its content bears on a **different** chain than the root being confirmed. One increment per refused row per stamp evaluation — refusal leaves no marker on the case, so a retried RESOLVED execution re-counts the same rows; the rate is bounded by refused-rows-per-stamp × stamp evaluations, not by resolutions alone.

**Healthy shape.** Both near zero. A sustained hedged-refute rate is an *elicitation* signal — the model keeps reporting failed fixes it does not trust — not a truth problem (the decisive-power gate is what protects the conclusion). Any sustained bearing-rejection rate means confirmations are being recorded against the wrong candidate cause; inspect affected cases before trusting their `CONFIRMED`-grade harvests.

```promql
# Hedged counterfactuals: elicitation drift if sustained.
rate(faultmaven_counterfactual_refute_hedged_total[24h])

# Bearing refusals at the stamp: should be ~zero; each event is one
# resolution whose confirmation row talked about a different chain.
rate(faultmaven_absence_confirmation_bearing_rejected_total[24h])
```

Matrix row: INV-30 in `investigation-invariants.md`; methodology §7.3 (decisive counterfactual force) and §9.5 (confirmation-row qualification + bearing).

## INV-31: MECE-arbitration hold telemetry

- `faultmaven_cause_identification_held_mece_total` — cause identification became MECE-contested: ≥2 simultaneously-validated DISTINCT standing roots stood unarbitrated (§7.1.2), holding `cause_state` at CANDIDATES and withholding the engine conclusion mirror. One increment per transition INTO the contest (edge-triggered on the persisted `cause_identification_contested` flag), never per turn while it stands. Duplicate emissions and same-live-causal-line roots do not count (they collapse to one cause); a counterfactually confirmed root settles the contest without an event.

**Healthy shape.** Near zero, and each event should clear within a few turns as discriminating evidence arrives (the context builder renders the discrimination ask on the contested roots). A sustained standing hold means the model keeps validating competing exclusive causes without running the test that separates them — an elicitation/search problem; the hold itself is the truth surface doing its job. Pair with the `cause_identification_mece_hold` WARNING (same event) for per-case forensics.

```promql
# MECE holds: each event is one case whose identification was withheld
# pending discrimination between competing validated causes.
rate(faultmaven_cause_identification_held_mece_total[24h])
```

Matrix row: INV-31 in `investigation-invariants.md`; methodology §7.1.2 (MECE arbitration).

## INV-32: solution-offer liveness telemetry

- `faultmaven_solution_offer_superseded_total{reason}` — a pending SOLUTION `ProposedAction` left liveness; `solution_proposed` is derived from live offers, so these events are what can move the DIAGNOSIS frame back out of "awaiting execution". One increment per superseded offer, labeled by reason: `reproposal` (a newer SOLUTION offer replaced it — the newest proposal IS the offer) and `license_lost` (the established-cause license that admitted the offer under M5 fell — failed-fix demotion, conclusion retraction, a MECE hold, or the working-conclusion proxy dropping below its bar (e.g. stagnation decay) — and the engine withdrew the offer rather than keep presenting a fix for a cause it no longer asserts).

**Healthy shape.** `reproposal` at a modest rate is routine iteration; a sustained high rate means the model churns fixes the user never executes (elicitation/UX signal). Note a single turn emitting N solution records counts N−1 intra-turn `reproposal` supersessions (only the last stands) — same-turn siblings are indistinguishable from cross-turn churn at the counter. `license_lost` near zero; each event is one case where a cause was established, a fix went on the table, and the license then fell — the withdrawal is the truth surface working. A sustained rate can mean the validation bars are admitting causes that don't survive (inspect INV-27/INV-29/INV-31 telemetry on the same window) OR that working-conclusion-proxied licenses are decaying under stagnation (check hypothesis-decay activity) — distinguish before suspecting this rule.

```promql
# Offer churn: fixes replaced before execution.
rate(faultmaven_solution_offer_superseded_total{reason="reproposal"}[24h])

# Withdrawn licenses: established causes knocked down after a fix was offered.
rate(faultmaven_solution_offer_superseded_total{reason="license_lost"}[24h])
```

Matrix row: INV-32 in `investigation-invariants.md`; lifecycle-logic §1.4 (state-update table). Pair with the `solution_offer_withdrawn` WARNING (same event, per-case forensics).

## INV-33: pending-action hygiene telemetry

- `faultmaven_pending_action_superseded_stale_total` — a shadowed DIAGNOSTIC pending `ProposedAction` was retired (`superseded_reason="stale_pending"`) when the SOLUTION offer it predated left pending state — WITHDRAWN on license loss OR ACCEPTED into TREATMENT — so it cannot resurface as the `<pending_action>` the LLM reads for compliance. One increment per retired action. MITIGATION is cause-independent (INV-32) and is never retired this way.

**Healthy shape.** Each stale retirement rides on a solution leaving pending (an acceptance or a `license_lost` withdrawal), so the rate is bounded by those transitions × open-diagnostic-asks-per-transition. A near-zero rate is normal — most offers have no stale diagnostic beneath them (a well-behaved investigation clears diagnostic asks before proposing a fix). A sustained rate means investigations repeatedly reach a fix with earlier evidence requests still open; if it correlates with `solution_offer_superseded_total{reason="license_lost"}` on the same window, read it as the same admission-bar / decay diagnosis.

```promql
# Stale diagnostic asks retired on solution withdrawal.
rate(faultmaven_pending_action_superseded_stale_total[24h])
```

Matrix row: INV-33 in `investigation-invariants.md`.

## INV-34: LLM-conclusion lifecycle telemetry

- `faultmaven_llm_rcc_cause_linked_total` — an LLM-authored `RootCauseConclusion` was attributed to a standing hypothesis (`validated_hypothesis_id`) by conservative single-STRONG-match lexical link at the per-turn recompute (§7.6). The link is what lets the disconfirmation-retraction lane reach an LLM conclusion. One increment per link written; an already-linked or unattributable conclusion does not count.
- `faultmaven_llm_rcc_retracted_disconfirmed_total` — an LLM-authored `RootCauseConclusion` was RETRACTED at source because its named cause was disconfirmed (M6 counterfactual refute / net-refutation, §7.3). The engine never re-authors the conclusion; it clears a proven-wrong one so no consumer asserts a disproven cause (NO-INCORRECT-CONCLUSION). Distinct from the MECE-contest READ-suppression, which mutates nothing and is counted by `cause_identification_held_mece_total`.

**Healthy shape.** Both sit near zero once a case's cause is named. A sustained `cause_linked` rate is benign (models re-wording the same cause each turn without a stable link) but a sustained *retracted* rate means models keep concluding causes that fixes then disconfirm — the same failed-fix signal as `solution_offer_superseded_total{reason="license_lost"}`; correlate them. A `cause_linked` rate near zero while cases reach RESOLVED is expected — most conclusions are engine-mirrored or need no retraction. Read `retracted` beside `absence_confirmation_*` and the M6 demotion logs for the disconfirmation story on a given deployment.

```promql
# LLM conclusions linked to a cause (attribution rate).
rate(faultmaven_llm_rcc_cause_linked_total[24h])
# LLM conclusions retracted on disconfirmation (the load-bearing signal).
rate(faultmaven_llm_rcc_retracted_disconfirmed_total[24h])
```

Matrix row: INV-34 in `investigation-invariants.md`; methodology §7.6.

## INV-36: hypothesis-dedup telemetry

- `faultmaven_hypothesis_dedup_skipped_total` — an LLM-emitted `hypotheses_to_add` item was skipped as a duplicate of an existing (any-state) or same-batch hypothesis (the MECE distinctness bar, `statements_name_same_cause`, §7.8). Protects the ≥2-active work gate — the axis that separates `INSUFFICIENT_EVIDENCE` from `NOT_YET_PRODUCTIVE` — from duplicate inflation. One increment per skipped item.

**Healthy shape.** Near zero. A sustained-nonzero rate is **not** a soundness alarm (the dedup already held the gate; nothing wrong was concluded) but a signal that the model is re-emitting causes it already posited — usually because it is not being shown, or not attending to, the standing hypotheses. The recovery is context/prompt-side (strengthen the standing-hypothesis block, or investigate why the model is not reading it), not an engine change. Read it beside work-gate crossing volume: a high skip rate on cases that never cross the gate honestly means the model is spinning on one idea — a `NOT_YET_PRODUCTIVE` shape the dedup now keeps the engine from mistaking for productive work.

```promql
# Duplicate hypotheses skipped (attend-to-standing-hypotheses signal).
rate(faultmaven_hypothesis_dedup_skipped_total[24h])
```

Matrix row: INV-36 in `investigation-invariants.md`; methodology §7.8.

## Conventions for adding new lifecycle metrics

Any new counter added to `lifecycle_metrics.py` should follow the same shape:

1. **Pair every rule-fire with a rule-outcome.** A guard that "fired N times" tells you nothing about whether the design goal was achieved; the ratio of fires to outcomes does.
2. **Name from the invariant.** `faultmaven_<invariant_property>_<event>_total`. Future audits should be able to map a metric back to its INV-XX row by name alone.
3. **Document in the matrix.** Reference the metric pair in the relevant INV-XX row's Enforcement column under `*Outcome telemetry:*`. Cross-reference back to this doc.
4. **Document the load-bearing query.** A counter without a documented ratio query is half-instrumentation. Future on-call needs to know what to compute, not just what to look at.

Composition seams (cross-tier dependencies in the matrix) are the natural candidates — they're where prompt-only enforcement and code-guarded enforcement cooperate, which is exactly the seam class most prone to dynamic drift.
