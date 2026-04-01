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

BGE-M3 is used for both KB ingestion and evidence vectorization. The model is loaded once and cached for the process lifetime — loading at import time or on first request depending on the startup path. The 1024-dimensional space provides strong semantic resolution for technical text including error messages, log fragments, and procedure descriptions across multiple languages.

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

The `context_metadata` parameter carries domain and service from case context, enabling case-aware reranking without the agent making an explicit filter decision. This parameter currently exists in the method signature but wiring from the KB tool call path is incomplete — see Section 7.

**Tiebreaking:** When two chunks produce the same weighted score, scope priority breaks the tie: personal > team > global. This ensures a user's own runbook surfaces above a generic global procedure when both are equally relevant.

The `k` highest-scoring chunks are returned.

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

This approach preserves the semantic coherence of runbook sections. A diagnostic step does not share a chunk with an unrelated prevention note because the markdown structure itself draws the boundary.

### Metadata Per Chunk

| Field | Purpose |
|-------|---------|
| `document_id` | Unique runbook identifier |
| `title` | Runbook title |
| `domain` | Engineering vertical (database, networking, compute, etc.) |
| `service` | Specific technology (postgresql, kubernetes, redis, etc.) |
| `symptom_class` | Failure modes addressed |
| `severity` | Severity level |
| `status` | Lifecycle state: draft, in-review, verified, community, stale, deprecated |
| `last_updated` | ISO date — used for staleness scoring in reranker and synthesis |
| `chunk_index` | Position within the chunked document |
| `total_chunks` | Total chunks for this document |
| `scope` | Tier: global, team, or personal |
| `owner_id` | Set for personal-scope chunks |
| `team_id` | Set for team-scope chunks |

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

**Relay instruction:** The synthesis prompt instructs the model to include detailed content from retrieved chunks and not merely summarize — the full procedure content should reach the user, not a compressed paraphrase of it.

---

## 5. Evidence Retrieval

Evidence retrieval is governed by the data preprocessing architecture and is documented in detail in [data-preprocessing-design-specification.md](../data-processing/data-preprocessing-design-specification.md). This section summarizes the vector retrieval layer specifically.

### Collection Strategy

Each case gets its own ChromaDB collection: `case_{case_id}`. Collections are ephemeral — they exist for the duration of the case and are cleaned up as part of case archival.

### Chunking Parameters

Evidence uses token-based chunking optimized for heterogeneous content (logs, configs, CSVs, JSON):

| Parameter | Value |
|-----------|-------|
| Chunk size | 4000 tokens |
| Overlap | 200 tokens |
| Split strategy | Section-aware: respects structural boundaries within files |
| Implementation | `services/preprocessing/chunking_service.py` |

The larger chunks (versus KB's 200–3000 character structure-aware chunks) preserve context for forensic analysis. A log entry only makes sense alongside its neighboring entries; a config file section should not be split from its keys.

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
| Chunking | Structure-aware, 200–3000 chars | Token-based, 4000 tokens with 200-token overlap |
| Lifecycle | Permanent | Ephemeral (per-case) |
| Collection | Single shared (`faultmaven_kb`) | Per-case (`case_{id}`) |
| Scope enforcement | Mandatory scope filter (invariant) | Scoped by case ownership |
| Retrieval pipeline | Hybrid search + reranking | Vector search (no reranking) |
| Primary role | Remediation | Diagnosis |

---

## 6. Tool Result Formatting

### KB Tool (`kb_qa`)

The synthesis prompt includes a relay instruction to the LLM: include detailed content from retrieved chunks, do not summarize. This preserves the actionable detail of runbook procedures in the response.

Source citations use document titles: `Sources: Kubernetes CrashLoopBackOff, PostgreSQL Connection Pool Exhaustion`.

### Evidence Tool (`case_evidence_search`)

The synthesis prompt instructs the LLM to cite with forensic precision: filename, line number, and timestamp. Citation format: `In filename, line 42: ...`.

The system prompt for `CaseEvidenceConfig` is explicitly forensic — it instructs the model to cite exact line numbers, include error messages verbatim, preserve chronological order, and distinguish severity levels.

### Evidence-vs-Knowledge Boundary

A strict rule governs what can be added as case evidence (`evidence_to_add`): only from user-submitted data. Evidence is never sourced from KB content, web searches, or LLM-generated inference. This boundary is enforced in the agent system prompt and is fundamental to the diagnosis/remediation separation.

---

## 7. Implementation Status

| Feature | Status | Notes |
|---------|--------|-------|
| Hybrid search (Stage 1 + Stage 2) | Implemented | `KnowledgeVectorStore.hybrid_search()` |
| Binary keyword search | Implemented | `$contains` via `where_document` — not BM25 |
| Four-signal reranker | Implemented | Vector, term overlap, metadata match, freshness |
| Scope tiebreaking (personal > team > global) | Implemented | Secondary sort key in `_rerank()` |
| Structure-aware KB chunking | Implemented | Markdown header + horizontal rule splits |
| Variable chunk sizes (200–3000 chars) | Implemented | Tiny section merging, oversized line-boundary splits |
| Staleness-aware synthesis | Implemented | `UnifiedKBConfig._staleness_note()` + system prompt instruction |
| Metadata enrichment (domain, service, status) | Implemented | Stored at ingestion, used in reranker |
| Scope safety invariant | Implemented | `_enforce_scope_invariant()` raises `ValueError` |
| Evidence 4-tier escalation | Implemented | See `data-preprocessing-design-specification.md` |
| Proactive vectorization | Implemented | Background trigger at DA mode start for large files |
| True BM25 hybrid search | Not implemented | ChromaDB does not expose BM25. Current keyword gate is binary `$contains`. |
| Cross-encoder reranker | Not implemented | Would replace term overlap signal with a dedicated reranking model |
| `context_metadata` wiring to KB tool | Partially implemented | Parameter exists in `hybrid_search()`. `KBToolAdapter` does not yet pass case domain/service from `ToolContext`. |
| Citation grounding | Not implemented | No hallucination-check pass to verify that citations correspond to actual retrieved chunks |

---

## 8. Key Files

| Component | File |
|-----------|------|
| KB vector store (hybrid search, reranker, scope invariant) | `faultmaven/infrastructure/knowledge/knowledge_vector_store.py` |
| KB-neutral Q&A tool (strategy pattern) | `faultmaven/modules/agent/tools/document_qa_tool.py` |
| KBConfig interface | `faultmaven/modules/agent/tools/kb_config.py` |
| Unified KB config (hybrid mode, staleness) | `faultmaven/modules/agent/tools/kb_configs/unified_kb_config.py` |
| Case evidence config (forensic synthesis) | `faultmaven/modules/agent/tools/kb_configs/case_evidence_config.py` |
| Unified KB tool (scope filter construction) | `faultmaven/modules/agent/tools/kb_qa.py` |
| KB and evidence tool adapters | `faultmaven/modules/agent/tools/kb_tool_adapter.py` |
| KB ingestion pipeline | `faultmaven/core/knowledge/ingestion.py` |
| Evidence chunking service | `faultmaven/services/preprocessing/chunking_service.py` |
| Model cache (BGE-M3 global singleton) | `faultmaven/infrastructure/knowledge/model_cache.py` |

---

## Related Documents

- **[Knowledge Base Architecture](./knowledge-base-architecture.md)** — KB storage design, 3-tier scope model, collection naming, access control, tier-by-tier ingestion details
- **[Runbook Content Architecture](./runbook-content-architecture.md)** — KB content taxonomy, runbook templates, quality gates, and lifecycle governance
- **[Data Preprocessing Design Specification](../data-processing/data-preprocessing-design-specification.md)** — Evidence classification, structural indexing, DA mode, vectorization triggers
- **[Orchestration Capabilities](../investigation-engine/orchestration-capabilities.md)** — How retrieval tools participate in the investigation engine's tool loop
