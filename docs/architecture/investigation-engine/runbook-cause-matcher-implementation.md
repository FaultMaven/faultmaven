# Runbook Cause Matcher — Implementation Plan

**Document Type:** Implementation companion to
[runbook-cause-matching.md](./runbook-cause-matching.md) (the component spec).
**Status:** In progress — increments 1–3 + 4a landed; 4b/5 pending. The matcher
is wired per-turn behind `enable_runbook_cause_matcher` (default off) but inert
until 4b supplies its resolvers.

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
| **2** | **v4 schemas + rung evaluator** | `cause_schemas.py` → rung-level (`RungResult`, `CauseMatch{belief,path}`, `CauseMatchResult{live_causes}`); drop v3 `mechanism/mitigation/resolution/verification`. `indicator_evaluator.py` → per-rung iteration + k-of-n belief + refutation pruning (spec §4). Keep the four predicate evaluators (`absent/contains/exit_code/threshold`) — already v4-correct. | **Done** |
| **3** | **Retrieve → resolve → match** | `kb_qa.aget_cause_matches` retrieves chunks, ranks distinct runbooks by `parent_document_id`, resolves each via an injected `resolve_causes(item_id)` → `metadata["causes"]`, builds `CauseRecord`s (per-entry tolerant), runs the evaluator → `List[CauseMatchResult]`. | **Done** |
| **4a** | **Lazy instantiation + per-turn wiring (flag OFF)** | Reuse `causal_graph.ingest_emitted_chain` (seed D, exact-match dedup, never-`VALIDATED`, `cn_` id render-back, edges). Hook in `_apply_investigation_updates` just before `_apply_chain_emission` so matcher nodes share the same dedup/derive/recompute pass. Flag `enable_runbook_cause_matcher` (FeatureSettings, default False). New `core/investigation/runbook_cause_matcher.py` (`chain_to_specs` / `instantiate_cause_chain` / `apply_runbook_cause_matcher`); `KnowledgeService.get_runbook_causes` (direct repo read — `get_document` drops `causes`); `CauseMatchResult.selected_record` threads the chosen chain; `KBToolAdapter.wrapped` exposes the tool. | **Done** |
| **4b-1** | **Hypothesis attachment (load-bearing chain)** | A matcher-instantiated chain with no hypothesis is structurally inert — invisible to `cause_state` / `any_chain_root_validated` / RCC synthesis (these read standing hypotheses, not bare nodes). `attach_matched_hypothesis` creates an ACTIVE hypothesis (`HypothesisCategory.OTHER`, `OPPORTUNISTIC`, belief→likelihood prior) rooted at the matched chain's ROOT and sets `path` via `chain_path_to_problem`. Idempotent: dedup by `root_node_id` (the matcher runs every turn; nodes dedup, so the hypothesis must too). Root stays CANDIDATE → no premature IDENTIFIED. `apply_runbook_cause_matcher` takes `hypothesis_manager`; the engine passes `self.hypothesis_manager`. | **Done** |
| **4b-2** | **Make matching fire (T2) + cost guard** | **Done.** Decision (analysis in §2.1): T1 deterministic is architecturally infeasible in FaultMaven (no runbook-step execution → no `step_output` to resolve; T1 is the spec's opportunistic fast-path, T2 the canonical floor), so matching fires on **T2** only. New `DocumentQATool.answer_yes_no` = a single-LLM-call boolean over evidence (retrieve top-k + one classifier YES/NO; conservative — no evidence / unparsed / error → False, never refutes). The engine builds `case_evidence_qa` from the `case_evidence_search` tool (`CaseEvidenceQAAdapter.wrapped`) and passes it to the evaluator; `step_output_resolver` stays `None`. **Cost guards:** top-1 runbook (`max_runbooks=1` → ≤1 candidate chain/case) + a per-case skip-guard (skip when `cause_state==IDENTIFIED` or a `Hypothesis.from_runbook_match` already exists — new provenance field). Still flag-OFF. | **Done** |
| **4b-3** | **interventions → Solution** | Map a matched Cause's interventions → `Solution` (quadrant → `immediate_action`/`longterm_fix`; `node_id` from the intervention `ref` → instantiated node; `quadrant`), flowing through the M5 gate (`_solution_cause_validated`), never bypassing it. Requires exposing the `chain_to_specs` ref→node_id mapping. Root `Statement` → `RootCauseConclusion` needs nothing new (engine-synthesized on a VALIDATED root via `synthesize_rcc_from_validated_root`). | Pending |
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
