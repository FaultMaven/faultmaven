---
name: rag-architecture
description: Triggers when modifying search, retrieval, query construction, vector query, reranker scoring, hybrid search, embedding lookup, or knowledge-base read-path operations under faultmaven/modules/knowledge/ or faultmaven/infrastructure/knowledge/. Do NOT trigger on ingestion, upload, runbook authoring, preprocessing, or chunking tasks.
---

# Skill: rag-architecture

**What this skill does:** Makes sure you read the current retrieval design docs *before* modifying the shared read path — how queries are embedded, how vector and keyword results are fused, and how candidates are reranked.

**What this skill does NOT do:** Restate the retrieval design. The docs are the source of truth and change with every reranker tuning pass or signal-weight revision.

---

## Authoritative Documents

Read these before acting. One of the four canonical source-of-truth documents declared in `docs/architecture/README.md` lives in this section:

1. **`docs/architecture/knowledge-and-ai/README.md`** — Start here. Declares reading order.
2. **`docs/architecture/knowledge-and-ai/knowledge-base-architecture.md`** — Canonical. 3-tier KB (user/global/case), storage, retrieval, federated search.
3. **`docs/architecture/knowledge-and-ai/vector-retrieval-architecture.md`** — Shared vector infrastructure: BGE-M3 embeddings (1024 dims, multilingual), two-stage hybrid search (vector + BM25), four-signal reranker, dynamic weighting, hard filters, fast mode.
4. **`docs/architecture/knowledge-and-ai/runbook-content-architecture.md`** — Runbook structure as it relates to retrievability.
5. **`docs/architecture/knowledge-and-ai/document-to-runbook-conversion.md`** — Conversion pipeline (cross-references retrieval).
6. **`docs/architecture/data-and-storage/vector-storage.md`** — Vector DB implementation details (ChromaDB, HNSW cosine).

If any referenced document does not exist at the path above, **stop and tell the user** — do not fabricate content to fill the gap.

---

## Code Scope

This skill covers changes to:
- `faultmaven/modules/knowledge/` — Knowledge base module (Vertical Module with `contracts.py`)
- `faultmaven/infrastructure/knowledge/` — Vector database adapters (ChromaDB)

---

## Procedure

1. **Read the knowledge-and-ai README** (`docs/architecture/knowledge-and-ai/README.md`) for the current reading order.
2. **Read the retrieval design docs.** KB architecture + vector-retrieval-architecture are almost always required for changes in this scope; vector-storage is required when touching persistence adapters.
3. **Read the target code** — search service, embedding service, vector store service, reranker — before editing.
4. **Apply the change** conforming to the documented retrieval architecture.

If the design docs and the existing code appear to contradict each other, **stop and ask the user which side is authoritative** before proceeding. Do not silently pick one side. Use `/design-check knowledge` for a full drift report.

---

## Scope Boundaries

**This skill governs the shared read path:**
- Query embedding and vector search
- Keyword/BM25 retrieval and fusion with vector results
- Reranker signals, weights, and scoring
- Hard filters (scope, tenancy, type)
- Knowledge-base Q&A primitives (user_kb, global_kb, case_evidence_qa)
- Federated search across the 3-tier KB

**This skill does NOT govern:**
- How data enters the vector store:
  - Diagnostic evidence ingestion → `ingestion-pipeline`
  - Runbook authoring / import / conversion → knowledge module write-path, out of scope here
- Agent tool orchestration that *calls* retrieval → `investigation-framework`
- Module structure / knowledge module boundary rules → `architecture`
