# Knowledge Base Architecture

**Document Type:** Component Specification
**Version:** 9.0
**Last Updated:** 2026-03-25

---

## Purpose

This document defines the **storage and retrieval architecture** for FaultMaven's knowledge base — the curated collection of runbooks, procedures, and troubleshooting guidance that the investigation engine draws on to remediate problems.

For the complementary concern of **what goes into the knowledge base** — content taxonomy, runbook templates, quality gates, and lifecycle governance — see [runbook-content-architecture.md](./runbook-content-architecture.md).

### Scope: Knowledge Base Only

The investigation engine consumes two fundamentally different types of data via RAG:

- **Case evidence** (logs, configs, metrics) — raw diagnostic data submitted during investigation. Used to **diagnose** what is happening.
- **Knowledge base** (runbooks, procedures, best practices) — curated remediation knowledge built before investigation. Used to **remediate** the problem.

Both reach the LLM through vector search and chunk synthesis, but they are as different as court evidence is from the law. The judge needs both to arrive at a judgment, but they are governed by entirely different rules.

**This document covers the knowledge base only.** For case evidence storage and preprocessing, see:

- [Case Evidence Store](../case-and-session/case-evidence-store.md) — Evidence lifecycle and storage
- [Data Preprocessing Design](../data-processing/data-preprocessing-design-specification.md) — How evidence is classified, extracted, and indexed

---

## 3-Tier Knowledge Architecture

FaultMaven implements a **3-tier knowledge base** system. The tiers differ in scope, ownership, and availability depending on deployment mode:

| Tier | Scope | Ownership | Content | Deployment |
|------|-------|-----------|---------|------------|
| **Global** | System-wide, all organizations | Platform admin | Pre-built troubleshooting guides, industry best practices, vendor documentation | Cloud only (pre-populated) |
| **Team** | Shared across team members within an organization | Team admin | Shared runbooks, incident logs, institutional memory | Cloud only |
| **Personal** | Private to one user within an organization | Individual user | Private notes, personal runbooks, drafts | Both Local and Cloud |

Team and Personal KBs are scoped to an **organization**. A user belongs to an organization, and their Personal KB and Team KB access are determined by that membership. The Global KB is platform-wide and independent of any organization.

### Deployment Differences

| | Local (Open Source) | Cloud (SaaS) |
|---|---|---|
| **Available tiers** | Personal only | Global + Team + Personal |
| **KB start state** | Empty — user builds from scratch | Pre-loaded Global KB included |
| **Team collaboration** | Not applicable (single-user) | Full team sharing and org-wide access |

In the local deployment, the Personal tier provides all KB functionality. The user builds their own knowledge base using the Dashboard and ingestion tools. In the cloud deployment, the Global tier provides immediate value out of the box, the Team tier captures institutional memory, and the Personal tier remains private.

---

## Storage Architecture

### Single Collection with Metadata Filtering (CURRENT)

All knowledge tiers share **one ChromaDB collection** (`faultmaven_kb`). Scope isolation is enforced via metadata filtering at query time, not via separate collections.

| Tier | Metadata Filter | Access Rule |
|------|----------------|-------------|
| Global | `{"scope": "global"}` | Read: all users. Write: platform admin only. |
| Team | `{"scope": "team", "team_id": "<id>"}` | Read: team members. Write: team admin (or via promotion approval). |
| Personal | `{"scope": "personal", "owner_id": "<id>"}` | Read/write: owner only. Promote to Team KB via approval. |

**Why one collection, not separate collections per tier:**

1. **No N+1 query problem** — A user in 5 teams would require 7 separate queries (global + personal + 5 teams) with per-tier collections, then manual merge/dedup/sort in Python. One collection = one query.
2. **ChromaDB is optimized for few large collections** — HNSW graph indexing works best with millions of vectors in few collections, not thousands of tiny collections.
3. **Roaring Bitmap metadata filtering** — ChromaDB pre-filters metadata before graph traversal. One query, one graph, one sorted top-K result.
4. **Unified ranking** — All scopes compete in the same similarity search. A highly relevant team runbook surfaces alongside a global best practice without manual merge logic.

```text
ChromaDB Instance
│
├── faultmaven_kb                    # ALL knowledge tiers (permanent)
│   ├── scope=global                 # Pre-built troubleshooting guides
│   ├── scope=team, team_id=sre      # SRE team shared runbooks
│   ├── scope=team, team_id=platform # Platform team procedures
│   ├── scope=personal, owner_id=alice  # Alice's private runbooks
│   └── scope=personal, owner_id=bob    # Bob's private procedures
│
├── case_{case_id}                   # Per-case evidence (ephemeral)
│   └── [uploaded logs, configs, metrics]
│
└── ...
```

**Scope safety invariant:** `KnowledgeVectorStore.search()` enforces that queries against `faultmaven_kb` MUST include a scope filter (`scope`, `owner_id`, or `team_id`) in the `where` clause. Unscoped queries raise `ValueError` — converting a fail-open data leak risk into a fail-closed guarantee. This is enforced in `infrastructure/knowledge/knowledge_vector_store.py`.

**A typical scoped query** for a user who belongs to the SRE team:

```python
where = {"$or": [
    {"scope": "global"},
    {"owner_id": user_id},
    {"team_id": {"$in": user_team_ids}}
]}
collection.query(query_texts=[question], where=where, n_results=k)
```

All tiers are **permanent** — knowledge persists until explicitly deleted by the owner (user, team admin, or system admin). This is in contrast to case evidence, which is ephemeral and tied to case lifecycle.

---

## Document Storage

Runbook source files are stored on disk alongside the vector database. The source files are the authoritative record of what's in ChromaDB — they enable re-ingestion, auditing, and editing.

### Runtime Storage Layout

```text
data/
├── knowledge/                          # Runbook source files (by scope)
│   ├── global/                         # Global KB — platform admin content
│   │   ├── k8s-crashloopbackoff.md
│   │   ├── pg-connection-pool-exhaustion.md
│   │   └── ...
│   ├── team_{team_id}/                 # Team KB — team-specific runbooks
│   │   └── our-payment-failover.md
│   └── user_{user_id}/                 # Personal KB — private runbooks
│       └── my-redis-notes.md
├── chroma/                             # ChromaDB vector database (derived from knowledge/)
├── evidence/                           # Uploaded evidence files (per-case)
└── faultmaven.db                       # SQLite database
```

**Design decisions:**

- **Flat by scope.** Each scope folder (`global/`, `team_{id}/`, `user_{id}/`) contains runbook files directly — no subdirectories by domain. Domain is captured in frontmatter metadata and ChromaDB, not in folder structure.
- **Source files are retained.** Vectors are derived artifacts. If the embedding model changes, chunking parameters are tuned, or ChromaDB is rebuilt, the source files are re-ingested. Without source files, vectors cannot be regenerated.
- **Scope determines the folder, not the ingestion path.** Whether a runbook was created by the KB Toolkit, the Dashboard upload, or the document-to-runbook conversion feature, it ends up in the same `data/knowledge/{scope}/` directory.

### Authoring vs Runtime

The KB Toolkit and FaultMaven runtime use different directories:

| Path | Purpose | Who writes | Who reads |
|------|---------|-----------|-----------|
| `faultmaven-kb-toolkit/data/runbooks/` | Authoring workspace — draft, validate, score | KB Toolkit (`kb-init`, `kb-researcher`) | Toolkit CLI (`kb-validate`, `kb-quality`) |
| `faultmaven/data/knowledge/{scope}/` | Runtime storage — ingested into ChromaDB via scan → verify | Dashboard scan + verify, conversion feature | FaultMaven API (scan/verify endpoints) |
| `faultmaven/docs/operations/runbooks/` | Community contributions — shared with the open-source community | Community members | Human readers (not ingested) |

To move toolkit-generated runbooks into FaultMaven for ingestion:

```bash
# Copy validated runbooks from toolkit to FaultMaven's global KB storage
cp faultmaven-kb-toolkit/data/runbooks/**/*.md faultmaven/data/knowledge/global/

# Then scan and verify from the Dashboard (KB → Drafts → Scan for runbooks → Verify)
# Or via API:
curl -X POST http://localhost:8090/api/v1/knowledge/scan -H "Authorization: Bearer $TOKEN"
# Then verify each draft to trigger ingestion into ChromaDB
```

---

## Offline Ingestion, Live Retrieval

A critical temporal separation governs the architecture:

- **Ingestion is offline/background.** Documents are processed and stored in ChromaDB *before* the user asks questions — when an admin runs the ingestion pipeline, a team member uploads shared procedures, or a user uploads personal runbooks via the Dashboard.
- **Retrieval is live/real-time.** Q&A tools query pre-populated collections during active troubleshooting. They perform pure retrieval and chunk synthesis — no ingestion, no preprocessing, no reasoning.

```text
Background (before investigation):
├── Global:   Admin runs ingestion pipeline → runbooks validated → chunks in global_kb
├── Team:     Team member uploads shared procedure → chunks in team_{team_id}_kb
└── Personal: User uploads personal runbook via Dashboard → chunks in user_{user_id}_kb

Live (during investigation):
└── Agent calls KB tool → semantic search on appropriate collection → chunk synthesis → answer
    ├── No ingestion happening
    ├── No preprocessing happening
    └── Pure retrieval + synthesis
```

### Ingestion Details

| Aspect | Value |
|--------|-------|
| Embedding model | BGE-M3 via sentence-transformers (1024 dims, multilingual) |
| Chunking | Structure-aware splitting on markdown headers (3000-char max, 100-char min, sentence-boundary fallback) |
| Supported formats | Markdown, TXT, PDF, DOCX, CSV, JSON, YAML |
| Ingestion pipeline | `KnowledgeIngester` in `core/knowledge/ingestion.py` |
| Ingestion workflow | Dashboard scan → verify (via `conversion_service.py`) |

#### KB vs Evidence Chunking

KB and evidence use different chunking strategies. KB uses structure-aware splitting on markdown headers (each `##` section becomes one chunk; variable size 100–3000 chars). Evidence uses token-based section-aware chunking with smaller embedding units and context expansion at retrieval time. Parameters, rationale, and current-vs-target status for both strategies are canonical in [vector-retrieval-architecture.md §5](./vector-retrieval-architecture.md#5-evidence-retrieval).

This difference affects how runbook content is authored — each `##` section becomes one chunk, so authors should aim for 400–900 characters per section. See [Runbook Content Architecture §3](./runbook-content-architecture.md#why-structure-matters-for-rag) for authoring guidance.

### Metadata Stored Per Chunk

**Common fields (all tiers):**

| Field | Purpose |
|-------|---------|
| `document_id` | Unique runbook identifier |
| `title` | Runbook title |
| `domain` | Engineering vertical (database, networking, compute, etc.) |
| `service` | Specific technology (postgresql, kubernetes, redis, etc.) |
| `symptom_class` | Failure modes addressed (comma-joined list) |
| `severity` | Severity level |
| `tags` | Additional search terms (comma-joined) |
| `status` | Lifecycle state (draft, in-review, verified, stale, deprecated) |
| `last_updated` | ISO date — used for staleness detection at retrieval time |
| `document_type` | Content type |
| `source_url` | Original source reference |
| `chunk_index` | Position within the chunked document |
| `total_chunks` | Total chunks for this document |
| `created_at` | Ingestion timestamp |

The taxonomy fields (`domain`, `service`, `symptom_class`) are defined in [runbook-content-architecture.md](./runbook-content-architecture.md) and propagated to ChromaDB metadata by the [KB Toolkit](https://github.com/FaultMaven/faultmaven-kb-toolkit) ingestion pipeline. These fields enable the hybrid search filtering described below.

---

## Retrieval Architecture

### Implementation Status

For the canonical implementation status of the retrieval pipeline (hybrid search, reranking, staleness, hard pre-filter, fast mode, scope tiebreaking), see [vector-retrieval-architecture.md §7](./vector-retrieval-architecture.md#7-implementation-status). This document covers KB-specific concerns only.

| Feature | Status | Notes |
| ------- | ------ | ----- |
| Federated search across tiers | Implemented | Single `answer_from_kb` tool searches all scopes (global + personal + team) via `$or` filter |
| Single-collection storage | Implemented | One `faultmaven_kb` collection with metadata-based scope filtering |
| Scope safety invariant | Implemented | `_enforce_scope_invariant()` raises `ValueError` on unscoped queries |

#### Current Tool Architecture

| Tool (registered name) | Class | File | Collection | Purpose |
| ---------------------- | ----- | ---- | ---------- | ------- |
| `answer_from_kb` | `AnswerFromKB` | `kb_qa.py` | `faultmaven_kb` | Unified KB Q&A (global + personal + team via `$or` filter) |
| `answer_from_case_evidence` | `AnswerFromCaseEvidence` | `case_evidence_qa.py` | `case_{case_id}` | Case-scoped forensic Q&A on vectorized evidence |

The old per-scope KB tools (`global_kb_qa`, `user_kb_qa`) and the alternate `answer_from_knowledge_base` name have been replaced with the single unified `answer_from_kb` tool — scope filtering is automatic based on user context resolved by `KBToolAdapter`.

### Design Principles

Three principles govern KB retrieval. The retrieval-pipeline mechanics are canonical in [vector-retrieval-architecture.md](./vector-retrieval-architecture.md); KB-arch describes the storage-layer surface only.

1. **Federated Search** — One knowledge tool, not three. The backend resolves user scope (global + personal + team) and merges results via the `$or` filter built by `AnswerFromKB`. See [Federated Search: Implementation](#federated-search-implementation) below for the scope-filter construction.
2. **Hybrid Search** — Two-stage retrieval (vector + keyword recall → four-signal reranker). See [vector-retrieval-architecture.md §3](./vector-retrieval-architecture.md#3-two-stage-retrieval-and-reranking-pipeline) for signal weights, dynamic reweighting, and the optional `filter_mode="hard"` pre-filter.
3. **Staleness-Aware Synthesis** — Per-chunk age/status warnings injected into LLM context; freshness signal in the reranker. See [vector-retrieval-architecture.md §4](./vector-retrieval-architecture.md#staleness-aware-synthesis) for the formatter.

### Federated Search: Implementation

KB-arch owns the scope-filter construction. The full tool path (adapter → filter → query → synthesis → return) is canonical in [vector-retrieval-architecture.md §4](./vector-retrieval-architecture.md#4-knowledge-base-retrieval) under Tool Path.

`AnswerFromKB` builds an `$or` scope filter from user context (resolved by `KBToolAdapter` from `ToolContext`):

```text
{"$or": [
    {"scope": "global"},                              # all users
    {"scope": "personal", "owner_id": user_id},      # user's own
    {"scope": "team", "team_id": {"$in": team_ids}}  # user's teams
]}
```

This filter is passed to the unified `faultmaven_kb` collection in the metadata-`where` argument. The scope safety invariant (`_enforce_scope_invariant()`) rejects any KB query that arrives without a scope clause — see [Storage Architecture](#single-collection-with-metadata-filtering-current).

**Case evidence is not federated.** `answer_from_case_evidence` queries per-case `case_{case_id}` collections with a forensic synthesis prompt — fundamentally different role (diagnose vs. remediate). The evidence-vs-knowledge boundary is established in the Purpose section.

### Strategy Pattern with KBConfig

The `DocumentQATool` base class is KB-neutral — it queries via an injected `KBConfig` strategy. Two concrete configs exist:

| Config Class | File | Collection | Scope handling | Synthesis prompt |
|--------------|------|------------|----------------|------------------|
| `UnifiedKBConfig` | `kb_configs/unified_kb_config.py` | `faultmaven_kb` | `$or` filter over `scope=global`, `(scope=personal, owner_id=…)`, `(scope=team, team_id ∈ …)` | Staleness-aware, prefers verified content |
| `CaseEvidenceConfig` | `kb_configs/case_evidence_config.py` | `case_{case_id}` | Per-case isolation | Forensic — preserves chronological order, cites filename/line numbers |

**KBConfig interface** (abstract base in `modules/agent/tools/kb_config.py`):

| Method / Property | Purpose |
|-------------------|---------|
| `get_collection_name(scope_id)` | Returns ChromaDB collection name |
| `format_chunk_metadata(metadata, score)` | Formats chunk context — including staleness warnings |
| `extract_source_name(metadata)` | Extracts source attribution with scope provenance |
| `get_citation_format()` | Citation style guidance for synthesis LLM |
| `format_response(answer, sources, chunk_count, confidence)` | Formats final response for agent |
| `requires_scope_id` (property) | Whether this tier needs a scope parameter |
| `cache_ttl` (property) | Cache duration in seconds |
| `system_prompt` (property) | Synthesis LLM system prompt |

The earlier per-tier `GlobalKBConfig` / `TeamKBConfig` / `UserKBConfig` design (one config per ChromaDB collection) was replaced by the unified config — see [Storage Architecture](#single-collection-with-metadata-filtering-current) for the rationale.

### Hybrid Search and Reranking

The full hybrid pipeline (parallel vector + keyword recall, four-signal reranker, hard pre-filter mode, dynamic weights, scope tiebreaking) lives in `infrastructure/knowledge/knowledge_vector_store.py`. See [vector-retrieval-architecture.md §3](./vector-retrieval-architecture.md#3-two-stage-retrieval-and-reranking-pipeline) for the full pipeline definition. KB-specific behaviour worth flagging here:

- The taxonomy fields (`domain`, `service`, `symptom_class`, `severity`) propagated at ingestion drive the metadata-match signal in the reranker and the optional `filter_mode="hard"` pre-filter.
- Filter values come from the case context (the investigation engine's `ProblemVerification` step identifies `affected_services`) — not from the user.
- When no filter context is available (e.g., early INQUIRY phase), the search runs unfiltered.

### Staleness-Aware Synthesis

`UnifiedKBConfig.format_chunk_metadata()` inspects `last_updated` and `status` per chunk and injects warnings directly into the context the synthesis LLM sees, so the warning propagates to the user without agent-side conditional handling. Chunks with `status: deprecated` are penalised by the reranker (-0.30); deprecated runbooks should also be purged from ChromaDB per lifecycle rules. See [vector-retrieval-architecture.md §4](./vector-retrieval-architecture.md#staleness-aware-synthesis) for the formatter behaviour.

### Scope Tiebreaking

When merged chunks have equal weighted scores, scope priority breaks the tie: **Personal > Team > Global**. Rationale: a personal runbook ("our payment service fails when Redis is down due to misconfigured retry") is more specific to the user's environment than a generic global runbook, even at similar relevance scores. Implemented in `_rerank()` via `SCOPE_PRIORITY = {"personal": 0, "team": 1, "global": 2}`.

### Extensibility

Adding a new KB tier requires:

1. Create `NewTierConfig(KBConfig)` — implement the interface methods
2. Register the config in the federated search layer with its scope resolution
3. Wire collection access in the DI container

`DocumentQATool` core remains unchanged. The federated search layer discovers authorized tiers from user context.

---

## Tier 1: Global Knowledge Base

**Scope:** System-wide — accessible to all users, all cases
**Deployment:** Cloud only (pre-populated with industry-standard content)

### Characteristics

- **Lifecycle:** Permanent — managed by the platform administrator
- **Ownership:** Platform admin. Independent of any organization.
- **Content:** Pre-built troubleshooting guides, error code references, vendor documentation, industry best practices
- **Access:** Read by all users across all organizations (auto-searched by agent). Write by platform admin only.
- **Start state:** Pre-populated in cloud deployment; not available in local deployment.

### Ingestion Pipeline

Global-tier ingestion uses the same scan → verify workflow as the other tiers — drop runbooks in `data/knowledge/global/`, scan, then verify from the Dashboard Drafts tab. End-user steps are canonical in [docs/guides/knowledge-base.md](../../guides/knowledge-base.md#ingestion-in-one-paragraph).

Tier-1-specific notes: Global runbooks are written by the platform admin and apply across all organizations. On first startup, `seed_builtin_runbooks()` copies 59 built-in runbooks from `resources/knowledge/builtin/` into this directory, after which they follow the same scan → verify path. The verify step triggers YAML frontmatter parsing, structural validation (per [runbook-content-architecture.md §4 Quality Gates](./runbook-content-architecture.md#4-quality-gates)), chunking, embedding, and ChromaDB write.

### Files

| Component | Location |
|-----------|----------|
| Scan + verify workflow | `modules/knowledge/domain/services/conversion_service.py` |
| Knowledge ingester | `core/knowledge/ingestion.py` |
| KBConfig (all tiers) | `modules/agent/tools/kb_configs/unified_kb_config.py` |

---

## Tier 2: Team Knowledge Base

**Scope:** Shared across team members within an organization
**Deployment:** Cloud only

### Team KB Characteristics

- **Lifecycle:** Permanent — managed by the team administrator
- **Ownership:** Team admin within an organization. Each team has its own KB, isolated from other teams.
- **Content:** Shared runbooks, incident playbooks, on-call procedures, team-specific troubleshooting guides, institutional memory from resolved incidents
- **Access:** Read by team members. Write by team admin (directly or via promotion approval). Isolated from other teams within the same organization.
- **Value:** Captures institutional knowledge that would otherwise be lost when team members rotate or leave. The middle tier between platform-wide best practices and private notes.

### Knowledge Promotion (Personal → Team)

Individual users can contribute to the Team KB by **promoting** their personal runbooks:

1. User authors a runbook in their Personal KB
2. User submits the runbook for promotion to the Team KB
3. Team admin reviews and approves (or rejects) the promotion
4. On approval, the runbook is added to the Team KB and becomes accessible to all team members

This flow ensures team knowledge quality is governed by the team admin while enabling bottom-up contribution from any team member. The original remains in the user's Personal KB.

### Implementation Status

Team KB scope filtering is **implemented end-to-end**:

- Team and organization models exist in the auth module (`modules/auth/domain/models/`)
- `team_members` junction table supports multi-team membership per user
- `TeamService.list_all_user_team_ids(user_id)` resolves all team memberships across orgs
- Team IDs are wired into `ToolContext.team_ids` during agent execution (via `AgentOrchestrationService`)
- The unified `answer_from_kb` tool builds a combined filter: `{"$and": [{"scope": "team"}, {"team_id": {"$in": team_ids}}]}`
- ChromaDB metadata stores `scope` + `team_id` at ingestion time
- API endpoints (`GET /knowledge/documents`) support `scope=team` filter with team membership check

**Remaining work:**

1. Team KB management API endpoints (upload, list, delete restricted to team admin role)
2. Promotion workflow (personal → team: submit, review, approve/reject with team admin approval gate)

---

## Tier 3: Personal Knowledge Base

**Scope:** Private to one user — accessible across all the user's cases
**Deployment:** Both Local and Cloud

### Personal KB Characteristics

- **Lifecycle:** Permanent — persists with user account
- **Ownership:** Individual user within an organization
- **Content:** Personal runbooks, private notes and drafts, personal checklists, lessons learned
- **Access:** Owner only. Other users cannot access. User can promote runbooks to Team KB (requires team admin approval).
- **Start state:** Empty in both deployments — user builds from scratch.

In the local (single-user) deployment, this is the only KB tier available. The user builds their entire knowledge base here. In the cloud deployment, users can additionally promote their personal runbooks to the Team KB for shared access.

### Storage Architecture

**Write path**: `upload_document()` and `verify_draft()` → `ingest_runbook()`, which writes the relational `knowledge_items` row first (source-of-truth) and then the ChromaDB chunks + embeddings. Both stores receive the same `kb_<uuid>` id; ChromaDB-side failures leave the SQL row in place for a future scan-and-recover pass to re-embed (rolling back would erase the only signal that re-embedding is needed). Conversion-bookkeeping rows (`conversion_jobs`, `conversion_drafts`) are written in addition for the upload-flow audit trail.

**Read path**: `list_documents()` and `get_document()` read from **SQLite** (`conversion_drafts` joined with `conversion_jobs`). Full content read from markdown file on disk. ChromaDB is not queried for listing or retrieval.

**Delete path**: `delete_document()` sets SQLite status to `deprecated` (the lifecycle terminal state per [runbook-content-architecture.md §5](./runbook-content-architecture.md#lifecycle-states) — "Replaced by a newer runbook or no longer applicable" extends to owner-initiated removal), then removes chunks from ChromaDB via `delete_documents_by_parent_id()`.

**Search path**: `hybrid_search()` queries ChromaDB with explicit BGE-M3 embeddings (1024 dims). Two-stage: vector + keyword recall, then 4-signal reranker.

### API Endpoints

Managed through the Knowledge module (`/api/v1/knowledge/`):

- `POST /documents` — Upload document (SQLite record + chunked vector indexing)
- `GET /documents` — List with filtering (type, tags, scope, domain, service, severity) — reads from SQLite
- `GET /documents/{id}` — Get specific document (SQLite + file from disk)
- `PUT /documents/{id}` — Update document
- `DELETE /documents/{id}` — Delete document and remove from ChromaDB
- `POST /search` — Semantic search across user's KB

### Personal KB Files

Personal KB shares the same unified infrastructure as Global and Team — scope is metadata-only.

| Component | Location |
|-----------|----------|
| Vector store (all tiers) | `infrastructure/knowledge/knowledge_vector_store.py` |
| Document inventory (SQLite) | `infrastructure/persistence/kb_document_repository.py` |
| KBConfig (all tiers) | `modules/agent/tools/kb_configs/unified_kb_config.py` |
| Knowledge service | `modules/knowledge/domain/services/knowledge_service.py` |
| Knowledge routes | `modules/knowledge/api/routes.py` |
| Domain model | `modules/knowledge/domain/models/knowledge_item.py` |

---

## Access Control

| Action | Global | Team | Personal |
|--------|--------|------|----------|
| **Upload directly** | Platform admin only | Team admin only | Owner only (Dashboard UI) |
| **Promote from Personal** | — | Team member submits, team admin approves | — (source tier) |
| **Query during case** | All users (auto-searched) | Team members only | Owner only |
| **Delete** | Platform admin only | Team admin | Owner only |
| **Cross-user access** | Shared read-only (all orgs) | Shared read-only (within team) | Forbidden |

---

## Runbook Catalog API

The Dashboard displays a catalog of all ingested runbooks — coverage overview, quality scores, staleness, and gap identification. This is served by a dedicated API endpoint.

### `GET /api/v1/knowledge/catalog`

Returns metadata for all runbooks the user has access to (filtered by scope/authorization).

**Response:**

```json
{
  "generated_at": "2026-03-24T10:00:00Z",
  "total": 12,
  "by_domain": { "compute": 5, "database": 3, "networking": 2, "messaging": 1, "security": 1 },
  "runbooks": [
    {
      "id": "k8s-crashloopbackoff",
      "title": "Kubernetes CrashLoopBackOff",
      "domain": "compute",
      "service": "kubernetes",
      "severity": "high",
      "status": "draft",
      "symptom_class": "crash_loop",
      "last_updated": "2026-03-23",
      "quality_score": 96.0,
      "version": "1.0.0",
      "verified_by": "kb-researcher"
    }
  ]
}
```

**Query parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `domain` | string | Filter by domain |
| `service` | string | Filter by service |
| `status` | string | Filter by lifecycle status |
| `scope` | string | Filter by KB tier (global, team, personal) |

**Dashboard UI:** The KB page shows a sortable, filterable table with domain grouping, color-coded severity and status, and gap indicators for domains with no runbooks. Clicking a runbook opens it for review/editing.

**Implementation:** The endpoint reads `KnowledgeItem` records from the database (not from disk). The quality score and research metadata are stored on the `KnowledgeItem` model. The KB Toolkit's `kb-catalog` CLI produces the same output format (JSON mode) for offline/admin use.

---

## Agent Tool Usage

During investigation, the agent has two retrieval tools — one for knowledge, one for evidence:

| Question Type | Tool | Example |
|---------------|------|---------|
| Remediation knowledge | `answer_from_kb` | "How to fix PostgreSQL connection pool exhaustion?" |
| Remediation with context | `answer_from_kb` (with case context metadata) | Same question, but `context_metadata={"domain": "database", "service": "postgresql"}` derived from the case's `affected_services` |
| Case-specific evidence | `answer_from_case_evidence` | "What errors are on line 1045 of the uploaded server.log?" |

The agent does not decide which KB tier to search — the federated search layer handles that automatically based on the user's authorization context. The agent focuses on *what to ask*, not *where to look*.

---

## Related Documents

### Knowledge Architecture

- **[Runbook Content Architecture](./runbook-content-architecture.md)** — What goes INTO the KB: taxonomy, templates, quality gates, lifecycle. Companion to this document.

### Evidence Architecture (Separate Concern)

- **[Case Evidence Store](../case-and-session/case-evidence-store.md)** — Storage and lifecycle for case-specific diagnostic evidence
- **[Data Preprocessing Design](../data-processing/data-preprocessing-design-specification.md)** — How submitted evidence is classified, extracted, and indexed

### Investigation Context

- **[Investigation Lifecycle Logic](../investigation-engine/investigation-lifecycle-logic.md)** — How both knowledge and evidence feed into the investigation engine
