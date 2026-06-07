# Runbook Cause Matching: Matching Runbook Cause Indicators to Case Evidence

**Document Type:** Component Specification
**Version:** 1.0
**Status:** Phase 1 shipped (evaluator module, predicates, schemas, KB-tool integration). Phase 2 pending: per-turn wiring into the milestone engine.

## Purpose

When a runbook is retrieved during investigation, it returns one or more `Cause` chunks (see [runbook-content-architecture.md §3](../knowledge-and-ai/runbook-content-architecture.md#3-standardized-runbook-template)). Each Cause carries an `Indicator` field — the criteria that must hold for that Cause to be the active one. This document specifies how the engine evaluates Indicators against current case state to attribute the active Cause.

The output of Indicator resolution drives:

- Which Cause's `Mitigation` or `Resolution` the agent proposes to the user
- Which Cause's `Statement` + `Mechanism` populate `RootCauseConclusion` at case completion (direct field copy — no LLM extraction)
- Whether the engine asks for additional Diagnostic Steps (zero Causes matched) or surfaces a candidate set for LLM disambiguation (≥2 Causes matched)

### Scope

This spec covers engine-side evaluation only. Runbook authoring of Indicators is in [runbook-content-architecture.md §3](../knowledge-and-ai/runbook-content-architecture.md#3-standardized-runbook-template). Investigation-lifecycle integration (when in the turn loop the evaluator runs, how its output flows into milestone state) is in [investigation-lifecycle-logic.md §1.4](./investigation-lifecycle-logic.md#14-automatic-milestone-tracking-and-stage-transitions).

---

## 1. Indicator Format Recap

Each `### Cause N` subsection in a v3 runbook contains an `**Indicator:**` field — a bullet list of entries that reference numbered Diagnostic Steps, named Symptoms, or carry the `[Default]` sentinel for the fallback Cause.

**Example:**

```markdown
### Cause A: Idle transactions exhausting the pool
**Indicator:**
- [Step 1] active connections > 80% of max_connections
- [Step 2] sessions with state = 'idle in transaction' older than 30 minutes present
<!-- match: {"step": 2, "predicate": "contains", "target": "idle in transaction"} -->
```

The prose is what the agent reads in retrieved chunks. The HTML-comment `<!-- match: ... -->` is an optional machine-readable hint — stripped at ingestion and lifted into ChromaDB metadata for deterministic evaluation. The hint body must be **strict JSON** (`json.loads()`-parseable; quoted keys, no trailing commas, no comments). See §3 for predicate vocabulary, §6 for the metadata schema.

**Cause heading convention.** Cause subsections are named `### Cause <X>: <name>` where `<X>` is a single uppercase letter (`A` through `Z`). `Z` is reserved for the fallback Cause whose Indicator is `[Default]`. Up to 25 named real Causes per runbook (A–Y).

---

## 2. Evaluation Strategy

Two paths, deterministic-first.

**Fast path (deterministic).** When the retrieved Cause chunk carries a `match_predicates` metadata array (parsed from `<!-- match: ... -->` hints at ingestion time), the engine evaluates each predicate against case state without an LLM call. Returns a boolean per Indicator entry.

**Fallback path (`case_evidence_qa`).** When no match-hint is present, or when a hint's predicate is unknown to the current evaluator, the engine delegates to the existing `case_evidence_qa` tool with the Indicator prose as the question — *"Does the case evidence satisfy: [Indicator text]?"* Returns a yes/no answer grounded in case evidence.

The fallback is not a degradation. `case_evidence_qa` is the canonical "judgment given case evidence" tool — Indicator resolution is exactly that question. The deterministic fast path is an optimization for cheap, unambiguous predicates.

**Match rule.** A Cause is matched when ALL its Indicator entries evaluate true. Partial matches do not count — they are equivalent to no match.

---

## 3. Predicate Vocabulary

Controlled vocabulary, extended deliberately. Each predicate has a documented semantics and an evaluator in `faultmaven/core/investigation/indicator_evaluator.py`.

| Predicate | Semantics | Example hint (strict JSON) |
| --- | --- | --- |
| `absent` | Target path is missing or empty in step output | `{"step": 4, "predicate": "absent", "target": "spec.containers[].resources.limits.memory"}` |
| `contains` | Target substring appears in step output text | `{"step": 1, "predicate": "contains", "target": "OOMKilled"}` |
| `exit_code` | Step's process exit code equals value | `{"step": 1, "predicate": "exit_code", "target": 137}` |
| `threshold` | Numeric target relative to threshold | `{"step": 3, "predicate": "threshold", "target": "memory_pct", "op": ">", "value": 0.85}` |

**Strict JSON only.** Hints are parsed via `json.loads()`. Unquoted keys, single quotes, trailing commas, JSON5/YAML-flow syntax, and embedded comments all fail validation as a hard error in Gate 2.

**Adding a predicate** requires updating: this table, the evaluator module, a registered test case, and a documented worked example. Predicates are not added per-runbook.

Avoid `regex` for as long as possible — it composes poorly with the others and tempts authors to write fragile match hints.

The `target` field is interpreted by the predicate's evaluator and may reference a dotted path into JSON-shaped step output, a substring of text-shaped output, or a known computed metric. Each predicate defines its own target resolution.

---

## 4. Multi-Match Policy

After evaluating all Causes in a retrieved runbook against current case state:

| Matched Causes | Engine Action | Evaluator output (§5) |
| --- | --- | --- |
| 0 | Engine selects the runbook's fallback Cause (the subsection whose Indicator contains `[Default]`; conventionally `### Cause Z: Unidentified`). Agent surfaces its Mitigation + asks the user to run the next unrun Diagnostic Step to disambiguate. | `verdict="none"`, `selected_cause=` the `[Default]` Cause |
| 1 | Engine attributes the active Cause. Agent proposes its Mitigation/Resolution. On user-confirmed fix, this Cause populates `RootCauseConclusion` (Statement → `root_cause`; Mechanism → `mechanism`) and `Solution` (Mitigation/Resolution blocks). | `verdict="single"`, `selected_cause=` the matched Cause |
| ≥2 | Engine surfaces the matched set to the LLM. Agent picks a disambiguating Diagnostic Step (one whose finding distinguishes between matched Causes) and asks the user. Multi-match within a single runbook is also a soft lint signal — Indicators should typically be mutually exclusive within a runbook. | `verdict="multiple"`, `selected_cause=None` (LLM disambiguates) |

The fallback Cause is identified by **the `[Default]` token in its Indicator field**, not by heading text. The heading `### Cause Z: Unidentified` is the authoring convention enforced by the runbook validator, but the engine's fallback selection reads only `is_fallback_cause` metadata (set when `[Default]` appears in the Indicator list).

**Cross-runbook matches.** Top-K Cause retrieval may return Causes from different runbooks. Cross-runbook disambiguation happens upstream of Indicator resolution, via the retrieval pipeline's existing service/domain filtering (`filter_mode="hard"` injects service into ChromaDB `where`; the four-signal reranker weights metadata match). Indicator resolution operates within a single runbook's Causes.

---

## 5. Output Schema

The evaluator returns one `CauseMatchResult` per retrieved runbook:

```python
class IndicatorResult(BaseModel):
    indicator_text: str           # Original prose
    matched: bool
    method: Literal["deterministic", "case_evidence_qa"]

class CauseMatch(BaseModel):
    cause_name: str
    indicator_results: list[IndicatorResult]
    matched: bool                  # All indicators true
    is_fallback: bool              # Indicator includes [Default]

class CauseMatchResult(BaseModel):
    runbook_id: str
    causes: list[CauseMatch]
    matched_causes: list[CauseMatch]   # Subset where matched=True
    verdict: Literal["none", "single", "multiple"]
    selected_cause: CauseMatch | None  # See resolution rule below
```

`verdict` drives the §4 branching. `selected_cause` carries the engine's resolved choice so downstream consumers do not have to re-derive it:

- `verdict="single"` → `selected_cause` = the one matched Cause
- `verdict="none"` → `selected_cause` = the runbook's fallback Cause (where `is_fallback=True`)
- `verdict="multiple"` → `selected_cause=None` (LLM disambiguates via a follow-up Diagnostic Step)

---

## 6. ChromaDB Metadata Schema

When the KB toolkit ingests a v3 runbook, each `### Cause N` chunk carries the following metadata. The chunk's embedded text remains the prose body of the Cause (HTML comments stripped); metadata is for engine-side structured access.

| Metadata field | Source | Used by |
|---|---|---|
| `cause_name` | `### Cause N: <name>` heading | Attribution + Resolution Summary report |
| `cause_statement` | `**Statement:**` field (≤300 chars) | Direct copy → `RootCauseConclusion.root_cause` |
| `cause_mechanism` | `**Mechanism:**` field (≤800 chars) | Direct copy → `RootCauseConclusion.mechanism` |
| `cause_indicators` | `**Indicator:**` list, prose form | Evaluator fallback path (`case_evidence_qa`) |
| `match_predicates` | Parsed `<!-- match: ... -->` hints, JSON array | Evaluator fast path (deterministic) |
| `cause_mitigation` | `**Mitigation:**` block | `Solution.immediate_action` source |
| `cause_resolution` | `**Resolution:**` block | `Solution.longterm_fix` source |
| `cause_verification` | `**Verification:**` field | `solution_verified` prompt criteria |
| `is_fallback_cause` | True iff Indicator includes `[Default]` | Multi-match policy (§4) — fallback selection |

Ingestion-time transforms in `kb_toolkit/core/chunker.py`:

1. Locate each `### Cause N` block.
2. Parse its sub-fields into a structured object.
3. Strip any `<!-- match: ... -->` HTML comments from the chunk body before embedding.
4. Attach parsed predicates as the chunk's `match_predicates` metadata.
5. Attach the other parsed fields as their respective metadata keys.

---

## 7. Implementation Status

Phase 1 is shipped: the evaluator module, predicate set, response schemas, and KB-tool wiring all exist on `main`. Phase 2 — per-turn integration into the milestone engine — is still pending; until it lands, `IndicatorEvaluator` is callable but the investigation loop does not yet invoke it. Ingestion-side pieces live in the sibling [`faultmaven-kb-toolkit`](https://github.com/FaultMaven/faultmaven-kb-toolkit) repo and are tracked there.

| Component | Location | Status |
|---|---|---|
| Evaluator module | `faultmaven/core/investigation/indicator_evaluator.py` | Shipped |
| Predicate implementations (`absent`, `contains`, `exit_code`, `threshold`) | `faultmaven/core/investigation/indicator_evaluator.py` | Shipped |
| `CauseChunk` / `IndicatorResult` / `CauseMatch` / `CauseMatchResult` schemas | `faultmaven/core/investigation/cause_schemas.py` | Shipped |
| `CauseChunk` on KB tool response | `faultmaven/modules/agent/tools/kb_qa.py` | Shipped |
| Comment-stripping at ingestion | `kb_toolkit/core/chunker.py` (sibling repo) | Tracked in kb-toolkit |
| Per-Cause metadata attachment | `kb_toolkit/core/ingester.py` (sibling repo) | Tracked in kb-toolkit |
| Per-turn integration into milestone engine | `faultmaven/core/investigation/milestone_engine.py` | Pending |

See [investigation-lifecycle-logic.md §1.4](./investigation-lifecycle-logic.md#14-automatic-milestone-tracking-and-stage-transitions) for where Indicator resolution fires within the per-turn flow.
