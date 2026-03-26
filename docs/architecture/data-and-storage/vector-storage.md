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
ChromaDB (single PersistentClient at data/chroma/ for local, HttpClient for cloud)
├── Collections:
│   ├── faultmaven_kb         (all KB docs: global/personal/team, metadata-filtered)
│   ├── faultmaven_runbooks   (runbook similarity recommendations)
│   ├── knowledge_items       (knowledge module search service)
│   ├── case_{case_id}        (per-case evidence, dynamic, ephemeral)
│   └── ...
```

**Architecture**: One shared ChromaDB client created in the DI container, injected into all vector stores. Local deployment uses `PersistentClient` (file-based at `data/chroma/chroma.sqlite3`), cloud uses `HttpClient` to external server. Same pattern as Redis/FakeRedis.

**Scope Isolation**: The `faultmaven_kb` collection uses metadata filtering — NOT separate collections per user/team. Scope fields (`scope`, `owner_id`, `team_id`) are stored at ingestion time. The unified `kb_qa` tool builds a combined filter:

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
# DI container creates one client (infrastructure.py:create_chromadb_client)
chromadb_client = create_chromadb_client(settings)  # PersistentClient or HttpClient

# Injected into all stores
ChromaDBVectorStore(client=chromadb_client, collection_name="faultmaven_kb")
CaseVectorStore(client=chromadb_client)   # dynamic case_{id} collections
VectorStoreService(client=chromadb_client) # knowledge_items collection
client = chromadb.HttpClient(host="chromadb.faultmaven.local", port=30080)
private_collection = client.get_or_create_collection(f"kb_private_{user_id}")
---

## 2. Vector Storage Systems

### 2.1 Knowledge Base (Unified)

**Purpose**: All runbooks and documentation — global, personal, and team-scoped
**Collection**: `faultmaven_kb` (single collection, metadata-filtered by scope)
**Lifecycle**: Permanent (user/admin-controlled deletion)
**Implementation**: `faultmaven/infrastructure/persistence/chromadb_store.py` (ChromaDBVectorStore)

**Scope Isolation**: Metadata fields `scope`, `owner_id`, `team_id` stored at ingestion. The unified `kb_qa` tool automatically filters by the user's accessible scopes.

**Characteristics**:

- Documents persist indefinitely (no TTL)
- BGE-M3 embeddings for semantic search
- Cross-scope relevance ranking (global and personal results compete on relevance)
- Single tool (`kb_qa`) returns best results regardless of scope

### 2.2 Case Working Memory

**Purpose**: Ephemeral per-case document storage during active troubleshooting
**Collections**: `case_{case_id}` (dynamic, one per case)
**Lifecycle**: Case lifetime — deleted when case closes/archives
**Implementation**: `faultmaven/infrastructure/persistence/case_vector_store.py` (CaseVectorStore)

**Characteristics**:

- Collections created on-demand when first document added
- Deleted when case closes via `delete_case_collection()`
- Case-scoped search (only within current case)
- Used by `case_evidence_search` tool

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

**Source of truth**: ChromaDB is the persistent store for KB documents. `KnowledgeService.list_documents()` queries ChromaDB via `ChromaDBVectorStore.list_documents()` — NOT Redis.

```python
# ChromaDBVectorStore methods for document management
await vector_store.list_documents(limit=100, offset=0, where={"scope": "global"})
await vector_store.get_document(document_id)
await vector_store.count()
await vector_store.delete_documents([document_id])
```

Redis is used only as a write-through cache for upload metadata. If Redis is unavailable (FakeRedis restart), documents persist in ChromaDB and remain listable.

### 3.3 Global KB Administration

**Batch Ingestion Flow**:

```python
# Admin uploads curated content
from faultmaven.tools.knowledge_ingester import KnowledgeIngester

ingester = KnowledgeIngester()
await ingester.ingest_directory("./knowledge_base_articles/")

# Steps:
1. Parse markdown files
2. Extract frontmatter metadata
3. Generate embeddings (BGE-M3)
4. Batch insert to global_kb collection
5. Rebuild search index
```

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

### 4.2 Multi-Collection Search (User KB with Sharing)

**Hybrid Search Pattern**:

```python
async def search_kb(user_id: str, query: str, k: int = 5) -> List[Document]:
    results = []

    # 1. Search user's private collection
    private_results = await chromadb.query(
        collection=f"kb_private_{user_id}",
        query_texts=[query],
        n_results=k
    )
    results.extend(private_results)

    # 2. Search shared collection with metadata filter
    user_teams = await get_user_teams(user_id)
    user_orgs = await get_user_orgs(user_id)

    shared_results = await chromadb.query(
        collection="kb_shared",
        query_texts=[query],
        where={
            "$or": [
                {"allowed_users": {"$contains": user_id}},
                {"allowed_teams": {"$in": user_teams}},
                {"organization_id": {"$in": user_orgs}}
            ]
        },
        n_results=k
    )
    results.extend(shared_results)

    # 3. Merge and re-rank by score
    return sorted(results, key=lambda x: x.score, reverse=True)[:k]
```

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

### 5.1 Global KB (global_kb)

**Lifecycle**: Permanent (never deleted)

**Creation**:
```bash
# Automatic on first document insert
python -m faultmaven.tools.knowledge_ingester --init
```

**Updates**:
- Admin-only via `KnowledgeIngester`
- Versioned updates (old docs archived)
- Index rebuild after batch updates

**Backup**:
- Daily snapshot to S3
- Point-in-time recovery available

### 5.2 User KB Collections

**Private Collections** (`kb_private_{user_id}`):
- **Creation**: On-demand when user uploads first document
- **Lifecycle**: Permanent (user-controlled deletion)
- **Deletion**: When user deletes account (cascade delete)

**Shared Collection** (`kb_shared`):
- **Creation**: System-initialized on deployment
- **Lifecycle**: Permanent
- **Access Control**: Metadata-based filtering during queries

**Management Operations**:
```python
# Create user KB collection
await user_kb_store.create_collection(user_id)

# Delete user KB (account deletion)
await user_kb_store.delete_collection(f"kb_private_{user_id}")
```

### 5.3 Case Collections (case_{case_id})

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

### 6.3 Global KB Query (via Tool)

**Agent Tool**: `answer_from_global_kb`

```python
# Tool invocation (internal)
result = await answer_from_global_kb.execute({
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
  - global_kb: 1
  - user_kb_*: ~1000 (per user)
  - case_*: ~500 (active cases)

chromadb.document_count:
  - global_kb: ~5000 articles
  - avg_per_user_kb: ~200 documents
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
chromadb-backup.sh --collection global_kb --destination s3://faultmaven-backups/chromadb/

# Retention: 30 days
# Incremental: Yes (only changed documents)
```

**Restore Procedure**:
```bash
# Restore from snapshot
chromadb-restore.sh --collection global_kb --source s3://faultmaven-backups/chromadb/2025-01-22/

# Verify integrity
python -m faultmaven.tools.verify_collection --collection global_kb
```

### 7.3 Index Maintenance

**Reindexing**:
```python
# Rebuild index for performance
from faultmaven.infrastructure.persistence.global_kb_vector_store import GlobalKBVectorStore

store = GlobalKBVectorStore()
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

- **[schemas/knowledge-schema.md](./schemas/knowledge-schema.md)** - Complete schema for User KB, Case Working Memory, Global KB
- **[../knowledge-and-ai/knowledge-base-architecture.md](../knowledge-and-ai/knowledge-base-architecture.md)** - 3-tier KB system, storage and retrieval architecture
- **[../knowledge-and-ai/runbook-content-architecture.md](../knowledge-and-ai/runbook-content-architecture.md)** - Runbook taxonomy, quality gates, lifecycle
- **[overview.md](./overview.md)** - Complete storage architecture overview

---

## Implementation Files

**Repository Implementations**:
- `faultmaven/infrastructure/persistence/user_kb_vector_store.py` - User KB storage
- `faultmaven/infrastructure/persistence/case_vector_store.py` - Case Working Memory
- `faultmaven/infrastructure/persistence/global_kb_vector_store.py` - Global KB storage

**Ingestion & Query**:
- `faultmaven/tools/knowledge_ingester.py` - Batch document ingestion (admin)
- `faultmaven/tools/global_kb_qa.py` - Global KB Q&A tool
- `faultmaven/services/preprocessing_service.py` - Case evidence preprocessing

**API Endpoints**:
- `faultmaven/api/v1/routes/case.py` - Evidence upload and search
- `faultmaven/api/v1/routes/knowledge.py` - User KB management
