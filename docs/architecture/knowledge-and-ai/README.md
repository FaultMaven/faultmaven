# Knowledge Base and AI

Documentation for FaultMaven's knowledge management, vector search, and AI agent intelligence systems.

## What to read first

| If you are… | Start with |
| ----------- | ---------- |
| Storing, scoping, or managing access to runbooks | [Knowledge Base Architecture](./knowledge-base-architecture.md) |
| Understanding how runbooks reach the KB (bootstrap vs verify) | [KB Ingestion Architecture](./kb-ingestion-architecture.md) |
| The KB pack — format, `KB_PACK_DIR`, build & offline delivery | [KB Pack Architecture](./kb-pack-architecture.md) |
| Tuning retrieval — embeddings, hybrid search, reranker | [Vector Retrieval Architecture](./vector-retrieval-architecture.md) |
| Writing runbook content (template, taxonomy, validation) | [Runbook Content Architecture](./runbook-content-architecture.md) |
| Implementing the document-to-runbook conversion feature | [Document-to-Runbook Conversion](./document-to-runbook-conversion.md) |
| Looking for end-user how-to | [`docs/guides/knowledge-base.md`](../../guides/knowledge-base.md) |

## Canonical authority by topic

Each cross-cutting topic has one canonical document. Other documents reference it rather than duplicating coverage. When two documents disagree, the canonical one wins.

| Topic | Canonical |
| ----- | --------- |
| 3-tier scope model (Global / Team / Personal) + scope safety invariant | [knowledge-base-architecture.md](./knowledge-base-architecture.md) |
| Single-collection design (`faultmaven_kb`) + tier scoping | [knowledge-base-architecture.md](./knowledge-base-architecture.md) |
| Ingestion paths (bootstrap vs verify), atomicity, idempotency | [kb-ingestion-architecture.md](./kb-ingestion-architecture.md) |
| KB pack format, `KB_PACK_DIR`, build (toolkit), offline delivery (local/cloud) | [kb-pack-architecture.md](./kb-pack-architecture.md) |
| Federated search — access control, team-membership resolution | [knowledge-base-architecture.md](./knowledge-base-architecture.md) |
| Embedding model (BGE-M3) + dimensions reference | [vector-retrieval-architecture.md](./vector-retrieval-architecture.md) |
| Two-stage hybrid search + four-signal reranker mechanics | [vector-retrieval-architecture.md](./vector-retrieval-architecture.md) |
| Chunking parameters (KB structure-aware; evidence target-vs-current) | [vector-retrieval-architecture.md](./vector-retrieval-architecture.md) |
| Staleness-aware synthesis (`format_chunk_metadata`, freshness signal) | [vector-retrieval-architecture.md](./vector-retrieval-architecture.md) |
| Federated-search tool implementation (`answer_from_kb` path) | [vector-retrieval-architecture.md](./vector-retrieval-architecture.md) |
| Runbook template (per-Cause structure, sub-fields, frontmatter) | [runbook-content-architecture.md](./runbook-content-architecture.md) |
| Taxonomy (domain / service / symptom_class vocabulary) | [runbook-content-architecture.md](./runbook-content-architecture.md) |
| Quality gates + validation rules + lifecycle states | [runbook-content-architecture.md](./runbook-content-architecture.md) |
| Conversion pipeline + draft API + Verify workflow | [document-to-runbook-conversion.md](./document-to-runbook-conversion.md) |

## Documents in this directory

- **[Knowledge Base Architecture](./knowledge-base-architecture.md)** — KB storage design: single-collection (`faultmaven_kb`), 3-tier scope model, scope safety invariant, access control, ingestion workflow.
- **[KB Ingestion Architecture](./kb-ingestion-architecture.md)** — The two ingestion paths (startup bootstrap for pre-deployed runbooks; conversion-drafts verify flow for case-generated/uploaded content), atomicity contract, idempotency, and the bug history that drove the current design.
- **[Vector Retrieval Architecture](./vector-retrieval-architecture.md)** — Shared vector infrastructure: BGE-M3 embeddings, two-stage hybrid search, four-signal reranker, KB vs. evidence chunking strategies, implementation status.
- **[Runbook Content Architecture](./runbook-content-architecture.md)** — What goes INTO the KB: v3 template (per-Cause subsections), taxonomy, quality gates, lifecycle governance, RAG-optimized authoring rules.
- **[Document-to-Runbook Conversion](./document-to-runbook-conversion.md)** — Converting uploaded documents into template-compliant runbooks: preprocessing pipeline, `KNOWLEDGE_PROVIDER` LLM, draft management API, Verify workflow.

## Key Technologies

- **Vector Database**: ChromaDB (single `faultmaven_kb` collection for KB; per-case `case_{id}` collections for evidence)
- **Embeddings**: BGE-M3 (1024 dims, multilingual) via sentence-transformers — shared by KB and evidence
- **Retrieval Pipeline**: Two-stage hybrid search (vector + binary keyword recall → four-signal reranker) for KB; pure vector search for evidence
- **Scope Safety**: `KnowledgeVectorStore` enforces scope filter invariant — unscoped KB queries raise `ValueError`
