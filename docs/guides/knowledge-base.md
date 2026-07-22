# Knowledge Base Guide

**Last Updated:** 2026-04-18

This guide is a navigation pointer. The previous content (template, taxonomy, three-system framing, ingestion model, directory layout) was materially out of date and has been superseded by the architecture documents listed below.

## What you probably want

| Goal | Where to look |
|------|---------------|
| Understand the 3-tier KB model (Global / Team / Personal) | [Knowledge Base Architecture](../architecture/knowledge-and-ai/knowledge-base-architecture.md) |
| Author a runbook (template, required frontmatter, quality gates) | [Runbook Content Architecture](../architecture/knowledge-and-ai/runbook-content-architecture.md) |
| Understand retrieval (hybrid search, reranker, staleness, scope safety) | [Vector Retrieval Architecture](../architecture/knowledge-and-ai/vector-retrieval-architecture.md) |
| Convert an existing document or resolved case into runbook drafts | [Document-to-Runbook Conversion](../architecture/knowledge-and-ai/document-to-runbook-conversion.md) |
| Bulk-author and validate runbooks via CLI | [`faultmaven-kb-toolkit`](https://github.com/FaultMaven/faultmaven-kb-toolkit) |
| Manage the KB through the web UI | Dashboard → KB page (Documents, Drafts, Convert) |

## Ingestion in one paragraph

There are two ingestion paths, by source. **Pre-deployed runbooks** ship in the **KB pack** (`resources/knowledge/pack`, or `KB_PACK_DIR`) — a bundle of runbooks + pre-computed vectors that the [KB bootstrap](../architecture/knowledge-and-ai/kb-ingestion-architecture.md) ingests automatically at API startup with **no embedding model** (so it takes seconds). The bootstrap is idempotent: unchanged runbooks are skipped via content-hash on every restart. **Case-generated drafts** (from the Convert-from-Case feature) and **document-converted drafts** (from a file upload) land in the `conversion_drafts` table and require a human "Verify" click in the Dashboard Drafts tab before they reach the KB. Both paths produce chunks (structure-aware, 100–3000 chars), BGE-M3 embeddings (1024 dims), and rows in the single `faultmaven_kb` ChromaDB collection with metadata-based scope filtering.

## Built-in runbooks (the KB pack)

The 91 built-in runbooks ship pre-chunked and pre-embedded in the KB pack and are ingested automatically on startup — no Dashboard step is required. To add or update pre-deployed runbooks, rebuild the pack in the [KB Toolkit](../architecture/knowledge-and-ai/kb-pack-architecture.md) (`kb-build-pack`) and deliver it — vendor the committed baseline, or update a running deployment's `KB_PACK_DIR` (local bind-mount / cloud MinIO) with no app-image rebuild. See [KB Pack Architecture](../architecture/knowledge-and-ai/kb-pack-architecture.md).

## Tools the agent uses during investigation

| Tool (registered name) | Purpose |
|-----------------------|---------|
| `answer_from_kb` | Federated KB Q&A — searches global + personal + team via metadata `$or` filter, returns chunk-grounded synthesis |
| `answer_from_case_evidence` | Forensic Q&A on per-case evidence in the `case_{case_id}` collection |

The agent does not pick a tier — `KBToolAdapter` resolves the user's authorized scopes from `ToolContext` automatically.
