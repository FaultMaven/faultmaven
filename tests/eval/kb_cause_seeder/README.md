# KB cause-seeder — flag-ON enabling eval

The re-runnable artifact behind the KB cause seeder's **enabling gate**. The
seeder is now **on by default** (`FAULTMAVEN_KB_CAUSE_SEEDER`, kill switch — set
`false` to disable); the flag-on decision was made after this flag-ON sim/eval
cleared its soundness gate on the hardest provider. Design + gate definition:
[`docs/architecture/knowledge-and-ai/kb-cause-seeder.md`](../../../docs/architecture/knowledge-and-ai/kb-cause-seeder.md).

It is **not** a CI test — it needs a live server, a real provider key, and the
flag ON. It lives here so the enabling-gate claim rests on a runnable artifact and
committed transcripts, not a doc assertion. The unit-level, LLM-agnostic seeder
tests (deterministic seeding, provenance-blindness, cap↔anchoring coupling,
observable skips, `and_group` reject, **R8 rung-indicator → evidence-need
seeding**) run in CI at
`tests/unit/core/investigation/test_kb_cause_seeder.py`.

**R8 (rung indicators → evidence-needs) is gated mechanically, not here.** A
seeded cause now emits its `rung_indicators` as PENDING `causal_verification`
evidence-needs (prior-not-gate: `priority=LOW`, `obtainability=UNKNOWN`, cleared by
motivator-based supersession when the hypothesis retires, provenance-blind). Every
one of those properties is a deterministic engine-state assertion, so R8's gate is
the unit + seam tests, not a live rep. What the live `smoke`/`mislead` runs add is
a *measurement* surface — whether a real model actually validates or refutes the
now-present seeded rung-needs — informing the deferred `interventions → Solution`
follow-on, never a new soundness gate.

## What it asserts

Every check reads **engine state** from `GET /debug/cases/{id}/causal-graph`
(node/hypothesis `state`, `validation_method`, `belief`, likelihood) — never model
wording. Model choice changes *whether a scenario's precondition is reachable*,
never the rule asserted (cross-cutting rule: model variation never changes engine
rules). A run passes iff every assertion passes (exit 0).

| Mode | Gate item(s) | Kind |
|---|---|---|
| `smoke` | seeding fires; cap `<=3`; all CANDIDATE/ACTIVE; prior `<=0.5`; no VALIDATED; no dup | deterministic (provider-independent) |
| `mislead` | no-collapse; `<=1` ACTIVE/root; **3b-neg** (no seed VALIDATED, soundness) — plus **3b-pos** (a non-seeded hyp beats the prior) and differential-hygiene as *measurements* | full guarantee gate |
| `exclusion` | deductive-exclusion never fabricates a VALIDATED seeded cause (H1 probe) | soundness gate |
| `postturn1` | one-shot seeding boundary | measurement (not a gate) |
| `smoke-degenerate` | every malformed cause rejected with its exact SkipClass; runbook seeds nothing + alarms; control good cause still seeds | deterministic, **in-process (no server/LLM)** |

`smoke-degenerate` is the odd one out: it needs **no server, no provider, no
flag** — it drives ~10 crafted malformed causes (the `DEGENERATE_CAUSES` corpus,
one per skip reason: fallback / quality-drop / unsupported-shape) straight through
the REAL seeder in-process and asserts each is rejected with its exact SkipClass,
the degenerate runbook trips the "contributed nothing" alarm, and a control good
cause still seeds. Because it is deterministic it is also pinned in CI
(`test_eval_instrumentation.py`); the corpus doubles as the fixture set the Phase-5
produce-path eval imports to assert its converted output never emits a bad shape.
The unit-level seam tests for the retrieval→seeder boundary (the engine wrapper's
flag gate / dedup / crash-isolation and the `get_runbook_causes` loader) live in
`tests/unit/core/investigation/test_kb_cause_seeder_seams.py`.

The old `mislead` **3b** check bundled a soundness half (no seed VALIDATED) with
an engagement half (a non-seeded hypothesis outranks the prior). They are split:
**3b-neg** is the soundness *gate*; **3b-pos** is an engagement *measurement* —
whether the model formed a competitor at all is prompt-dependent, so 3b-pos is
NOT-EXERCISED (not a breach) on a run where the engine never engaged.

The `exclusion` mode is the **exclusion-under-seeding** probe: it refutes all-but-one
seeded sibling and pressures the engine to "conclude the survivor by elimination,"
verifying that seeded siblings do not bias what `validate_by_exclusion` (#593)
quantifies over — a seeded survivor validates only under the genuine deductive
preconditions (`>=2` siblings absolutely excluded), never off a merely-seeded,
un-refuted differential.

## Running it

Stand up an isolated server (own data dir, flag ON, a chosen provider) and drive
each mode against it. From a checkout / worktree root:

```bash
# 1. Isolated server on :8091 (own ./data, does not touch a :8090 dev server).
#    Set CHAT_PROVIDER to the provider under test; key comes from .env / env.
CHAT_PROVIDER=fireworks \
FAULTMAVEN_KB_CAUSE_SEEDER=true \
AUTH_MODE=local \
CHROMADB_KB_PERSIST_DIR=./data/chroma-kb \
CHROMADB_EVIDENCE_PERSIST_DIR=./data/chroma-evidence \
  python -m uvicorn faultmaven.main:app --host 127.0.0.1 --port 8091

# 2. Drive each scenario (each creates its own case; --dump/--json are optional).
python tests/eval/kb_cause_seeder/run_seed_eval.py http://127.0.0.1:8091 smoke
python tests/eval/kb_cause_seeder/run_seed_eval.py http://127.0.0.1:8091 mislead
python tests/eval/kb_cause_seeder/run_seed_eval.py http://127.0.0.1:8091 exclusion
python tests/eval/kb_cause_seeder/run_seed_eval.py http://127.0.0.1:8091 postturn1

# smoke-degenerate needs NO server (the base_url arg is ignored) — it drives the
# crafted bad-cause corpus through the real seeder in-process:
python tests/eval/kb_cause_seeder/run_seed_eval.py - smoke-degenerate

# 3. Average a batch. Because an LLM-driven assertion is only meaningful on the
#    runs that exercised it (see "Instrumentation" below), one run is noisy —
#    write each run's machine-readable result with --json and aggregate:
for i in $(seq 1 8); do
  python tests/eval/kb_cause_seeder/run_seed_eval.py http://127.0.0.1:8091 mislead \
    --json results/mislead-$i.json
done
python tests/eval/kb_cause_seeder/aggregate_runs.py results/mislead-*.json
```

The engine constants the assertions key on (`KB_SEED_PRIOR=0.3`,
`SEED_PRIOR_CAP=0.5`, `MAX_SEEDED_CAUSES=3`, `DEDUCTIVE_EXCLUSION_MAX_BELIEF=0.05`)
are mirrored at the top of `run_seed_eval.py` with source pointers; they are
stable and documented. `ARGOCD_RUNBOOK_ID` (`kb_c350de1303f6`) is content-derived,
so stable across pack rebuilds unless the runbook body changes.

## Instrumentation

An LLM-driven run only *exercises* an assertion when the model reaches the state
the assertion is about. Reporting that faithfully is what stopped a single lucky
run from reading as a clean pass (the ≤1-ACTIVE property measured 7/8, not 8/8,
once runs were averaged — see below).

- **Three assertion states, not pass/fail.** Every check declares an
  *exercised-predicate* alongside its held-condition and records one of **HELD**
  (reached the state, stayed sound), **BREACHED** (reached it, broke the rule),
  or **NOT-EXERCISED** (the precondition never arose — vacuously green, *not*
  evidence of safety). A run exits non-zero only on a BREACHED **gate**;
  NOT-EXERCISED gates and measurements never fail it. Example exercised-predicates:
  a `mislead` "no conclusion on a seeded cause" gate is NOT-EXERCISED when the
  engine drew no conclusion; the `exclusion` fabrication gates are NOT-EXERCISED
  unless a seeded root actually reached VALIDATED / DEDUCTIVE.
- **`--json PATH` + `aggregate_runs.py`.** `--json` writes the run's metadata,
  every assertion's state, and the measurements. `aggregate_runs.py file...`
  reports **held-rate over *exercised* runs** (`HELD / (HELD + BREACHED)`,
  excluding NOT-EXERCISED) per assertion, grouped by mode — the honest batch
  picture the flag-on decision keys on.
- **Crash-tolerant driver.** A turn that 500s mid-scenario (the very
  no-collapse-under-pressure case this eval exists to catch) is recorded
  (`crashed_at_turn`) rather than aborting the run; the driver still dumps and
  asserts the final graph, so a collapse that *also* crashes the turn is caught
  instead of masked. If even the debug read fails, that is a hard `final graph
  readable` breach, not a masked pass.
- **Self-describing summaries.** Every run stamps commit, the server-resolved
  provider/model (from `/debug/llm-providers`), the driver's `flag_env`, and the
  behavioral `seeding_observed` into both the printed summary and the JSON — so a
  recorded run says exactly which code + provider + flag produced it.
- **Differential-hygiene measurement (`mislead`).** Reports the ACTIVE/refuted
  trajectory across turns and flags proliferation-without-refutation (the run6
  blind spot: 3→7→8 ACTIVE, 0 refuted — per-root dedup passed while the
  differential never pruned). Measurement, not a gate.

## The per-provider bar — hardest provider (BEST_EFFORT)

The design doc's enabling gate originally read "per provider." The bar is
**the hardest provider (BEST_EFFORT), not every provider**, for a substantive
reason, not an expedient one:

- The two guarantee properties — **no-collapse / no-incorrect-conclusion** and
  **prior-not-gate** — are *structural*: a seeded cause is CANDIDATE-only,
  evidence-less, provenance-blind, capped at `<=0.5`, and never invokes a
  VALIDATED writer. These hold **by construction, LLM-agnostically** (and are
  pinned by the CI unit tests). No provider can break them.
- The only **prompt-strength-dependent** properties — over-deference,
  paraphrase-duplication (`<=1` ACTIVE/seeded-cause), crowd-out — are *weakest on
  a BEST_EFFORT model* (schema not enforced, weaker instruction-following). A pass
  there is the **binding** case; a STRICT provider (OpenAI/Anthropic/Gemini) can
  only do better on exactly these. Requiring green on every provider adds cost
  without adding assurance beyond the weakest-link pass.

On **fireworks `deepseek-v4-flash`** (the dev/demo default and weakest-prompt
provider) — see
[`recorded-runs/2026-07-16-fireworks-deepseek-v4-flash.md`](recorded-runs/2026-07-16-fireworks-deepseek-v4-flash.md):

- **Gate items 1 and 4 (structural) and the `exclusion` probe: clean pass, every
  run.** `smoke` is deterministic; `mislead`/`exclusion` never let a seeded cause
  reach VALIDATED, always conclude on the true off-seed cause, always satisfy
  3b-neg. `postturn1` confirms the one-shot boundary.
- **Gate item 3 (≤1 ACTIVE per seeded cause) is a pass-RATE, not a clean pass.**
  Run strict + averaged (via `aggregate_runs.py`; this is a *quality*,
  prompt-strength-dependent property — see the design doc's "Prompt alignment"),
  it measured **7/8** on a 2026-07-16 batch: one run hit the documented
  below-INV-36-bar **paraphrase-duplication** (the LLM re-emitted a reworded copy
  of a seeded cause → 2 ACTIVE hypotheses on one root). Soundness held in that run
  too. **An earlier single-run "clean pass" was not robust — averaging is exactly
  why this harness is now committed.**

**Flag-on decision (open):** whether that ≤1-ACTIVE rate clears enabling-gate
item 3 is a deliberate, product-level call, not something this eval declares met.
The design doc's intended envelope for the residual is *prompt + per-provider
eval* (not a new seed-specific semantic-dedup backstop — that is #658 territory).
Options if the rate is judged insufficient: (a) accept it (soundness is never at
risk); (b) strengthen the seeded-directive prompt against paraphrase re-emission;
(c) revisit whether INV-36's mutual-Jaccard bar should catch more seeded-cause
rewords. Re-run this harness to re-measure after any prompt change.

STRICT-provider runs remain **optional, non-blocking** cross-checks. When last
attempted they hit *seeder-independent* external walls, not seeder regressions:
Gemini's multi-turn `mislead` returns a structured-output serving-limit 400
("schema produces too many states for serving"; the seeded node count does not
inflate the output schema — `root_node_ref`/`produces` are plain strings, not
node-id enums), OpenAI returned 402 (no credit), and claude-haiku hit a usage
limit. Re-run them here when credit/limits reset; a STRICT provider clearing the
prompt-dependent items only *reinforces* the BEST_EFFORT-bar pass.
