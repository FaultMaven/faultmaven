# Vector Retrieval Architecture

**Document Type:** Component Specification
**Version:** 1.0
**Last Updated:** 2026-04-01
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

**Pre-retrieval filtering on this context should be the default path, not an optimization.** Every design decision should be evaluated through the lens of "does this exploit the context we uniquely have access to?" The `context_metadata` parameter in `hybrid_search()` exists for this purpose — but wiring it end-to-end requires cross-component integration: copilot page-comprehension → API request body → `ToolContext` fields → `KBToolAdapter` → `hybrid_search()`. This is the highest-leverage remaining integration.

```text
ChromaDB Instance
│
├── faultmaven_kb               # All KB tiers (global, team, personal) — permanent
│   ├── scope=global
│   ├── scope=team, team_id=...
│   └── scope=personal, owner_id=...
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
| Similarity metric | Cosine (HNSW index) |
| Loading | Globally cached via `model_cache.get_bge_m3_model()` |

BGE-M3 is the canonical embedding model for both KB ingestion and evidence vectorization. The model is loaded once and cached for the process lifetime. The 1024-dimensional space provides strong semantic resolution for technical text including error messages, log fragments, and procedure descriptions across multiple languages.

> **Note:** The legacy `KnowledgeSearchService` references `text-embedding-3-small` (1536 dimensions) via `EmbeddingService`, and the `KnowledgeItem` model defines `EMBEDDING_DIMENSIONS = 1536`. These are from a deprecated code path (see §7 notes on `KnowledgeSearchService`). BGE-M3 at 1024 dimensions is the active implementation.

---

## 3. Two-Stage Retrieval and Reranking Pipeline

This pipeline is implemented in `infrastructure/knowledge/knowledge_vector_store.py` and is currently used by the KB retrieval path (`KnowledgeVectorStore.hybrid_search`). It replaces the previous single-pass vector search.

### Stage 1: Recall

Two ChromaDB queries run in parallel against the same collection and scope filter:

**Query A — Pure vector search:** Retrieves `k * 3` candidates (minimum 15) ranked by cosine similarity. This is the broad semantic net.

**Query B — Keyword-constrained vector search:** For each extracted keyword, runs a vector search with `where_document={"$contains": keyword}` to require that the keyword is present in the chunk text. Results are then ranked by cosine similarity within that filtered set. Up to 3 keywords are used; results from all keyword passes are deduplicated.

> **Important:** The keyword gate is binary `$contains`, not BM25. There is no term frequency or inverse document frequency scoring. The value of this path is catching identifier matches — error codes, service names, CamelCase tokens — that pure embedding search tends to underweight.

Keywords are extracted heuristically, with identifier-like tokens (error codes matching `ERR-\d+`, CamelCase names like `CrashLoopBackOff`, dotted names like `java.lang.OutOfMemoryError`) prioritized over generic terms.

Results from both queries are merged by deduplication, keeping the higher cosine score for any chunk that appears in both.

### Stage 2: Reranking

Each candidate chunk is scored across four weighted signals:

| Signal | Weight | Computation |
|--------|--------|-------------|
| Vector similarity | 40% | Original cosine score from ChromaDB (already 0–1) |
| Term overlap | 25% | Fraction of non-stop-word query terms found in chunk text (binary, not TF-IDF) |
| Metadata match | 20% | Domain/service alignment with `context_metadata` + verification status bonus/penalty |
| Freshness | 15% | Half-life decay: `1 / (1 + age_days / 365)` based on `last_updated` |

**Metadata match scoring details:**

| Condition | Score delta |
|-----------|-------------|
| Chunk domain matches case context domain | +0.30 |
| Chunk service matches case context service | +0.30 |
| Status is `verified` | +0.40 |
| Status is `community` | +0.20 |
| Status is `deprecated` | -0.30 |
| Status is `draft` | -0.10 |

**Context metadata: hard filter vs soft boost (not yet implemented).** When the extension provides high-confidence context (e.g., the user is on a PostgreSQL dashboard), domain/service should be applied as a **hard pre-filter** in the ChromaDB `where` clause — like scope filtering. Irrelevant chunks (Kubernetes runbooks for a PostgreSQL issue) should never enter Stage 1. When confidence is low or context is ambiguous, fall back to the soft rerank boost (+0.30) described above. The design calls for a `filter_mode` parameter on `hybrid_search()`: `"hard"` adds to the `where` clause, `"soft"` (default) applies as rerank boost only. Currently `context_metadata` only feeds the soft rerank path.

**Tiebreaking:** When two chunks produce the same weighted score, scope priority breaks the tie: personal > team > global. This ensures a user's own runbook surfaces above a generic global procedure when both are equally relevant.

The `k` highest-scoring chunks are returned.

### Dual Retrieval Paths (Planned)

The latency budget differs by context. During an active incident, an SRE waiting 8 seconds will abandon the tool. During post-incident review or agent investigation loops, 30 seconds is acceptable.

| Path | When | Strategy | Target Latency |
|------|------|----------|----------------|
| **Fast** | Interactive copilot queries | Metadata-filtered vector search → top-5 → generate. Skip keyword recall and reranking. Extension context metadata does the heavy lifting. | < 2s retrieval |
| **Deep** | Agent investigation loops (DA mode) | Full hybrid search → rerank top-20 → top-5 → generate with reasoning chain. Latency amortized across OODA cycles. | < 5s retrieval |

Currently only the deep path is implemented. The fast path would be a `search_mode="fast"` on `KBConfig` that bypasses Stage 2 reranking and relies on pre-retrieval metadata filtering for precision.

### Dynamic Hybrid Weights (Planned)

The current reranker weights (40/25/20/15) are fixed. SRE queries vary widely: pasting `CrashLoopBackOff` is a lexical match problem (text weight should dominate), while asking "why is my service slow after deploying the new cache layer" is a semantic problem. A fixed ratio underweights lexical match for the exact-identifier queries that are extremely common in SRE workflows.

Planned approach: detect identifier-like tokens in the query (error codes matching `ERR-\d+`, CamelCase names, dotted names, status codes, file paths) via regex. When present, shift term overlap weight from 25% to 40% and reduce vector similarity from 40% to 25%. No ML needed — pattern detection is sufficient.

---

## 4. Knowledge Base Retrieval

### Collection and Scope

All KB tiers share a single ChromaDB collection: `faultmaven_kb`. Scope isolation is enforced at query time via metadata filtering, not by separate collections.

A typical federated query for a user who belongs to one team:

```python
where = {"$or": [
    {"scope": "global"},
    {"$and": [{"scope": "personal"}, {"owner_id": user_id}]},
    {"$and": [{"scope": "team"}, {"team_id": {"$in": team_ids}}]},
]}
```

**Scope safety invariant:** `KnowledgeVectorStore.search()` and `hybrid_search()` reject any query against `faultmaven_kb` that does not include `scope`, `owner_id`, or `team_id` in the `where` clause. Unscoped queries raise `ValueError`. This converts a fail-open cross-tenant data leak risk into a fail-closed guarantee. Case evidence collections (`case_*`) are exempt.

### Chunking Strategy

KB documents use structure-aware chunking distinct from the character-based chunking described in older versions of knowledge-base-architecture.md. The current ingestion pipeline splits on markdown structural boundaries:

- Primary split points: `##` and `###` headers, horizontal rules (`---`)
- Chunk size bounds: 200–3000 characters (variable, not fixed)
- Tiny sections (below 200 characters) are merged with the adjacent section
- Oversized sections are split at line boundaries

YAML frontmatter is stripped before chunking — a runbook with 300 chars of frontmatter doesn't waste its first chunk on metadata that adds no retrieval value.

This approach preserves the semantic coherence of runbook sections. A diagnostic step does not share a chunk with an unrelated prevention note because the markdown structure itself draws the boundary.

### Metadata Per Chunk

| Field | Stored | Purpose |
|-------|--------|---------|
| `document_id` | Yes | Unique runbook identifier |
| `title` | Yes | Runbook title |
| `domain` | Yes | Engineering vertical (database, networking, compute, etc.) |
| `service` | Yes | Specific technology (postgresql, kubernetes, redis, etc.) |
| `status` | Yes | Lifecycle state: draft, in-review, verified, community, stale, deprecated |
| `last_updated` | Yes | ISO date — used for staleness scoring in reranker and synthesis |
| `chunk_index` | Yes | Position within the chunked document |
| `total_chunks` | Yes | Total chunks for this document |
| `scope` | Yes | Tier: global, team, or personal |
| `owner_id` | Yes | Set for personal-scope chunks |
| `team_id` | Yes | Set for team-scope chunks |
| `document_type` | Yes | Content classification (e.g., troubleshooting_guide) |
| `tags` | Yes | Comma-separated tags from frontmatter |
| `symptom_class` | **Not stored** | Present in runbook frontmatter but not extracted into chunk metadata. Would improve retrieval for symptom-matching queries. |
| `severity` | **Not stored** | Present in runbook frontmatter but not extracted into chunk metadata. |

### Staleness-Aware Synthesis

The `UnifiedKBConfig.format_chunk_metadata()` method computes staleness at retrieval time from the `last_updated` field and injects warnings directly into the chunk context text that the synthesis LLM sees:

- Age > 180 days: `STALE (N days old)` warning prepended to chunk context
- Age > 90 days: `Last updated: N days ago` note included
- `deprecated` status: penalty applied in reranker; content should be excluded from the collection via lifecycle governance

The synthesis LLM's system prompt (in `UnifiedKBConfig.system_prompt`) explicitly instructs the model to warn users when chunks are stale or in draft status, and to prefer verified, recently-updated content. This means staleness warnings propagate naturally to the user response without special agent-side handling.

### Tool Path

```text
Agent calls: kb_qa(question)
  │
  ├── KBToolAdapter.execute_with_context()
  │     Extracts user_id and team_ids from ToolContext
  │
  ├── AnswerFromKB._arun(question, user_id, team_ids)
  │     Builds combined $or scope filter
  │
  ├── DocumentQATool.answer_question()
  │     Detects search_mode="hybrid" from UnifiedKBConfig
  │
  ├── KnowledgeVectorStore.hybrid_search()
  │     Stage 1: vector + keyword recall
  │     Stage 2: rerank with 4-signal scoring
  │
  ├── LLMRouter.route() with UnifiedKBConfig.system_prompt
  │     max_tokens=2000, temperature=0.3
  │     Synthesis guided by staleness-aware system prompt
  │
  └── UnifiedKBConfig.format_response()
        Returns answer with source citations
```

**Relay vs synthesis tension:** The design intent is that full procedure detail reaches the user — a compressed paraphrase of a runbook loses the actionable steps. However, the current `DocumentQATool` synthesis prompt instructs the LLM to "be concise and factual," which encourages compression. The relay instruction is applied later in `_format_tool_result()` which wraps the KB result with "Include the detailed content below — do NOT summarize into a single sentence." These two instructions can conflict. The synthesis prompt should be aligned with the relay intent — preserve procedural detail, cite sources accurately, compress only background context.

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
| Implementation | `services/preprocessing/chunking_service.py` | — |

**Design rationale:** Once evidence is vectorized (Tier 4), it becomes the primary search path for all subsequent semantic queries — follow-up questions, agent-initiated correlations, cross-file analysis. The embedding must be precise enough to surface the right 512-token window when the user asks "what happened at 14:32?" Forensic context (the surrounding log entries) is retrieved at query time by expanding the matched chunk's position in the original file, not by inflating the chunk size.

This follows the same principle as KB chunking: embed at the granularity of a single coherent unit (a runbook section, a log event window, a config block), not at the granularity of a page.

### 4-Tier Evidence Escalation

Vector search (Tier 4) is the most expensive tier and is not the first resort. The full escalation path:

| Tier | Mechanism | Cost | Trigger |
|------|-----------|------|---------|
| 0 + 1 | Structural indexing (11 domain extractors, runs on upload) | $0 | Always, on upload |
| 2 | Keyword/regex search on raw files (`search_file`) | $0 | Agent tool call |
| 3 | LLM-interpreted analysis on file sections (`deep_analysis`) | ~$0.01 | Agent tool call |
| 4 | Chunk + embed + semantic search (`case_evidence_search`) | ~$0.05+ | See vectorization triggers below |

### Vectorization Triggers

Vectorization of evidence files is triggered by two paths:

**Proactive:** Files above the vectorization size threshold are submitted for background vectorization when DA (Directed Analysis) mode starts for that evidence item. This ensures the vector index is ready before the agent exhausts keyword and deep analysis attempts.

**Reactive fallbacks:** If proactive vectorization did not complete or was not triggered:
- 3 or more empty `search_file` results against a file
- Tool execution timeout
- Low confidence score (< 0.2) from `deep_analysis`

The `searchable="true"` attribute on evidence XML in the context builder signals to the agent that a file has been indexed and `case_evidence_search` can be called against it.

### Evidence vs. KB — Why Different Strategies

| Aspect | KB (Runbooks) | Evidence (Logs, Configs, Metrics) |
|--------|--------------|-----------------------------------|
| Chunking | Structure-aware, 200–3000 chars | Type-aware, 512 tokens with 50-token overlap + context window at retrieval |
| Lifecycle | Permanent | Ephemeral (per-case) |
| Collection | Single shared (`faultmaven_kb`) | Per-case (`case_{id}`) |
| Scope enforcement | Mandatory scope filter (invariant) | Scoped by case ownership |
| Retrieval pipeline | Hybrid search + reranking | Vector search (no reranking) |
| Primary role | Remediation | Diagnosis |

---

## 6. Tool Result Formatting

### KB Tool (`kb_qa`)

The design intent is relay — full procedure detail should reach the user. The `_format_tool_result()` wrapper appends "Include the detailed content below — do NOT summarize into a single sentence." However, the inner `DocumentQATool` synthesis prompt says "be concise and factual," creating a tension (see §7 open issues).

Source citations use document titles: `Sources: Kubernetes CrashLoopBackOff, PostgreSQL Connection Pool Exhaustion`.

### Evidence Tool (`case_evidence_search`)

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
| Hard pre-filter mode | **Implemented** | `filter_mode="hard"` injects domain/service into ChromaDB where clause |
| Fast search mode | **Implemented** | `search_mode="fast"` skips Stage 2 reranking |
| Scope tiebreaking | **Implemented** | personal > team > global secondary sort in `_rerank()` |
| Staleness-aware synthesis | **Implemented** | `_staleness_note()` + system prompt: "preserve procedural detail" |
| Scope safety (pre-filtering) | **Implemented** | ChromaDB `where` clause pre-filters before ANN search. `_enforce_scope_invariant()` raises `ValueError` on unscoped queries. |
| Extension context → KB metadata filters | **Partially done** | `hybrid_search()` accepts `context_metadata`. Wiring from copilot → API → `KBToolAdapter` incomplete. |
| True BM25 | **Not done** | ChromaDB does not expose BM25. Binary `$contains` is a partial substitute. Would need `rank_bm25` lib or separate index. |
| Cross-encoder reranker | **Not done** | Would add a dedicated reranking model (e.g., `ms-marco-MiniLM`) between retrieval and synthesis. Adds model dependency + latency. |
| Citation grounding | **Not done** | No post-generation verification that cited chunks support the claims attributed to them. |
| Result caching | **Not done** | `KBConfig.cache_ttl` exists but nothing reads it — no caching layer wraps `hybrid_search()` or `answer_question()`. |

### KB Ingestion & Storage

| Feature | Status | Notes |
|---------|--------|-------|
| Structure-aware KB chunking | **Implemented** | `ContentChunker` — markdown header splits, 200–3000 chars. Wired into all ingestion paths via `_index_document_in_vector_store()`. |
| Explicit BGE-M3 embeddings (KB) | **Implemented** | All KB index and query paths use `model_cache.get_bge_m3_model()` (1024 dims). No ChromaDB default embedding in KB paths. |
| Metadata enrichment | **Implemented** | domain, service, status, severity, symptom_class stored per chunk at ingestion, used in reranker. Also stored in SQLite (`conversion_drafts`) for dashboard filtering. |
| SQLite document inventory | **Implemented** | `list_documents()`, `get_document()`, `delete_document()` use SQLite, not ChromaDB. |
| Redis removed from KB lifecycle | **Implemented** | No Redis reads or writes for KB documents. SQLite is the sole inventory. |
| Chunk-aware deletion | **Implemented** | `delete_documents_by_parent_id()` removes all chunks for a document. |
| Batch activation endpoint | **Implemented** | `POST /knowledge/drafts/verify-batch` — batch activate up to 100 drafts per request. |
| Dashboard scan-on-mount | **Implemented** | Scan removed from server startup. Dashboard triggers `POST /knowledge/scan` on KB page mount. |

### Evidence Retrieval

| Feature | Status | Notes |
|---------|--------|-------|
| Evidence 4-tier escalation | **Implemented** | See `data-preprocessing-design-specification.md` |
| Proactive vectorization | **Implemented** | Background trigger at DA mode start for large files |
| Evidence embedding consistency | **Not done** | Case evidence collections use ChromaDB default embedding (384 dims), not BGE-M3. Internally consistent but mismatched with KB. See `docs/working/WIP-evidence-embedding-consistency.md`. |
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

All KB paths (indexing and querying) use explicit BGE-M3 embeddings (1024 dims) via `model_cache.get_bge_m3_model()`. No KB path relies on ChromaDB's default embedding function. This applies to:
- `_index_document_in_vector_store()` — chunk embeddings at ingestion
- `KnowledgeVectorStore.search()` — `query_embeddings` for vector recall
- `KnowledgeVectorStore._single_keyword_search()` — `query_embeddings` for keyword-constrained search
- `ChromaDBVectorStore.search()` — `query_embeddings` for direct search
- `RunbookKB.index_runbook()` — explicit embeddings passed to `add_documents()`

**Note:** Case evidence collections (`case_{id}`) currently use ChromaDB's default embedding (384 dims) for both add and query. This is internally consistent but architecturally inconsistent with KB collections. See `docs/working/WIP-evidence-embedding-consistency.md` for the planned fix.

### Superseded Code

**`KnowledgeSearchService`** (`modules/knowledge/domain/services/search_service.py`) — operates on `KnowledgeItem` objects (whole documents), not ChromaDB chunks. Only used by the background `KnowledgeIndexingJob`. The main search path uses `KnowledgeVectorStore` directly.

**`ChromaDBVectorStore.list_documents()` / `get_document()`** — removed. These methods fetched chunks and deduplicated in Python. Replaced by SQLite queries in `KnowledgeService`.

**Redis KB key patterns** — removed. `upload_document()`, `get_document()`, `list_documents()`, `delete_document()` no longer read or write Redis. The `get_job_status()` method and `GET /knowledge/jobs/{job_id}` endpoint were also removed.

### Deployment Note

On first startup, `seed_builtin_runbooks()` copies 59 runbooks from `resources/knowledge/builtin/` to `data/knowledge/global/`. The Dashboard triggers `POST /api/v1/knowledge/scan` on mount, which discovers new files and creates draft records. Users activate runbooks via "Activate" (single or batch) from the Dashboard, which chunks + embeds + stores in ChromaDB.

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
| Evidence chunking service | `faultmaven/services/preprocessing/chunking_service.py` |

---

## Related Documents

- **[Knowledge Base Architecture](./knowledge-base-architecture.md)** — KB storage design, 3-tier scope model, collection naming, access control, tier-by-tier ingestion details
- **[Runbook Content Architecture](./runbook-content-architecture.md)** — KB content taxonomy, runbook templates, quality gates, and lifecycle governance
- **[Data Preprocessing Design Specification](../data-processing/data-preprocessing-design-specification.md)** — Evidence classification, structural indexing, DA mode, vectorization triggers
- **[Orchestration Capabilities](../investigation-engine/orchestration-capabilities.md)** — How retrieval tools participate in the investigation engine's tool loop
