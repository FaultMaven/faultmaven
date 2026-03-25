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
| `faultmaven/data/knowledge/{scope}/` | Runtime storage — ingested into ChromaDB | Ingestion pipeline, Dashboard upload, conversion feature | `ingest_runbooks.py`, FaultMaven API |
| `faultmaven/docs/operations/runbooks/` | Community contributions — shared with the open-source community | Community members | Human readers (not ingested) |

To move toolkit-generated runbooks into FaultMaven for ingestion:

```bash
# Copy validated runbooks from toolkit to FaultMaven's global KB storage
cp faultmaven-kb-toolkit/data/runbooks/**/*.md faultmaven/data/knowledge/global/

# Ingest into ChromaDB
cd faultmaven
python -m faultmaven.scripts.ingest_runbooks --runbook-dir data/knowledge/global
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
| Chunking | 1000-character chunks, 200-character overlap, sentence boundary splitting |
| Supported formats | Markdown, TXT, PDF, DOCX, CSV, JSON, YAML |
| Ingestion pipeline | `KnowledgeIngester` in `core/knowledge/ingestion.py` |
| Batch ingestion | `scripts/ingest_runbooks.py` (with validation, change detection, progress tracking) |

#### KB vs Evidence Chunking — Why They Differ

Knowledge and evidence use different chunking strategies because they serve different retrieval needs:

| Aspect | Knowledge (Runbooks) | Evidence (Logs, Configs, Metrics) |
|--------|---------------------|----------------------------------|
| **Strategy** | Character-based with sentence boundary splitting | Token-based with section-aware splitting |
| **Chunk size** | 1000 characters, 200-char overlap | 4000 tokens (~16KB), section-aware |
| **Implementation** | `core/knowledge/ingestion.py:350` | `services/preprocessing/chunking_service.py:33` |
| **Rationale** | Runbooks are well-structured markdown with predictable sections. Smaller chunks ensure each chunk is topically focused — a diagnostic step doesn't share a chunk with an unrelated prevention tip. Character-based splitting is sufficient because markdown structure provides natural boundaries. | Evidence files are heterogeneous (logs, CSVs, JSON configs) with no predictable structure. Larger chunks preserve context — a log entry only makes sense with surrounding entries. Section-aware splitting respects structural boundaries within files (e.g., config file sections, log timestamp groups). |
| **Impact on retrieval** | Small, focused chunks → high precision per chunk, multiple chunks needed for full answer | Large, context-rich chunks → each chunk provides enough context for forensic analysis |

This difference is intentional and affects how content is authored. Runbook authors should keep related information (symptoms + error messages, diagnostic commands + expected output) within the same section to ensure they land in the same chunk. The [Runbook Content Architecture](./runbook-content-architecture.md) template is designed with this chunking strategy in mind.

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

This section documents the **current implementation** and **planned improvements**. Features marked as **PLANNED** represent the target design; features marked as **CURRENT** describe what exists in code today.

| Feature | Status | Current Reality | Target Design |
|---------|--------|-----------------|---------------|
| Federated search | **PLANNED** | 3 separate tools: `global_kb_qa`, `user_kb_qa`, `answer_from_case_evidence` | Single `answer_from_knowledge_base` tool |
| Collection naming | **CURRENT** | Single unified collection `faultmaven_kb` with metadata-based scope filtering. `KnowledgeVectorStore` enforces scope invariant. | No change needed |
| Hybrid search (metadata filtering) | **PLANNED** | Pure vector similarity, no metadata filtering | `domain_filter`/`service_filter` from case context |
| Staleness-aware synthesis | **PLANNED** | No staleness warnings injected | Warning injection via `format_chunk_metadata()` |
| Fast-track confidence | **CURRENT** | `KB_FAST_TRACK_THRESHOLD = 0.7` in `milestone_engine.py:3788` | No change needed |
| Tier-based reranking | **PLANNED** | N/A (single-tier queries) | Personal > Team > Global tiebreaker |
| Team KB | **PLANNED** | Infrastructure exists, no `TeamKBConfig` | Full team KB with promotion workflow |

#### Current Baseline (3-Tool Architecture)

The milestone engine (`milestone_engine.py:2206-2207`) registers tools individually:

```python
has_global_kb = "global_kb_qa" in tool_names
has_user_kb = "user_kb_qa" in tool_names
```

The agent must decide which KB to query. During INQUIRY (fast-track resolution), this adds reasoning overhead — the agent shouldn't need to choose between global and personal KB.

| Tool (Current) | Collection | Purpose |
|----------------|------------|---------|
| `global_kb_qa` | `global_kb` | System-wide remediation knowledge |
| `user_kb_qa` | `user_{user_id}_kb` | User's personal runbooks |
| `answer_from_case_evidence` | `case_{case_id}_evidence` | Forensic analysis of uploaded case evidence |

### Design Principles

Three principles govern the **target retrieval design**:

1. **Federated Search** — The agent calls one knowledge tool, not three. The backend searches all authorized tiers concurrently and merges results. (**PLANNED** — current baseline uses 3 separate tools.)
2. **Hybrid Search** — Vector similarity is augmented with metadata filtering (domain, service) derived from case context, reducing irrelevant retrievals. (**PLANNED** — metadata fields are stored at ingestion time but no query path uses them yet.)
3. **Staleness-Aware Synthesis** — The synthesis LLM sees lifecycle warnings (stale, deprecated) injected directly into chunk context, and propagates them to the user. (**PLANNED** — `last_updated` and `status` are stored in metadata; injection logic not yet implemented.)

### Federated Search: One Knowledge Tool (PLANNED)

**The problem with the current 3-tool approach:** LLMs degrade when given too many overlapping tools. With separate `global_kb_qa` and `user_kb_qa` plus the case evidence tool, the agent wastes reasoning tokens deciding *which library to visit* instead of *what to ask*. The tier distinction matters for governance (who can write), not for retrieval (who can read).

**The target design:** A single `answer_from_knowledge_base` tool that performs a federated search across all authorized tiers.

```text
Agent calls: answer_from_knowledge_base(question, domain_filter?, service_filter?)
  │
  ├── Backend resolves user context (org_id, team_id, user_id)
  │
  ├── Concurrent search across authorized collections:
  │   ├── global_kb                    (all users)
  │   ├── team_{team_id}_kb           (if user belongs to team)
  │   └── user_{user_id}_kb           (user's own)
  │
  ├── Merge chunks from all tiers, rerank by score (with tier tiebreaker)
  │
  ├── Inject staleness warnings for stale chunks (see below)
  │
  ├── Synthesis LLM produces unified answer with tier provenance in citations
  │   e.g., "[Global KB: pg-connection-pool-runbook] ..."
  │   e.g., "[Team KB: our-pg-failover-procedure] ..."
  │
  └── Return answer to agent
```

**Case evidence remains a separate tool.** `answer_from_case_evidence` is not part of the federated search. Evidence uses a forensic synthesis prompt and serves a fundamentally different role (diagnose) than knowledge (remediate). This boundary is the evidence-vs-knowledge distinction established in the Purpose section.

**Target tool interface (2 tools, down from current 3):**

| Tool | Parameters | Purpose |
|------|-----------|---------|
| `answer_from_knowledge_base` | `question`, `domain_filter?`, `service_filter?` | Remediation knowledge from all authorized KB tiers |
| `answer_from_case_evidence` | `case_id`, `question` | Forensic analysis of uploaded case evidence |

### Strategy Pattern with KBConfig (Preserved)

The federated search changes the tool interface, not the internal architecture. Each tier still has its own `KBConfig` that handles collection naming, citation formatting, and cache TTL. The `DocumentQATool` core remains KB-neutral — it queries one collection at a time. The federated search layer orchestrates concurrent calls across configs and merges the results.

**KBConfig interface** (abstract base in `modules/agent/tools/kb_config.py`):

| Method / Property | Purpose |
|-------------------|---------|
| `get_collection_name(scope_id)` | Returns ChromaDB collection name for this tier |
| `format_chunk_metadata(metadata, score)` | Formats chunk context — including staleness warnings |
| `extract_source_name(metadata)` | Extracts source attribution with tier provenance |
| `get_citation_format()` | Citation style guidance for synthesis LLM |
| `format_response(answer, sources, chunk_count, confidence)` | Formats final response for agent |
| `requires_scope_id` (property) | Whether this tier needs a scope parameter |
| `cache_ttl` (property) | Cache duration in seconds |
| `system_prompt` (property) | Synthesis LLM system prompt |

**Tier configurations:**

| Config Class | Collection | Scope | Cache | Citation Prefix |
|-------------|------------|-------|-------|-----------------|
| `GlobalKBConfig` | `global_kb` | none | 7 days | `[Global KB: ...]` |
| `TeamKBConfig` | `team_{team_id}_kb` | team_id | 12 hours | `[Team KB: ...]` |
| `UserKBConfig` | `user_{user_id}_kb` | user_id | 24 hours | `[Personal KB: ...]` |

### Hybrid Search: Metadata Filtering (PLANNED)

> **Implementation status:** The taxonomy metadata fields (`domain`, `service`, `symptom_class`) are stored in ChromaDB at ingestion time via the KB Toolkit pipeline. However, no query path currently uses them — all retrieval is pure vector similarity. This is the **single highest-value missing feature** for retrieval precision.

Pure vector similarity search has a precision problem: "connection pool exhausted" retrieves PostgreSQL, MySQL, and Redis runbooks with similar scores. The agent needs the PostgreSQL runbook — not all three.

**The target design:** The federated search accepts optional `domain_filter` and `service_filter` parameters that are passed as ChromaDB `where` clauses, narrowing the search space before vector similarity runs.

```text
Agent calls: answer_from_knowledge_base(
    question="How to fix connection pool exhaustion?",
    domain_filter="database",
    service_filter="postgresql"
)

Backend:
  1. Build where_clause: {"domain": "database", "service": "postgresql"}
  2. For each authorized collection:
     collection.query(
         query_embedding=embed(question),
         where=where_clause,       # Metadata pre-filter
         n_results=k * 2           # Oversample for reranking
     )
  3. Merge results across tiers, rerank by score
  4. Return top-k chunks to synthesis LLM
```

**Where do the filters come from?** Not from the user — from the **case context**. The investigation engine already identifies the affected service during the `ProblemVerification` step (part of INQUIRY → INVESTIGATING transition). The agent can derive `domain_filter` and `service_filter` from the case's `affected_services` property without any user interaction.

**Fallback:** If no filters are provided (e.g., early in investigation before problem verification), the search runs unfiltered across all chunks — same behavior as pure vector search.

### Staleness-Aware Synthesis (PLANNED)

The [Runbook Content Architecture](./runbook-content-architecture.md) defines lifecycle states (DRAFT, IN-REVIEW, VERIFIED, STALE, DEPRECATED) and staleness rules (>6 months since `last_updated`). The retrieval architecture must act on this at runtime.

**The solution:** `KBConfig.format_chunk_metadata()` inspects the `last_updated` and `status` fields in each chunk's metadata. If the runbook is stale or deprecated, the formatter injects a warning directly into the context text that the synthesis LLM sees:

```text
Normal chunk context:
  [Global KB: pg-connection-pool-runbook, section: Diagnostic Steps]
  SELECT count(*), state FROM pg_stat_activity GROUP BY state;
  ...

Stale chunk context:
  [Global KB: pg-connection-pool-runbook, section: Diagnostic Steps]
  ⚠️ WARNING: This runbook was last updated on 2025-06-15 (>6 months ago).
  Commands and procedures may be outdated. Verify before executing.
  SELECT count(*), state FROM pg_stat_activity GROUP BY state;
  ...
```

**Why inject into context, not handle in the agent?** The synthesis LLM naturally includes the warning in its answer because it's part of the retrieved text. No special agent logic needed — the warning propagates to the user as a natural part of the response. This is simpler and more reliable than conditional agent-side handling.

**Deprecated content:** Chunks with `status: deprecated` are excluded from retrieval results entirely. They should not be in ChromaDB (deprecated runbooks are purged per lifecycle rules), but the filter provides defense in depth.

**Staleness computation:** `format_chunk_metadata()` computes staleness on the fly from `last_updated`, independent of whether a background job has formally transitioned the runbook to STALE status. This means staleness warnings work even before the lifecycle state machine is fully implemented.

### Fast-Track Confidence Threshold (CURRENT)

The investigation lifecycle defines a fast-track path: INQUIRY → RESOLVED when a KB search finds a high-confidence match. This is **already implemented** in the milestone engine.

**Threshold:** `KB_FAST_TRACK_THRESHOLD = 0.7` (70% cosine similarity)
**Location:** `milestone_engine.py:3788`

**Signal path:**

```text
1. Agent calls KB tool during INQUIRY phase
2. DocumentQATool returns chunks with cosine similarity scores
3. Milestone engine stores the best match in case.inquiry.knowledge_matches
4. _check_fast_track_resolution() validates:
   - knowledge_resolution exists (agent proposed a KB-based answer)
   - best_match.relevance_score >= 0.7 (threshold met)
5. If both: INQUIRY → RESOLVED (fast-track)
6. If score < 0.7: fast-track blocked, continues to INVESTIGATING
```

**Why 0.7?** Cosine similarity of 0.7 with BGE-M3 embeddings indicates strong semantic alignment — the query and the runbook are addressing the same failure mode. Below 0.7, the match is likely tangential (e.g., same technology but different failure mode). This threshold was tuned against real incident queries and may need adjustment as the KB grows.

### Tier-Based Reranking (PLANNED)

When the federated search (once implemented) merges chunks from Global, Team, and Personal KBs, all tiers produce cosine similarity scores from the same embedding model — scores are directly comparable. However, **tier provenance should influence ranking** as a tiebreaker.

**Rationale:** A personal runbook that says "our payment service fails when Redis is down due to misconfigured retry" is more specific and more valuable than a global runbook about generic Redis troubleshooting, even if both score similarly on "Redis connection failure."

**Tiebreaker policy (applied when scores are within 0.05 of each other):**

1. **Personal** — highest priority. User-authored content is the most specific to their environment.
2. **Team** — second priority. Captures institutional memory specific to the organization.
3. **Global** — lowest priority. Generic best practices, valuable but less specific.

**Implementation approach:** After merging chunks from all tiers, apply a small score boost:

```text
adjusted_score = raw_score + tier_bonus
  where tier_bonus:
    Personal: +0.03
    Team:     +0.02
    Global:   +0.00
```

This ensures that when a personal runbook and a global runbook score within 0.05 of each other, the personal one surfaces first. At score gaps larger than 0.05, the raw relevance score dominates — a highly relevant global runbook still beats a weakly relevant personal one.

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

```bash
# Batch ingestion with validation (Global KB)
python -m faultmaven.scripts.ingest_runbooks --runbook-dir data/knowledge/global --validate

# Filter by domain or status
python -m faultmaven.scripts.ingest_runbooks --runbook-dir data/knowledge/global --domain database --status verified

# Dry run (validate without ingesting)
python -m faultmaven.scripts.ingest_runbooks --runbook-dir data/knowledge/global --dry-run
```

The pipeline includes YAML frontmatter parsing, structural validation, MD5-based change detection, and progress tracking. For content standards enforced by this pipeline, see [runbook-content-architecture.md](./runbook-content-architecture.md).

### Files

| Component | Location |
|-----------|----------|
| Ingestion pipeline | `scripts/ingest_runbooks.py` |
| Knowledge ingester | `core/knowledge/ingestion.py` |
| KBConfig | `modules/agent/tools/kb_configs/global_kb_config.py` |

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

Team KB is **designed but not yet fully implemented**. The infrastructure supports it:

- Team and organization models exist in the auth module (`modules/auth/domain/models/`)
- `KBConfig` Strategy Pattern supports adding Team KB with zero changes to `DocumentQATool`
- Collection naming convention defined: `team_{team_id}_kb`

**Remaining work:**

1. Create `TeamKBConfig(KBConfig)` implementation
2. Register `TeamKBConfig` in the federated search layer (no separate tool wrapper needed)
3. Add team KB management API endpoints (upload, list, delete for team admin)
4. Implement promotion workflow (submit, review, approve/reject) with team admin approval gate
5. Wire team_id and organization_id scoping into the knowledge module

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

### API Endpoints

Managed through the Knowledge module (`/api/v1/knowledge/`):

- `POST /documents` — Upload document (with background embedding)
- `GET /documents` — List with filtering (type, tags)
- `GET /documents/{id}` — Get specific document
- `PUT /documents/{id}` — Update document
- `DELETE /documents/{id}` — Delete document and remove from ChromaDB
- `POST /search` — Semantic search across user's KB

### Personal KB Files

| Component | Location |
|-----------|----------|
| Vector store | `infrastructure/persistence/user_kb_vector_store.py` |
| KBConfig | `modules/agent/tools/kb_configs/user_kb_config.py` |
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
| Remediation knowledge | `answer_from_knowledge_base` | "How to fix PostgreSQL connection pool exhaustion?" |
| Remediation with context | `answer_from_knowledge_base` (with filters) | Same question, but `domain_filter="database"`, `service_filter="postgresql"` derived from case context |
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
