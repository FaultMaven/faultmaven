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
ChromaDB Instance
├── Host: chromadb.faultmaven.local
├── Port: 30080 (NodePort in K8s)
├── Auth: Token-based (optional)
└── Collections:
    ├── global_kb (Global KB - system-wide)
    ├── user_kb_{user_id} (User KB - per user)
    ├── kb_private_{user_id} (User KB - private documents)
    ├── kb_shared (Shared KB documents with metadata filtering)
    └── case_{case_id} (Case Working Memory - ephemeral)
```

**Design Decision**: Single instance with multiple collections (vs separate instances)
- **Pros**: Simpler deployment, resource efficiency, easier backup
- **Cons**: Shared resource pool (mitigated by collection-level isolation)
- **Scaling**: Collections are independently queryable; ChromaDB handles isolation

### 1.2 Embedding Model

**Current**: BGE-M3 (BAAI/bge-m3)
- **Dimensions**: 1024
- **Max Sequence Length**: 8192 tokens
- **Language Support**: Multilingual (100+ languages)
- **Model Size**: ~2.3GB
- **Loading**: Cached in memory via `model_cache.get_bge_m3_model()`

**Location**: Loaded in-process (not external service)
- `KnowledgeIngester`: For global KB document ingestion
- `PreprocessingService`: For case evidence chunking
- Q&A Tools: Generate query embeddings on the fly

### 1.3 Connection Management

**Three Client Patterns**:

1. **GlobalKBVectorStore** (Global KB):
```python
# Singleton pattern, connects to single collection
client = chromadb.HttpClient(host="chromadb.faultmaven.local", port=30080)
collection = client.get_or_create_collection("global_kb")
```

2. **CaseVectorStore** (Case Evidence):
```python
# Multi-collection pattern, dynamic collection per case
client = chromadb.HttpClient(host="chromadb.faultmaven.local", port=30080)
collection = client.get_or_create_collection(f"case_{case_id}")
```

3. **UserKBVectorStore** (User KB):
```python
# Per-user collections for private documents
client = chromadb.HttpClient(host="chromadb.faultmaven.local", port=30080)
private_collection = client.get_or_create_collection(f"kb_private_{user_id}")
shared_collection = client.get_or_create_collection("kb_shared")
```

---

## 2. Three Vector Storage Systems

### 2.1 User Knowledge Base

**Purpose**: User-scoped persistent storage for runbooks and procedures
**Collections**: `kb_private_{user_id}` (private) + `kb_shared` (shared)
**Lifecycle**: Permanent (user-controlled deletion)
**Implementation**: `faultmaven/infrastructure/persistence/user_kb_vector_store.py`

**Characteristics**:
- Documents persist indefinitely (no TTL)
- BGE-M3 embeddings for semantic search
- Sub-second search for typical queries
- Supports sharing at user, team, and organization levels

**Use Cases**:
- Store troubleshooting runbooks
- Share procedures across teams
- Build organizational knowledge base

See [schemas/knowledge-schema.md](./schemas/knowledge-schema.md) for complete schema and sharing architecture.

### 2.2 Case Working Memory

**Purpose**: Ephemeral per-case document storage during active troubleshooting
**Collections**: `case_{case_id}`
**Lifecycle**: Case lifetime + 7 days grace period
**Implementation**: `faultmaven/infrastructure/persistence/case_vector_store.py`

**Characteristics**:
- Collections created on-demand when first document added
- Automatically deleted when case closes + 7 days
- Case-scoped search (only within current case)
- Used by `answer_from_case_evidence` tool

**Use Cases**:
- QA sub-agent: "What does this uploaded PDF say?"
- Semantic search within case evidence
- Temporary document reference during investigation

### 2.3 Global Knowledge Base

**Purpose**: System-wide troubleshooting documentation shared across ALL users
**Collections**: `global_kb` (single shared collection)
**Lifecycle**: Permanent (admin-controlled)
**Implementation**: `faultmaven/tools/global_kb_qa.py`

**Characteristics**:
- Read-only for all authenticated users
- Pre-populated by FaultMaven team
- Curated best practices and methodologies
- Updated periodically by administrators

**Use Cases**:
- Industry-standard troubleshooting approaches
- Common error patterns and solutions
- Best practices and methodology guides

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
2. Generate BGE-M3 embeddings
3. Store in kb_private_{user_id} collection
4. Save metadata to PostgreSQL (kb_documents table)
5. Return document_id to client

# Python API
await user_kb_store.add_documents(user_id, documents)
```

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
- **[../knowledge-and-ai/knowledge-base-architecture.md](../knowledge-and-ai/knowledge-base-architecture.md)** - RAG pipeline and conceptual architecture
- **[../knowledge-and-ai/qa-tools-design.md](../knowledge-and-ai/qa-tools-design.md)** - Q&A tool design and prompt engineering
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
