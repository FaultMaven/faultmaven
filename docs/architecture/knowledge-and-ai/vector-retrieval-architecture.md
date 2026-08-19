# Vector Retrieval Architecture

**Document Type:** Component Specification
**Version:** 1.0
**Last Updated:** 2026-07-15
**Status:** Current (post RAG revamp)

---

## Purpose

This document describes the shared vector retrieval infrastructure that underlies both the Knowledge Base and Evidence systems in FaultMaven. It covers the embedding model, the two-stage retrieval and reranking pipeline, the distinct collection strategies for each retrieval domain, and the current implementation status.

For the content structure and quality rules governing what goes into the KB, see [runbook-content-architecture.md](./runbook-content-architecture.md). For the evidence preprocessing pipeline that feeds the evidence vector store, see [data-preprocessing-design-specification.md](../data-processing/data-preprocessing-design-specification.md). For how retrieved content reaches the LLM during investigation, see [orchestration-capabilities.md](../investigation-engine/orchestration-capabilities.md).

---

## 1. Overview

FaultMaven uses ChromaDB as its vector database with BGE-M3 embeddings across both retrieval domains. The same infrastructure serves fundamentally different data:

| Domain | Collection Pattern | Data Characteristics | Role in Investigation |
|--------|--------------------|---------------------|----------------------|
| **Knowledge Base** | `faultmaven_kb` (single, permanent) | Pre-built runbooks and best practices; stable; curated before investigation | Remediation — answers "how do we fix it?" |
| **Evidence** | `case_{case_id}` (per-case, ephemeral) | Logs, configs, metrics uploaded during investigation; dynamic; case-specific | Diagnosis — answers "what is happening?" |

Both domains share the same embedding model and ChromaDB instance, but diverge on collection strategy, chunking parameters, lifecycle, and retrieval tooling. The separation is intentional — court evidence and the law both inform a judgment, but they are governed by entirely different rules.

### Design Principle: Extension Context as Retrieval Advantage

FaultMaven's browser extension provides rich implicit context that most RAG systems lack. When an SRE investigates a Kubernetes pod crash, they don't type "show me runbooks for namespace=production, service=payment-gateway." They paste the error and expect the system to figure it out. The extension knows the page, the service, the error class, the technology stack.

**Pre-retrieval filtering on this context should be the default path, not an optimization.** Every design decision should be evaluated through the lens of "does this exploit the context we uniquely have access to?" The `context_metadata` parameter in `hybrid_search()` exists for this purpose. Case-derived context (the affected service from the case's problem verification) is wired end-to-end today as a **soft** rerank boost: the engine derives it (`derive_kb_context_metadata()`), carries it on `ToolContext.kb_context_metadata`, and `KBToolAdapter` threads it through `AnswerFromKB` → `DocumentQATool` → `hybrid_search(context_metadata=…, filter_mode="soft")`. The remaining, higher-confidence integration is the copilot page-comprehension → API request body → `ToolContext` path that would justify the **hard** pre-filter (`filter_mode="hard"`); that cross-repo wiring is deferred.

The service-metadata signal rides **only** on the agent QA tools path, which is the sole caller of `hybrid_search()`. The engine's own KB pre-fetch — `_prefetch_kb_context` (which also feeds the KB cause seeder) — takes a different route: `KnowledgeService.search_knowledge` → `KnowledgeVectorStore.search`, a single-pass pure-vector search (cosine similarity, `score = 1.0 − distance / 2`) with **no reranker and no metadata-match boost**. So the prefetch/seeder path ranks by plain retrieval score, and the case's affected-service signal does not influence it. Wiring the signal there is possible future work (tracked as tech-debt issue #710).

```text
ChromaDB Instance
│
├── faultmaven_kb               # All KB tiers (global, team, personal) — permanent
│   ├── scope=global
│   └── scope=personal, owner_id=...   # team shares stay on this floor;
│       #  team visibility comes from the resource_shares id-allowlist, not metadata
│
├── case_{uuid}                 # Per-case evidence — ephemeral, tied to case lifecycle
├── case_{uuid}                 # ...
└── ...
```

---

## 2. Embedding Model

| Property | Value |
|----------|-------|
| Model | BGE-M3 |
| Library | sentence-transformers 3.0.1+ |
| Dimensions | 1024 |
| Language support | Multilingual |
| Similarity metric | Cosine — computed from an `l2` HNSW index (see below) |
| Loading | Globally cached via `model_cache.get_bge_m3_model()` |

**The index space is `l2`, not `cosine`, and scores are converted.** No collection declares `hnsw:space`, so every one uses ChromaDB's default `l2`, whose distance is *squared* euclidean. Because BGE-M3 output is L2-normalized, that distance is `2 − 2·cos`. Ranking by it is identical to ranking by cosine **on the pure-vector path** — but the score is not cosine until converted, and any consumer that combines it with another signal or compares it to a constant is affected. `faultmaven/infrastructure/vector_similarity.py` holds the one conversion (`cos = 1 − distance / 2`) that every store calls.

Getting this wrong is invisible in ranking and only shows up in an *absolute* comparison. It did: four stores used `1 − distance`, which is `2·cos − 1`, so the KB relevance threshold — documented as a cosine floor of 0.3 — was really a cosine floor of 0.65 and refused correctly-retrieved on-topic queries ([#1072](https://github.com/FaultMaven/faultmaven/issues/1072)). Declaring `cosine` is **not** an alternative fix: ChromaDB silently ignores a configuration space that disagrees with an existing collection, so it changes nothing without a full reindex.

BGE-M3 is the canonical embedding model for both KB ingestion and evidence vectorization. The model is loaded once and cached for the process lifetime. The 1024-dimensional space provides strong semantic resolution for technical text including error messages, log fragments, and procedure descriptions across multiple languages.

**Embedding dimensions reference:**

| Dimensions | Source | Status |
| ---------- | ------ | ------ |
| **1024** | BGE-M3 via `model_cache.get_bge_m3_model()` | **Active** — canonical for both KB and evidence retrieval (`KnowledgeItem.EMBEDDING_DIMENSIONS = 1024`) |
| 384 | ChromaDB default embedding | **Not used.** No read or write path falls back to it |

BGE-M3 is the only embedding space, on both sides of every collection. There is no second one to fall back to: ChromaDB pins a collection's dimension on its first write, so a default-embedded write is unsearchable by a BGE-M3 query *and* blocks every later BGE-M3 write to that collection on dimension. A query issued in the other space is not a degraded answer to the right question, it is a different question asked of a space nothing was indexed in — and the caller cannot tell it from "searched and found nothing".

---

## 3. Two-Stage Retrieval and Reranking Pipeline

This pipeline is implemented in `infrastructure/knowledge/knowledge_vector_store.py` and is currently used by the KB retrieval path (`KnowledgeVectorStore.hybrid_search`). It replaces the previous single-pass vector search.

### Stage 1: Recall

Both arms query the same collection and scope filter, and **share a single query embedding**. The query text is embedded once in `hybrid_search()` and that vector is passed into every ChromaDB call below — the recall query and each keyword probe alike. This is a latency property, not a ranking one: BGE-M3 runs locally on CPU at 1.2–2.3s per call, so embedding inside the keyword loop (as the code did until the vector was hoisted) made the dominant cost of a KB lookup scale with keyword count while recomputing an identical vector each time.

The arms run sequentially rather than concurrently. Once the embedding is shared, each ChromaDB query costs roughly 40ms, so there is no longer anything material to parallelize.

**Query A — Pure vector search:** Retrieves `k * 3` candidates (minimum 15) ranked by cosine similarity. This is the broad semantic net.

**Query B — Keyword-constrained vector search:** For each extracted keyword, runs a vector search with `where_document={"$contains": keyword}` to require that the keyword is present in the chunk text. Results are then ranked by cosine similarity within that filtered set. Up to 3 keywords are used, so this arm issues up to 3 separate ChromaDB queries; results from all keyword passes are deduplicated. Every probe reuses the one query vector — only the `where_document` filter differs between them.

> **Important:** The keyword gate is binary `$contains`, not BM25. There is no term frequency or inverse document frequency scoring. The value of this path is catching identifier matches — error codes, service names, CamelCase tokens — that pure embedding search tends to underweight.

Keywords are extracted heuristically, with identifier-like tokens (error codes matching `ERR-\d+`, CamelCase names like `CrashLoopBackOff`, dotted names like `java.lang.OutOfMemoryError`) prioritized over generic terms.

Results from both queries are merged by deduplication, keeping the higher cosine score for any chunk that appears in both.

### Stage 2: Reranking

Each candidate chunk is scored across four weighted signals:

| Signal | Weight | Computation |
|--------|--------|-------------|
| Vector similarity | 40% | Cosine score from ChromaDB, converted per §above (see note) |
| Term overlap | 25% | Fraction of non-stop-word query terms found in chunk text (binary, not TF-IDF) |
| Metadata match | 20% | Domain/service alignment with `context_metadata` + verification status bonus/penalty |
| Freshness | 15% | Half-life decay: `1 / (1 + age_days / 365)` based on `last_updated` |

**The 40% is only 40% now.** Signal 1 reads the store's score directly, so before #1072 it carried `2·cos − 1`. The `−1` is constant across candidates and drops out of the ordering, but the *slope* did not: the vector signal moved the composite by `0.8·cos` while the other three — genuine 0–1 quantities — moved it by their stated weights. The vector signal was effectively double-weighted, and these four numbers never described the blend they configured. Correcting the conversion makes them accurate for the first time; measured against the shipped KB, top-5 ordering changes on roughly a third of queries, always as a reshuffle within the same document set rather than a different set of runbooks.

The weights have **not** been retuned on the corrected scale, deliberately. Picking new ones without a measured relevance judgement would repeat the error #1072 was: a plausible number, reasoned rather than observed. They now mean what they say, which is the precondition for tuning them, not a substitute for it.

**Metadata match scoring details:**

| Condition | Score delta |
|-----------|-------------|
| Chunk domain matches case context domain | +0.30 |
| Chunk service matches case context service | +0.30 |
| Status is `verified` | +0.40 |
| Status is `in-review` | +0.10 |
| Status is `draft` | -0.10 |
| Status is `stale` | -0.20 |
| Status is `deprecated` | -0.30 |

**Context metadata: hard filter vs soft boost.** When the extension provides high-confidence context (e.g., the user is on a PostgreSQL dashboard), domain/service should be applied as a **hard pre-filter** in the ChromaDB `where` clause — like scope filtering. Irrelevant chunks (Kubernetes runbooks for a PostgreSQL issue) should never enter Stage 1. When confidence is low or context is ambiguous, fall back to the soft rerank boost (+0.30) described above. The `filter_mode` parameter on `hybrid_search()` is implemented — `"hard"` adds domain/service to the `where` clause (`_apply_hard_metadata_filter`), `"soft"` (default) applies the rerank boost only. The **soft** path is wired end-to-end: the engine feeds the case's affected service into `hybrid_search(context_metadata=…, filter_mode="soft")` on every KB retrieval, so the metadata-match signal fires on service alignment rather than status alone. What remains is the **hard** path — threading *high-confidence* context from copilot → API → `KBToolAdapter` so a caller can safely select `"hard"` and drop irrelevant chunks pre-retrieval. Today every live caller uses the soft rerank path. (Domain is not yet supplied by the engine: the case model has no domain field, and a fabricated default would create false exact-matches; only `service` is currently derived.)

**Tiebreaking:** When two chunks produce the same weighted score, scope priority breaks the tie: personal > team > global. This ensures a user's own runbook surfaces above a generic global procedure when both are equally relevant.

The `k` highest-scoring chunks are returned.

### Dual Retrieval Paths (Planned)

The latency budget differs by context. During an active incident, an SRE waiting 8 seconds will abandon the tool. During post-incident review or agent investigation loops, 30 seconds is acceptable.

| Path | When | Strategy | Target Latency |
|------|------|----------|----------------|
| **Fast** | Interactive copilot queries | Metadata-filtered vector search → top-5 → generate. Skip keyword recall and reranking. Extension context metadata does the heavy lifting. | < 2s retrieval |
| **Deep** | Agent investigation loops (DA mode) | Full hybrid search → rerank top-20 → top-5 → generate with reasoning chain. Latency amortized across investigation turns. | < 5s retrieval |

Currently only the deep path is implemented. The fast path would be a `search_mode="fast"` on `KBConfig` that bypasses Stage 2 reranking and relies on pre-retrieval metadata filtering for precision.

### Dynamic Hybrid Weights

The natural-language reranker weights are 40/25/20/15. SRE queries vary widely: pasting `CrashLoopBackOff` is a lexical match problem (text weight should dominate), while asking "why is my service slow after deploying the new cache layer" is a semantic problem. A fixed ratio would underweight lexical match for the exact-identifier queries that are extremely common in SRE workflows.

The reranker therefore detects identifier-like tokens in the query (error codes, CamelCase names, dotted names, status codes, file paths) via regex (`_rerank`, `RERANK_WEIGHT_*_ID` constants). When present, it shifts term overlap weight from 25% to 40% and reduces vector similarity from 40% to 25%. No ML needed — pattern detection is sufficient.

---

## 4. Knowledge Base Retrieval

### Collection and Scope

The KB pipeline queries a single ChromaDB collection (`faultmaven_kb`) with a metadata-`where` clause that filters by scope. The clause shape and the scope-safety invariant that guards it are canonical in [knowledge-base-architecture.md](./knowledge-base-architecture.md) — see "Single Collection with Metadata Filtering" and "Federated Search: Implementation". This section covers what the *retrieval pipeline* does with that input; the storage rules live in KB-arch.

`KnowledgeVectorStore.search()` and `hybrid_search()` accept the scope-`where` from the caller and reject any KB query that doesn't carry one (`ValueError`). Case evidence collections (`case_*`) are exempt from this check.

### Runbook Similarity: One Shared Collection, the KB Scope Model

Runbooks have **no collection of their own** and **no parallel index**. `RunbookKnowledgeBase` is constructed over the same `ChromaDBVectorStore` (`faultmaven_kb`) described above, and reads the rows the one live KB writer (`KnowledgeService._index_document_in_vector_store`, reached from `ingest_runbook`) puts there. Two predicates do all the work:

- **`document_type == "runbook"` separates runbooks from other KB documents.** It is what the live writer stamps on every runbook chunk. (The retired `report_type` predicate matched only rows a dead write path would have written, so dedup could only return `[]` — fm#1030.)
- **The caller-supplied KB scope filter is the isolation.** A similarity query names no id and no owner, so the same visible-id allowlist as every other KB read (`build_kb_scope_filter`: global ∪ owned ∪ team-shared, ADR-011 D3) is the only thing keeping one principal's personal runbooks out of another's results. The filter is built by the CALLER — which principal governs is a per-call-site decision (requester on the report-recommendation route, case owner in the engine) — and `search_runbooks`/`search_by_text` **refuse a falsy filter with a typed error rather than querying unscoped**.

The clause is `{"$and": [{"document_type": "runbook"}, <scope_filter>]}` — the scope filter composes as one operand whether it is a bare single condition (global-only) or an `$or` of arms. ChromaDB (>= 1.0) validates that a `where` mapping carries exactly one operator, so the multi-key implicit-AND form is rejected outright.

One runbook is N chunk rows, so the search fetches `top_k × 3` chunks, collapses by `parent_document_id` taking the **max** chunk similarity per runbook, and returns the top `top_k` distinct runbooks as honest KB-item references (`item_id`, `title`, `scope`, `similarity_score`).

Full invariants: [runbook-dedup.md](./runbook-dedup.md). The id half of the isolation rule is `get_document_visible` / `get_suggestion_visible` ([rbac.md "Tenant-Scoped Resolution"](../security/rbac.md#tenant-scoped-resolution)), and the allowlist half is the `organization_id` predicate on `resource_shares` in both directions of the share resolution.

### Chunking Strategy

KB documents use structure-aware chunking. The ingestion pipeline splits on markdown structural boundaries:

- Primary split points: `##` and `###` headers, horizontal rules (`---`)
- Chunk size bounds: 100–3000 characters (variable, not fixed)
- Tiny sections (below 100 characters) are merged with the adjacent section
- Oversized sections are split at line boundaries

YAML frontmatter is stripped before chunking — a runbook with 300 chars of frontmatter doesn't waste its first chunk on metadata that adds no retrieval value.

This approach preserves the semantic coherence of runbook sections. A diagnostic step does not share a chunk with an unrelated prevention note because the markdown structure itself draws the boundary.

### Metadata Per Chunk — Retrieval Use

The full list of fields stored on each chunk is canonical in [knowledge-base-architecture.md "Metadata Stored Per Chunk"](./knowledge-base-architecture.md#metadata-stored-per-chunk). The retrieval pipeline consumes a subset of those fields:

| Field | Used by | Purpose |
| ----- | ------- | ------- |
| `scope`, `owner_id`, `parent_document_id` | `where` clause | Scope filter (built by `build_kb_scope_filter`; team arm is a `parent_document_id` `$in` allowlist resolved from `resource_shares`, not a `team_id` metadata match) |
| `domain`, `service` | Reranker metadata-match signal (soft boost, wired) + hard pre-filter (`filter_mode="hard"`, mechanism only) | `service` fed from the case's `problem_verification.affected_services[0]` as a soft boost; `domain` not yet supplied by the engine |
| `symptom_class`, `severity` | Reranker metadata-match signal | Boost chunks whose taxonomy aligns with the query's failure-mode classification |
| `status` | Reranker status weighting | `verified` +0.40, `in-review` +0.10, `draft` -0.10, `stale` -0.20, `deprecated` -0.30 |
| `last_updated` | Reranker freshness signal + synthesis prompt | Half-life decay; `format_chunk_metadata()` injects age warnings into LLM context |

### Staleness-Aware Synthesis

The `UnifiedKBConfig.format_chunk_metadata()` method computes staleness at retrieval time from the `last_updated` field and injects warnings directly into the chunk context text that the synthesis LLM sees:

- Age > 180 days: `STALE (N days old)` warning prepended to chunk context
- Age > 90 days: `Last updated: N days ago` note included
- `deprecated` status: penalty applied in reranker; content should be excluded from the collection via lifecycle governance

The synthesis LLM's system prompt (in `UnifiedKBConfig.system_prompt`) explicitly instructs the model to warn users when chunks are stale or in draft status, and to prefer verified, recently-updated content. This means staleness warnings propagate naturally to the user response without special agent-side handling.

### Tool Path

```text
Agent calls: answer_from_kb(question)
  │
  ├── KBToolAdapter.execute_with_context()
  │     Extracts user_id, team_ids, and kb_context_metadata from ToolContext
  │
  ├── AnswerFromKB._arun(question, user_id, team_ids, context_metadata)  # kb_qa.py
  │     Builds combined $or scope filter
  │
  ├── DocumentQATool.answer_question(..., context_metadata)
  │     Detects search_mode="hybrid" from UnifiedKBConfig
  │
  ├── KnowledgeVectorStore.hybrid_search(context_metadata, filter_mode="soft")
  │     Stage 1: vector + keyword recall
  │     Stage 2: rerank with 4-signal scoring (metadata match uses context)
  │
  ├── Relevance gate: refuse synthesis if max chunk score < 0.5 (cosine)
  │     Returns "searched, nothing close enough" WITHOUT calling the LLM
  │
  ├── LLMRouter.route() with UnifiedKBConfig.system_prompt
  │     max_tokens=SYNTHESIS_MAX_TOKENS (2000), temperature=0.3
  │     Plain chat call: no tools, no response_format
  │     Synthesis guided by staleness-aware system prompt
  │
  └── UnifiedKBConfig.format_response()
        Returns answer with source citations
```

**Engine pre-fetch path (no rerank).** The Tool Path above is the *only* caller that reaches `hybrid_search()` and therefore the *only* path carrying the service-metadata soft boost. The engine's `_prefetch_kb_context` — the symptom-verification KB pull that also feeds the KB cause seeder — instead calls `KnowledgeService.search_knowledge` → `KnowledgeVectorStore.search`: single-pass pure-vector similarity, no keyword recall, no four-signal reranker, no `context_metadata`. Its ranking is plain retrieval score; the case's affected-service signal is not applied. Bringing the signal onto that path is a possible future refinement (tech-debt #710).

**The synthesis answer ceiling.** `DocumentQATool.SYNTHESIS_MAX_TOKENS` (2000) is not the limit that actually binds a KB answer. Whatever the synthesizer writes is wrapped by `MilestoneEngine._format_tool_result()` — about 590 characters of relay instruction and citation guidance — and the combined string is then truncated to `MilestoneEngine.TOOL_RESULT_MAX_CHARS` (8000) before it re-enters the model's context. That leaves roughly 7,410 characters for the answer itself, about 1,850 tokens at the 3.9–4.1 characters per token measured on real KB answers. The token budget therefore already exceeds what the pipeline will accept, and raising it on its own is inert: the surplus is generated, billed, and then discarded. Because the two constants live in different modules with nothing structural holding them together, `tests/unit/modules/agent/tools/test_kb_synthesis_budget.py` pins their agreement in both directions — the budget must be able to fill the character cap, and must not reach far past it. Change them together or not at all.

**The answer is told its allowance.** Sizing the two constants against each
other keeps them consistent; it does not make the *model* aware of either. The
synthesis prompt asks for full diagnostic steps and resolution procedures and
says to compress background rather than actionable steps — with no length
target to apply that rule against — so the answer was written to whatever the
material wanted and the surplus removed downstream. Measured over one
simulation run, 3 KB answers in 5 overflowed the relay allowance, by 540–1249
characters. `DocumentQATool.KB_ANSWER_RELAY_CHARS` (7,000) is that missing
target, stated in the prompt so the model spends the budget deliberately. It
sits below the 7,410-character relay budget because `format_response` appends
the `Sources:` line to the answer before it is wrapped. It is deliberately
**not** imported from `milestone_engine` — the engine imports this package's
tools, so the dependency runs one way only — and is pinned against the engine's
cap by the same cross-module test.

**When it still overflows, the cut takes the middle.** The relay trim keeps the
head, which for this payload deletes the remediation steps the prompt exists to
preserve and the `Sources:` line the relay suffix instructs the model to cite
"from the content above". `MilestoneEngine.KB_QA_ANSWER_TAIL_SHARE` (0.35)
reserves the answer's ending instead, so an oversized answer loses background
from its middle — marked with a count — rather than its procedure's conclusion.
This is scoped to `kb_qa`; every other tool result keeps the plain head-first
cut, because their content is data rather than a procedure and their tools
already budget themselves against the cap.

Both of the engine's cut sites elide, not only the formatter. PII redaction
runs between them and *expands* text — an IPv4 becomes a 29-character
placeholder — so an answer the formatter sized exactly to the budget
re-crosses the cap and `_truncate_tool_result` fires. That site preserves the
relay suffix, but cutting the answer head-first there would discard the same
remediation and source line one step later.

The allowance itself belongs to the KB config, not to the synthesis prompt.
`AnswerFromCaseEvidence` subclasses `DocumentQATool` and shares the prompt, but
its results are not relayed through the `kb_qa` branch — no wrapper, no elide,
a plain cut at the full cap — so `KBConfig.answer_char_allowance` returns
`None` there and the length rule is simply not stated. A KB states a number
only when it has one.

Answers do reach this ceiling in practice. Observed synthesis answers run 5,261–7,729 characters, and the longest of those was truncated by the engine rather than by the token budget. Whether 8,000 characters is the right allowance for a runbook procedure inside the investigation context is a separate question from the budget's internal coherence, and remains open.

**The relevance gate, and how its threshold is set.** `UnifiedKBConfig.relevance_threshold` (0.5, cosine) refuses synthesis when no retrieved chunk clears it, so the synthesizer is never asked to ground an answer in chunks that merely share vocabulary — the canonical case being a ZooKeeper query landing on Kafka chunks via "leader election". Evidence retrieval opts out (`CaseEvidenceConfig` returns `None`): for forensic analysis the closest available content is always worth returning.

The threshold is **derived from a measured distribution, not reasoned from first principles**, because reasoning produced a wrong number twice in #1072 — once in the conversion, once in the premise. BGE-M3 does not send unrelated text toward orthogonality; against the shipped 91-runbook KB, in true cosine:

| Query class | Observed cosine |
|---|---|
| On-topic, correct runbook at rank 1 | 0.591 – 0.750 |
| Off-topic, unrelated domain | 0.358 – 0.413 |
| Off-topic, adjacent vocabulary (ZooKeeper → Kafka) | 0.477 |

0.5 is the lowest value that still rejects the adjacent-vocabulary case, and clears the weakest on-topic observation by 0.09. It is biased low on purpose: a false refusal is a positive false claim about KB coverage, while a false accept only puts loosely related runbooks in front of a synthesizer already told to say when information is missing. **Re-derive it against both populations if the embedding model or corpus changes — do not nudge it.** A relative test (top-1 vs top-k spread) was evaluated and rejected: off-topic spread measured *wider* than on-topic (0.061 vs 0.023), which inverts the signal.

Two properties of the gate worth knowing. First, the score it reads is always the raw per-chunk cosine, even in hybrid mode — `_rerank` computes its four-signal composite to sort by and never writes it back, so the keyword and term-overlap legs reorder results but cannot lift one over the floor. That is deliberate: the threshold is calibrated in cosine, and the composite is not a scale anyone has calibrated. Second, the refusal text says only that the search returned nothing close enough; it does **not** claim the KB lacks coverage. A similarity score is not evidence about what the KB contains, and asserting otherwise is what made the mis-scaled threshold actively harmful rather than merely unhelpful — the investigating model was handed a false statement about its own knowledge base and told to stop asking (same rule as #943).

**Relay vs synthesis:** Full procedure detail must reach the engine — a compressed paraphrase of a runbook loses the actionable steps. The `DocumentQATool` synthesis prompt is aligned with this relay intent: it instructs the LLM to "preserve procedural detail — include full diagnostic steps, commands, and resolution procedures rather than summarizing them" and to "compress only background context, never actionable steps." `UnifiedKBConfig.system_prompt` reinforces this ("provide step-by-step instructions when procedures are available"), and `_format_tool_result()` wraps the `kb_qa` result with "Preserve key details, diagnostic steps, and resolution procedures — do NOT collapse it into a single sentence." All three now pull in the same direction; the earlier "be concise and factual" instruction that conflicted with relay has been removed.

---

## 5. Evidence Retrieval

Evidence retrieval is governed by the data preprocessing architecture and is documented in detail in [data-preprocessing-design-specification.md](../data-processing/data-preprocessing-design-specification.md). This section summarizes the vector retrieval layer specifically.

### Collection Strategy

Each case gets its own ChromaDB collection: `case_{case_id}`. Collections are ephemeral — they exist for the duration of the case and are cleaned up as part of case archival.

### Chunking Parameters

> **Implementation note:** The parameters below describe the target design. The current implementation uses 4000-token chunks with 200-token overlap (`ChunkingService`). See §7 for implementation status.

Evidence should use smaller chunks for retrieval precision, with context provided at query time rather than baked into the embedding:

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Chunk size | 512 tokens | Small enough for precise semantic matching — an error event isn't diluted by surrounding mundane log lines |
| Overlap | 50 tokens | Minimal overlap; evidence boundaries are structural (timestamps, blank lines), not semantic |
| Context window | ±10 lines at retrieval time | Forensic context provided in tool results, not in the embedding. Same pattern as `search_file` keyword results. |
| Split strategy | Type-aware via chunk type discriminator | Logs: temporal window boundaries. Configs: section/key boundaries. Metrics: anomaly window boundaries. |
| Implementation | `modules/preprocessing/chunking_service.py` | — |

**Design rationale:** Once evidence is vectorized (Tier 4), it becomes the primary search path for all subsequent semantic queries — follow-up questions, agent-initiated correlations, cross-file analysis. The embedding must be precise enough to surface the right 512-token window when the user asks "what happened at 14:32?" Forensic context (the surrounding log entries) is retrieved at query time by expanding the matched chunk's position in the original file, not by inflating the chunk size.

This follows the same principle as KB chunking: embed at the granularity of a single coherent unit (a runbook section, a log event window, a config block), not at the granularity of a page.

### 4-Tier Evidence Escalation

Vector search (Tier 4) is the most expensive tier and is not the first resort. The full escalation path:

| Tier | Mechanism | Cost | Trigger |
|------|-----------|------|---------|
| 0 + 1 | Structural indexing (11 domain extractors, runs on upload) | $0 | Always, on upload |
| 2 | Keyword/regex search on raw files (`search_file`) | $0 | Agent tool call |
| 3 | LLM-interpreted analysis on file sections (`deep_analysis`) | ~$0.01 | Agent tool call |
| 4 | Chunk + embed + semantic search (`answer_from_case_evidence`) | ~$0.05+ | See vectorization triggers below |

### Vectorization Triggers

Vectorization of evidence files is triggered by two paths:

**Proactive:** Files above the vectorization size threshold are submitted for background vectorization when DA (Directed Analysis) mode starts for that evidence item. This ensures the vector index is ready before the agent exhausts keyword and deep analysis attempts.

**Reactive fallbacks:** If proactive vectorization did not complete or was not triggered:
- 3 or more empty `search_file` results against a file
- Tool execution timeout
- Low confidence score (< 0.2) from `deep_analysis`

The `searchable="true"` attribute on evidence XML in the context builder signals to the agent that a file has been indexed and `answer_from_case_evidence` can be called against it.

### Evidence vs. KB — Why Different Strategies

| Aspect | KB (Runbooks) | Evidence (Logs, Configs, Metrics) |
|--------|--------------|-----------------------------------|
| Chunking | Structure-aware, 100–3000 chars | Type-aware, 512 tokens with 50-token overlap + context window at retrieval |
| Lifecycle | Permanent | Ephemeral (per-case) |
| Collection | Single shared (`faultmaven_kb`) | Per-case (`case_{id}`) |
| Scope enforcement | Mandatory scope filter (invariant) | Scoped by case ownership |
| Retrieval pipeline | Hybrid search + reranking | Vector search (no reranking) |
| Primary role | Remediation | Diagnosis |

---

## 6. Tool Result Formatting

### KB Tool (`answer_from_kb`)

The design intent is relay — full procedure detail should reach the engine. The `_format_tool_result()` wrapper directs the LLM to "Preserve key details, diagnostic steps, and resolution procedures — do NOT collapse it into a single sentence," and the inner `DocumentQATool` synthesis prompt is aligned with it ("preserve procedural detail … compress only background context, never actionable steps"). See §4 "Relay vs synthesis" for the full chain.

Source citations use document titles: `Sources: Kubernetes CrashLoopBackOff, PostgreSQL Connection Pool Exhaustion`.

### Evidence Tool (`answer_from_case_evidence`)

The synthesis prompt instructs the LLM to cite with forensic precision: filename, line number, and timestamp. Citation format: `In filename, line 42: ...`.

The system prompt for `CaseEvidenceConfig` is explicitly forensic — it instructs the model to cite exact line numbers, include error messages verbatim, preserve chronological order, and distinguish severity levels.

### Evidence-vs-Knowledge Boundary

A strict rule governs what can be added as case evidence (`evidence_to_add`): only from user-submitted data. Evidence is never sourced from KB content, web searches, or LLM-generated inference. This boundary is enforced in the agent system prompt and is fundamental to the diagnosis/remediation separation.

### Citation Confidence Tiers (Planned)

Not all claims carry equal evidentiary weight. A future improvement is to distinguish three tiers in agent responses:

1. **Direct evidence** — a retrieved chunk directly states the claim. High confidence, single source citation.
2. **Correlated evidence** — multiple retrieved chunks together support an inference (e.g., "Logs show OOM events correlating with the deployment at 14:32, and metrics show memory climbing from 14:15"). Medium confidence, multiple citations, agent should show reasoning chain.
3. **Speculative synthesis** — the agent connects dots not explicitly connected in any source. Low confidence, explicitly flagged as hypothesis.

This maps onto FaultMaven's existing hypothesis lifecycle (CAPTURED → ACTIVE → VALIDATED/REFUTED) where confidence scoring already distinguishes verified from speculative findings. Extending this to citation-level confidence would make the trust boundary explicit in the UX.

---

## 7. Implementation Status

### KB Retrieval Pipeline

| Feature | Status | Notes |
|---------|--------|-------|
| Hybrid search (Stage 1 + Stage 2) | **Implemented** | `KnowledgeVectorStore.hybrid_search()` |
| Binary keyword search | **Implemented** | `$contains` via `where_document` — not BM25 |
| Four-signal reranker | **Implemented** | Vector, term overlap, metadata match, freshness |
| Dynamic hybrid weights | **Implemented** | Identifier-heavy queries shift term overlap to 40%, vector to 25% |
| Hard pre-filter mode | **Implemented (mechanism only)** | `filter_mode="hard"` injects domain/service into the ChromaDB where clause (`_apply_hard_metadata_filter`). Not yet invoked end-to-end — see "Extension context → KB metadata filters" below. |
| Fast search mode | **Not done** | Declared in the `search_mode` property docstring, but no config returns `"fast"` and nothing dispatches it. Planned low-latency path (§3 Dual Retrieval Paths). |
| Scope tiebreaking | **Implemented** | personal > team > global secondary sort in `_rerank()` |
| Staleness-aware synthesis | **Implemented** | `_staleness_note()` + system prompt: "provide step-by-step instructions when procedures are available" (the "preserve procedural detail" instruction is in the synthesis prompt — see §4 "Relay vs synthesis") |
| Scope safety (pre-filtering) | **Implemented** | ChromaDB `where` clause pre-filters before ANN search. `_enforce_scope_invariant()` raises `ValueError` on unscoped queries. |
| Case context → KB soft rerank boost | **Implemented** | Engine derives the affected service (`derive_kb_context_metadata()`) onto `ToolContext.kb_context_metadata`; threaded through `KBToolAdapter` → `AnswerFromKB` → `DocumentQATool` → `hybrid_search(context_metadata=…, filter_mode="soft")`. `service` only; `domain` not yet supplied by the case model. |
| Copilot high-confidence context → hard pre-filter | **Deferred** | `hybrid_search()` accepts `context_metadata` + `filter_mode="hard"`, but no live caller selects `"hard"`. Requires copilot page-comprehension → API → `KBToolAdapter` cross-repo wiring. |
| True BM25 | **Not done** | ChromaDB does not expose BM25. Binary `$contains` is a partial substitute. Would need `rank_bm25` lib or separate index. |
| Cross-encoder reranker | **Not done** | Would add a dedicated reranking model (e.g., `ms-marco-MiniLM`) between retrieval and synthesis. Adds model dependency + latency. |
| Citation grounding | **Not done** | No post-generation verification that cited chunks support the claims attributed to them. |
| Result caching | **Not done** | `KBConfig.cache_ttl` exists but nothing reads it — no caching layer wraps `hybrid_search()` or `answer_question()`. |

### KB Ingestion & Storage

| Feature | Status | Notes |
|---------|--------|-------|
| Structure-aware KB chunking | **Implemented** | `ContentChunker` — markdown header splits, 100–3000 chars. Used by the conversion-drafts / document ingestion path (`_index_document_in_vector_store()`). The startup bootstrap does **not** chunk: it ingests the KB pack's pre-chunked, pre-embedded vectors directly. |
| Explicit BGE-M3 embeddings (KB) | **Implemented** | All KB index and query paths use `model_cache.get_bge_m3_model()` (1024 dims). No ChromaDB default embedding in KB paths. |
| Metadata enrichment | **Implemented** | domain, service, status, severity, symptom_class stored per chunk at ingestion, used in reranker. The document inventory lives in SQLite: `knowledge_items` for ingested runbooks (built-ins from the pack + activated drafts), `conversion_drafts` for pending (unverified) drafts. |
| SQLite document inventory | **Implemented** | `list_documents()`, `get_document()`, `delete_document()` use SQLite, not ChromaDB. |
| Redis removed from KB lifecycle | **Implemented** | No Redis reads or writes for KB documents. SQLite is the sole inventory. |
| Chunk-aware deletion | **Implemented** | `delete_documents_by_parent_id()` removes all chunks for a document. |
| Batch activation endpoint | **Implemented** | `POST /knowledge/drafts/verify-batch` — batch activate up to 100 drafts per request. |
| Startup KB scan | **Removed** | The server no longer scans a directory on startup — KB ingestion is owned by the bootstrap (KB pack). `POST /knowledge/scan` remains as a manual draft-reconciliation endpoint, not an automatic ingestion path. |

### Evidence Retrieval

| Feature | Status | Notes |
|---------|--------|-------|
| Evidence 4-tier escalation | **Implemented** | See `data-preprocessing-design-specification.md` |
| Proactive vectorization | **Implemented** | Background trigger at DA mode start for large files |
| Evidence embedding consistency | **Implemented** | Case evidence collections now use explicit BGE-M3 (1024 dims) for both indexing and querying, matching KB. Graceful fallback to ChromaDB default if BGE-M3 unavailable. |
| Chunk type discriminator | **Not done** | First-class `chunk_type` for type-specific chunking logic. Core to evidence chunking design (§5). |
| Evidence 512-token chunking + context window | **Not done** | Replaces current 4000-token chunks. Embed small for precision, expand at retrieval for forensic context. |
| Evidence-to-KB feedback loop | **Not done** | Product-level feature: surface KB curation suggestions from repeated evidence patterns. |

### Storage Architecture

ChromaDB is used **only for vector search** during investigations. All document CRUD (list, get, delete, statistics) uses SQLite (`conversion_drafts` + `conversion_jobs` tables). Redis is not used for KB document storage.

| Store | Purpose |
|-------|---------|
| SQLite | Document inventory, metadata, status, CRUD, dashboard filters |
| ChromaDB | Chunked vector embeddings for RAG search |
| Disk | Markdown source files in `data/knowledge/{scope}/` |

### Embedding Consistency

All paths (KB and evidence) use explicit BGE-M3 embeddings (1024 dims) via `model_cache.get_bge_m3_model()`. No active code path relies on ChromaDB's default embedding function.

**KB paths:**
- `_index_document_in_vector_store()` — chunk embeddings at ingestion
- `KnowledgeVectorStore.search()` — `query_embeddings` for vector recall
- `KnowledgeVectorStore._single_keyword_search()` — `query_embeddings` for keyword-constrained search
- `ChromaDBVectorStore.search()` — `query_embeddings` for direct search
- `RunbookKnowledgeBase.search_runbooks()` — pre-computed `query_embeddings` for dedup similarity (read-only; runbooks are written by `_index_document_in_vector_store`, the first path above)

**Evidence paths:**

- `store_in_vector_db_background()` — generates BGE-M3 embeddings, passes to `CaseVectorStore.add_documents(embeddings=...)`
- `CaseVectorStore.search()` — encodes query with BGE-M3, uses `query_embeddings`

Neither path degrades to a second embedding space when BGE-M3 is unavailable (e.g. `sentence-transformers` not installed). The read path raises `KnowledgeBaseError` via the shared `infrastructure/embedding_guard.py`; the write path writes nothing and returns `VectorIndexOutcome.EMBEDDER_UNAVAILABLE`, and `vectorize_file` reports the file as not indexed rather than as searchable. An unavailable embedder must never reach the investigating model as a statement about what the index holds.

### Superseded Code

**`KnowledgeSearchService` / `EmbeddingService` / `KnowledgeIndexingJob`** — removed. This was the previous-generation document-level indexing writer (external-API embedding via `EmbeddingService`, out-of-process batch job). It was fully superseded by the current writer — inline chunk-level ingest (`_index_document_in_vector_store` → `ContentChunker` + in-process BGE-M3) plus the pre-embedded KB pack at bootstrap — and its CLI job entry could never run. The only writer to `faultmaven_kb` is now `KnowledgeVectorStore.add_documents`.

**`ChromaDBVectorStore.list_documents()` / `get_document()`** — removed. These methods fetched chunks and deduplicated in Python. Replaced by SQLite queries in `KnowledgeService`.

**Redis KB key patterns** — removed. `upload_document()`, `get_document()`, `list_documents()`, `delete_document()` no longer read or write Redis. The `get_job_status()` method and `GET /knowledge/jobs/{job_id}` endpoint were also removed.

---

## 8. Key Files

| Component | File |
|-----------|------|
| KB vector store (hybrid search, reranker, scope invariant) | `faultmaven/infrastructure/knowledge/knowledge_vector_store.py` |
| ChromaDB store (chunk add/delete, vector search) | `faultmaven/infrastructure/persistence/chromadb_store.py` |
| Content chunker (structure-aware splitting) | `faultmaven/modules/knowledge/domain/services/content_chunker.py` |
| Knowledge service (document CRUD, ingestion) | `faultmaven/modules/knowledge/domain/services/knowledge_service.py` |
| Conversion service (scan, verify, batch) | `faultmaven/modules/knowledge/domain/services/conversion_service.py` |
| KB-neutral Q&A tool (strategy pattern) | `faultmaven/modules/agent/tools/document_qa_tool.py` |
| KBConfig interface | `faultmaven/modules/agent/tools/kb_config.py` |
| Unified KB config (hybrid mode, staleness) | `faultmaven/modules/agent/tools/kb_configs/unified_kb_config.py` |
| Unified KB tool (scope filter construction) | `faultmaven/modules/agent/tools/kb_qa.py` |
| KB and evidence tool adapters | `faultmaven/modules/agent/tools/kb_tool_adapter.py` |
| Frontmatter extraction | `faultmaven/utils/frontmatter.py` |
| Model cache (BGE-M3 global singleton) | `faultmaven/infrastructure/model_cache.py` |
| Vector metadata schema | `faultmaven/models/vector_metadata.py` |
| Evidence chunking service | `faultmaven/modules/preprocessing/chunking_service.py` |

---

## Related Documents

- **[Knowledge Base Architecture](./knowledge-base-architecture.md)** — KB storage design, 3-tier scope model, collection naming, access control, tier-by-tier ingestion details
- **[Runbook Content Architecture](./runbook-content-architecture.md)** — KB content taxonomy, runbook templates, quality gates, and lifecycle governance
- **[Data Preprocessing Design Specification](../data-processing/data-preprocessing-design-specification.md)** — Evidence classification, structural indexing, DA mode, vectorization triggers
- **[Orchestration Capabilities](../investigation-engine/orchestration-capabilities.md)** — How retrieval tools participate in the investigation engine's tool loop
