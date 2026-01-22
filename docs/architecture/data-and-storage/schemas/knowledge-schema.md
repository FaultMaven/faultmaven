# Knowledge Base Storage Schema

This document covers FaultMaven's three knowledge storage systems: User Knowledge Base, Case Working Memory, and Global Knowledge Base.

## Table of Contents

1. [User Knowledge Base Storage](#1-user-knowledge-base-storage) - User-scoped persistent runbooks and procedures
2. [Case Working Memory Storage](#2-case-working-memory-storage) - Ephemeral per-case document storage
3. [Global Knowledge Base Storage](#3-global-knowledge-base-storage) - System-wide troubleshooting documentation

---

## 1. User Knowledge Base Storage

### 1.1 Architecture Overview

**Purpose**: User-scoped persistent storage for runbooks, procedures, documentation

**Storage**: ChromaDB with per-user collections
**Collection Naming**: `user_kb_{user_id}`
**Implementation**: `faultmaven/infrastructure/persistence/user_kb_vector_store.py`

### 1.2 Storage Characteristics

**Permanent Storage**:
- Documents persist indefinitely (no TTL)
- User controls lifecycle through explicit deletion
- Grows with user's documented knowledge

**Semantic Search**:
- BGE-M3 embeddings for vector similarity
- Sub-second search for typical queries
- Relevance ranking by cosine similarity

### 1.3 Document Structure

```python
class KnowledgeDocument(BaseModel):
    document_id: str
    user_id: str
    title: str
    content: str
    document_type: str  # troubleshooting, configuration, runbook

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
# Add documents
await user_kb_store.add_documents(user_id, documents)

# Semantic search
results = await user_kb_store.search(user_id, query="DB timeouts", k=5)

# List all documents
documents = await user_kb_store.list_documents(user_id)

# Delete document
await user_kb_store.delete_document(user_id, document_id)
```

### 1.5 Knowledge Base Sharing

**Purpose**: Enable collaboration by sharing runbooks and documentation with users, teams, and organizations

**Implementation**: See `docs/schema/004_kb_sharing_infrastructure.sql`

#### 1.5.1 Architecture Change

**From**: Per-user collections (`user_kb_{user_id}`)
```
user_kb_alice  → Alice's private documents only
user_kb_bob    → Bob's private documents only
```

**To**: Hybrid model with visibility control
```
kb_private_alice  → Alice's private documents (backward compatible)
kb_private_bob    → Bob's private documents
kb_shared         → All shared documents with metadata filtering
```

**Metadata Filtering**: Each document in `kb_shared` includes:
- `owner_user_id`: Document owner
- `visibility`: private, shared, team, organization
- `allowed_users`: Array of user IDs with access
- `allowed_teams`: Array of team IDs with access
- `org_id`: Organization ID (for org-wide documents)

#### 1.5.2 Document Metadata Table

**Storage**: PostgreSQL (`kb_documents` table) + ChromaDB (document chunks)

```sql
CREATE TABLE kb_documents (
    doc_id VARCHAR(20) PRIMARY KEY,
    owner_user_id VARCHAR(20) NOT NULL,
    org_id VARCHAR(20) REFERENCES organizations(org_id),

    title VARCHAR(500) NOT NULL,
    description TEXT,
    document_type kb_document_type NOT NULL,  -- runbook, procedure, etc.

    chromadb_collection VARCHAR(100) NOT NULL,  -- Which collection stores this
    chromadb_doc_count INTEGER DEFAULT 0,       -- Number of chunks

    visibility kb_visibility NOT NULL DEFAULT 'private',  -- private, shared, team, organization
    tags TEXT[],

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
```

#### 1.5.3 Sharing Mechanisms

**Individual User Sharing**:
```sql
-- kb_document_shares table
CREATE TABLE kb_document_shares (
    doc_id VARCHAR(20) REFERENCES kb_documents(doc_id),
    shared_with_user_id VARCHAR(20) NOT NULL,
    permission kb_share_permission NOT NULL DEFAULT 'read',  -- read, write
    shared_by VARCHAR(20) NOT NULL,
    shared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (doc_id, shared_with_user_id)
);
```

**Python API**:
```python
# Share runbook with specific user
await kb_service.share_document(
    doc_id="kbdoc_123",
    shared_with_user_id="user_bob",
    permission="read",
    shared_by="user_alice"
)
```

**Team-Based Sharing**:
```sql
-- kb_document_team_shares table
CREATE TABLE kb_document_team_shares (
    doc_id VARCHAR(20) REFERENCES kb_documents(doc_id),
    team_id VARCHAR(20) REFERENCES teams(team_id),
    permission kb_share_permission NOT NULL DEFAULT 'read',
    shared_by VARCHAR(20) NOT NULL,
    shared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (doc_id, team_id)
);
```

**Python API**:
```python
# Share runbook with entire SRE team
await kb_service.share_document_with_team(
    doc_id="kbdoc_123",
    team_id="team_sre_oncall",
    permission="read",
    shared_by="user_alice"
)
```

**Organization-Wide Sharing**:
```sql
-- kb_document_org_shares table
CREATE TABLE kb_document_org_shares (
    doc_id VARCHAR(20) REFERENCES kb_documents(doc_id),
    org_id VARCHAR(20) REFERENCES organizations(org_id),
    permission kb_share_permission NOT NULL DEFAULT 'read',
    shared_by VARCHAR(20) NOT NULL,
    shared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (doc_id, org_id)
);
```

**Python API**:
```python
# Share runbook with entire organization
await kb_service.share_document_with_org(
    doc_id="kbdoc_123",
    org_id="org_acme_corp",
    permission="read",
    shared_by="user_alice"
)
```

#### 1.5.4 Access Control Model

| Permission | Capabilities |
|-----------|--------------|
| **read** | View document content, search, download |
| **write** | Read + edit content, update metadata, delete (if owner) |

**Owner Always Has Write**: Document owner always has write permission regardless of sharing settings.

#### 1.5.5 Access Resolution for Search

```python
# When user searches KB, return documents where user has access
def get_accessible_documents(user_id: str) -> List[str]:
    """Return doc_ids user can access"""
    return documents where:
        1. owner_user_id = user_id  (user's own documents)
        OR
        2. doc_id IN kb_document_shares WHERE shared_with_user_id = user_id
        OR
        3. doc_id IN kb_document_team_shares
           WHERE team_id IN (user's teams)
        OR
        4. doc_id IN kb_document_org_shares
           WHERE org_id IN (user's organizations)
```

**SQL Function**:
```sql
-- Check if user can access KB document
SELECT user_can_access_kb_document('user_alice', 'kbdoc_123');
-- Returns: true/false

-- Get user's permission level for document
SELECT get_user_kb_document_permission('user_alice', 'kbdoc_123');
-- Returns: 'read' | 'write' | NULL
```

#### 1.5.6 Audit Trail

**Table**: `kb_sharing_audit`

Tracks all KB sharing actions:
- Document shared/unshared
- Permission changes
- Visibility changes
- Who performed action
- When action occurred

```sql
SELECT * FROM kb_sharing_audit
WHERE doc_id = 'kbdoc_123'
ORDER BY action_at DESC;
```

#### 1.5.7 Views

**user_accessible_kb_documents**: All KB documents user can access
```sql
SELECT
    doc_id,
    title,
    document_type,
    visibility,
    user_permission  -- 'owner', 'read', 'write'
FROM user_accessible_kb_documents
WHERE 'user_alice' IN (owner_user_id, allowed_users);
```

**kb_document_sharing_summary**: Sharing statistics per document
```sql
SELECT
    doc_id,
    title,
    visibility,
    user_share_count,    -- How many users it's shared with
    team_share_count,    -- How many teams
    org_share_count      -- How many organizations
FROM kb_document_sharing_summary;
```

#### 1.5.8 ChromaDB Collection Strategy

**Private Documents**:
- Collection: `kb_private_{user_id}`
- Metadata: `{"visibility": "private", "owner_user_id": "user_alice"}`
- Access: Owner only

**Shared Documents**:
- Collection: `kb_shared`
- Metadata includes access control:
  ```json
  {
    "doc_id": "kbdoc_123",
    "owner_user_id": "user_alice",
    "visibility": "shared",  // or "team" or "organization"
    "allowed_users": ["user_bob", "user_charlie"],
    "allowed_teams": ["team_sre"],
    "org_id": "org_acme_corp"
  }
  ```
- Access: Filtered by metadata during search

**Search Implementation**:
```python
# Search both private and shared collections
async def search_kb(user_id: str, query: str) -> List[Document]:
    results = []

    # Search user's private collection
    private_results = await chromadb.query(
        collection=f"kb_private_{user_id}",
        query_texts=[query]
    )
    results.extend(private_results)

    # Search shared collection with metadata filter
    shared_results = await chromadb.query(
        collection="kb_shared",
        query_texts=[query],
        where={
            "$or": [
                {"allowed_users": {"$contains": user_id}},
                {"allowed_teams": {"$in": get_user_teams(user_id)}},
                {"org_id": {"$in": get_user_orgs(user_id)}}
            ]
        }
    )
    results.extend(shared_results)

    return sorted(results, key=lambda x: x.score, reverse=True)
```

---

## 2. Case Working Memory Storage

### 2.1 Architecture Overview

**Purpose**: Ephemeral session-specific RAG for temporary document storage during active troubleshooting

**Key Differences from User KB**:
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
- Same BGE-M3 embeddings as User KB
- Case-scoped search (only within current case)
- Used by `answer_from_case_evidence` tool

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

## 3. Global Knowledge Base Storage

### 3.1 Architecture Overview

**Purpose**: System-wide troubleshooting documentation shared across ALL users

**Three Knowledge Systems**:
1. **Global KB** - System-wide best practices (THIS SECTION)
2. **User KB** - User's personal runbooks (Section 1)
3. **Case Working Memory** - Temporary case uploads (Section 2)

**Storage**: ChromaDB (shared collection)
**Collection Naming**: `global_kb` (single shared collection)
**Implementation**: `faultmaven/tools/global_kb_qa.py`

### 3.2 Storage Characteristics

**Shared Storage**:
- Single collection accessible to all users (read-only)
- Pre-populated by FaultMaven team
- Curated best practices and methodologies
- Updated periodically by system administrators

**Content Types**:
- Industry-standard troubleshooting approaches
- Common error patterns and solutions
- Best practices and anti-patterns
- Methodology guides (SRE, DevOps)
- Tool usage examples

### 3.3 Document Structure

```python
class GlobalKBDocument(BaseModel):
    document_id: str              # e.g., "kb_001"
    title: str
    content: str
    category: str                 # "methodology", "pattern", "tool", "best_practice"

    metadata: Dict[str, Any] = {
        "author": "FaultMaven Team",
        "version": str,
        "tags": List[str],
        "difficulty": str,
        "last_updated": str,
        "popularity_score": float,  # Based on usage
        "effectiveness_score": float,  # Based on user feedback
    }

    created_at: datetime
    updated_at: datetime
```

### 3.4 Tool Integration

**Agent Tool**: `answer_from_global_kb`

```python
# Agent flow
1. User asks: "Standard approach for diagnosing memory leaks?"
2. Agent calls answer_from_global_kb tool
3. Tool performs semantic search on global_kb collection
4. Retrieves top 5 relevant articles
5. Synthesis LLM generates answer with KB article citations
6. Agent provides general best practices response

# Example tool invocation
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

### 3.5 Access Control

**Read Access**: All authenticated users
**Write Access**: System administrators only

**Update Process**:
```python
# Admin tool for updating global KB
async def update_global_kb(
    admin_user: User,
    documents: List[GlobalKBDocument]
):
    if "admin" not in admin_user.roles:
        raise PermissionDeniedError()

    await global_kb_store.add_documents("global_kb", documents)
    await global_kb_store.rebuild_index()  # Optimize search index
    logger.info(f"Global KB updated by {admin_user.username}")
```

### 3.6 Performance Optimization

**Caching Strategy**:
- 7-day cache TTL (global KB changes rarely)
- Pre-computed embeddings for fast search
- Popular articles cached in Redis L2

**Search Performance**:
- Sub-200ms typical query time
- Pre-warmed cache for common queries
- Batch embedding generation for updates

---

## Related Documentation

- **[vector-storage.md](../vector-storage.md)** - ChromaDB implementation details and operations
- **[case-schema.md](./case-schema.md)** - Case data model and investigation storage
- **[../knowledge-and-ai/knowledge-base-architecture.md](../../knowledge-and-ai/knowledge-base-architecture.md)** - RAG pipeline and embeddings
- **[overview.md](../overview.md)** - Complete storage architecture overview
