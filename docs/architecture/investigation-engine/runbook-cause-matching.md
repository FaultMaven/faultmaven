# Runbook Cause Matching: Matching Runbook Causal Chains to Case Evidence

**Document Type:** Component Specification
**Version:** 2.0
**Status:** Target-state for the v4 causal-chain template. The flat-Cause Phase-1
evaluator (predicates, schemas, KB-tool integration) is shipped; the rung-level
matcher + lazy instantiation described here is the design the per-turn wiring
implements. No consumer is live yet — a v4 runbook behaves like a v3 runbook until
this matcher lands (see [§7](#7-implementation-status)).

## Purpose

When a runbook is retrieved during investigation it returns one or more `Cause`
chunks — each a **causal chain**: a single ROOT cause and a `root → … → D` ladder
with per-rung indicators and quadrant-tagged interventions (see
[runbook-content-architecture.md §3](../knowledge-and-ai/runbook-content-architecture.md#3-standardized-runbook-template)).
This document specifies how the engine matches a retrieved Cause's rungs against
current case state, instantiates the matched structure into the case's causal
graph, and surfaces it.

The output of Cause matching drives:

- Which Cause's `Interventions` the agent proposes (by quadrant → `Solution`).
- Which Cause's root `Statement` seeds `RootCauseConclusion.root_cause` on
  confirmation (direct field copy — no LLM extraction).
- Whether the engine asks for more Diagnostic Steps (nothing matched), attributes
  a chain (clean single match), or surfaces a candidate set for LLM disambiguation
  (multiple live chains).

The runbook is a **prior, not a gate** (governing principle): a partially-matching
chain still surfaces at lower confidence; a clean deterministic match is the fast
path. Structure never causes a relevant runbook to be missed.

### Scope

Engine-side matching + instantiation only. Runbook authoring is in
[runbook-content-architecture.md §3](../knowledge-and-ai/runbook-content-architecture.md#3-standardized-runbook-template);
the causal-graph data model is in [investigation-data-models.md](./investigation-data-models.md)
and the methodology in [two-dimensional-hypothesis-methodology.md](./two-dimensional-hypothesis-methodology.md).
Lifecycle integration (when in the turn loop the matcher runs) is in
[investigation-lifecycle-logic.md §1.4](./investigation-lifecycle-logic.md#14-automatic-milestone-tracking-and-stage-transitions).

---

## 1. Cause Chain Recap

Each `### Cause X` is a chain with **one ROOT** (no AND-sets — co-necessity is
folded into the root statement). Sub-fields: `Statement`, optional `Chain` (a
linear `<ref>:` ladder — `root`, `s1`, …, reserved `D`), `Indicators` (per rung,
token-anchored), and quadrant-tagged `Interventions`.

```markdown
### Cause A: Idle transactions exhausting the pool
**Statement:** Sessions in idle-in-transaction hold connection slots, exhausting max_connections.
**Chain:**
- root: idle-in-transaction sessions never release their slot
- s1: free slots accumulate toward zero
- D: clients fail with "too many connections"
**Indicators:**
- root: [Step 2] idle-in-transaction sessions older than 30 min present
  <!-- match: {"step": 2, "predicate": "contains", "target": "idle in transaction"} -->
- s1: [Step 1] active connections > 80% of max_connections
  <!-- match: {"step": 1, "predicate": "threshold", "target": "active_pct", "op": ">", "value": 0.8} -->
**Interventions:**
- **remediation** (root): ALTER SYSTEM SET idle_in_transaction_session_timeout = '30s';
```

The prose is what the agent reads in retrieved chunks. Each `<!-- match: … -->`
is an optional machine-readable predicate, stripped at ingestion. When a Cause has
no `Chain`, it is a degenerate `root → D` chain (tolerant — never an error).

**Fallback Cause.** Exactly one Cause per runbook has `Indicators: [Default]`
(conventionally `### Cause Z: Unidentified`). Fallback selection reads the
`is_fallback_cause` flag, not the heading text.

---

## 2. Matching and Instantiation

### 2.1 Layered evaluation (robustness → efficiency)

The deterministic tier is per-rung; the semantic tier is per-cause (see the #545
note below). The robust (semantic) tier is always available; the fast tier is an
optimization:

| Tier | Mechanism | Cost | Role |
|---|---|---|---|
| **T1 — deterministic** | `match_predicates` (the `<!-- match -->` hints) evaluated against step output (`absent`/`contains`/`exit_code`/`threshold`, §3) | $0, no LLM | Efficiency — used only when a clean predicate exists *and* its referenced step has run |
| **T2 — semantic (per cause)** | ONE `case_evidence_qa` per Cause: *"Is the case explained by this cause?"*, judged from the Cause's symptom-level description (name + statement + non-problem chain prose) | ~1 LLM call per Cause | **Robustness floor** — always available; matches symptom-level evidence the per-rung operator indicators can't |
| **T3 — structural prior** | vector similarity of the retrieved Cause chunk to the case ranks it a candidate even before any rung is tested | retrieval only | A relevant runbook is never invisible just because its rungs aren't tested yet |

`case_evidence_qa` is not a degradation — it is the canonical "judgment given case
evidence" tool.

> **T2 is per-cause, not per-rung (#545; supersedes the original per-rung design).**
> T2 originally judged each rung indicator ("Does the evidence satisfy:
> `[Step 3] kubectl describe job shows BackoffLimitExceeded`?"). Live diagnosis
> showed this never fires: the rung indicators are written at **tool/API-output
> level**, while case evidence is **symptom-level**, so the classifier honestly
> answers NO to every indicator → belief 0 for every cause → verdict `none`.
> **Rejected alternatives:** stripping the `[Step N]` prefix (the residual still
> names API fields the evidence lacks) and matching the raw chain-node statements
> (also implementation-specific) — both still returned NO. The semantic tier is
> now **one holistic judgment per cause** over the Cause's symptom-level
> description (`indicator_evaluator._build_cause_condition`), which fires the
> matching cause and discriminates the rest (clean `single`). The deterministic
> T1 tier (per-rung predicates + refutation) is unchanged.

### 2.2 Instantiation into the case graph (lazy + dedup)

When the matcher engages a Cause for a live case, it instantiates the chain into
the case's causal graph **lazily** and through the engine's existing
node-identity machinery:

1. The case already owns `D` (`CausalNode` `node_type=PROBLEM`, seeded from
   `problem_verification.symptom_statement`). The runbook's `D` rung maps to it —
   the runbook's `## Symptom Recognition` is the *match target*, not the source of
   `D`'s statement.
2. **Dedup before minting (critical).** Before creating a node for a seeded rung,
   check it against existing case nodes via the **same exact-match dedup** the
   engine uses for LLM-emitted nodes; if the cause already stands in the graph,
   **reuse** that node (and its `cn_…` id). Only genuinely-new rungs mint fresh
   ids.
3. Render the resulting `cn_…` ids back into LLM context like any other node, so
   the LLM *extends* the seeded chain rather than re-emitting the same cause as a
   parallel duplicate root. Without steps 2–3 a seeded node and an LLM-emitted
   node for the same cause **fragment into duplicate roots** — the engine's
   hardest prior bug.
4. Create `CausalEdge`s per the linear chain order (no authored `and_group`);
   attach `match_predicates` / indicator criteria for §2.1.
5. **Never set `node_state=VALIDATED`** — runbook structure is a *prior*; nodes
   validate only via case evidence (M4). This is the schema-level guarantee that
   "structure is a prior, never a gate" cannot be violated. `actionable` is
   engine-derived (set only on a validated ROOT); instantiation leaves the `False`
   default.

Eager pre-instantiation on retrieval is rejected: it would inject un-tested rungs
the dedup path never saw, re-opening the fragmentation bug.

---

## 3. Predicate Vocabulary

Controlled vocabulary, extended deliberately. Each predicate has a documented
semantics and an evaluator in `faultmaven/core/investigation/indicator_evaluator.py`.

| Predicate | Semantics | Example hint (strict JSON) |
| --- | --- | --- |
| `absent` | Target path is missing or empty in step output | `{"step": 4, "predicate": "absent", "target": "spec.containers[].resources.limits.memory"}` |
| `contains` | Target substring appears in step output text | `{"step": 1, "predicate": "contains", "target": "OOMKilled"}` |
| `exit_code` | Step's process exit code equals value | `{"step": 1, "predicate": "exit_code", "target": 137}` |
| `threshold` | Numeric target relative to threshold | `{"step": 3, "predicate": "threshold", "target": "memory_pct", "op": ">", "value": 0.85}` |

**Strict JSON only.** Hints are parsed via `json.loads()`. Unquoted keys, single
quotes, trailing commas, JSON5/YAML-flow, and embedded comments all fail Gate 2.

**Adding a predicate** requires updating: this table, the evaluator module, a
registered test case, and a documented worked example. Predicates are not added
per-runbook. Avoid `regex` — it composes poorly and tempts fragile hints.

---

## 4. Chain-Level Verdict (graceful degradation)

A Cause does **not** require all rungs to match to be surfaced — that would be the
rigidity trap the governing principle warns against.

- **Confidence is monotone in matched rungs**, not all-or-nothing. *k of n* rungs
  matched → the chain is a live candidate with belief scaled by depth and rung
  importance (high-value rungs are mid-ladder, uncertain, and divergent between
  Causes).
- **Refutation prunes.** A rung whose indicator is *contradicted* (REFUTES
  evidence) drops the chain hard. (M7 AND-member pruning applies only to
  engine-formed AND-sets at runtime; runbooks author none.)
- **Strict-all** ("every rung deterministically true") is retained only as the
  threshold for **deterministic single attribution** — the efficiency case.
  Everything looser still matches, at lower confidence.

After evaluating all Causes in a retrieved runbook:

| Live chains | Engine action | Verdict |
| --- | --- | --- |
| 0 | Select the runbook's fallback Cause (`is_fallback_cause`); agent surfaces its `mitigation` and asks the user to run the next unrun Diagnostic Step to disambiguate. | `none` |
| 1 | Attribute the active Cause. Agent proposes its `Interventions`. On user-confirmed fix the root `Statement` populates `RootCauseConclusion.root_cause` and the interventions populate `Solution` (`mitigation`/`defensive_fix` → `immediate_action`, `remediation` → `longterm_fix`; plus `node_id` + `quadrant`). | `single` |
| ≥2 | Surface the matched set to the LLM; agent picks a disambiguating Diagnostic Step (one whose finding splits the candidates) and asks the user. | `multiple` |

**Cross-runbook matches.** Top-K retrieval may return Causes from different
runbooks. Cross-runbook narrowing happens upstream via the retrieval pipeline's
service/domain filtering (`filter_mode="hard"` + the four-signal reranker). Chain
matching operates within a single runbook's Causes.

---

## 5. Output Schema

The matcher returns one `CauseMatchResult` per retrieved runbook. Rung-level:

```python
class RungResult(BaseModel):
    rung_ref: str                  # root | s1 | s2 | … | D
    indicator_text: str            # original prose
    matched: bool
    refuted: bool                  # contradicted by evidence (prunes the chain)
    method: Literal["deterministic", "case_evidence_qa", "untested"]
    # Post-#545: per-rung results carry only the deterministic T1 outcome
    # ("deterministic" / "untested"); the "case_evidence_qa" value is retained
    # for back-compat but no longer emitted (T2 is per-cause, not per-rung).

class CauseMatch(BaseModel):
    cause_name: str
    path: list[str]                # rung refs root→D
    rung_results: list[RungResult]
    belief: float                  # 0 if refuted or no rungs; else 1.0 if the
                                   # per-cause holistic T2 supports the cause,
                                   # otherwise the T1 matched-rung fraction (#545)
    is_fallback: bool              # Indicators include [Default]

class CauseMatchResult(BaseModel):
    runbook_id: str
    causes: list[CauseMatch]
    live_causes: list[CauseMatch]      # belief above the surface threshold, not refuted
    verdict: Literal["none", "single", "multiple"]
    selected_cause: CauseMatch | None  # set for single; the fallback for none; None for multiple
```

`verdict` drives §4. `selected_cause` carries the resolved choice so downstream
consumers do not re-derive it.

---

## 6. Where the Cause Graph Structure Lives

Graph structure is **instantiation data, not a retrieval key**, so it lives in the
pre-built **KB pack** (`resources/knowledge/pack/pack.json`), **not** in ChromaDB
metadata. The vector store keeps only scalar retrieval filters.

**ChromaDB chunk metadata (retrieval only):** `domain`, `service`,
`symptom_class`, `severity`, `scope`, `status`, `last_updated`, `tags`, plus
`cause_name`/`item_id` for attribution. No graph blobs in the vector store.

**KB-pack per-Cause record (instantiation):** the toolkit chunker
(`kb_toolkit/core/chunker.py`) already strips `<!-- match -->` comments and lifts
per-Cause fields (`cause_statement`, `cause_chain`, `cause_indicators`,
`cause_interventions`, parsed `match_predicates`, `is_fallback_cause`). The pack
builder carries these into `pack.json` keyed by `item_id`/cause:

| Pack field (per Cause) | Source | Used by |
|---|---|---|
| `cause_name`, `cause_statement` | heading + `Statement` | attribution; root `CausalNode.statement` |
| `chain_nodes` | `Chain:` rungs `[{ref, statement, node_type}]` | rung node instantiation (§2.2) |
| `chain_edges` | chain order (+ `converges:`) `[{cause_ref, effect_ref}]` — linear, no `and_group` | `CausalEdge` instantiation |
| `rung_indicators` | `Indicators:` per rung (ref→prose) | T2 semantic fallback |
| `match_predicates` | `<!-- match -->` per rung | T1 deterministic |
| `interventions` | `Interventions:` `[{ref, quadrant, command, verification, risk?, duration?}]` | `Solution.node_id` / `quadrant` / `immediate_action` / `longterm_fix` |
| `is_fallback_cause` | `[Default]` present | fallback selection (§4) |

The matcher resolves a retrieved chunk's `item_id` → pack record to get the graph
for instantiation. A degenerate (no-`Chain`) Cause carries the `root → D` pair.

---

## 7. Implementation Status

The flat-Cause Phase-1 pieces are shipped and the pack now ships + persists the
per-Cause graph record; the rung-level matcher + lazy instantiation (§2, §4, §5)
remain the v4 target. **No consumer is live yet**: a v4 runbook behaves exactly
like a v3 runbook until the per-turn matcher lands — v4 authoring is
forward-investment (encode the graph now, the matcher catches up). The
incremental, flag-gated activation path is in
[runbook-cause-matcher-implementation.md](./runbook-cause-matcher-implementation.md).

| Component | Location | Status |
|---|---|---|
| Predicate evaluators (`absent`/`contains`/`exit_code`/`threshold`) | `faultmaven/core/investigation/indicator_evaluator.py` | Shipped (flat) |
| `CauseChunk` / match schemas | `faultmaven/core/investigation/cause_schemas.py` | Shipped (flat); rung-level (§5) pending |
| v4 chunker: strip hints + per-Cause metadata | `kb_toolkit/core/chunker.py` | **Implemented** |
| Pack-side per-Cause graph record (§6) | `kb_toolkit/core/pack_builder.py` | **Implemented** (in `pack.json` `causes`) |
| Persist pack `causes` at ingest → `knowledge_items.metadata` | `faultmaven/bootstrap/kb_init.py`, `…/knowledge_service.py` | **Implemented** (impl-doc increment 1) |
| Layered matcher (T1/T2/T3, §2.1) + lazy instantiation/dedup (§2.2) | `faultmaven/core/investigation/` | **Pending** (increments 2–4) |
| Chain-level k-of-n verdict (§4) | matcher | **Pending** (increment 2) |
| Per-turn integration into milestone engine | `faultmaven/core/investigation/milestone_engine.py` | **Pending** (increment 4, flag-gated) |

See [investigation-lifecycle-logic.md §1.4](./investigation-lifecycle-logic.md#14-automatic-milestone-tracking-and-stage-transitions)
for where Cause matching fires within the per-turn flow.
