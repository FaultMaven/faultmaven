# Knowledge Base and AI

Documentation for FaultMaven's knowledge management, vector search, and AI agent intelligence systems.

## Architecture Documents

- **[Vector Retrieval Architecture](./vector-retrieval-architecture.md)** — Shared vector infrastructure: BGE-M3 embeddings, two-stage hybrid search pipeline, four-signal reranker, KB vs. evidence collection strategies, implementation status
- **[Knowledge Base Architecture](./knowledge-base-architecture.md)** — KB storage design: single-collection (faultmaven_kb), 3-tier scope model, scope safety invariant, access control, ingestion workflow
- **[Runbook Content Architecture](./runbook-content-architecture.md)** — What goes INTO the KB: taxonomy, templates, quality gates, lifecycle governance, RAG-optimized authoring rules
- **[Document-to-Runbook Conversion](./document-to-runbook-conversion.md)** — Converting uploaded documents into template-compliant runbooks: preprocessing pipeline, KNOWLEDGE_PROVIDER LLM, draft management

## Handover Documents

- **[Frontend Conversion Handover](./HANDOVER-conversion-frontend.md)** — API contracts, UI flow, TypeScript interfaces, and Dashboard integration guide for the conversion feature frontend

## Key Technologies

- **Vector Database**: ChromaDB (single `faultmaven_kb` collection for KB; per-case `case_{id}` collections for evidence)
- **Embeddings**: BGE-M3 (1024 dims, multilingual) via sentence-transformers — shared by KB and evidence
- **Retrieval Pipeline**: Two-stage hybrid search (vector + binary keyword recall → four-signal reranker) for KB; pure vector search for evidence
- **Scope Safety**: `KnowledgeVectorStore` enforces scope filter invariant — unscoped KB queries raise `ValueError`
