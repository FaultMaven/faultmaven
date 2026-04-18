# Knowledge Base Storage Schema

This document covers FaultMaven's two knowledge storage systems: the unified Knowledge Base (all scopes) and Case Working Memory.

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
**Scope Fields**: `scope` (`global` | `personal` | `team`), `owner_id`, `team_id` — stored at ingestion, filtered at query time
**Implementation**: `faultmaven/infrastructure/knowledge/knowledge_vector_store.py` (`KnowledgeVectorStore`)
**Search Tool**: `faultmaven/modules/agent/tools/kb_qa.py` — the unified `answer_from_kb` tool serves all scopes

**Scope-invariant enforcement**: `KnowledgeVectorStore` rejects any query to `faultmaven_kb` that lacks a scope filter. Cross-tenant isolation is enforced at the infrastructure layer — not just at the application layer.

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

```python
class KnowledgeDocument(BaseModel):
    document_id: str
    title: str
    content: str
    document_type: str  # troubleshooting, configuration, runbook

    # Scope metadata (required — enforced at ingest)
    scope: str          # "global" | "personal" | "team"
    owner_id: str | None  # user_id when scope == "personal"
    team_id: str | None   # team_id when scope == "team"

    metadata: Dict[str, Any] = {
        "author": str,
        "version": str,
        "tags": List[str],
        "source_url": str,
        "last_updated": str,
        "difficulty": str,  # beginner, intermediate, advanced
        "category": str,
    }

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
    scope_filter={
        "$or": [
            {"scope": "global"},
            {"$and": [{"scope": "personal"}, {"owner_id": user_id}]},
            {"$and": [{"scope": "team"}, {"team_id": {"$in": user_teams}}]},
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
- Automatically deleted when case closes or archives
- 7-day grace period after case closure for forensics
- No cross-case sharing

**Semantic Search**:

- Same BGE-M3 embeddings as the unified KB
- Case-scoped search (only within current case)
- Used by the `answer_from_case_evidence` tool

### 2.3 Collection Metadata

```python
# Collection metadata with TTL tracking
{
    "case_id": "case_abc123",
    "created_at": "2025-01-15T10:30:00Z",
    "type": "case_working_memory",
    "case_status": "investigating",  # Updated on case status change
    "expiry_date": None,  # Set when case closes
    "cleanup_after": "2025-02-01T10:30:00Z"  # case_closed_at + 7 days
}
```

### 2.4 Lifecycle Management

```python
# Case lifecycle integration
async def close_case(case_id: str):
    case = await case_repository.get(case_id)
    case.status = CaseStatus.RESOLVED
    case.resolved_at = datetime.now(timezone.utc)
    await case_repository.save(case)

    # Mark case vector store for cleanup
    cleanup_date = case.resolved_at + timedelta(days=7)
    await case_vector_store.schedule_cleanup(case_id, cleanup_date)

# Cleanup job (runs daily)
async def cleanup_expired_case_collections():
    expired = await case_vector_store.get_expired_collections()
    for collection_name in expired:
        await case_vector_store.delete_collection(collection_name)
        logger.info(f"Deleted expired collection: {collection_name}")
```

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
await case_vector_store.delete_collection(case_id)
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

### 3.2 Admin Batch Ingestion

```python
# Admin uploads curated content via the knowledge ingester
from faultmaven.modules.knowledge.domain.services.ingestion import KnowledgeIngester

ingester = KnowledgeIngester()
await ingester.ingest_directory(
    path="./resources/knowledge/builtin/",
    scope="global",
)

# Pipeline steps:
# 1. Parse markdown files
# 2. Extract frontmatter metadata
# 3. Generate BGE-M3 embeddings (1024-dim, multilingual)
# 4. Batch insert to faultmaven_kb with scope="global"
```

On first startup FaultMaven auto-ingests 59 built-in runbooks from `resources/knowledge/builtin/` into `faultmaven_kb` under `scope="global"`.

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

## Related Documentation

- **[vector-storage.md](../vector-storage.md)** - ChromaDB implementation details and operations
- **[case-schema.md](./case-schema.md)** - Case data model and investigation storage
- **[../knowledge-and-ai/knowledge-base-architecture.md](../../knowledge-and-ai/knowledge-base-architecture.md)** - RAG pipeline and embeddings
- **[overview.md](../overview.md)** - Complete storage architecture overview
