### Knowledge Base (KB) Persistence Design

This document describes how FaultMaven persists Knowledge Base (KB) data. SQLite is the document inventory — the `knowledge_items` table is the source of truth for published documents — ChromaDB stores vector chunks for RAG search, and markdown files on disk hold built-in runbook source content.

### Goals

- Persist KB document metadata across API restarts
- Support fast list/get/filter/delete operations via SQLite
- Support semantic retrieval via ChromaDB (vector chunks)
- Clear separation: SQLite for inventory, ChromaDB for search, disk for content

### Architecture Overview

- **SQLite** (`knowledge_items` table) — published document inventory, metadata, status, CRUD (source of truth). `conversion_drafts` + `conversion_jobs` are the review queue for the conversion pipeline only (the Drafts tab), **not** the published inventory.
- **ChromaDB** (`faultmaven_kb` collection) — chunked vector embeddings for RAG search only
- **Disk** (`data/knowledge/{scope}/`) — markdown source files
- **Redis** — not used for KB documents (used only for sessions, rate limiting)

### SQLite Data Model

Document metadata is stored in the `conversion_drafts` table:

| Column | Purpose |
|--------|---------|
| `id` | Draft ID (PK) |
| `conversion_id` | FK to `conversion_jobs` |
| `runbook_id` | Stable document identifier |
| `title` | Display name |
| `file_path` | Path to markdown on disk |
| `status` | `draft`, `verified`, `deactivated`, `deleted` |
| `knowledge_item_id` | ID used as `parent_document_id` in ChromaDB chunks |
| `domain` | Dashboard filter (compute, database, networking, etc.) |
| `service` | Dashboard filter (kubernetes, postgresql, redis, etc.) |
| `severity` | Dashboard filter (high, medium, low) |
| `tags` | Comma-separated tag list |
| `document_type` | Default `runbook` |
| `quality_score` | Quality rating from scorer |
| `verified_at` | Activation timestamp |
| `verified_by` | Who activated |

Scope and ownership come from the joined `conversion_jobs` table (`scope`, `user_id`, `team_id`).

### ChromaDB Storage Model

- Collection: `faultmaven_kb`
- Documents are split into structure-aware chunks (200-3000 chars, markdown header boundaries)
- Each chunk has explicit BGE-M3 embedding (1024 dims) — not ChromaDB default
- Chunk metadata includes: `parent_document_id`, `chunk_index`, `total_chunks`, `title`, `domain`, `service`, `severity`, `scope`, `owner_id`, `team_id`

### Request Flows

- **Upload** (POST `/api/v1/knowledge/documents`)
  1. Validate and read file
  2. Create `ConversionDraftModel` + `ConversionJobModel` in SQLite (status=verified)
  3. Write markdown to disk in `data/knowledge/{scope}/`
  4. Insert `knowledge_items` row + chunk/embed/store in ChromaDB via `ingest_runbook()` (SQL-first dual write)
  5. Return `{ document_id, status, metadata }`

- **Activate** (POST `/api/v1/knowledge/conversions/{id}/drafts/{draft_id}/verify`)
  1. Update SQLite: status=verified, populate domain/service/severity/tags from frontmatter
  2. Insert `knowledge_items` row (verification_level=COMMUNITY, verified_by=user_id) + chunk/embed/store in ChromaDB via `ingest_runbook()` (SQL-first dual write)
  3. Set `knowledge_item_id` on draft record

- **Batch Activate** (POST `/api/v1/knowledge/drafts/verify-batch`)
  1. Process each draft sequentially via `verify_draft()`
  2. Return per-item status (verified/failed/skipped)

- **List** (GET `/api/v1/knowledge/documents`)
  1. Query `knowledge_items WHERE is_published=True` via `list_for_inventory`, with RBAC (org tenancy + personal → owner only, team → members only) enforced **in-query**
  2. Apply optional scope/type/tag filters over the tenant-isolated set
  3. Paginate and return with scope counts

- **Get** (GET `/api/v1/knowledge/documents/{id}`)
  1. Query `knowledge_items` by `item_id`
  2. Content comes from the stored `knowledge_items.content` row (not disk)

- **Delete** (DELETE `/api/v1/knowledge/documents/{id}`) — provenance-gated:
  1. **Built-in** (`kb_<12 hex>` id) → unpublish: set `is_published=False` AND delete ChromaDB vectors (retrieval ignores `is_published`, so the vectors must go; the row is kept so the on-disk file doesn't resurrect it)
  2. **Authored** (UUID id) → hard delete the `knowledge_items` row + its ChromaDB vectors

- **Search** (RAG during investigations)
  1. `KnowledgeVectorStore.hybrid_search()` — two-stage: vector + keyword recall, then 4-signal reranker
  2. All queries use explicit BGE-M3 embeddings (`query_embeddings`), not ChromaDB defaults

### Configuration

- SQLite: `data/faultmaven.db` (created by Alembic migrations)
- ChromaDB: `data/chroma-kb/` (PersistentClient for local deployment)
- Embedding model: BGE-M3 (1024 dims) via `model_cache`

### Failure Modes

- SQLite unavailable: document CRUD fails, search still works via ChromaDB
- ChromaDB unavailable: search fails, document CRUD still works via SQLite
- Embedding model unavailable: ingestion fails (chunks not stored), existing search works

### Testing

- Upload → List (document appears in SQLite) → Restart → List again (persists)
- Activate draft → verify chunks in ChromaDB with correct embeddings and metadata
- Delete → status changes to deactivated, chunks removed from ChromaDB
- Search returns chunks with domain/service metadata for reranking
