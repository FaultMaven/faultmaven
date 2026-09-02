# KB cause-seeder — flag-ON enabling eval

The re-runnable artifact behind the KB cause seeder's **enabling gate**. The
seeder is **off by default** (`FAULTMAVEN_KB_CAUSE_SEEDER`; set `true` to enable
for a measurement run). This eval proved the seeds *sound*; the on-vs-off A/B in
[`recorded-runs/2026-09-02-seeder-ab-local.md`](recorded-runs/2026-09-02-seeder-ab-local.md)
measured whether they *help* and turned the default off (fm#1295). Design + gate
definition:
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

## Sibling driver: `run_corroboration_eval.py` (#1144, offline)

A **second** driver lives here, measuring a different question. `run_seed_eval.py`
above asks whether seeding is *sound once it fires*;
`run_corroboration_eval.py` asks **which runbooks should be allowed to seed at
all** — the admission question #1144 was opened on, where off-domain causes
(an NGINX-502 chain, a MongoDB WiredTiger chain) were seeded into a Kubernetes
OOMKilled case and one became its displayed working conclusion.

It is the artifact behind `KB_SEED_MIN_CORROBORATING_CHUNKS`, and **the thing to
re-run before re-sizing that threshold** — which is what
`faultmaven_kb_cause_seed_uncorroborated_total` is there to prompt.

Unlike the driver above it needs **no server and no provider key** (its `e2e`
mode enables the seeder itself, so the default flip does not turn it off) —
only an ingested KB (a ChromaDB collection plus the `knowledge_items` rows), so
it is deterministic and re-runnable offline:

```bash
python tests/eval/kb_cause_seeder/run_corroboration_eval.py guards   # the table that chose the guard
python tests/eval/kb_cause_seeder/run_corroboration_eval.py sweep    # why no score floor works
python tests/eval/kb_cause_seeder/run_corroboration_eval.py e2e      # real wrapper, guard off vs on
python tests/eval/kb_cause_seeder/run_corroboration_eval.py grounding                  # what each ground decides
python tests/eval/kb_cause_seeder/run_corroboration_eval.py grounding --no-term-index  # ... in the other term-index state
```

`grounding` applies the **engine's** `kb_hit_grounding`, never a copy of it: a
driver that re-implements the predicate reports on a gate it does not share,
which is how #1285 — a ground whose firings were 36:1 wrong — stayed invisible
here while this mode said the gate was working. It prints the per-verdict
decision rate with its denominator, because "the gate turned nothing away" and
"the gate is not applying" are the same number in the seed columns. Run it in
**both** term-index states: without the index `term_coverage` degrades to an
unweighted binary fraction, a different quantity on the same scale.

Paths default to `data/chroma-kb` and `data/faultmaven.db`; override with
`--chroma` / `--db`. The 24 problem statements live in
`corroboration-statements.json` — 16 written as a user would actually type them,
each paired with the runbook-title fragments that count as an on-domain seed,
plus 8 carrying no concrete failure signature, where the correct outcome is to
seed nothing. Expectations are title *fragments*, so a pack rebuild does not
invalidate them.

What it found, and what the guard is:

| Guard | on-domain kept | off-domain kept |
|---|---|---|
| rank alone (the #1144 defect) | 14/14 | 27/27 |
| score ≥ 0.66 | 6/14 | 5/27 |
| also in `kb_context`/Sources | 14/14 | 19/27 |
| **corroboration (shipped)** | **13/14** | **6/27** |

A guard is good when the two columns move **apart**, not down together — which
is why a score floor was rejected rather than tuned: on-domain seeds scored
0.603–0.731 and off-domain ones 0.519–0.715, so the ranges overlap and no floor
separates them. End to end (`e2e`), corroboration took on-domain seeding from
13/16 to **16/16** while three cases moved to the *right* runbook.

⚠️ **Its blind spot, stated because it already cost something.** Every runbook in
the shipped pack is long (smallest 9 chunks, median 14), so this corpus cannot
exercise the *length-relative* half of the rule. That is exactly how the first
cut of #1144 shipped a flat threshold which would have made compact personal
runbooks — a document that chunks whole, i.e. the flywheel's own output —
permanently unseedable. If you extend the corpus, extend it with **short**
documents. The length rule itself is pinned in CI, in
`tests/unit/core/investigation/test_kb_cause_seeder_seams.py`.

These numbers are **corpus facts, not invariants**: they move when the pack
moves, which is why this is not a CI test. The invariants it motivated are.

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
  picture the 2026-07 flag-on decision keyed on.
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

**Flag-on decision (closed 2026-09-02 — superseded):** whether that ≤1-ACTIVE
rate cleared enabling-gate item 3 was accepted as a known residual in 2026-07;
the question is now moot, because the on-vs-off A/B
([`recorded-runs/2026-09-02-seeder-ab-local.md`](recorded-runs/2026-09-02-seeder-ab-local.md))
turned the default off on benefit grounds (fm#1295). The paragraph below is kept
as the record of the residual it measured.
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
