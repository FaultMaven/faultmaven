# Runbook Cause Matcher — Implementation Plan

**Document Type:** Implementation companion to
[runbook-cause-matching.md](./runbook-cause-matching.md) (the component spec).
**Status:** In progress — increment 1 landed; 2–5 pending, all flag-gated.

The spec describes the target behaviour; this document is the **how/when** —
the incremental, flag-gated path that takes the matcher from dormant to live
without risking the investigation engine.

## Why this exists

The spec's §7 status table says it plainly: *"No consumer is live — a v4 runbook
behaves exactly like a v3 runbook until the per-turn matcher lands."* The pieces
exist but are unwired:

- The KB pack ships a rich per-Cause graph record (`chain_nodes`, `chain_edges`,
  `rung_indicators`, `match_predicates`, quadrant `interventions`) — but ingestion
  dropped it, so it never reached storage.
- `cause_schemas.py` / `indicator_evaluator.py` / `kb_qa.aget_top_causes` form a
  complete **v3, flat-Cause** matcher with **zero production callers**; it reads
  ChromaDB chunk metadata that the v4 pipeline no longer produces.

So the work is: persist the graph record, move the matcher to the rung-level v4
shape, and wire it into the turn loop behind a flag.

## Data path (decided by spec §6)

Graph structure is **instantiation data, not a retrieval key** — it lives in the
KB pack, *not* ChromaDB. We persist it at ingest into the existing
`knowledge_items.metadata` JSON column (no migration; the column already
round-trips through ORM → repository → domain model). The matcher resolves a
retrieved chunk's `item_id` → `knowledge_items.metadata.get("causes")`.

Co-locating the record in the `knowledge_items` row keeps it consistent with the
orphan-prune (which deletes the whole row → causes gone with it) and with
re-ingestion **on a runbook-body change** (delete + recreate refreshes causes
atomically). No separate cleanup path, no side table, no query surface we don't
need.

Two contracts the consumer (increment 4) must honour:

- **Read with `.get("causes")`, never `["causes"]`.** It is absent/None for
  upload-path and pre-v4 runbooks; the database backend returns `{}` for a
  no-causes row while the in-memory backend returns `None`.
- **Causes refresh on a body change, not a causes-only edit.** The idempotency
  skip keys on the markdown content hash (`kb_init.py`), which does not include
  `causes`. A pack-builder change that revises a Cause's graph *without* changing
  the runbook prose is skipped on re-ingest — refresh requires a body change or a
  forced re-ingest. (Folding a pack/causes fingerprint into the skip check is a
  reasonable follow-up; pre-production, nothing is deployed yet.)

## Increments (each its own reviewable, flag-gated PR)

| # | Increment | Scope | Status |
|---|---|---|---|
| **1** | **Persist causes at ingest** | `PackRunbook.causes` field + load mapping (`kb_pack.py`); `kb_init` passes `causes=`; `KnowledgeService.ingest_runbook(causes=...)` writes `metadata["causes"]`. Inert until consumed. | **Done** |
| **2** | **v4 schemas + rung evaluator** | `cause_schemas.py` → rung-level (`RungResult`, `CauseMatch{belief,path}`, `CauseMatchResult{live_causes}`); drop v3 `mechanism/mitigation/resolution/verification`. `indicator_evaluator.py` → per-rung iteration + k-of-n belief + refutation pruning (spec §4). Keep the four predicate evaluators (`absent/contains/exit_code/threshold`) — already v4-correct. | Pending |
| **3** | **Retrieve → resolve → match** | `kb_qa.aget_top_causes` retrieves chunks for `item_id`+score, maps via `metadata["causes"]` to the v4 Cause model, runs the evaluator → `CauseMatchResult`. Retire `_parse_cause_chunk`'s metadata-key construction. | Pending |
| **4** | **Lazy instantiation + per-turn wiring (flag OFF)** | Reuse `causal_graph.ingest_emitted_chain` (seed D, exact-match dedup, never-`VALIDATED`, `cn_` id render-back, edges). Hook in `_apply_investigation_updates` just before `_apply_chain_emission` so matcher nodes share the same dedup/derive/recompute pass. Map interventions → `Solution` (quadrant → `immediate_action`/`longterm_fix`, `node_id`, `quadrant`); root `Statement` → `RootCauseConclusion` only on validation; M5 gate applies. | Pending |
| **5** | **Enable + validate** | Flip `enable_runbook_cause_matcher` on in test/sim; validate against the sim harness + unit suite (no duplicate-root fragmentation, no premature `VALIDATED`, `cause_state` correct). | Pending |

## Reuse (deliberately not reinvented)

- **`causal_graph.ingest_emitted_chain`** is the single node-identity machinery
  (seed `D`, exact-match dedup before minting, `cn_` id render-back, linear
  edges, nodes default `CANDIDATE`). The matcher converts `selected_cause`'s
  `chain_nodes`/`chain_edges` into its duck-typed specs and feeds it — it does
  **not** write `case.causal_nodes` directly. This is what makes the soundness
  guarantees automatic.
- **The four predicate evaluators** already match the v4 predicate vocabulary.
- **The flag pattern**: `enable_runbook_cause_matcher: bool` on `FeatureSettings`
  (default `False`, `validation_alias="ENABLE_RUNBOOK_CAUSE_MATCHER"`), read via
  `get_settings().features...`.

## Soundness guarantees (held by construction)

- **Never `VALIDATED` at instantiation** — nodes default `CANDIDATE`; validation
  only via `derive_node_states` from real evidence (M4). Schema-enforced.
- **Prior, not a gate** — k-of-n verdict (not strict-all); a partially-matching
  chain still surfaces; the T2 semantic fallback (`case_evidence_qa`) is always
  available, so predicates never gate.
- **No duplicate roots** — exact-match dedup + `cn_` render-back (the engine's
  hardest prior bug, already solved in `ingest_emitted_chain`).
- **Off by default** — every increment is inert or flag-gated until increment 5.

## Test surfaces

`test_causal_graph_ingestion.py`, `test_derive_node_states.py` (never-VALIDATED),
`test_chain_cause_state.py`, `test_solution_validation_gate.py` (M5),
`test_indicator_evaluator.py` (T1 predicates), and the `fm-sre-simulator` harness
for end-to-end turn behaviour.
