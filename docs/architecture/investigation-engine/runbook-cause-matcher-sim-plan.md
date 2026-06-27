# Runbook Cause Matcher — Increment 5 Sim Validation Plan

**Purpose:** behaviorally validate the runbook Cause matcher against a *live* API
server (via `fm-sre-simulator`) before the flag is defaulted on. The unit + the
in-process e2e suites prove the wiring is correct and sound *by construction*;
this plan answers the questions only real, multi-turn runs can:

1. Is the **T2 false-match rate** acceptable (does the LLM-judged semantic match
   instantiate the *right* runbook, not a vocabulary-adjacent one)?
2. Do the **soundness guarantees hold under real pressure** (no premature
   conclusion, no wrong-cause bias, no graph fragmentation)?
3. Is the matcher **net-positive** (faster/better convergence vs flag-off), at an
   acceptable **LLM cost**?

The result drives two decisions: whether to flip the default on, and how to tune
`SURFACE_THRESHOLD`, `_MATCHER_MAX_PRIOR`, and `max_runbooks`.

## Prerequisites

- Deploy the API server with **`ENABLE_RUNBOOK_CAUSE_MATCHER=true`** in `.env`
  (boot-time flag; restart after setting). Keep a second deployment / run with it
  **off** for the A/B comparison.
- KB pack ingested (the 59/91 shipped v4 runbooks) into ChromaDB.
- A **STRICT** `CHAT_PROVIDER` and a configured **`CLASSIFIER_PROVIDER`** (the T2
  `answer_yes_no` uses `get_classifier_model()`); real keys → real LLM cost.
- Run each scenario through `fm-sre-simulator` against the real turn API
  (`POST /api/v1/cases` then multipart `POST /api/v1/cases/{id}/turns`).

## Scenarios

| # | Scenario | Setup | Expected matcher behavior |
|---|---|---|---|
| S1 | **True positive** | Case whose uploaded evidence clearly matches one shipped runbook's Cause (e.g. ArgoCD wave-order, k8s PVC pending). | Matcher instantiates that runbook's chain (CANDIDATE) + a capped hypothesis; as evidence accrues the root VALIDATES; documented fixes surface; the LLM proposes the runbook's fix through M5. |
| S2 | **True negative (off-topic)** | Case unrelated to any shipped runbook. | Matcher abstains (verdict `none`); **no** spurious chains/hypotheses; behavior ≈ flag-off baseline. |
| S3 | **Near-miss / ambiguous** | Case that shares vocabulary with a runbook but is a different cause. | Primary **false-match probe.** Ideally abstains; if it instantiates, the wrong chain must stay a CANDIDATE prior and be dismissable — never drives the conclusion. |
| S4 | **Wrong-runbook bias** | Evidence superficially matches runbook X; the true cause is Y. | Soundness probe: the capped-0.5 prior must **not** push the LLM to conclude X. The investigation must still reach Y from evidence. |
| S5 | **Cost / idempotency** | Any matching case, run 6–10 turns. | The expensive T2 match runs **≈ once** (skip-guard: `cause_state==IDENTIFIED` or an existing runbook-match hypothesis), not every turn. |

## Metrics & assertions

- **T2 false-match rate** = (#cases where the matcher instantiated a chain whose
  root cause ≠ the true cause) / (#cases where it instantiated anything). Target:
  low. If high → raise `SURFACE_THRESHOLD` and/or improve runbook indicator prose.
- **No premature conclusion (soundness, hard gate):** in no case does a
  matcher-seeded hypothesis *alone* (no real evidence) drive `cause_state →
  IDENTIFIED`, a `RootCauseConclusion`, or resolution. Concretely: a
  runbook-match hypothesis's `likelihood` never exceeds `_MATCHER_MAX_PRIOR`
  (0.5) until evidence raises it, so `working_conclusion.likelihood ≥ 0.6`
  (`_cause_identified`) is never satisfied by the prior alone. (Unit-covered;
  confirm it holds in live multi-turn runs.)
- **No premature VALIDATED:** the matcher root stays `CANDIDATE` until real
  CAUSAL_EVIDENCE validates it via `derive_node_states`. (Unit-covered; confirm.)
- **No duplicate-root fragmentation:** when the LLM independently emits the same
  cause the matcher seeded, `ingest_emitted_chain`'s exact-match dedup must merge
  them (no double representation, no orphan-chain nudge loop). Inspect the causal
  graph for duplicate roots.
- **cause_state correctness:** transitions only on real evidence, never from the
  matcher prior.
- **LLM cost:** count matcher-attributable LLM calls per case (T2 `answer_yes_no`
  per rung of the top-1 runbook, ≈ once per case). Compare to the convergence
  benefit.
- **Convergence benefit (the value question):** A/B the same scenarios flag-on vs
  flag-off — turns-to-resolution, correctness of the final cause, quality of the
  proposed fix. The matcher must be **net-positive** to justify defaulting on.

## Tuning knobs (from the metrics)

- `indicator_evaluator.SURFACE_THRESHOLD` (0.5) — raise if S3 false-match rate is
  high (require more rungs to match before surfacing).
- `runbook_cause_matcher._MATCHER_MAX_PRIOR` (0.5) — the conclusion-safety cap;
  lower only if S4 shows residual bias, raise only with strong evidence it
  under-ranks true matches (must stay < 0.6).
- `_DEFAULT_MAX_RUNBOOKS` / the engine's `max_runbooks=1` firing cap — widen only
  if S1 misses multi-runbook cases, at a cost multiple.

## Decision gate (default-on)

Flip the default to `True` **only if all hold:** S3 false-match rate acceptable;
**zero** soundness violations across S1–S4 (no premature conclusion / VALIDATED /
wrong-cause bias / fragmentation); S5 cost bounded; and S1/S4 A/B shows the
matcher is net-positive. Otherwise, tune and re-run, or keep it opt-in.
