# Knowledge Base Architecture

**Document Type:** Component Specification
**Version:** 9.2
**Last Updated:** 2026-08-28

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

**Every chunk carries its tenant (#1168).** Each KB chunk's metadata holds the `enterprise_id` of the `knowledge_items` row it belongs to — the ADR-017 isolation key, stamped by the one live writer (`KnowledgeService._index_document_in_vector_store`, which takes it as a required keyword from each caller as the row's own value, never the ambient request tenant). Global-tier chunks carry the platform (Standalone) enterprise their SQL row carries. The indexer refuses a missing or non-string tenant before it does any work, and `KnowledgeVectorStore.add_documents` — the only store the container wires into `KnowledgeService` — refuses a KB chunk without a non-blank `enterprise_id`, so every write the service makes carries the stamp. `KnowledgeVectorStore` is the only writer of the collection in this codebase: `ChromaDBVectorStore` has no method that writes a document (#1782). There is no `organization_id` in vector metadata: ADR-017 makes it billing attribution, never a visibility predicate, and `VectorMetadata` refuses it. **No read consults the stamp yet** — conjuncting it onto every KB read, outside the `$or`, is #1775, and it lands after the backfill of chunks written before the stamp existed (#1777) reports none left unstamped. Until then the tenant control is `build_kb_scope_filter` alone.

**Why one collection, not separate collections per tier:**

1. **No N+1 query problem** — A user in 5 teams would require 7 separate queries (global + personal + 5 teams) with per-tier collections, then manual merge/dedup/sort in Python. One collection = one query.
2. **ChromaDB is optimized for few large collections** — HNSW graph indexing works best with millions of vectors in few collections, not thousands of tiny collections.
3. **Roaring Bitmap metadata filtering** — ChromaDB pre-filters metadata before graph traversal. One query, one graph, one sorted top-K result.
4. **Unified ranking** — All scopes compete in the same similarity search. A highly relevant team runbook surfaces alongside a global best practice without manual merge logic.

```text
ChromaDB Instance
│
├── faultmaven_kb                    # ALL knowledge tiers (permanent)
│   ├── scope=global                 # Pre-built troubleshooting guides (org-free platform tier;
│   │                                #   enterprise_id = the Standalone enterprise)
│   ├── scope=personal, owner_id=alice  # Alice's private runbooks (team shares stay on this floor)
│   └── scope=personal, owner_id=bob    # Bob's private procedures
│   #  Every chunk also carries enterprise_id = its knowledge_items row's (#1168).
│   #  No scope=team rows: team visibility is resolved at query time from the
│   #  resource_shares id-allowlist, not stored as vector metadata.
│
├── case_{case_id}                   # Per-case evidence (ephemeral)
│   └── [uploaded logs, configs, metrics]
│
└── ...
```

**Filter-presence check:** `KnowledgeVectorStore.search()` refuses a query against `faultmaven_kb` whose `where` clause names none of the `SCOPE_FILTER_KEYS` (`scope`, `owner_id`, `parent_document_id`) — `_require_kb_filter_present()` in `infrastructure/knowledge/knowledge_vector_store.py` raises `ValueError`. It checks that a filter is **present**, never that it is **scoped**: `{"scope": {"$ne": "no-such-scope"}}` names a scope key and returns the whole corpus, and passes. What it buys is that a query which forgot to filter at all cannot run. (`organization_id` left the key set in #1168: no chunk carries it, so a clause keyed on it alone is refused as unscoped.) The tenant control is `build_kb_scope_filter` below, plus the AST pin that every filtered KB read derives its clause from it. ChromaDB's own tenant dimension is the `enterprise_id` stamp every KB write the service makes carries since **#1168**; conjuncting it on read is **#1775**.

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

Runbook text is stored in the database, not on disk. A built-in runbook's text ships in the KB pack and, once ingested, is `knowledge_items.content`; an authored runbook's text is `conversion_drafts.content` while it is a draft and `knowledge_items.content` once verified. ChromaDB holds the derived vectors, which can be regenerated from those rows (and, for built-ins, from the pack).

### Runtime Storage Layout

```text
data/
├── knowledge/                          # Standalone scan import directory (hand-placed .md files only)
│   ├── global/                         # Imported as global-scope drafts (platform operator only)
│   ├── team_{team_id}/                 # Imported as PERSONAL drafts (a standalone deployment has no teams)
│   └── user_{user_id}/                 # Imported as personal drafts (`personal_{user_id}/` works too)
├── chroma-kb/                          # ChromaDB KB vectors (derived from knowledge_items / the pack)
├── chroma-evidence/                    # ChromaDB case-evidence vectors
├── evidence/                           # Uploaded evidence files (per-case)
└── faultmaven.db                       # SQLite database (knowledge_items, conversion_drafts, ...)
```

**Design decisions:**

- **Scope comes from the first directory.** A file under `user_{id}/`, `personal_{id}/` or `team_{id}/` imports as a personal draft (a `team_{id}/` file too, because the scan runs only where there are no teams and a team scope with no team would publish a row only its owner sees; the scan's `notes` name each such file, #1986); anything else (including `global/`) as a global draft, which only a platform operator's scan imports. Domain is captured in frontmatter metadata and ChromaDB, not in folder structure.
- **Vectors are derived artifacts.** If the embedding model changes, chunking parameters are tuned, or ChromaDB is rebuilt, the vectors are regenerated from their sources: the KB pack for built-ins, `knowledge_items.content` for published runbooks. Without those rows, vectors cannot be regenerated.
- **Authored text lives in the database, not in this tree.** A runbook made by the Dashboard upload, the template create or the document-to-runbook conversion is a draft row holding its text (`conversion_drafts.content`) and, once verified, a `knowledge_items` row; nothing is written under `data/knowledge/` (#1950). The tree is the standalone scan's import source and no startup step reads it.

### Authoring vs Runtime

The KB Toolkit and FaultMaven runtime use different directories:

| Path | Purpose | Who writes | Who reads |
|------|---------|-----------|-----------|
| `faultmaven/resources/knowledge/runbooks/<domain>/` | **Authoritative built-in runbook sources** (public, transparent, PR-able). Edit/contribute here. | Maintainers, community PRs; KB Toolkit tools | KB Toolkit (`kb-validate`, `kb-quality`, `kb-build-pack`) |
| `faultmaven/resources/knowledge/pack/` (or `KB_PACK_DIR`) | The **KB pack** — generated from the sources above (runbooks + build-time vectors), ingested at startup with no embedding model | `kb-build-pack` (toolkit), vendored here | FaultMaven API |
| `faultmaven/data/knowledge/{scope}/` | **Standalone import directory**: `/knowledge/scan` copies each hand-placed file's text into a draft row. Nothing writes here, and under `TENANT_PROVIDER=multi` the scan refuses (no shared disk). | Manual file drop (standalone) | `/knowledge/scan` |
| `faultmaven/docs/operations/runbooks/` | Community contributions — shared with the open-source community | Community members | Human readers (not ingested) |

To add/update a built-in runbook: edit the source in this repo, rebuild the **KB
pack** with the toolkit, and deliver it. Whether that needs a new app image depends
on how the pack reaches the API (the default GHCR image bakes `resources/` in and
the default compose mounts no `resources/` directory):

```bash
# 1. Edit the authoritative source (public, PR-able):
#    faultmaven/resources/knowledge/runbooks/<domain>/<runbook>.md

# 2. Rebuild the pack in faultmaven-kb-toolkit (it reads the public sources):
kb-build-pack --version 2026-07-08 --tar

# 3. Vendor it as the committed baseline (a baseline is live only in an image
#    built with it), or deliver it to a running deployment's KB_PACK_DIR — see
#    kb-pack-architecture.md §Delivery for every deployment. Run from the
#    directory that holds faultmaven/ and faultmaven-kb-toolkit/:
cp -r faultmaven-kb-toolkit/dist/kb-pack/* faultmaven/resources/knowledge/pack/

# 4. Make it live. From source, in the faultmaven/ checkout, with KB_PACK_DIR
#    unset: build an image that carries the pack, then start it
#    (`./faultmaven.sh restart` alone re-creates the OLD image):
./faultmaven.sh stop && ./faultmaven.sh start --build
#    The startup bootstrap then ingests the pack idempotently (no model;
#    unchanged runbooks skipped, removed ones pruned).
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
| Pre-deployed runbooks | Startup bootstrap — `faultmaven/bootstrap/kb_init.py` ingests the **KB pack** (pre-chunked + pre-embedded; no model) via `ingest_runbook(prechunked=...)`, idempotent on the pack content hash stamped on the row |
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
| `enterprise_id` | Owning tenant: the `knowledge_items` row's `enterprise_id` (the Standalone enterprise for the global tier). Required on every KB write the service makes since #1168; read conjunct #1775 |
| `domain` | Engineering vertical (vocabulary: [runbook-content-architecture.md §Taxonomy Schema](./runbook-content-architecture.md#taxonomy-schema)) |
| `service` | Specific technology (postgresql, kubernetes, redis, etc.) |
| `symptom_class` | Failure modes addressed (comma-joined list) |
| `severity` | Severity level (vocabulary: [§Taxonomy Schema](./runbook-content-architecture.md#taxonomy-schema)) |
| `tags` | Additional search terms (comma-joined) |
| `status` | Lifecycle state (vocabulary: [§Taxonomy Schema](./runbook-content-architecture.md#taxonomy-schema)) |
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
| Federated search across tiers | Implemented | Single `kb_qa` tool (wrapping `AnswerFromKB`) searches all scopes (global + personal + team) via `$or` filter |
| Single-collection storage | Implemented | One `faultmaven_kb` collection with metadata-based scope filtering |
| Filter-presence check | Implemented | `_require_kb_filter_present()` raises `ValueError` when a KB `where` clause names no scope key. Presence, not tenancy — the vector-layer tenant control is the `enterprise_id` stamp (#1168) |
| Tenant stamp on every KB chunk | Write side implemented (#1168) | Every KB chunk carries its row's `enterprise_id`; `add_documents` refuses a KB chunk without one. Backfill of older chunks: #1777. Read conjunct: #1775 — not yet consulted by any read |

#### Current Tool Architecture

| Tool (registered name) | Class | File | Collection | Purpose |
| ---------------------- | ----- | ---- | ---------- | ------- |
| `kb_qa` | `AnswerFromKB` | `kb_qa.py` | `faultmaven_kb` | Unified KB Q&A (global + personal + team via `$or` filter) |
| `answer_from_case_evidence` | `AnswerFromCaseEvidence` | `case_evidence_qa.py` | `case_{case_id}` | Case-scoped forensic Q&A on vectorized evidence |

The old per-scope KB tools (`global_kb_qa`, `user_kb_qa`) and the alternate `answer_from_knowledge_base` name have been replaced with the single unified `kb_qa` tool (wrapping `AnswerFromKB`) — scope filtering is automatic based on user context resolved by `KBToolAdapter`.

### Two channels: the push and the pull

Knowledge reaches the model two ways, and they have different economics and different
controls.

| Channel | Mechanism | Who decides | Governed by |
| ------- | --------- | ----------- | ----------- |
| **Pull** | The `kb_qa` tool (`kb_qa.py`) | The model elects it, per turn | Always available |
| **Push** | `KbPrefetcher.prefetch_kb_context` (`milestone_engine/kb_prefetch.py`) — a deterministic hybrid search fired at case transitions, whose top hits are written to `case.kb_context` and rendered into every subsequent prompt as `<knowledge_context>` | Nobody; it fires on the trigger | `KB_PREFETCH_ENABLED` (default `true`) |

**`KB_PREFETCH_ENABLED=false` turns off the push only.** The tool stays registered and
electable, so the model can still retrieve a runbook whenever it judges one useful;
what stops is knowledge arriving *unasked* in every prompt. A single switch covering
both channels is deliberately not offered — it would remove the model's ability to ask
even where asking helps. The measurement behind the split (fm#1360) is recorded on the
setting itself in `config/settings.py`: across 86 consumed turns the model elected the
tool on roughly two thirds of the turns where it was on the table, and in every case of
the sample. "Push off" is therefore not "no knowledge", and it is not "no RAG".

Two properties of the gate are worth knowing:

- **It is enforced at both ends.** The pre-fetch skips the search and clears
  `case.kb_context`; `visible_kb_context` (`core/investigation/kb_push.py`) additionally
  returns `[]` whatever a reloaded case still carries, because `kb_context` is persisted
  and outlives the flag being turned off.
- **It fails open, on purpose.** `kb_push_enabled()` returns `True` when settings cannot
  be read. This gate decides whether retrieved knowledge is *removed*, so an unreadable
  configuration must leave behaviour as it was rather than silently strip a runbook out
  of a prompt.

**Operating it.** Set `KB_PREFETCH_ENABLED=false` in `.env` locally, or in the
deployment's ConfigMap on Kubernetes. `GET /admin/config/status` reports a `kb_prefetch`
entry, and reports whether the push is *effective* rather than merely configured — a
deployment with no knowledge service shows it off regardless of the flag. Client-visible
consequence, published in API contract 3.3.0: `TurnResponse.sources` is empty for every
turn in a deployment with the push disabled, and a client must read that as "no citation
to show", never as a retrieval failure.

**Which turn a source belongs to** (API contract 11.2.0). Both pre-fetch triggers fire
while a turn's response is applied, after its answer was generated, so the hits first
reach the NEXT turn's prompt. A turn's `sources` is therefore what its own prompt
carried: the prompt build reports the entries whose header survived the section budget
in the prompt the model answered from (none for a template without the KB slot or the
minimal fallback), and the engine passes that report on, never `case.kb_context` read
afterwards. The context stands in every prompt until a pre-fetch
replaces it, so `sources` repeats turn to turn; `Source.new_this_turn` marks the excerpts
the previous turn's prompt did not carry, decided against the previous assistant row's
stored sources rather than a turn number, so a failed or retried turn cannot mislabel
it. The list is persisted with the assistant row, and `GET /cases/{case_id}/messages`
returns it as the typed `Message.sources`, so history shows it exactly where the live
turn did.

### Design Principles

Three principles govern KB retrieval. The retrieval-pipeline mechanics are canonical in [vector-retrieval-architecture.md](./vector-retrieval-architecture.md); KB-arch describes the storage-layer surface only.

1. **Federated Search** — One knowledge tool, not three. The backend resolves user scope (global + personal + team) and merges results via the `$or` filter built by `AnswerFromKB`. See [Federated Search: Implementation](#federated-search-implementation) below for the scope-filter construction.
2. **Hybrid Search** — Two-stage retrieval (vector + keyword recall → four-signal reranker). See [vector-retrieval-architecture.md §3](./vector-retrieval-architecture.md#3-two-stage-retrieval-and-reranking-pipeline) for signal weights, dynamic reweighting, and the optional `filter_mode="hard"` pre-filter.
3. **Staleness-Aware Synthesis** — Per-chunk age/status warnings injected into LLM context; freshness signal in the reranker. See [vector-retrieval-architecture.md §4](./vector-retrieval-architecture.md#staleness-aware-synthesis) for the formatter.

### Federated Search: Implementation

KB-arch owns the scope-filter construction. The full tool path (adapter → filter → query → synthesis → return) is canonical in [vector-retrieval-architecture.md §4](./vector-retrieval-architecture.md#4-knowledge-base-retrieval) under Tool Path.

`kb_qa` searches under the scope filter `KBToolAdapter` reads off `ToolContext.kb_scope_filter`: the case driver's, built by `case_retrieval_scope` from `build_kb_scope_filter` (#1919):

```text
{"$or": [
    {"scope": "global"},                                 # all users
    {"owner_id": driver_id},                             # the driver's own
    {"parent_document_id": {"$in": shared_ids}}          # ids shared to the driver's teams
]}
```

The `shared_ids` arm is resolved from `resource_shares` (`resolve_shared_kb_ids`) — the personal/global arms come straight from the driver's own ids, so a filter built for one user can never surface another's non-shared content. Empty `shared_ids` collapses the filter to `personal ∪ global`.

This filter is passed to the unified `faultmaven_kb` collection in the metadata-`where` argument. The filter-presence check (`_require_kb_filter_present()`) rejects any KB query that arrives naming no scope key at all — see [Storage Architecture](#single-collection-with-metadata-filtering-current).

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
- No live caller supplies filter values today. The source the retrieval design names is the copilot's page context (copilot → API → `ToolContext` → `KBToolAdapter`), which is not built. A case-derived soft boost, reading `ProblemVerification.affected_services`, was removed with that field in #1880: the field recorded the user's own service ("checkout"), while a runbook's `service` is the technology ("postgresql"), so the two never matched. Nothing wrote the field anyway.
- With no filter context, the search runs unfiltered and the metadata signal scores lifecycle status alone.

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

- **Creation routes** (`convert`, `runbooks/create`, `documents` upload) — the scope is a request field, gated at the route (403).
- **Publish / mint** (`verify_draft`, `verify-batch`, `scan`) — the scope is only known after the conversion-job row or on-disk file is inspected, so the gate lives in the service: `verify_draft` refuses to publish a `global` draft (`AuthorizationError` → 403), and `scan` skips global-inferred files a caller may not author while still discovering personal and `team_`-directory ones (the latter as personal drafts, #1986). This closes the pre-#770 hole where a non-admin could verify a system-owned global draft or mint a global draft via scan.

### Ingestion Pipeline

Global-tier ingestion runs automatically at API startup via the **KB bootstrap** (`faultmaven/bootstrap/kb_init.py`). The bootstrap loads the shipped **KB pack** (`faultmaven/bootstrap/kb_pack.py`) and, for each runbook in it, writes a row to `knowledge_items` plus the pack's pre-computed chunk vectors to ChromaDB via `KnowledgeService.ingest_runbook(prechunked=...)`. Because the pack ships pre-chunked and pre-embedded, startup does **no chunking and no embedding** — it is pure SQL + vector writes and completes in seconds. Idempotent: a runbook is skipped on every restart while the pack's content hash equals the one stamped on its row at ingest (`metadata.pack_content_hash`, #1966), and runbooks no longer in the pack are pruned from both stores.

Tier-1-specific notes: Global runbooks are written by the platform admin and apply across all organizations. The 91 built-in runbooks ship pre-chunked and pre-embedded in the **KB pack** (`resources/knowledge/pack`, or `KB_PACK_DIR`); on startup the bootstrap ingests them directly — no separate "verify" step is needed because the platform vendor / admin is the verifier for pre-deployed content. The verify-via-Dashboard flow is reserved for case-generated and document-converted drafts that need a human gate.

The **conversion-drafts path** (case-generated / document-converted content, not the pack) is the one that triggers YAML frontmatter parsing, structural validation (per [runbook-content-architecture.md §4 Quality Gates](./runbook-content-architecture.md#4-quality-gates)), chunking, and embedding on ingest; both paths finish with an atomic write to both stores — see [`kb-ingestion-architecture.md`](./kb-ingestion-architecture.md) for the atomicity contract.

### Files

| Component | Location |
|-----------|----------|
| Atomic ingest (entry point) | [`modules/knowledge/domain/services/knowledge_service.py`](../../../faultmaven/modules/knowledge/domain/services/knowledge_service.py) — `ingest_runbook()` |
| Startup bootstrap (pre-deployed runbooks) | [`bootstrap/kb_init.py`](../../../faultmaven/bootstrap/kb_init.py) |
| Conversion + verify (case-generated drafts) | [`modules/knowledge/domain/services/conversion_service/`](../../../faultmaven/modules/knowledge/domain/services/conversion_service/) — `verify_draft()` |
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

Team KB scope filtering is built on every retrieval path:

- Team and organization models exist in the auth module (`modules/auth/domain/models/`)
- `team_members` junction table supports multi-team membership per user
- `TeamService.list_all_user_team_ids(user_id)` resolves all team memberships across orgs, excluding retired teams
- Inside an investigation, the pre-fetch, `kb_qa` and the runbook dedup take one scope from `case_retrieval_scope(case)` (`core/investigation/milestone_engine/retrieval_scope.py`, #1919): the **case driver's** knowledge, global ∪ the driver's personal KB ∪ the runbooks shared to the driver's teams. The driver is the case's effective driver (`case.effective_driver_id`; the creator when none was handed the case, ADR-020, #1898), never the session user. `kb_qa` receives it as `ToolContext.kb_scope_filter`
- `search_documents` and report recommendations build the requesting user's scope with `build_kb_scope_filter`, whose team arm is `{"parent_document_id": {"$in": shared_ids}}`
- ChromaDB metadata stores only the immutable floor (`scope` = `global`/`personal` + `owner_id`, plus the tenant's `enterprise_id`, #1168) at ingestion time — never `team_id`; team visibility lives in the `resource_shares` table (ADR-013 §D4)
- API endpoints (`GET /knowledge/documents`) support `scope=team` filter with team membership check

**Stored excerpts are gated per viewer (#1919).** The model's answer written from the driver's knowledge is accepted disclosure to everyone who reads the case. The stored copies of runbook text are not: a turn's `sources` (the excerpts its prompt carried, persisted on the assistant row) are checked against the reader when `GET /cases/{id}/messages` or `POST /cases/{id}/turns` returns them (`gate_kb_sources`, `modules/knowledge/contracts.py`). The check is the id-addressed document read's rule (global, the viewer's own, or shared to one of the viewer's teams; published or the viewer's own), batched as `visible_document_ids`, one query per response. A source the viewer cannot open, or one with no `document_id`, is returned with empty `content` and `metadata` `{"access": "restricted"}`. The operator break-glass transcript (`/admin/cases`) shows the stored copy.

**Built: team KB management.** `POST /knowledge/documents` with `scope=team` and a `team_id` uploads into a team's knowledge; `GET /knowledge/documents?scope=team` lists it; `DELETE /knowledge/documents/{id}` removes it. The gate on upload is team **membership** (`is_team_member`, `modules/knowledge/api/routes.py`: a caller can publish only to a team they belong to, otherwise 403), not a team-admin role. Delete and update are owner-based (`ensure_document_write_allowed`: the document's owner; under single-tenant the platform operator may also write, under `TENANT_PROVIDER=multi` the owner only), consistent with ADR-011 D8 (a shared runbook is read-only to the team; its author edits and deletes it).

**Remaining work:**

1. Promotion workflow (personal → team: submit, review, approve/reject with team admin approval gate)

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

**Write path**: `upload_document()` and `verify_draft()` → `ingest_runbook()`, which writes the relational `knowledge_items` row first (source-of-truth) and then the ChromaDB chunks + embeddings. Both stores receive the same `kb_<uuid>` id; on a ChromaDB-side failure (raise OR 0 chunks) `ingest_runbook()` deletes the just-written SQL row before re-raising, so the two stores never diverge (the earlier "leave the SQL row for scan-and-recover" policy produced half-state rows that downstream scans mis-classified — see `kb-ingestion-architecture.md`). Conversion-bookkeeping rows (`uploaded_files`, `conversion_jobs`, `conversion_drafts`) are written in addition for the upload-flow audit trail, after `ingest_runbook()` succeeds, because the draft's `knowledge_item_id` is a foreign key to the item; if they fail to write, `upload_document()` removes them and withdraws the item (#2365).

**Read path**: `list_documents()` and `get_document()` read from **`knowledge_items`** — the source of truth for the published inventory (both bootstrap built-ins and `verify_draft` promotions land there). Content comes from the stored `knowledge_items.content` row, not disk. RBAC (org tenancy + personal-owner / team-member isolation) is enforced **in-query** by the repository (`list_for_inventory`); tag/scope filtering and pagination are applied over that already tenant-isolated set. `conversion_drafts` is **only** the review queue (the Drafts tab = `status='draft'`); it is no longer the document inventory. ChromaDB is not queried for listing or retrieval.

**Delete path**: `delete_document()` is provenance-gated by id shape:

- **Built-in** (deterministic `kb_<12 hex>` id) → **unpublish**: set `is_published=False` *and* delete its ChromaDB vectors. A bare flag flip is insufficient — investigation retrieval (`kb_qa`) filters ChromaDB by scope only and does **not** honor `is_published`, so the vectors must be removed for the runbook to actually leave investigations. The row is kept (the pack still carries the runbook, so a hard delete would be resurrected by the next bootstrap).
- **Authored** (random-UUID id) → **hard delete**: drop the `knowledge_items` row and its ChromaDB vectors, and discard the draft that published it: its text is kept, it leaves the drafts list, and its slot is freed (#2369, [conversion §9.4](./document-to-runbook-conversion.md#94-single-owner-principle-for-draft-mutations)).

A document reported removed has no retrievable chunk, now or after any restart, reconcile, repair or `kb_seed` run (#1966). Retrieval never checks that a chunk's row exists, so a chunk whose row is gone would stay searchable for good. The reconcile pass deletes such chunks for built-ins only; authored chunks stranded by a vector delete that failed before #1966 are the authored-orphan sweep's (#1982, [ingestion step 6](kb-ingestion-architecture.md#algorithm)), which reports them by default and deletes them only when an operator arms it, and only where the pass's row read is proven unfiltered by row-level security.

**What a restart or `kb_seed` run does to a removed document:**

- An **unpublished built-in stays unpublished, with no vectors.** The pack skip compares the pack's content hash with the hash stamped on the row when the pack last ingested it (`metadata.pack_content_hash`), not with the row's current content, so an operator's edit is never mistaken for a new pack version. A row ingested before the stamp existed is compared by content once and stamped — except an unpublished one, which is taken as this pack version (its retirement outranks a content edit), stamped and skipped, so an older edited-and-unpublished built-in is not republished by the first boot after the upgrade. The stamp is written only when the pass's role may UPDATE `knowledge_items`; the `kb_seed` maintenance role under `TENANT_PROVIDER=multi` holds INSERT and DELETE but deliberately no UPDATE, so there the stamp is skipped and the content comparison is used — exact there, because a tenant session cannot edit or unpublish a platform row (RLS refuses). The reconcile pass never repairs an unpublished row, and it deletes any vectors an unpublished built-in still has (an earlier version's repair re-embedded them). **Only a new pack version of that runbook republishes it** — a shipped update is a new document. The one exception is `kb_seed` under `TENANT_PROVIDER=multi`: its maintenance role cannot back-write the stamp onto an existing row (and a pass whose capability check failed behaves the same way), so an older unpublished built-in that was never stamped stays unpublished across pack versions until an operator republishes it.
- A **deleted authored document** has no row, so nothing re-indexes it: repair works from rows, and it reads the row again after embedding, discarding what it wrote if a delete or unpublish landed meanwhile. Where the pass's role may UPDATE the table (the web-startup bootstrap, and SQLite) that read is locked, so it also waits for a delete still holding the row: `FOR SHARE` on PostgreSQL, the writer lock on SQLite (taken by a no-op write that is rolled back). The pass asks once (`has_table_privilege`) rather than issuing what the role would be refused. Under `kb_seed`'s maintenance role the read is plain: it still sees a delete or unpublish that committed during the embed — so `kb_seed` repairs orphaned rows and discards what a completed delete removed — but it does not wait for one inside the milliseconds between that delete's vector delete and its commit. The same plain read is used for a pass in which the `has_table_privilege` check itself raises: it logs a WARNING and answers False, so that one pass reopens the same millisecond window, the window the #1966 ruling accepted for `kb_seed`. Skipping repair for such a pass was offered and declined.

The delete itself is ordered so that a failure can only leave the document as it was or in one recoverable split state:

1. The row change is issued **uncommitted**: a `DELETE`, or a targeted `UPDATE … SET is_published = false` that writes no other column but `updated_at`, so it cannot revert an edit committed after the delete read the row. Ahead of a `DELETE`, in the same transaction, the drafts naming the item are discarded (`discard_drafts_naming_item`): their key to it is `ON DELETE SET NULL`, so after the `DELETE` none names it. Every rollback below undoes the discard with the row change. If it matched no row — absent, deleted concurrently, or refused by an RLS write policy — it is rolled back, the result is not-removed, and the vector store is never called.
2. **One** vector delete attempt (`delete_documents_by_parent_id(..., retries=0)`). A failure rolls the row change back and raises `KnowledgeBaseError`: the document is still listed, `DELETE /documents/{id}` answers **503** saying the delete did not complete and a retry finishes it, and bulk delete reports that id with a constant sentence. Its vectors are not promised intact — a remote store can fail after applying the delete, or part of it.
3. Commit. A commit that fails, or a task cancelled at it, after the vector delete leaves the split state — row present, vectors missing — logged at ERROR with the id: the document is still live, a retried delete completes it, and otherwise the reconcile/repair pass re-indexes the published row.
4. Team share rows are removed after the commit, best effort (an ERROR log on failure): a share row whose resource row is gone grants nothing.

The transaction is held across the single vector call, unlike the update path's no-hold rule (which exists for a 60–120 s embed): on SQLite the hold is the single writer lock, so it is kept to one attempt and the caller is the retry loop. The update path's own row write is targeted too, so an unpublish that commits during an edit is never reverted; if the row is unpublished once the edit commits, the vectors the edit wrote are dropped.

**Search path**: `hybrid_search()` queries ChromaDB with explicit BGE-M3 embeddings (1024 dims). Two-stage: vector + keyword recall, then 4-signal reranker.

### API Endpoints

Managed through the Knowledge module (`/api/v1/knowledge/`):

- `POST /documents` — Upload document (SQLite record + chunked vector indexing). Accepts `text/markdown` and `text/plain` only; other content types are rejected with 415.
- `GET /documents` — List documents — reads from SQLite. **Note:** `domain=`, `service=`, and `severity=` filter query params are specified but not yet implemented; all documents in scope are returned.
- `GET /documents/{id}` — Get specific document (SQLite + file from disk)
- `PUT /documents/{id}` — Update document metadata in-place (`update_document_metadata`: loads the row, applies updates, persists, and re-indexes ChromaDB on content change). New content is stored as sent, line endings normalised, and never rewritten; content holding a credential or a destructive command is refused with 422 naming each one's line and type, and nothing is saved (#2012, [Case-Scoped PII Redaction](../security/case-scoped-pii-redaction.md) §Knowledge Item Titles).
- `DELETE /documents/{id}` — Delete document and remove from ChromaDB
- `POST /search` — Semantic search (vector embeddings + hybrid reranker) across user's authorized scopes
- `POST /documents/search` — Full-text keyword search over document **title and content**, on word boundaries (#1288). Scored `0.7 * title + 0.3 * content` on a 0–1 scale; documents matching neither field are not returned. Distinct from `/search` — no vector retrieval.

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
| **Upload directly** | Platform admin only | Team member, into a team they belong to | Owner only (Dashboard UI) |
| **Promote from Personal** | — | Team member submits, team admin approves | — (source tier) |
| **Query during case** | All users (auto-searched) | Team members only | Owner only |
| **Delete** | Platform admin only | Document owner (single-tenant: also the platform operator; owner-only under multi) | Owner only |
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
| Remediation knowledge | `kb_qa` | "How to fix PostgreSQL connection pool exhaustion?" |
| Case-specific evidence | `answer_from_case_evidence` | "What errors are on line 1045 of the uploaded server.log?" |

The agent does not decide which KB tier to search — the federated search layer handles that automatically based on the user's authorization context. The agent focuses on *what to ask*, not *where to look*.

---

## Implementation Notes

### Runbook Similarity — No Second Collection

Runbook similarity and deduplication at report-generation time (not investigative Q&A) is served by `RunbookKnowledgeBase` in `infrastructure/knowledge/runbook_kb.py`, which exposes `search_by_text()` / `search_runbooks()` (pure vector, no hybrid search). It is **read-only**: runbooks are published through `KnowledgeService.ingest_runbook`, the same writer as every other KB item, and dedup reads the chunk rows that writer stamps (fm#1030).

It is **not** an exception to the single-collection design. `RunbookKnowledgeBase` is constructed over an injected `ChromaDBVectorStore` and never selects a collection, so it reads whatever collection that store is bound to — `faultmaven_kb`, the same one every KB tier shares.

The separation is therefore metadata, exactly like the tiers above it: `{"document_type": "runbook"}` distinguishes runbook rows from other document chunks, and the caller-supplied KB scope filter (`build_kb_scope_filter`: global ∪ owned ∪ team-shared) supplies the isolation that a similarity query — which names no id and no owner — cannot get any other way. Both are mandatory on `search_runbooks`: a falsy scope filter is refused with a typed error, never queried unscoped. See [runbook-dedup.md](./runbook-dedup.md) for the full invariants and [vector-retrieval-architecture.md §4](./vector-retrieval-architecture.md) for the clause shape.

Results are honest KB-item references (`item_id` = `parent_document_id`, `title`, `scope`, best-chunk `similarity_score`) — the search does not rebuild report rows, and it collapses a runbook's N chunks to one match.

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
`search_documents()` (scope-enforcing) and the agent's `kb_qa` tool
(`hybrid_search`). See [vector-retrieval-architecture.md](./vector-retrieval-architecture.md).

### Knowledge Suggestions — Removed (#1897)

A case becomes a runbook through one writer: `ConversionService.convert_from_case`,
triggered from chat at resolution behind the CONFIRMED-cause gate, writing a
draft into the case owner's **personal** scope
([document-to-runbook-conversion.md](./document-to-runbook-conversion.md)).

An earlier second writer — the knowledge-suggestion subsystem
(`POST /cases/{case_id}/extract-knowledge`, the six `/knowledge/suggestions`
review routes, and the `knowledge_suggestions` table) — extracted a runbook from
raw case text into a platform-admin review queue whose approval published at
**global** scope. It skipped the cause gate, could never publish under
`TENANT_PROVIDER=multi` (approval is refused there), and had no first-party
client. It was removed whole, with no replacement (owner ruling 2026-10-09;
contract 13.0.0; migration `011_remove_knowledge_suggestions`, which also drops
the never-written `knowledge_items.source_suggestion_id`). Nothing promotes a
case-generated runbook to the global tier: global content ships through the KB
pack and the platform-admin authoring routes above.

---

## Related Documents

### Knowledge Architecture

- **[Runbook Content Architecture](./runbook-content-architecture.md)** — What goes INTO the KB: taxonomy, templates, quality gates, lifecycle. Companion to this document.

### Evidence Architecture (Separate Concern)

- **[Case Evidence Store](../case-and-session/case-evidence-store.md)** — Storage and lifecycle for case-specific diagnostic evidence
- **[Data Preprocessing Design](../data-processing/data-preprocessing-design-specification.md)** — How submitted data is classified, extracted, and indexed

### Investigation Context

- **[Investigation Lifecycle Logic](../investigation-engine/investigation-lifecycle-logic.md)** — How both knowledge and evidence feed into the investigation engine
