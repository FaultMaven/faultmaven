# Knowledge Base and Vector Storage Architecture
## Three Distinct Vector Store Systems

**Document Type:** Component Specification
**Version:** 3.0
**Last Updated:** 2025-10-16
**Status:** 🟢 **PARTIALLY IMPLEMENTED**

---

## Critical Architectural Distinction

FaultMaven implements **three completely separate vector storage systems**, each with distinct purposes, lifecycles, ownership models, and use cases:

| System | Purpose | Lifecycle | Ownership | UI Context | Use Case |
|--------|---------|-----------|-----------|------------|----------|
| **User Knowledge Base** | Personal runbooks & docs | Permanent (per user) | User account | Knowledge Management UI | General guidance, best practices |
| **Global Knowledge Base** | System-wide documentation | Permanent (system) | System/Admin | N/A (admin only) | System-wide troubleshooting reference |
| **Case Working Memory** | Troubleshooting evidence | Tied to case (deleted on close) | Case (owned by user) | Active Case Chat UI | Case-specific document Q&A |

**Critical Design Principle**: These three systems must **never be confused** in implementation. They use different ChromaDB collections, different access patterns, different tools, and serve completely different purposes.

---

## System 1: User Knowledge Base (Per-User Permanent Storage)

**Purpose**: Long-term personal knowledge repository for runbooks, procedures, and reference documentation

**Ownership**: User account (independent of any case)

### Characteristics

- **Lifecycle**: Permanent - persists with user account
- **Scope**: User-specific - accessible across all user's cases
- **UI Context**: Dedicated Knowledge Management interface (separate from troubleshooting chat)
- **Access Pattern**: User can search their own KB from any case
- **Use Cases**:
  - Personal runbooks and procedures
  - Team documentation and playbooks
  - Reference architecture documents
  - Troubleshooting checklists
  - Best practices and lessons learned

### ChromaDB Collection Naming

```python
# Format: user_{user_id}_kb
# Example: user_alice123_kb, user_bob456_kb
collection_name = f"user_{user_id}_kb"
```

### Implementation Status

⚠️ **NOT YET IMPLEMENTED** - Planned for Phase 2

### Planned Components

```python
# To be created:
class UserKnowledgeBase:
    """
    Per-user permanent knowledge base for runbooks and documentation.

    Separate from:
    - Global KB (system-wide docs)
    - Case Working Memory (temporary case evidence)
    """

    def __init__(self, user_id: str):
        self.collection_name = f"user_{user_id}_kb"

    async def add_document(self, document: Document) -> None:
        """Add document to user's permanent KB (from Knowledge Management UI)"""

    async def search(self, query: str, k: int = 5) -> List[Document]:
        """Search user's KB (available from any case)"""

    async def delete_document(self, doc_id: str) -> None:
        """Delete document from user's KB"""
```

### Tool Integration

```python
# Tool available to main agent in any case
@tool
async def search_user_knowledge_base(
    user_id: str,
    query: str
) -> str:
    """
    Search user's personal knowledge base for runbooks and procedures.

    Use this when user references their own documentation or asks about
    procedures they've documented before.
    """
```

---

## System 2: Global Knowledge Base (System-Wide Permanent Storage)

**Purpose**: System-wide documentation and troubleshooting reference (admin-managed)

**Ownership**: System/Admin (shared across all users)

### Characteristics

- **Lifecycle**: Permanent - managed by system administrators
- **Scope**: Global - accessible to all users
- **UI Context**: Admin interface only (not directly visible to users)
- **Access Pattern**: Main agent automatically searches during troubleshooting
- **Use Cases**:
  - System architecture documentation
  - Error code reference
  - Common troubleshooting patterns
  - Technology stack documentation
  - Vendor documentation excerpts

### ChromaDB Collection Naming

```python
# Single global collection
collection_name = "faultmaven_kb"
```

### Implementation Status

✅ **IMPLEMENTED**

### Key Components

- **Vector Store**: `ChromaDBVectorStore` ([chromadb_store.py](../../faultmaven/infrastructure/persistence/chromadb_store.py))
- **Knowledge Ingester**: `KnowledgeIngester` ([ingestion.py](../../faultmaven/core/knowledge/ingestion.py))
- **Knowledge Tool**: `KnowledgeBaseTool` ([knowledge_base.py](../../faultmaven/tools/knowledge_base.py))

### Configuration

```bash
CHROMADB_URL=http://chromadb.faultmaven.local:30080
CHROMADB_COLLECTION=faultmaven_kb
EMBEDDING_MODEL=BAAI/bge-m3
```

### Tool Integration

```python
# Already implemented - used by main agent
@tool
async def search_knowledge_base(query: str) -> str:
    """
    Search global knowledge base for system documentation.

    Use this for general troubleshooting questions that don't require
    case-specific uploaded documents.
    """
```

---

## System 3: Case Working Memory (Ephemeral Case-Specific Storage)

**Purpose**: Temporary storage for evidence uploaded during active troubleshooting case

**Ownership**: Case (which is owned by a user)

### Characteristics

- **Lifecycle**: Tied to case lifecycle - deleted when case closes or is archived
- **Scope**: Case-specific - isolated per case_id
- **UI Context**: Active troubleshooting chat UI (document upload in chat)
- **Access Pattern**: QA sub-agent for detailed document questions
- **Use Cases**:
  - Uploaded log files for current incident
  - Configuration files from affected system
  - Stack traces and error dumps
  - Performance metrics and traces
  - Screenshots and diagnostic output

### ChromaDB Collection Naming

```python
# Format: case_{case_id}
# Example: case_abc123, case_xyz789
collection_name = f"case_{case_id}"
```

### Implementation Status

✅ **IMPLEMENTED** (2025-10-16)

### Key Components

- **CaseVectorStore**: Case-specific document management ([case_vector_store.py](../../faultmaven/infrastructure/persistence/case_vector_store.py))
- **AnswerFromDocumentTool**: QA sub-agent for document queries ([answer_from_document.py](../../faultmaven/tools/answer_from_document.py))
- **Background Cleanup**: TTL enforcement ([case_cleanup.py](../../faultmaven/infrastructure/tasks/case_cleanup.py))

### Configuration

```bash
SYNTHESIS_PROVIDER=openai  # Dedicated LLM for QA sub-agent
CHROMADB_URL=http://chromadb.faultmaven.local:30080
# Lifecycle: Tied to case status (deleted when case closes/archives)
# Cleanup: Triggered by case state transitions, not time-based
```

### Usage Pattern

```python
# 1. User uploads document IN troubleshooting chat
await case_vector_store.add_documents(
    case_id="abc123",
    documents=[{
        "id": "server_log_1",
        "content": "...",
        "metadata": {"filename": "server.log"}
    }]
)

# 2. User asks question about uploaded document
result = await answer_from_document_tool.answer_question(
    case_id="abc123",
    question="What error occurred at 10:45?",
    k=5
)

# 3. Case closes → collection deleted automatically via case lifecycle hook
```

### Tool Integration

```python
# QA sub-agent for case-specific documents
@tool
async def answer_from_document(
    case_id: str,
    question: str
) -> str:
    """
    Answer question using documents uploaded to THIS case.

    Use this when user asks detailed questions about files they just
    uploaded in the current troubleshooting session (logs, configs, etc).

    NOT for general knowledge or user's permanent runbooks.
    """
```

---

## Comparison Matrix

### Functional Differences

| Feature | User KB | Global KB | Case Working Memory |
|---------|---------|-----------|---------------------|
| **Data Source** | User uploads via KB UI | Admin ingests docs | User uploads in chat |
| **Query Interface** | Search from any case | Auto-searched by agent | QA sub-agent |
| **LLM Provider** | CHAT_PROVIDER | CHAT_PROVIDER | SYNTHESIS_PROVIDER |
| **Deletion** | User deletes manually | Admin manages | Auto-delete when case closes |
| **Persistence** | Forever (until deleted) | Forever (until deleted) | Case lifecycle (active→closed) |
| **Cross-Case Access** | ✅ Yes (all user's cases) | ✅ Yes (all cases) | ❌ No (case-isolated) |

### Use Case Examples

#### User Knowledge Base
```
User: "Check my runbook for database failover procedure"
→ Search user_{user_id}_kb collection
→ Return user's personal runbook
```

#### Global Knowledge Base
```
User: "What causes PostgreSQL connection pool exhaustion?"
→ Search faultmaven_kb collection
→ Return general PostgreSQL documentation
```

#### Case Working Memory
```
User: [uploads server.log]
User: "What error is on line 1045 of this log?"
→ Search case_{case_id} collection
→ QA sub-agent synthesizes answer from uploaded log
```

---

## ChromaDB Collection Architecture

```
ChromaDB Instance (chromadb.faultmaven.local:30080)
│
├── faultmaven_kb                    # Global Knowledge Base (System)
│   └── [system-wide documentation]
│
├── user_alice123_kb                 # User KB: Alice (Permanent)
│   └── [alice's runbooks]
│
├── user_bob456_kb                   # User KB: Bob (Permanent)
│   └── [bob's procedures]
│
├── case_abc123                      # Case Working Memory (active case)
│   └── [case abc123 uploaded logs]
│   └── [Lifecycle: deleted when case closes]
│
└── case_xyz789                      # Case Working Memory (active case)
    └── [case xyz789 uploaded configs]
    └── [Lifecycle: deleted when case closes]
```

---

## Access Control Matrix

| User Action | User KB | Global KB | Case Working Memory |
|-------------|---------|-----------|---------------------|
| **Upload document** | ✅ Via KB UI | ❌ Admin only | ✅ Via chat |
| **Search during case** | ✅ Own KB only | ✅ Auto-searched | ✅ Own cases only |
| **Delete document** | ✅ Own docs only | ❌ Admin only | ✅ Own cases only |
| **Access other user's data** | ❌ Forbidden | ✅ Shared | ❌ Forbidden |

---

## RAG Pipeline Comparison

### User Knowledge Base RAG (Planned)

```
User Query → Search user_{user_id}_kb → Retrieve Personal Docs
    → Augment Main Agent Context → CHAT_PROVIDER → Response
```

**Example**: "According to my runbook, what's the first step for database recovery?"

---

### Global Knowledge Base RAG (Implemented)

```
User Query → Search faultmaven_kb → Retrieve System Docs
    → Augment Main Agent Context → CHAT_PROVIDER → Response
```

**Example**: "What are common causes of Redis connection timeouts?"

---

### Case Working Memory RAG (Implemented)

```
Document Question → Search case_{case_id} → Retrieve Case Evidence
    → Build Synthesis Prompt → SYNTHESIS_PROVIDER → Direct Answer
```

**Example**: "Show me all ERROR lines from the server.log I just uploaded"

---

## Implementation Roadmap

### Phase 1: ✅ COMPLETE (Current State)
- [x] Global Knowledge Base (system-wide docs)
- [x] Case Working Memory (ephemeral case evidence)
- [x] QA Sub-Agent pattern
- [x] TTL-based cleanup

### Phase 2: 📋 PLANNED (Next 3 Months)
- [ ] User Knowledge Base (per-user permanent KB)
- [ ] Knowledge Management UI
- [ ] User KB search tool for main agent
- [ ] User KB access control

### Phase 3: 🔮 FUTURE (6+ Months)
- [ ] Multi-document synthesis across KBs
- [ ] Smart chunking strategies
- [ ] Advanced caching layer
- [ ] Knowledge graph integration

---

## Implementation Files

### System 1: User Knowledge Base (NOT YET IMPLEMENTED)
- ⏳ `faultmaven/infrastructure/persistence/user_knowledge_base.py` - To be created
- ⏳ `faultmaven/tools/user_kb_search.py` - To be created
- ⏳ `faultmaven/api/v1/routes/knowledge.py` - To be enhanced

### System 2: Global Knowledge Base (IMPLEMENTED)
- ✅ `faultmaven/infrastructure/persistence/chromadb_store.py`
- ✅ `faultmaven/core/knowledge/ingestion.py`
- ✅ `faultmaven/tools/knowledge_base.py`

### System 3: Case Working Memory (IMPLEMENTED)
- ✅ `faultmaven/infrastructure/persistence/case_vector_store.py`
- ✅ `faultmaven/tools/answer_from_document.py`
- ✅ `faultmaven/infrastructure/tasks/case_cleanup.py`

---

## Related Documents

- [Working Memory Feature Documentation](../features/working-memory-session-rag.md) - Case Working Memory details
- [Working Memory Implementation Summary](../implementation/working-memory-implementation-summary.md)
- [Investigation Phases Framework](./investigation-phases-and-ooda-integration.md)
- [Data Preprocessing Design](./data-preprocessing-design.md)

---

**Document Version**: 3.0
**Last Updated**: 2025-10-16
**Status**:
- Global KB: ✅ Implemented
- Case Working Memory: ✅ Implemented
- User KB: ⏳ Planned (Phase 2)
