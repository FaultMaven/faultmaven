# Knowledge Base Storage Schema

**Last Updated**: 2026-04-19

This document covers FaultMaven's two knowledge storage systems: the unified Knowledge Base (all scopes) and Case Working Memory.

## Deployment Applicability

> **Read this before interpreting any DDL in this document.**

The relational tables in this document (`knowledge_items`, `knowledge_suggestions`, `conversion_jobs`, `conversion_drafts`) are **Tier 1 (logical schema)** — both SQLite (Local Deployment) and PostgreSQL (Cloud Deployment) implement all columns listed. The following are **Tier 2 (PostgreSQL-only)** augmentations:

- `CHECK` constraints on `verification_level` (0–2 range) exist in the live ORM and apply to both dialects for simple integer range checks. The `embedding_vector` column type switch from `TEXT` to `vector(1024)` (pgvector) is Tier 2 (PostgreSQL-only).
- `llm_config_overrides` — table exists in both schemas but is only populated in Cloud Deployment (`AUTH_MODE` environment, dashboard-managed config). Local Deployment reads from `.env` exclusively. See the per-table applicability matrix in [deployment-schema-strategy.md §2](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md).

**Scope-isolation enforcement**: §1.1 states that `KnowledgeVectorStore` rejects any query to `faultmaven_kb` that lacks a scope filter. This enforcement lives in `faultmaven/infrastructure/knowledge/knowledge_vector_store.py` (the wrapper class), not in `faultmaven/infrastructure/persistence/chromadb_store.py`. The base `ChromaDBVectorStore` passes `filters` through verbatim without enforcing a scope filter. Cross-tenant isolation is guaranteed by `KnowledgeVectorStore`, not the base store.

For the full policy, see [deployment-schema-strategy.md](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md).

---

## Table of Contents

1. [Knowledge Base Storage (Unified)](#1-knowledge-base-storage-unified) - All runbooks and documentation (global, personal, team) in one collection with metadata scope filtering
2. [Case Working Memory Storage](#2-case-working-memory-storage) - Ephemeral per-case document storage
3. [Global-Scope Content Management](#3-global-scope-content-management) - Admin curation of global-scope content within the unified KB
4. [Conversion Storage](#4-conversion-storage-runbook-draft-pipeline) - Document-to-runbook and case-to-runbook draft pipeline

---

## 1. Knowledge Base Storage (Unified)

### 1.1 Architecture Overview

**Purpose**: All runbooks, procedures, and documentation — global, personal, and team-scoped — stored in a single collection with scope-aware metadata filtering at query time.

**Storage**: Single ChromaDB collection with metadata-based scope filtering
**Collection**: `faultmaven_kb`
**Scope Fields**: `scope` (`global` | `personal`), `owner_id` — the **immutable floor** stored in chunk metadata at ingestion. Team visibility is **not** a metadata field: it lives in the relational `resource_shares` table (ADR-013 §D4) and is resolved to an id allowlist (`parent_document_id ∈ {…}`) at query time. A team-shared item is tagged `scope=personal` in metadata (never `team`, which would orphan the chunk on unshare, nor `global`, which would leak it).
**Implementation**: `faultmaven/infrastructure/knowledge/knowledge_vector_store.py` (`KnowledgeVectorStore`)
**Search Tool**: `faultmaven/modules/agent/tools/kb_qa.py` — the unified `answer_from_kb` tool serves all scopes

**Scope-invariant enforcement**: `KnowledgeVectorStore` rejects any query to `faultmaven_kb` that lacks a scope filter. Cross-tenant isolation is enforced by the `KnowledgeVectorStore` wrapper class in `faultmaven/infrastructure/knowledge/knowledge_vector_store.py` — not by the base `ChromaDBVectorStore` (which passes filters through verbatim). Application-layer callers must always provide a scope filter; the wrapper enforces this invariant at the infrastructure layer.

### 1.2 Storage Characteristics

**Permanent Storage**:
- Documents persist indefinitely (no TTL)
- User controls lifecycle through explicit deletion
- Grows with user's documented knowledge

**Semantic Search**:

- BGE-M3 embeddings (1024 dimensions, multilingual) for vector similarity
- Sub-second search for typical queries
- Relevance ranking by cosine similarity

### 1.3 Document Structure

> **Conceptual schema** — what a KB document logically contains at ingest. The on-disk representation is a markdown file under `data/knowledge/{scope}/`; the on-vector-store representation is per-chunk metadata in ChromaDB (see [vector-retrieval-architecture.md §4 "Metadata Per Chunk"](../../knowledge-and-ai/vector-retrieval-architecture.md#4-knowledge-base-retrieval) for the canonical chunk-level schema). The closest in-process Pydantic types are `KnowledgeBaseDocument` (`faultmaven/models/api.py`) and `KBDocument` (`faultmaven/models/interfaces_kb.py`).

```python
# Conceptual ingest-time document — not a single canonical Pydantic class.
KnowledgeDocument:
    document_id: str
    title: str
    content: str
    document_type: str   # troubleshooting | configuration | runbook

    # Scope metadata (required — enforced at ingest). The chunk metadata carries
    # only the immutable floor; team visibility lives in the resource_shares
    # table (ADR-013 §D4), resolved to an id allowlist at query time.
    scope: str           # "global" | "personal" (a team item is tagged "personal")
    owner_id: str | None # user_id (author) — an author always sees their own

    # Frontmatter-derived metadata
    metadata:
        author: str
        version: str
        tags: List[str]
        source_url: str
        last_updated: str
        difficulty: str  # beginner | intermediate | advanced
        category: str

    created_at: datetime
    updated_at: datetime
```

### 1.4 Access Patterns

```python
# Add documents (scope set at ingestion)
await knowledge_vector_store.add_documents(
    documents,
    scope="personal",
    owner_id=user_id,
)

# Semantic search — scope filter is mandatory
results = await knowledge_vector_store.search(
    query="DB timeouts",
    k=5,
    # visible-id allowlist (ADR-011 D3): global ∪ owned ∪ shared-to-my-teams.
    # shared_ids is resolved in SQL from resource_shares by the caller.
    scope_filter={
        "$or": [
            {"scope": "global"},
            {"owner_id": user_id},
            {"parent_document_id": {"$in": shared_ids}},
        ]
    },
)

# The unified kb_qa tool builds this filter automatically from the caller's identity.
```

---

## 2. Case Working Memory Storage

### 2.1 Architecture Overview

**Purpose**: Ephemeral session-specific RAG for temporary document storage during active troubleshooting

**Key Differences from the Unified KB**:
- **Lifecycle**: Ephemeral (deleted when case closes)
- **Scope**: Case-specific collections (`case_{case_id}`)
- **TTL**: Tied to case lifecycle + 7 days cleanup
- **Use Case**: QA sub-agent for "What does this uploaded PDF say?"

**Storage**: ChromaDB
**Collection Naming**: `case_{case_id}`
**Implementation**: `faultmaven/infrastructure/persistence/case_vector_store.py`

### 2.2 Storage Characteristics

**Ephemeral Storage**:

- Collections created on-demand when first document added
- Deleted when case closes (immediate via `delete_case_collection`) or swept by `cleanup_orphaned_collections(active_case_ids)` (reactive pattern — not scheduled)
- No cross-case sharing

> **Note**: The 7-day grace period TTL, `schedule_cleanup`, `get_expired_collections`, and daily cleanup job described below in "Future / Not Implemented" are **aspirational — not implemented**. The live `CaseVectorStore` uses immediate deletion (`delete_case_collection`) and a reactive orphan sweep (`cleanup_orphaned_collections(active_case_ids)`). Collection metadata carries only `case_id` + `created_at` — no `expiry_date` or `cleanup_after` fields. See [deployment-schema-strategy.md §5](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md) for the open decision on whether to implement scheduled TTL cleanup.

**Semantic Search**:

- Same BGE-M3 embeddings as the unified KB
- Case-scoped search (only within current case)
- Used by the `answer_from_case_evidence` tool

### 2.3 Collection Metadata

```python
# Collection metadata — only case_id and created_at are stored (live implementation)
{
    "case_id": "case_abc123",
    "created_at": "2025-01-15T10:30:00Z",
    "type": "case_working_memory",
}
```

### 2.4 Lifecycle Management

```python
# Case lifecycle integration — live implementation
async def close_case(case_id: str):
    case = await case_repository.get(case_id)
    case.state = CaseState.RESOLVED
    case.resolved_at = datetime.now(timezone.utc)
    await case_repository.save(case)

    # Immediately delete the case vector collection on close
    await case_vector_store.delete_case_collection(case_id)

# Orphan sweep (reactive — not scheduled; called when active case list is known)
async def sweep_orphaned_collections(active_case_ids: list[str]):
    await case_vector_store.cleanup_orphaned_collections(active_case_ids)
```

### 2.4.1 Future / Not Implemented

The following TTL-based cleanup design is **not implemented**. Retained here for reference if a scheduled cleanup job is added in the future (see [deployment-schema-strategy.md §5](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md)).

```python
# NOT IMPLEMENTED — aspirational TTL-based cleanup
async def close_case_with_ttl(case_id: str):
    # ...
    cleanup_date = case.resolved_at + timedelta(days=7)
    await case_vector_store.schedule_cleanup(case_id, cleanup_date)  # method does not exist

# NOT IMPLEMENTED — daily scheduled job
async def cleanup_expired_case_collections():
    expired = await case_vector_store.get_expired_collections()  # method does not exist
    for collection_name in expired:
        await case_vector_store.delete_case_collection(collection_name)
```

The metadata fields `expiry_date`, `cleanup_after`, and `case_status` are not stored in live collection metadata.

### 2.5 Access Patterns

```python
# Add case-specific documents
await case_vector_store.add_documents(case_id, documents)

# Case-scoped search
results = await case_vector_store.search(
    case_id="case_abc123",
    query="error on page 5 of PDF",
    k=5
)

# Delete collection when case closes
await case_vector_store.delete_case_collection(case_id)
```

---

## 3. Global-Scope Content Management

Global runbooks and methodologies live in the unified `faultmaven_kb` collection with `scope="global"` metadata — they are not stored in a separate collection. This section covers the admin-only curation and ingestion workflow specific to global content. Personal and team scopes follow §1.4.

### 3.1 Content Types

- Industry-standard troubleshooting approaches
- Common error patterns and solutions
- Best practices and anti-patterns
- Methodology guides (SRE, DevOps)
- Tool usage examples

### 3.2 Startup Bootstrap Ingestion (built-in runbooks)

```text
# faultmaven/bootstrap/kb_init.py — bootstrap_kb()
# Built-ins ship in the KB pack (resources/knowledge/pack, or KB_PACK_DIR),
# pre-chunked + pre-embedded by faultmaven-kb-toolkit (kb-build-pack).
#
# Per runbook (idempotent by content_hash):
#   ingest_runbook(prechunked=[(chunk_text, vector), ...], scope=<from pack>)
#   → knowledge_items SQL row + the pack's chunks/vectors into faultmaven_kb.
#   NO embedding model runs at startup.
```

On first startup FaultMaven ingests the 59 built-in runbooks from the **KB pack**
into `faultmaven_kb` (`scope="global"`) in seconds — no model load. See
[`kb-pack-architecture.md`](../../knowledge-and-ai/kb-pack-architecture.md).

### 3.3 Access Control

**Read Access**: All authenticated users (via scope filter — global content is visible to everyone)
**Write Access**: System administrators only

```python
async def update_global_content(
    admin_user: User,
    documents: List[KnowledgeDocument]
):
    if "admin" not in admin_user.roles:
        raise PermissionDeniedError()

    await knowledge_vector_store.add_documents(
        documents,
        scope="global",
    )
    logger.info(f"Global KB content updated by {admin_user.username}")
```

### 3.4 Performance Characteristics

- Global-scope queries benefit from ChromaDB's HNSW index across the full `faultmaven_kb` collection
- Sub-200ms typical query time for global-only searches
- Popular queries cached via the shared LLM cache layer (response cache, not vector cache)

---

## 4. Conversion Storage (Runbook Draft Pipeline)

### 4.1 Conversion Jobs Table

Tracks conversion requests — both document-to-runbook and case-to-runbook.

```sql
CREATE TABLE conversion_jobs (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    organization_id VARCHAR(36),
    scope VARCHAR(20) NOT NULL,              -- global, team, personal
    team_id VARCHAR(36),
    status VARCHAR(20) NOT NULL DEFAULT 'processing',  -- processing, completed, partial, failed
    source_filename VARCHAR(255) NOT NULL,
    source_content_type VARCHAR(100) NOT NULL,
    source_size_bytes INTEGER NOT NULL,
    source_path VARCHAR(500) NOT NULL,
    source_type VARCHAR(20) NOT NULL DEFAULT 'document',  -- 'document' or 'case'
    case_id VARCHAR(36),                     -- populated when source_type = 'case'
    failure_modes_detected INTEGER NOT NULL DEFAULT 0,
    analysis_result JSON,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX ix_conversion_jobs_user_id ON conversion_jobs(user_id);
CREATE INDEX ix_conversion_jobs_case_id ON conversion_jobs(case_id);
```

### 4.2 Conversion Drafts Table

Individual runbook drafts generated from a conversion job.

```sql
CREATE TABLE conversion_drafts (
    id VARCHAR(36) PRIMARY KEY,
    conversion_id VARCHAR(36) NOT NULL REFERENCES conversion_jobs(id) ON DELETE CASCADE,
    runbook_id VARCHAR(100) NOT NULL,
    title VARCHAR(255) NOT NULL,
    file_path VARCHAR(500) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'draft',       -- draft, verified, deleted
    source_type VARCHAR(20) NOT NULL DEFAULT 'document', -- mirrors job source_type
    validation_passed BOOLEAN NOT NULL DEFAULT TRUE,
    validation_errors JSON,
    validation_warnings JSON,
    quality_score NUMERIC(5,1),
    quality_details JSON,
    knowledge_item_id VARCHAR(36),           -- set after verify & ingest

    -- KB metadata — populated from runbook frontmatter during scan/verify
    -- These columns exist in the live ORM (models.py:2069) and are documented here.
    domain VARCHAR(50),                      -- e.g. 'databases', 'networking'
    service VARCHAR(100),                    -- e.g. 'postgresql', 'redis'
    severity VARCHAR(20),                    -- e.g. 'critical', 'warning'
    tags TEXT,                               -- JSON array or comma-separated string
    document_type VARCHAR(50) DEFAULT 'runbook',  -- document classification

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verified_at TIMESTAMPTZ,
    verified_by VARCHAR(36)
);
CREATE INDEX ix_conversion_drafts_conversion_id ON conversion_drafts(conversion_id);
```

### 4.3 Source Type Tracking

The `source_type` field distinguishes the two conversion pipelines:

| Value | Source | Entry Point | Notes |
| --- | --- | --- | --- |
| `document` | Uploaded file (PDF, DOCX, MD, etc.) | `POST /knowledge/convert` | Default. Multi-failure-mode analysis. |
| `case` | Resolved investigation case | `POST /knowledge/convert-from-case` | Single failure mode from case data. `case_id` populated. |

Both produce drafts with the canonical runbook template and enter the same review workflow (edit → verify → ingest).

### 4.4 VectorMetadata Tags Format

ChromaDB metadata only accepts scalar values. Tags are stored as **comma-joined strings**, not lists:

```python
# Correct (ChromaDB compatible)
metadata = {"tags": "postgresql,aws-rds,pgbouncer"}

# Wrong (ChromaDB rejects lists)
metadata = {"tags": ["postgresql", "aws-rds", "pgbouncer"]}
```

The `VectorMetadata.to_chroma_metadata()` method handles this conversion. The `KnowledgeIngester` uses `",".join(document.tags)` for the same purpose.

---

## 5. Relational Knowledge Tables

The ChromaDB vector store holds chunk embeddings for fast semantic search. The relational tables below hold the authoritative record of each knowledge item and its provenance, and support the human-in-the-loop (HITL) review pipeline.

### 5.1 knowledge_items

**Purpose**: The relational KB entry — one row per published or draft knowledge item. Stores full content, verification state, usage counters, and a stub for future pgvector embeddings. This table is the source of truth for item lifecycle; ChromaDB holds the chunked embeddings derived from `content`.

**When written**: Populated three ways, all landing here as the source of truth for the published inventory: (1) the startup KB **bootstrap**, which ingests built-in runbook files directly (`verified_by=NULL`, `verification_level=COMMUNITY`, deterministic `kb_<12 hex>` id); (2) the conversion pipeline (`conversion_drafts` → `verify_draft` → `knowledge_items`, random-UUID id); (3) direct admin/API ingestion. The dashboard inventory surface (`list_documents` / `get_document` / `delete_document`) reads this table — **not** `conversion_drafts`. The `knowledge_item_id` FK on `conversion_drafts` and `knowledge_suggestions` links forward to the promoted item.

> **`verified_by` contract**: FK to `users.user_id` — a real user or `NULL`, **never a sentinel string**. Platform/built-in trust is carried by `verification_level` (COMMUNITY), not a fake verifier.

**Key columns** (see `models.py:1679`, 29 columns total):

| Column | Type | Notes |
| --- | --- | --- |
| `item_id` | VARCHAR(36) PK | Width updated in storage redesign 2026-04 Phase 4 (FK width normalization to VARCHAR(36)) |
| `organization_id` | VARCHAR(36) | No FK — items persist independently of org lifecycle. Width updated in storage redesign 2026-04 Phase 4 (FK width normalization to VARCHAR(36)) |
| `scope` | VARCHAR(20) | `personal\|team\|global` — enforced by CHECK (Tier 1) |
| `owner_id` | VARCHAR(36) nullable | Set when scope = personal |
| `team_id` | VARCHAR(36) nullable | Set when scope = team |
| `title` | VARCHAR(512) NOT NULL | |
| `content` | TEXT NOT NULL | Full runbook/doc text |
| `item_type` | VARCHAR(64) | `troubleshooting_guide\|error_pattern\|solution_template\|api_documentation\|configuration_guide\|best_practice\|faq\|runbook` |
| `category` | VARCHAR(128) nullable | Free-text category label |
| `tags` | TEXT (JSON array) | |
| `embedding_model` | VARCHAR(128) | Default `bge-m3` |
| `embedding_vector` | TEXT nullable | Stub for future pgvector — Tier 2 (PostgreSQL-only) will switch type to `vector(1024)` |
| `embedding_version` | INTEGER | Monotonically increasing; CHECK >= 1 |
| `source_url` | VARCHAR(2048) nullable | |
| `author` | VARCHAR(255) nullable | |
| `language` | VARCHAR(8) | Default `en` |
| `verification_level` | INTEGER | 0 = experimental, 1 = community, 2 = admin\_verified; CHECK 0–2 (Tier 1) |
| `verification_reason` | VARCHAR(512) nullable | |
| `verified_by` | VARCHAR(36) nullable | Width updated in storage redesign 2026-04 Phase 4 (FK width normalization to VARCHAR(36)) |
| `verified_at` | TIMESTAMPTZ nullable | |
| `source_suggestion_id` | VARCHAR(36) nullable | FK (logical) to `knowledge_suggestions.suggestion_id`. Width updated in storage redesign 2026-04 Phase 4 |
| `view_count` | INTEGER | Usage counter; CHECK >= 0 |
| `helpful_count` | INTEGER | Feedback counter; CHECK >= 0 |
| `not_helpful_count` | INTEGER | Feedback counter; CHECK >= 0 |
| `last_retrieved_at` | TIMESTAMPTZ nullable | Updated on each retrieval |
| `is_published` | BOOLEAN | False = draft/hidden from search |
| `metadata` | TEXT (JSON) | Stored as `knowledge_metadata` Python attribute |

**Applicability**: Both deployments (✅ Both). Cross-reference the unified ChromaDB `faultmaven_kb` collection — chunk embeddings in ChromaDB are derived from `knowledge_items.content`; the `item_id` is stored in ChromaDB chunk metadata as the source document reference.

### 5.2 knowledge_suggestions

**Purpose**: HITL (human-in-the-loop) review pipeline for candidate runbooks extracted from resolved cases. A suggestion moves through PII scanning, human review, and then promotion to a published `knowledge_items` row (or rejection).

**When written**: Created by the conversion service when a case is converted to a runbook draft. Also created by any pathway that surfaces a "candidate KB entry" for admin review.

**PII scanning lifecycle**: `pii_scan_status` moves through `not_scanned` → `scanning` → `clean` (or `pii_detected` → `remediated` or `scan_failed`). Only `clean` or `remediated` suggestions may be reviewed by a human.

**Key columns** (see `models.py:1801`, 26 columns total):

| Column | Type | Notes |
| --- | --- | --- |
| `suggestion_id` | VARCHAR(36) PK | Width updated in storage redesign 2026-04 Phase 4 (FK width normalization to VARCHAR(36)) |
| `organization_id` | VARCHAR(36) | Width updated in storage redesign 2026-04 Phase 4 (FK width normalization to VARCHAR(36)) |
| `case_id` | VARCHAR(36) | Source case (logical FK — no DB constraint). Width updated in storage redesign 2026-04 Phase 4 |
| `status` | VARCHAR(32) | `pending_review\|approved\|rejected\|draft` |
| `suggested_title` | VARCHAR(512) NOT NULL | |
| `suggested_content` | TEXT NOT NULL | |
| `suggested_type` | VARCHAR(64) | Default `troubleshooting_guide` |
| `extracted_by` | VARCHAR(36) | User or system that triggered extraction. Width updated in storage redesign 2026-04 Phase 4 |
| `extracted_at` | TIMESTAMPTZ | |
| `include_messages` | BOOLEAN | Whether case messages were included in extraction |
| `include_evidence` | BOOLEAN | Whether evidence was included |
| `pii_scan_status` | VARCHAR(32) | `not_scanned\|scanning\|clean\|pii_detected\|remediated\|scan_failed` |
| `pii_scan_result` | TEXT (JSON) nullable | Raw scan output |
| `pii_remediated_by` | VARCHAR(36) nullable | Width updated in storage redesign 2026-04 Phase 4 |
| `pii_remediated_at` | TIMESTAMPTZ nullable | |
| `source_case_title` | VARCHAR(512) nullable | Denormalized for display in review inbox |
| `message_count` | INTEGER | Lineage counter; CHECK >= 0 |
| `evidence_count` | INTEGER | Lineage counter; CHECK >= 0 |
| `reviewed_by` | VARCHAR(36) nullable | Width updated in storage redesign 2026-04 Phase 4 |
| `reviewed_at` | TIMESTAMPTZ nullable | |
| `review_notes` | TEXT nullable | |
| `rejection_reason` | TEXT nullable | |
| `knowledge_item_id` | VARCHAR(36) nullable | Set when suggestion is promoted to a published item. Width updated in storage redesign 2026-04 Phase 4 |
| `metadata` | TEXT (JSON) | Stored as `suggestion_metadata` Python attribute |

**Applicability**: Both deployments (✅ Both). The conversion service runs in both Local and Cloud deployments.

### 5.3 llm_config_overrides (Config Domain — Cloud-only behavior)

`llm_config_overrides` is a Config-domain table (not Knowledge domain) included here for cross-reference completeness. It stores dashboard-applied key/value LLM configuration overrides that take precedence over environment variables.

**Applicability**: Cloud-only behavior (🌐). The table exists in both schemas per the no-divergence rule, but Local Deployment reads from `.env` exclusively — the table is never populated in local mode. See [deployment-schema-strategy.md §2](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md) and `faultmaven/config/llm_config_overrides.py` for the hot-reload logic.

---

## Related Documentation

- **[vector-storage.md](../vector-storage.md)** - ChromaDB implementation details and operations
- **[case-schema.md](./case-schema.md)** - Case data model and investigation storage
- **[../knowledge-and-ai/knowledge-base-architecture.md](../../knowledge-and-ai/knowledge-base-architecture.md)** - RAG pipeline and embeddings
- **[overview.md](../overview.md)** - Complete storage architecture overview
- **[deployment-schema-strategy.md](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md)** - Tier 1/2 dialect policy and per-table applicability matrix

---

**Changelog**:

| Version | Date | Changes |
| --- | --- | --- |
| 1.3 | 2026-04-19 | Audit fix (storage redesign Phase 9): normalized entity-ID column widths from VARCHAR(64) to VARCHAR(36) throughout §5.1 (`knowledge_items`: item\_id, organization\_id, verified\_by, source\_suggestion\_id) and §5.2 (`knowledge_suggestions`: suggestion\_id, organization\_id, case\_id, extracted\_by, pii\_remediated\_by, reviewed\_by, knowledge\_item\_id). Reflects Phase 4 FK width normalization. Non-entity VARCHAR(64) columns (item\_type, suggested\_type) are unchanged. |
| 1.2 | 2026-04-19 | Aligned with deployment-schema-strategy.md v2.1 (no functional changes to knowledge domain). Consistency check pass: updated all deployment-schema-strategy.md links to GitHub URL format. Confirmed `knowledge_items.embedding_vector` TEXT stub (Tier 1) / `vector(1024)` (Tier 2 PG pgvector) — correct and unchanged. Confirmed `llm_config_overrides` as infrastructure layer (not knowledge domain) per strategy doc §11.3 — unchanged. |
| 1.1 | 2026-04-18 | Aligned with deployment-schema-strategy.md v1.0. Added Deployment Applicability banner clarifying Tier 1/2 policy, scope-isolation enforcement location (KnowledgeVectorStore wrapper, not ChromaDBVectorStore base), and llm\_config\_overrides Cloud-only behavior. Corrected §1.1 scope-isolation description. Corrected §2.2 ephemeral storage — TTL/scheduled-cleanup is aspirational, not implemented; reactive orphan sweep is the live behavior. Added §4.2 undocumented conversion\_drafts columns (domain, service, severity, tags, document\_type). Added §5 with full narratives for knowledge\_items (29 cols, verification lifecycle, embedding\_vector pgvector stub) and knowledge\_suggestions (26 cols, 6-value pii\_scan\_status HITL pipeline). Added §5.3 llm\_config\_overrides cross-reference. |
