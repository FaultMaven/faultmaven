# Knowledge Base Architecture

**Document Type:** Component Specification
**Version:** 9.1
**Last Updated:** 2026-07-15

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
| **Global** | System-wide, all organizations | Platform admin | Pre-built troubleshooting guides, industry best practices, vendor documentation | Both Standalone and Cloud (pre-populated) |
| **Team** | Shared across team members within an organization | Team admin | Shared runbooks, incident logs, institutional memory | Cloud only |
| **Personal** | Private to one user within an organization | Individual user | Private notes, personal runbooks, drafts | Both Standalone and Cloud |

Team and Personal KBs are scoped to an **organization**. A user belongs to an organization, and their Personal KB and Team KB access are determined by that membership. The Global KB is platform-wide and independent of any organization.

### Deployment Differences

| | Standalone | Cloud |
|---|---|---|
| **Available scopes** | Global + Personal | Global + Team + Personal |
| **KB start state** | Ships with the global runbook pack | Ships with the global runbook pack |
| **Team collaboration** | Not applicable (single-user) | Full team sharing and org-wide access |

Both deployments ship with the same global runbook pack, so neither starts empty. In Standalone, the Global scope provides the shipped runbooks and the Personal scope holds the operator's own runbooks and drafts. In Cloud, multi-tenancy additionally activates the Team scope for runbooks shared across an organization (institutional memory), while the Personal scope remains private.

---

## Storage Architecture

### Single Collection with Metadata Filtering (CURRENT)

All knowledge tiers share **one ChromaDB collection** (`faultmaven_kb`). Scope isolation is enforced via metadata filtering at query time, not via separate collections.

| Tier | Read filter arm | Access Rule |
|------|----------------|-------------|
| Global | `{"scope": "global"}` | Read: all users. Write: platform admin only. |
| Team | `{"parent_document_id": {"$in": shared_ids}}` — ids resolved from `resource_shares` | Read: team members. Write: team admin (or via promotion approval). |
| Personal | `{"owner_id": "<id>"}` | Read/write: owner only. Promote to Team KB via approval. |

**Team visibility is not a metadata tag.** A team-shared item keeps its personal floor in ChromaDB metadata (`scope=personal`, `owner_id=<author>`) — no `team`/`team_id` is ever written. Team membership becomes visibility at query time: the `resource_shares` table (ADR-013 §D4) is the single source of truth, and its `knowledge_item` ids for the caller's teams (`resolve_shared_kb_ids`) are injected as the `parent_document_id` `$in` allowlist arm. This makes sharing unshare-proof — dropping a share row removes visibility with nothing to clean up in the vector store.

**Why one collection, not separate collections per tier:**

1. **No N+1 query problem** — A user in 5 teams would require 7 separate queries (global + personal + 5 teams) with per-tier collections, then manual merge/dedup/sort in Python. One collection = one query.
2. **ChromaDB is optimized for few large collections** — HNSW graph indexing works best with millions of vectors in few collections, not thousands of tiny collections.
3. **Roaring Bitmap metadata filtering** — ChromaDB pre-filters metadata before graph traversal. One query, one graph, one sorted top-K result.
4. **Unified ranking** — All scopes compete in the same similarity search. A highly relevant team runbook surfaces alongside a global best practice without manual merge logic.

```text
ChromaDB Instance
│
├── faultmaven_kb                    # ALL knowledge tiers (permanent)
│   ├── scope=global                 # Pre-built troubleshooting guides (org-free platform tier)
│   ├── scope=personal, owner_id=alice  # Alice's private runbooks (team shares stay on this floor)
│   └── scope=personal, owner_id=bob    # Bob's private procedures
│   #  No scope=team rows: team visibility is resolved at query time from the
│   #  resource_shares id-allowlist, not stored as vector metadata.
│
├── case_{case_id}                   # Per-case evidence (ephemeral)
│   └── [uploaded logs, configs, metrics]
│
└── ...
```

**Scope safety invariant:** `KnowledgeVectorStore.search()` enforces that queries against `faultmaven_kb` MUST include a scope filter — one of the `SCOPE_FILTER_KEYS` (`scope`, `owner_id`, `organization_id`, or `parent_document_id`) — in the `where` clause. Unscoped queries raise `ValueError` — converting a fail-open data leak risk into a fail-closed guarantee. This is enforced in `infrastructure/knowledge/knowledge_vector_store.py`.

**A typical scoped query** for a user who belongs to the SRE team (built by `build_kb_scope_filter`):

```python
where = {"$or": [
    {"scope": "global"},
    {"owner_id": user_id},
    {"parent_document_id": {"$in": shared_ids}}  # ids shared to the user's teams
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
| `faultmaven/resources/knowledge/runbooks/<domain>/` | **Authoritative built-in runbook sources** (public, transparent, PR-able). Edit/contribute here. | Maintainers, community PRs; KB Toolkit tools | KB Toolkit (`kb-validate`, `kb-quality`, `kb-build-pack`) |
| `faultmaven/resources/knowledge/pack/` (or `KB_PACK_DIR`) | The **KB pack** — generated from the sources above (runbooks + build-time vectors), ingested at startup with no embedding model | `kb-build-pack` (toolkit), vendored here | FaultMaven API |
| `faultmaven/data/knowledge/{scope}/` | Runtime workspace for **authored / converted** runbooks (draft → verify flow) and `/knowledge/scan` reconciliation. Pre-deployed runbooks no longer use this directory. | Conversion feature, manual file drop | FaultMaven API |
| `faultmaven/docs/operations/runbooks/` | Community contributions — shared with the open-source community | Community members | Human readers (not ingested) |

To add/update a built-in runbook: edit the source in this repo, rebuild the **KB
pack** with the toolkit, and deliver it — no app-image rebuild needed:

```bash
# 1. Edit the authoritative source (public, PR-able):
#    faultmaven/resources/knowledge/runbooks/<domain>/<runbook>.md

# 2. Rebuild the pack in faultmaven-kb-toolkit (it reads the public sources):
kb-build-pack --version 2026-07-08 --tar

# 3. Vendor it as the committed baseline (or deliver to a running deployment's
#    KB_PACK_DIR — see kb-pack-architecture.md for local/cloud delivery):
cp -r dist/kb-pack/* faultmaven/resources/knowledge/pack/

# 4. Restart the API — the startup bootstrap ingests the pack idempotently
#    (no model; unchanged runbooks skipped, removed ones pruned).
./faultmaven.sh restart
```

Pre-deployed runbooks bypass the `conversion_drafts` table entirely. See [`kb-pack-architecture.md`](./kb-pack-architecture.md) for the pack format, `KB_PACK_DIR`, build and delivery, and [`kb-ingestion-architecture.md`](./kb-ingestion-architecture.md) for the two-path ingestion model and atomicity guarantees.

---

## Offline Ingestion, Live Retrieval

A critical temporal separation governs the architecture:

- **Ingestion is offline/background.** Documents are processed and stored in ChromaDB *before* the user asks questions — when an admin runs the ingestion pipeline, a team member uploads shared procedures, or a user uploads personal runbooks via the Dashboard.
- **Retrieval is live/real-time.** Q&A tools query pre-populated collections during active troubleshooting. They perform pure retrieval and chunk synthesis — no ingestion, no preprocessing, no reasoning.

```text
Background (before investigation):
├── Global:   Admin runs ingestion pipeline → runbooks validated → chunks in faultmaven_kb (scope=global)
├── Team:     Team member uploads shared procedure → chunks in faultmaven_kb (personal floor + resource_shares row)
└── Personal: User uploads personal runbook via Dashboard → chunks in faultmaven_kb (scope=personal)

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
| Ingestion entry point | `KnowledgeService.ingest_runbook()` (atomic: SQL row + ChromaDB chunks or neither) |
| Pre-deployed runbooks | Startup bootstrap — `faultmaven/bootstrap/kb_init.py` ingests the **KB pack** (pre-chunked + pre-embedded; no model) via `ingest_runbook(prechunked=...)`, content-hash idempotent |
| Case-generated drafts | `conversion_drafts` → `ConversionService.verify_draft()` (atomic: status flips to VERIFIED only after successful ingestion) |
| Architecture detail | [`kb-ingestion-architecture.md`](./kb-ingestion-architecture.md) |

#### KB vs Evidence Chunking

KB and evidence use different chunking strategies. KB uses structure-aware splitting on markdown headers (each `##` section becomes one chunk; variable size 100–3000 chars). Evidence uses token-based section-aware chunking with smaller embedding units and context expansion at retrieval time. Parameters, rationale, and current-vs-target status for both strategies are canonical in [vector-retrieval-architecture.md §5](./vector-retrieval-architecture.md#5-evidence-retrieval).

This difference affects how runbook content is authored — each `### Cause` subsection becomes one chunk, so authors should aim for 400–1200 characters per subsection. See [Runbook Content Architecture §3](./runbook-content-architecture.md#why-structure-matters-for-rag) for authoring guidance.

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
    {"scope": "global"},                                 # all users
    {"owner_id": user_id},                               # user's own
    {"parent_document_id": {"$in": shared_ids}}          # ids shared to user's teams
]}
```

The `shared_ids` arm is resolved from `resource_shares` (`resolve_shared_kb_ids`) — the personal/global arms come straight from the caller's own ids, so a filter built for one user can never surface another's non-shared content. Empty `shared_ids` collapses the filter to `personal ∪ global`.

This filter is passed to the unified `faultmaven_kb` collection in the metadata-`where` argument. The scope safety invariant (`_enforce_scope_invariant()`) rejects any KB query that arrives without a scope clause — see [Storage Architecture](#single-collection-with-metadata-filtering-current).

**Case evidence is not federated.** `answer_from_case_evidence` queries per-case `case_{case_id}` collections with a forensic synthesis prompt — fundamentally different role (diagnose vs. remediate). The evidence-vs-knowledge boundary is established in the Purpose section.

### Strategy Pattern with KBConfig

The `DocumentQATool` base class is KB-neutral — it queries via an injected `KBConfig` strategy. Two concrete configs exist:

| Config Class | File | Collection | Scope handling | Synthesis prompt |
|--------------|------|------------|----------------|------------------|
| `UnifiedKBConfig` | `kb_configs/unified_kb_config.py` | `faultmaven_kb` | `$or` filter over `scope=global`, `owner_id=…`, `parent_document_id ∈ shared_ids` | Staleness-aware, prefers verified content |
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
**Deployment:** Both Standalone and Cloud (ships pre-populated with the global runbook pack)

### Characteristics

- **Lifecycle:** Permanent — managed by the platform administrator
- **Ownership:** Platform admin. Independent of any organization.
- **Content:** Pre-built troubleshooting guides, error code references, vendor documentation, industry best practices
- **Access:** Read by all users across all organizations (auto-searched by agent). Write by platform admin only.
- **Start state:** Pre-populated in both deployments via the shipped global runbook pack.

**Global-authoring enforcement (single source of truth):** every path that authors global-scope content applies one policy —
`modules/knowledge/domain/global_authoring.py` (`ensure_global_authoring_allowed` / `is_global_authoring_allowed`), reused at the API layer by `modules/knowledge/api/platform_tier.py`. It refuses any tenant session under `TENANT_PROVIDER=multi` (org admins included — global content ships only via the audited `kb_seed` maintenance job, #770) and requires the `admin` role single-tenant. Enforcement points:

- **Creation routes** (`convert`, `runbooks/create`, `documents` upload, `suggestions/{id}/approve`) — the scope is a request field, gated at the route (403).
- **Publish / mint** (`verify_draft`, `verify-batch`, `scan`) — the scope is only known after the conversion-job row or on-disk file is inspected, so the gate lives in the service: `verify_draft` refuses to publish a `global` draft (`AuthorizationError` → 403), and `scan` skips global-inferred files a caller may not author while still discovering personal/team ones. This closes the pre-#770 hole where a non-admin could verify a system-owned global draft or mint a global draft via scan.

### Ingestion Pipeline

Global-tier ingestion runs automatically at API startup via the **KB bootstrap** (`faultmaven/bootstrap/kb_init.py`). The bootstrap loads the shipped **KB pack** (`faultmaven/bootstrap/kb_pack.py`) and, for each runbook in it, writes a row to `knowledge_items` plus the pack's pre-computed chunk vectors to ChromaDB via `KnowledgeService.ingest_runbook(prechunked=...)`. Because the pack ships pre-chunked and pre-embedded, startup does **no chunking and no embedding** — it is pure SQL + vector writes and completes in seconds. Idempotent: unchanged runbooks are skipped via content-hash comparison on every restart, and runbooks no longer in the pack are pruned from both stores.

Tier-1-specific notes: Global runbooks are written by the platform admin and apply across all organizations. The 91 built-in runbooks ship pre-chunked and pre-embedded in the **KB pack** (`resources/knowledge/pack`, or `KB_PACK_DIR`); on startup the bootstrap ingests them directly — no separate "verify" step is needed because the platform vendor / admin is the verifier for pre-deployed content. The verify-via-Dashboard flow is reserved for case-generated and document-converted drafts that need a human gate.

The **conversion-drafts path** (case-generated / document-converted content, not the pack) is the one that triggers YAML frontmatter parsing, structural validation (per [runbook-content-architecture.md §4 Quality Gates](./runbook-content-architecture.md#4-quality-gates)), chunking, and embedding on ingest; both paths finish with an atomic write to both stores — see [`kb-ingestion-architecture.md`](./kb-ingestion-architecture.md) for the atomicity contract.

### Files

| Component | Location |
|-----------|----------|
| Atomic ingest (entry point) | [`modules/knowledge/domain/services/knowledge_service.py`](../../../faultmaven/modules/knowledge/domain/services/knowledge_service.py) — `ingest_runbook()` |
| Startup bootstrap (pre-deployed runbooks) | [`bootstrap/kb_init.py`](../../../faultmaven/bootstrap/kb_init.py) |
| Conversion + verify (case-generated drafts) | [`modules/knowledge/domain/services/conversion_service.py`](../../../faultmaven/modules/knowledge/domain/services/conversion_service.py) — `verify_draft()` |
| Reset / hot-rebuild | `fm-reset-kb` ([`faultmaven/cli/reset_kb.py`](../../../faultmaven/cli/reset_kb.py)) |
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

Team KB scope filtering is **built on the seeder path, not on the tool path** — the
filter, the share table and the resolver all exist, but the agent's KB tool never
receives the resolved ids (see Remaining work 1):

- Team and organization models exist in the auth module (`modules/auth/domain/models/`)
- `team_members` junction table supports multi-team membership per user
- `TeamService.list_all_user_team_ids(user_id)` resolves all team memberships across orgs
- `MilestoneEngine._prefetch_kb_context` resolves the **case owner's** teams (keyed on `case.user_id`, deliberately not the session user, so one user's case can never surface another's shares) to shared `knowledge_item` ids via `resolve_shared_kb_ids` against `resource_shares`, and passes them to `build_kb_scope_filter` — so the **KB cause-seeder prefetch** does see team-shared items
- The unified `answer_from_kb` tool builds the combined filter via `build_kb_scope_filter`, whose team arm is `{"parent_document_id": {"$in": shared_ids}}`
- ChromaDB metadata stores only the immutable floor (`scope` = `global`/`personal` + `owner_id`) at ingestion time — never `team_id`; team visibility lives in the `resource_shares` table (ADR-013 §D4)
- API endpoints (`GET /knowledge/documents`) support `scope=team` filter with team membership check

**Remaining work:**

1. **`ToolContext.shared_kb_ids` is never populated on the live turn path.** The
   `kb_qa` tool reads the team arm from `context.shared_kb_ids`
   (`kb_tool_adapter.py`), but `MilestoneEngine._build_tool_context` does not set
   it, so it defaults to `[]` and `build_kb_scope_filter` omits the team arm
   entirely. The only writer was `AgentOrchestrationService`, deleted in #982 —
   and that writer sat on the separate `/sessions/execute` surface, never on
   `/turns`, so the tool has never seen team-shared items on the live path.
   Deleting the dead writer did not cause this; it removed the last code that
   made the wiring look present. This **fails closed** (global ∪
   owner-personal; no cross-tenant exposure), but a team-shared runbook is
   invisible to the agent's KB tool even though the seeder prefetch above finds
   it. Fixing it means resolving the shared ids where the context is built —
   `_build_tool_context` is synchronous, so the resolution has to happen upstream
   and be threaded in.
2. Team KB management API endpoints (upload, list, delete restricted to team admin role)
3. Promotion workflow (personal → team: submit, review, approve/reject with team admin approval gate)

---

## Tier 3: Personal Knowledge Base

**Scope:** Private to one user — accessible across all the user's cases
**Deployment:** Both Standalone and Cloud

### Personal KB Characteristics

- **Lifecycle:** Permanent — persists with user account
- **Ownership:** Individual user within an organization
- **Content:** Personal runbooks, private notes and drafts, personal checklists, lessons learned
- **Access:** Owner only. Other users cannot access. User can promote runbooks to Team KB (requires team admin approval).
- **Start state:** The Personal scope starts empty in both deployments — the user builds it up. (The Global scope ships pre-populated; see Tier 1.)

In Standalone, the Personal and Global scopes are available (Team is Cloud-only); the user builds up their Personal KB here, while the Global scope ships pre-populated. In Cloud, users can additionally promote their personal runbooks to the Team KB for shared access.

### Storage Architecture

**Write path**: `upload_document()` and `verify_draft()` → `ingest_runbook()`, which writes the relational `knowledge_items` row first (source-of-truth) and then the ChromaDB chunks + embeddings. Both stores receive the same `kb_<uuid>` id; on a ChromaDB-side failure (raise OR 0 chunks) `ingest_runbook()` deletes the just-written SQL row before re-raising, so the two stores never diverge (the earlier "leave the SQL row for scan-and-recover" policy produced half-state rows that downstream scans mis-classified — see `kb-ingestion-architecture.md`). Conversion-bookkeeping rows (`conversion_jobs`, `conversion_drafts`) are written in addition for the upload-flow audit trail.

**Read path**: `list_documents()` and `get_document()` read from **`knowledge_items`** — the source of truth for the published inventory (both bootstrap built-ins and `verify_draft` promotions land there). Content comes from the stored `knowledge_items.content` row, not disk. RBAC (org tenancy + personal-owner / team-member isolation) is enforced **in-query** by the repository (`list_for_inventory`); tag/scope filtering and pagination are applied over that already tenant-isolated set. `conversion_drafts` is **only** the review queue (the Drafts tab = `status='draft'`); it is no longer the document inventory. ChromaDB is not queried for listing or retrieval.

**Delete path**: `delete_document()` is provenance-gated by id shape:

- **Built-in** (deterministic `kb_<12 hex>` id) → **unpublish**: set `is_published=False` *and* delete its ChromaDB vectors. A bare flag flip is insufficient — investigation retrieval (`kb_qa`) filters ChromaDB by scope only and does **not** honor `is_published`, so the vectors must be removed for the runbook to actually leave investigations. The row is kept (a hard delete would be resurrected by the next bootstrap from the on-disk file); the deletion survives restart because the content-hash skip won't re-vectorize an unchanged row.
- **Authored** (random-UUID id) → **hard delete**: drop the `knowledge_items` row and its ChromaDB vectors.

**Search path**: `hybrid_search()` queries ChromaDB with explicit BGE-M3 embeddings (1024 dims). Two-stage: vector + keyword recall, then 4-signal reranker.

### API Endpoints

Managed through the Knowledge module (`/api/v1/knowledge/`):

- `POST /documents` — Upload document (SQLite record + chunked vector indexing). Accepts `text/markdown` and `text/plain` only; other content types are rejected with 415.
- `GET /documents` — List documents — reads from SQLite. **Note:** `domain=`, `service=`, and `severity=` filter query params are specified but not yet implemented; all documents in scope are returned.
- `GET /documents/{id}` — Get specific document (SQLite + file from disk)
- `PUT /documents/{id}` — Update document metadata in-place (`update_document_metadata`: loads the row, applies updates, persists, and re-indexes ChromaDB on content change).
- `DELETE /documents/{id}` — Delete document and remove from ChromaDB
- `POST /search` — Semantic search (vector embeddings + hybrid reranker) across user's authorized scopes
- `POST /documents/search` — Full-text search on document title (substring match). Distinct from `/search` — no vector retrieval.

### Personal KB Files

Personal KB shares the same unified infrastructure as Global and Team — scope is metadata-only.

| Component | Location |
|-----------|----------|
| Vector store (all tiers) | `infrastructure/knowledge/knowledge_vector_store.py` |
| Document inventory (SQLite) | `modules/knowledge/infrastructure/persistence/knowledge_item_repository.py` (`DatabaseKnowledgeItemRepository`) |
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

**Implementation status: Not yet implemented.** The catalog endpoint and its Dashboard UI are designed but not currently registered. The design below is the target.

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

## Implementation Notes

### Runbook Similarity — No Second Collection

Runbook similarity and deduplication at report-generation time (not investigative Q&A) is served by `RunbookKnowledgeBase` in `infrastructure/knowledge/runbook_kb.py`, which exposes `index_runbook()`, `index_document_derived_runbook()`, and `search_runbooks()` (pure vector, no hybrid search).

It is **not** an exception to the single-collection design. `RunbookKnowledgeBase` is constructed over an injected `ChromaDBVectorStore` and never selects a collection, so it reads and writes whatever collection that store is bound to — `faultmaven_kb`, the same one every KB tier shares. Its `COLLECTION_NAME = "faultmaven_runbooks"` constant is **decorative**: it is logged once at init and referenced nowhere in any read or write path.

The separation is therefore metadata, exactly like the tiers above it: `{"report_type": "runbook"}` distinguishes runbook rows from document chunks, and `{"organization_id": <org>}` supplies the tenant isolation that a similarity query — which names no id and no owner — cannot get from the scope-`where`. Both predicates are mandatory on `search_runbooks`, and both keys are declared on `VectorMetadata` so `add_documents` normalization does not drop them. See [vector-retrieval-architecture.md §4](./vector-retrieval-architecture.md) for the clause shape.

Sharing a collection also means `search_runbooks` cannot return stored rows — it **rebuilds** a `CaseReport` from their metadata. Every field it reconstructs (`case_id`, `case_title`, `runbook_source`, and the document-driven `document_title`/`original_document_id`) is therefore declared on `VectorMetadata` too. A key the schema does not declare is now refused by `add_documents` rather than dropped in silence, because a dropped key does not read as missing at search time — it reads as the reconstruction's fallback value. Before #912 that made a document-driven runbook come back labelled `incident_driven`; the defect was latent rather than observed, since `index_runbook` still has no production caller, but it sat directly in the path any writer would take.

`report_id` is deliberately *not* among them: it is the ChromaDB row id, which is where the search reads it from.

### `VerificationLevel` (trust/authority system)

`KnowledgeItem` carries a `VerificationLevel` integer enum (`EXPERIMENTAL=0`, `COMMUNITY=1`, `ADMIN_VERIFIED=2`) that tracks the authority of the knowledge's source. This is separate from the frontmatter `status` field (lifecycle: `draft` → `verified` → `stale` → `deprecated`). The two model **different axes** and are deliberately not merged:

- **`status` is the ranking signal.** The four-signal reranker reads frontmatter `status` (verified +0.40 / in-review +0.10 / draft −0.10 / stale −0.20 / deprecated −0.30). This is the *editorial lifecycle* of a document and is what tunes retrieval relevance.
- **`verification_level` is source provenance only.** It records *who validated* the underlying knowledge (experimental / community / admin-verified) and is surfaced as a trust label in the API/UI (`is_admin_verified`, `/knowledge` responses). It is intentionally **not** a reranker input — provenance should not, on its own, reorder retrieval; a stale admin-verified runbook must still decay by `status`.

So there is one ranking authority (`status`) and one provenance record (`verification_level`); they are complementary, not competing.

### Reasoning-Context Retrieval APIs — Superseded (removed)

An earlier `AdvancedKnowledgeRetrieval` subsystem and two `KnowledgeService`
entry points (`search_with_reasoning_context`, `curate_knowledge_for_reasoning`,
plus `discover_related_knowledge`) offered "reasoning-context" retrieval with
multi-level caching and query expansion. They were **never wired to a live
caller**: the sole internal call target (`_execute_optimized_retrieval`) invoked
a method (`search_with_context`) that did not exist on `AdvancedKnowledgeRetrieval`,
so every call raised `AttributeError` and fell through to a fallback that hardcoded
`scope=global` — a scope-safety bypass. The subsystem and its dead entry points
have been removed; live retrieval goes through `search_knowledge()` /
`search_documents()` (scope-enforcing) and the agent's `answer_from_kb` tool
(`hybrid_search`). See [vector-retrieval-architecture.md](./vector-retrieval-architecture.md).

---

## Knowledge Suggestions (Case → KB Review Workflow)

Separate from the document-to-runbook *conversion* pipeline, the **suggestion**
subsystem captures free-form knowledge extracted from a resolved case and routes
it through human review before it becomes a `KnowledgeItem`. It is a
human-in-the-loop (HITL) queue with a mandatory PII gate.

**Model** (`modules/knowledge/domain/models/suggestion.py`, table
`knowledge_suggestions`): a `KnowledgeSuggestion` carries the suggested
title/content/type, extraction lineage (source case, who/when, message +
evidence counts), review metadata, and a bidirectional `knowledge_item_id` link
set on approval. Two enums drive it:

- `SuggestionStatus`: `PENDING_REVIEW → APPROVED` (creates a `KnowledgeItem`) /
  `REJECTED` (archived) / `DRAFT` (needs more work).
- `PIIScanStatus`: `NOT_SCANNED → SCANNING → CLEAN | PII_DETECTED → REMEDIATED`
  (or `SCAN_FAILED`). A suggestion `is_ready_for_review()` only when the scan is
  `CLEAN` or `REMEDIATED`.

**PII gate (HITL invariant).** `approve()` raises `ConflictError`
(`conflict_reason="not_ready_for_review"` → HTTP 409) unless the PII scan is
clean/remediated; `mark_pii_remediated()` raises 409
(`conflict_reason="no_pii_detected"`) if there is nothing to remediate. Editing
content (`update_content`) resets the scan to `NOT_SCANNED` — any edit re-arms
the gate.

**Flow & endpoints.** Extraction is initiated from the case side
(`POST /cases/{case_id}/extract-knowledge` → `SuggestionService.extract_knowledge_from_case`),
producing a `PENDING_REVIEW` suggestion. Review happens over the knowledge
routes: `GET /knowledge/suggestions` (list), `GET /knowledge/suggestions/{id}`,
`PUT /knowledge/suggestions/{id}` (edit — resets PII scan),
`POST /knowledge/suggestions/{id}/approve` (201; creates the `KnowledgeItem` and
links it), `POST /knowledge/suggestions/{id}/reject`, and
`POST /knowledge/suggestions/{id}/remediate-pii`.

---

## Related Documents

### Knowledge Architecture

- **[Runbook Content Architecture](./runbook-content-architecture.md)** — What goes INTO the KB: taxonomy, templates, quality gates, lifecycle. Companion to this document.

### Evidence Architecture (Separate Concern)

- **[Case Evidence Store](../case-and-session/case-evidence-store.md)** — Storage and lifecycle for case-specific diagnostic evidence
- **[Data Preprocessing Design](../data-processing/data-preprocessing-design-specification.md)** — How submitted evidence is classified, extracted, and indexed

### Investigation Context

- **[Investigation Lifecycle Logic](../investigation-engine/investigation-lifecycle-logic.md)** — How both knowledge and evidence feed into the investigation engine
