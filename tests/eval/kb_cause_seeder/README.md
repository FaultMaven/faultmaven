# KB cause-seeder — flag-ON enabling eval

The re-runnable artifact behind the KB cause seeder's **enabling gate**. The
seeder ships dark (`FAULTMAVEN_KB_CAUSE_SEEDER`, default off); turning it on in a
deployment requires this flag-ON sim/eval to pass. Design + gate definition:
[`docs/architecture/knowledge-and-ai/kb-cause-seeder.md`](../../../docs/architecture/knowledge-and-ai/kb-cause-seeder.md).

It is **not** a CI test — it needs a live server, a real provider key, and the
flag ON. It lives here so the enabling-gate claim rests on a runnable artifact and
committed transcripts, not a doc assertion. The unit-level, LLM-agnostic seeder
tests (deterministic seeding, provenance-blindness, cap↔anchoring coupling,
observable skips, `and_group` reject) run in CI at
`tests/unit/core/investigation/test_kb_cause_seeder.py`.

## What it asserts

Every check reads **engine state** from `GET /debug/cases/{id}/causal-graph`
(node/hypothesis `state`, `validation_method`, `belief`, likelihood) — never model
wording. Model choice changes *whether a scenario's precondition is reachable*,
never the rule asserted (cross-cutting rule: model variation never changes engine
rules). A run passes iff every assertion passes (exit 0).

| Mode | Gate item(s) | Kind |
|---|---|---|
| `smoke` | seeding fires; cap `<=3`; all CANDIDATE/ACTIVE; prior `<=0.5`; no VALIDATED; no dup | deterministic (provider-independent) |
| `mislead` | no-collapse; no-crowd-out; `<=1` ACTIVE/root; **3b** prior-not-gate | full guarantee gate |
| `exclusion` | deductive-exclusion never fabricates a VALIDATED seeded cause (H1 probe) | soundness gate |
| `postturn1` | one-shot seeding boundary | measurement (not a gate) |

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

# 2. Drive each scenario (each creates its own case; --dump is optional).
python tests/eval/kb_cause_seeder/run_seed_eval.py http://127.0.0.1:8091 smoke
python tests/eval/kb_cause_seeder/run_seed_eval.py http://127.0.0.1:8091 mislead
python tests/eval/kb_cause_seeder/run_seed_eval.py http://127.0.0.1:8091 exclusion
python tests/eval/kb_cause_seeder/run_seed_eval.py http://127.0.0.1:8091 postturn1
```

The engine constants the assertions key on (`KB_SEED_PRIOR=0.3`,
`SEED_PRIOR_CAP=0.5`, `MAX_SEEDED_CAUSES=3`, `DEDUCTIVE_EXCLUSION_MAX_BELIEF=0.05`)
are mirrored at the top of `run_seed_eval.py` with source pointers; they are
stable and documented. `ARGOCD_RUNBOOK_ID` (`kb_c350de1303f6`) is content-derived,
so stable across pack rebuilds unless the runbook body changes.

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

So the gate passes on **fireworks `deepseek-v4-flash`** (the dev/demo default and
the weakest-prompt provider). See
[`recorded-runs/2026-07-16-fireworks-deepseek-v4-flash.md`](recorded-runs/2026-07-16-fireworks-deepseek-v4-flash.md):
all of `smoke` / `mislead` / `exclusion` pass, and `postturn1` confirms the
one-shot boundary.

STRICT-provider runs remain **optional, non-blocking** cross-checks. When last
attempted they hit *seeder-independent* external walls, not seeder regressions:
Gemini's multi-turn `mislead` returns a structured-output serving-limit 400
("schema produces too many states for serving"; the seeded node count does not
inflate the output schema — `root_node_ref`/`produces` are plain strings, not
node-id enums), OpenAI returned 402 (no credit), and claude-haiku hit a usage
limit. Re-run them here when credit/limits reset; a STRICT provider clearing the
prompt-dependent items only *reinforces* the BEST_EFFORT-bar pass.
