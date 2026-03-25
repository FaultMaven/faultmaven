# Knowledge Base and AI

Documentation for FaultMaven's knowledge management, vector search, and AI agent intelligence systems.

## Architecture Documents

- **[Knowledge Base Architecture](./knowledge-base-architecture.md)** — Storage and retrieval: single-collection design (faultmaven_kb), scope isolation via metadata filtering, unified KB tool, scope safety invariant, staleness-aware synthesis
- **[Runbook Content Architecture](./runbook-content-architecture.md)** — What goes INTO the KB: taxonomy, templates, quality gates, lifecycle governance, RAG-optimized authoring rules
- **[Document-to-Runbook Conversion](./document-to-runbook-conversion.md)** — Converting uploaded documents into template-compliant runbooks: preprocessing pipeline, KNOWLEDGE_PROVIDER LLM, draft management

## Handover Documents

- **[Frontend Conversion Handover](./HANDOVER-conversion-frontend.md)** — API contracts, UI flow, TypeScript interfaces, and Dashboard integration guide for the conversion feature frontend

## Key Technologies

- **Vector Database**: ChromaDB (single `faultmaven_kb` collection, metadata-filtered scopes)
- **Embeddings**: BGE-M3 (1024 dims, multilingual) via sentence-transformers
- **RAG**: Retrieval-Augmented Generation for knowledge-grounded responses
- **Scope Safety**: `KnowledgeVectorStore` enforces scope filter invariant — unscoped KB queries are rejected
