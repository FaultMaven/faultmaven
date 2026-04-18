# Vector Storage Implementation

**Status**: ✅ Production Ready
**Purpose**: Complete operational guide for ChromaDB vector storage across all FaultMaven knowledge systems

---

## Table of Contents

1. [Physical Architecture](#1-physical-architecture) - ChromaDB deployment and embedding models
2. [Three Vector Storage Systems](#2-three-vector-storage-systems) - User KB, Case Working Memory, Global KB
3. [Document Ingestion Pipeline](#3-document-ingestion-pipeline) - How data gets vectorized
4. [Query Execution Flow](#4-query-execution-flow) - How semantic search works
5. [Collection Lifecycle Management](#5-collection-lifecycle-management) - Creation, usage, cleanup
6. [API Endpoints](#6-api-endpoints) - RESTful interfaces
7. [Operational Procedures](#7-operational-procedures) - Admin tasks, monitoring, backup

---

## 1. Physical Architecture

### 1.1 ChromaDB Deployment

**Single Instance, Multiple Collections Pattern**:

```
ChromaDB KB Instance (PersistentClient at data/chroma-kb/ for local, HttpClient for cloud)
├── Collections:
│   ├── faultmaven_kb         (all KB docs: global/personal/team, metadata-filtered)
│   ├── faultmaven_runbooks   (runbook similarity recommendations)
│   └── knowledge_items       (knowledge module search service)

ChromaDB Evidence Instance (PersistentClient at data/chroma-evidence/ for local, HttpClient for cloud)
├── Collections:
│   ├── case_{case_id}        (per-case evidence, dynamic, ephemeral)
│   └── ...
```

**Architecture**: Two ChromaDB clients created in the DI container — one for permanent KB collections (`kb_chromadb_client` at `data/chroma-kb/`), one for ephemeral case evidence (`evidence_chromadb_client` at `data/chroma-evidence/`). Local deployment uses `PersistentClient` (file-based), cloud uses `HttpClient` to external server. Separate instances ensure KB data is protected from evidence churn and can be backed up independently.

**Scope Isolation**: The `faultmaven_kb` collection uses metadata filtering — NOT separate collections per user/team. Scope fields (`scope`, `owner_id`, `team_id`) are stored at ingestion time. `KnowledgeVectorStore` enforces a scope-invariant check that rejects any query to `faultmaven_kb` without a scope filter. The unified `answer_from_kb` tool (in `faultmaven/modules/agent/tools/kb_qa.py`) builds a combined filter:

```python
{"$or": [
    {"scope": "global"},
    {"$and": [{"scope": "personal"}, {"owner_id": user_id}]},
    {"$and": [{"scope": "team"}, {"team_id": {"$in": team_ids}}]},
]}
```

### 1.2 Embedding Model

**Current**: BGE-M3 (BAAI/bge-m3)

- **Dimensions**: 1024
- **Max Sequence Length**: 8192 tokens
- **Language Support**: Multilingual (100+ languages)
- **Model Size**: ~2.3GB
- **Loading**: Cached in memory via `model_cache.get_bge_m3_model()`

**Location**: Loaded in-process (not external service)

- `KnowledgeIngester`: For KB document ingestion
- `PreprocessingService`: For case evidence chunking
- Q&A Tools: Generate query embeddings on the fly

### 1.3 Connection Management

**Shared Client Pattern** (Principle 5: Composition Root):

All vector stores receive the same ChromaDB client via DI injection. No store creates its own client.

```python
# DI container creates two clients (infrastructure.py)
kb_client = create_kb_chromadb_client(settings)          # PersistentClient @ data/chroma-kb/
evidence_client = create_evidence_chromadb_client(settings)  # PersistentClient @ data/chroma-evidence/

# KB client injected into permanent stores
ChromaDBVectorStore(client=kb_client, collection_name="faultmaven_kb")
VectorStoreService(client=kb_client)       # knowledge_items collection
KnowledgeVectorStore(client=kb_client)     # permanent KB collections

# Evidence client injected into ephemeral store
CaseVectorStore(client=evidence_client)    # dynamic case_{id} collections
```

---

## 2. Vector Storage Systems

### 2.1 Knowledge Base (Unified)

**Purpose**: All runbooks and documentation — global, personal, and team-scoped
**Collection**: `faultmaven_kb` (single collection, metadata-filtered by scope)
**Lifecycle**: Permanent (user/admin-controlled deletion)
**Implementation**: `faultmaven/infrastructure/persistence/chromadb_store.py` (ChromaDBVectorStore)

**Scope Isolation**: Metadata fields `scope`, `owner_id`, `team_id` stored at ingestion. The unified `answer_from_kb` tool automatically filters by the user's accessible scopes.

**Characteristics**:

- Documents persist indefinitely (no TTL)
- BGE-M3 embeddings for semantic search
- Cross-scope relevance ranking (global and personal results compete on relevance)
- Single tool (`answer_from_kb`) returns best results regardless of scope

### 2.2 Case Working Memory

**Purpose**: Ephemeral per-case document storage during active troubleshooting
**Collections**: `case_{case_id}` (dynamic, one per case)
**Lifecycle**: Case lifetime — deleted when case closes/archives
**Implementation**: `faultmaven/infrastructure/persistence/case_vector_store.py` (CaseVectorStore)

**Characteristics**:

- Collections created on-demand when first document added
- Deleted when case closes via `delete_case_collection()`
- Case-scoped search (only within current case)
- Used by the `answer_from_case_evidence` tool

### 2.3 Additional Collections

**`faultmaven_runbooks`** — Runbook similarity recommendations (report_type, domain metadata). Used by `RunbookKB` for "this incident looks like runbook X" matching.

**`knowledge_items`** — Knowledge module search service items (organization_id, item_type, category). Used by `KnowledgeSearchService` for hybrid search.

---

## 3. Document Ingestion Pipeline

### 3.1 Case Evidence Upload Flow

**Complete Pipeline** (Steps 1-8):

```
┌─────────────────────────────────────────────────────────────────┐
│ POST /api/v1/cases/{case_id}/data (Evidence Upload)            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Step 1: File Extraction                                         │
│ - Read uploaded file content                                    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Step 2: Classification (DataClassifier)                         │
│ - Detect data type (LOGS, CONFIG, METRICS, TEXT, CODE, etc.)   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Step 3: Content Extraction (Type-Specific Extractors)           │
│ - JSON/YAML parsing, log parsing, metric parsing, etc.         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Step 4: Large Document Handling (ChunkingService)               │
│ - IF content >8K tokens:                                        │
│   → MAP: Split into chunks (4K tokens each)                     │
│   → MAP: Summarize each chunk in parallel                       │
│   → REDUCE: Synthesize final summary                            │
│ - ELSE: Pass through unchanged                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Step 5: PII Sanitization (DataSanitizer)                        │
│ - Remove sensitive data (emails, IPs, keys, etc.)              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Step 6: Store in Data Storage (DataRepository)                  │
│ - Save preprocessed content and metadata                        │
│ - Return data_id and summary to client                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Step 7: Return 201 Response to Client                           │
│ - Headers: Location: /api/v1/cases/{case_id}/data/{data_id}    │
│ - Body: {data_id, summary, data_type, ...}                     │
│ - Response time: ~3-4 seconds                                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Step 8: Background Vectorization (_store_evidence_in_vector_db) │
│ - Runs asynchronously AFTER response sent                      │
│ - Stores in CaseVectorStore (ChromaDB collection)              │
│ - Embeddings generated server-side by ChromaDB                 │
│ - Silent failure if vector storage unavailable                 │
│ - Processing time: ~0.5-1 second                               │
└─────────────────────────────────────────────────────────────────┘
```

**Key Design Decisions**:
1. **Background Processing**: Uses FastAPI `BackgroundTasks` to ensure vectorization runs AFTER response is sent to client
2. **Graceful Degradation**: Vector storage failures don't affect upload success (evidence still stored in data storage)
3. **Map-Reduce for Large Documents**: Documents >8K tokens are chunked and summarized before vectorization
4. **Pluggable Storage**: Works with both InMemory and ChromaDB backends via .env configuration

### 3.2 User KB Document Ingestion

**Manual Upload Flow**:

```python
# User uploads runbook via API
POST /api/v1/knowledge/documents

# Service flow
1. Validate document (format, size, ownership)
2. Store in faultmaven_kb collection with scope/owner_id/team_id metadata
3. ChromaDB generates embeddings server-side
4. Return document_id to client

# Python API
await vector_store.add_documents([doc_dict])  # ChromaDBVectorStore
```

**Metadata passthrough**: `ChromaDBVectorStore.add_documents()` normalizes metadata through `VectorMetadata` which includes all scope fields (`scope`, `owner_id`, `team_id`). Tags are serialized as comma-joined strings (ChromaDB rejects list values in metadata).

### 3.2.1 Document Listing and Retrieval

**Source of truth**: SQLite (`conversion_drafts` table) is the document inventory. `KnowledgeService.list_documents()` and `get_document()` query SQLite, not ChromaDB. ChromaDB is used exclusively for vector similarity search during investigations.

```python
# KnowledgeService methods (query SQLite)
await knowledge_service.list_documents(scope="global", limit=50, offset=0)
await knowledge_service.get_document(document_id)  # SQLite + file read from disk

# ChromaDB methods (vector search only)
await vector_store.search(query, k=5, filters={"scope": "global"})
await vector_store.delete_documents_by_parent_id(document_id)
```

Redis is not used for KB document storage. Document metadata persists in SQLite across restarts.

### 3.3 Global-Scope Admin Ingestion

**Batch Ingestion Flow**:

```python
# Admin ingests curated global content into the unified KB
from faultmaven.modules.knowledge.domain.services.ingestion import KnowledgeIngester

ingester = container.get_knowledge_ingester()
await ingester.ingest_directory(
    path="./resources/knowledge/builtin/",
    scope="global",
)

# Steps:
# 1. Parse markdown files
# 2. Extract frontmatter metadata
# 3. Generate embeddings (BGE-M3, 1024-dim)
# 4. Batch insert to faultmaven_kb with scope="global"
```

On first startup, FaultMaven auto-ingests 59 built-in runbooks from `resources/knowledge/builtin/` with `scope="global"`.

---

## 4. Query Execution Flow

### 4.1 Semantic Search Architecture

**Single Query Flow**:

```
User Query: "How to diagnose PostgreSQL connection timeouts?"
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 1. Query Embedding Generation                                   │
│    - Load BGE-M3 model from cache                              │
│    - Generate 1024-dim embedding vector                        │
│    - Processing time: ~50ms                                    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 2. Vector Similarity Search (ChromaDB)                          │
│    - Cosine similarity against collection                      │
│    - Return top K documents (default k=5)                      │
│    - Processing time: ~100-150ms                               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 3. Result Ranking & Filtering                                   │
│    - Sort by similarity score (descending)                     │
│    - Filter by metadata (if applicable)                        │
│    - Apply access control (User KB sharing)                    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 4. Return Results                                               │
│    - Document chunks with scores                               │
│    - Metadata (source, title, tags)                            │
│    - Total processing time: ~200ms                             │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 Scope-Filtered Search (Unified KB)

**Single-collection pattern**: Every KB query runs against `faultmaven_kb` with a mandatory scope filter derived from the caller's identity. There is no multi-collection merge — ChromaDB's metadata pushdown + HNSW returns the top-K globally relevant results across all accessible scopes in one round-trip.

```python
async def search_kb(user_id: str, query: str, k: int = 5) -> List[Document]:
    user_teams = await get_user_teams(user_id)

    return await knowledge_vector_store.search(
        query=query,
        k=k,
        scope_filter={
            "$or": [
                {"scope": "global"},
                {"$and": [{"scope": "personal"}, {"owner_id": user_id}]},
                {"$and": [{"scope": "team"}, {"team_id": {"$in": user_teams}}]},
            ]
        },
    )
```

`KnowledgeVectorStore` rejects any `faultmaven_kb` query missing the scope filter — cross-tenant isolation is infrastructure-enforced, not application-enforced.

### 4.3 Performance Characteristics

| Operation | Target | Measured | Notes |
|-----------|--------|----------|-------|
| Query embedding generation | < 100ms | 50ms avg | BGE-M3 in-memory |
| Vector similarity search | < 200ms | 150ms avg | ChromaDB HTTP |
| KB semantic search (total) | < 300ms | 200ms avg | End-to-end |
| Case evidence search | < 300ms | 250ms avg | Includes metadata filtering |
| Global KB search | < 200ms | 150ms avg | No access control overhead |

**Optimization Techniques**:
- Embedding model cached in memory
- ChromaDB connection pooling
- Popular queries cached in Redis L2 (7-day TTL for Global KB)
- Pre-warmed cache for common queries

---

## 5. Collection Lifecycle Management

### 5.1 Unified KB Collection (`faultmaven_kb`)

**Lifecycle**: Permanent. Single collection created at deployment; never deleted as a whole. Individual documents are added, updated, or removed by users and admins. Global, personal, and team content coexist in this collection, distinguished by the `scope` metadata field.

**Creation**:

```bash
# Automatic at deployment. On first startup, FaultMaven auto-ingests the 59
# built-in runbooks from resources/knowledge/builtin/ with scope="global".
```

**Updates**:

- Global-scope content: admin-only, ingested via the `KnowledgeIngester` service
- Personal-scope content: added via `POST /api/v1/knowledge/documents` by the owner
- Team-scope content: added via the same endpoint with `scope=team` and a `team_id`
- Deletion of individual documents via `delete_documents_by_parent_id()`

**Backup**:

- Daily snapshot of the `data/chroma-kb/` directory (local) or of the external ChromaDB instance (cloud)
- Point-in-time recovery from snapshots

### 5.2 Case Collections (`case_{case_id}`)

**Lifecycle**: Case lifetime + 7 days grace period

**Creation**:
```python
# Automatic on first evidence upload
await case_vector_store.add_documents(case_id, documents)
# Creates case_{case_id} collection if not exists
```

**TTL Management**:
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

**Monitoring**:
- Daily cleanup job logs to CloudWatch
- Alert if cleanup fails 3 consecutive days
- Metrics: `chromadb.collection_count`, `chromadb.expired_collections`

---

## 6. API Endpoints

### 6.1 Case Evidence Vector Search

**Endpoint**: `POST /api/v1/cases/{case_id}/data/search`

```python
# Request
{
  "query": "error on page 5 of PDF",
  "k": 5,
  "filters": {
    "data_type": "TEXT"
  }
}

# Response
{
  "results": [
    {
      "data_id": "data_abc123",
      "content": "...",
      "score": 0.92,
      "metadata": {
        "filename": "system_logs.pdf",
        "page": 5
      }
    }
  ],
  "total": 12,
  "processing_time_ms": 250
}
```

### 6.2 User KB Semantic Search

**Endpoint**: `POST /api/v1/knowledge/search`

```python
# Request
{
  "query": "PostgreSQL connection pool tuning",
  "k": 5,
  "include_shared": true
}

# Response
{
  "results": [
    {
      "doc_id": "kbdoc_123",
      "title": "PostgreSQL Connection Pool Best Practices",
      "content": "...",
      "score": 0.89,
      "visibility": "team",
      "source": "user_kb"
    }
  ],
  "total": 8,
  "processing_time_ms": 200
}
```

### 6.3 KB Query (via Tool)

**Agent Tool**: `answer_from_kb` (unified — serves all scopes via metadata filter)

```python
# Tool invocation (internal)
result = await answer_from_kb.execute({
    "question": "How to analyze Java thread dumps?",
    "k": 5
})

# Returns:
{
    "answer": "To analyze Java thread dumps, follow these steps: ...",
    "sources": [
        {"article_id": "kb_042", "title": "Java Thread Dump Analysis"},
        {"article_id": "kb_089", "title": "Common Thread Deadlock Patterns"}
    ],
    "confidence": 0.92
}
```

---

## 7. Operational Procedures

### 7.1 Monitoring

**Key Metrics**:
```yaml
chromadb.query_latency_ms:
  - p50: 100ms
  - p95: 250ms
  - p99: 500ms

chromadb.embedding_generation_ms:
  - p50: 50ms
  - p95: 150ms

chromadb.collection_count:
  - faultmaven_kb: 1 (plus faultmaven_runbooks, knowledge_items)
  - case_*: ~500 (active cases)

chromadb.document_count:
  - faultmaven_kb (global): ~5000 articles
  - faultmaven_kb (personal): ~200 per user avg
  - avg_per_case: ~50 evidence items
```

**Alerting**:
- Query latency p95 > 500ms (5 min window)
- Collection count growth rate > 100/hour (case creation spike)
- Embedding generation failures > 5% (model loading issue)

### 7.2 Backup & Restore

**Backup Strategy**:

```bash
# Daily snapshot to S3
chromadb-backup.sh --collection faultmaven_kb --destination s3://faultmaven-backups/chromadb/

# Retention: 30 days
# Incremental: Yes (only changed documents)
```

**Restore Procedure**:

```bash
# Restore from snapshot
chromadb-restore.sh --collection faultmaven_kb --source s3://faultmaven-backups/chromadb/2025-01-22/

# Verify integrity
python scripts/verify_vector_storage.py --collection faultmaven_kb
```

### 7.3 Index Maintenance

**Reindexing**:

```python
# Rebuild index for performance
from faultmaven.infrastructure.knowledge.knowledge_vector_store import KnowledgeVectorStore

store: KnowledgeVectorStore = container.get_knowledge_vector_store()
await store.rebuild_index()
# Rebuilds HNSW index for faster similarity search
```

**Scheduled Maintenance**:
- Weekly index optimization (Sunday 2 AM UTC)
- Monthly full reindex (first Sunday)
- Vacuum expired collections (daily)

### 7.4 Disaster Recovery

**Recovery Time Objective (RTO)**: 4 hours
**Recovery Point Objective (RPO)**: 24 hours

**Failure Scenarios**:

1. **ChromaDB instance failure**:
   - Restore from S3 snapshot (30 min)
   - Replay Write-Ahead Log (1 hour)
   - Verify data integrity (30 min)

2. **Corrupted collection**:
   - Drop collection
   - Restore from backup
   - Reindex documents

3. **Embedding model unavailable**:
   - Fallback to cached embeddings (30 days TTL)
   - Queue ingestion jobs for retry
   - Alert on-call engineer

---

## Related Documentation

- **[schemas/knowledge-schema.md](./schemas/knowledge-schema.md)** - Schema for the unified KB and Case Working Memory
- **[../knowledge-and-ai/knowledge-base-architecture.md](../knowledge-and-ai/knowledge-base-architecture.md)** - KB system architecture, storage, and retrieval
- **[../knowledge-and-ai/runbook-content-architecture.md](../knowledge-and-ai/runbook-content-architecture.md)** - Runbook taxonomy, quality gates, lifecycle
- **[overview.md](./overview.md)** - Complete storage architecture overview

---

## Implementation Files

**Repository Implementations**:

- `faultmaven/infrastructure/knowledge/knowledge_vector_store.py` - Unified KB vector store (`faultmaven_kb`)
- `faultmaven/infrastructure/persistence/chromadb_store.py` - Generic ChromaDB adapter (`ChromaDBVectorStore`)
- `faultmaven/infrastructure/persistence/case_vector_store.py` - Case Working Memory (ephemeral `case_{id}` collections)
- `faultmaven/infrastructure/knowledge/runbook_kb.py` - Runbook similarity KB (`faultmaven_runbooks`)

**Ingestion & Query**:

- `faultmaven/modules/knowledge/domain/services/ingestion.py` - Batch document ingestion
- `faultmaven/modules/agent/tools/kb_qa.py` - Unified `answer_from_kb` tool (all scopes)
- `faultmaven/modules/agent/tools/case_evidence_qa.py` - `answer_from_case_evidence` tool
- `faultmaven/modules/preprocessing/` - Case evidence preprocessing

**API Endpoints**:

- `faultmaven/modules/case/api/` - Evidence upload and search
- `faultmaven/modules/knowledge/api/routes.py` - KB management
