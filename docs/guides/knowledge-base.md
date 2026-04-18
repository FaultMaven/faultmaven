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

Place runbook `.md` files under `data/knowledge/{global|team_<id>|personal_<id>}/`, then trigger a scan from the Dashboard KB page (or `POST /api/v1/knowledge/scan`). Scanned files appear as drafts in the Drafts tab. Verifying a draft chunks the file (structure-aware, 100–3000 chars), generates BGE-M3 embeddings (1024 dims), and stores chunks in the single `faultmaven_kb` ChromaDB collection with metadata-based scope filtering. There is no auto-on-PR-merge ingestion.

## Auto-seeded built-in runbooks

On first startup, FaultMaven copies 59 runbooks from `resources/knowledge/builtin/` to `data/knowledge/global/`. The Dashboard scans on KB-page mount and you can activate them from the Drafts tab (single or batch via `POST /knowledge/drafts/verify-batch`).

## Tools the agent uses during investigation

| Tool (registered name) | Purpose |
|-----------------------|---------|
| `answer_from_kb` | Federated KB Q&A — searches global + personal + team via metadata `$or` filter, returns chunk-grounded synthesis |
| `answer_from_case_evidence` | Forensic Q&A on per-case evidence in the `case_{case_id}` collection |

The agent does not pick a tier — `KBToolAdapter` resolves the user's authorized scopes from `ToolContext` automatically.
