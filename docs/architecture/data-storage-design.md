# FaultMaven Data Storage Architecture v1.0

## Executive Summary

This document defines the comprehensive data storage architecture for FaultMaven's four primary data categories:
1. **User Information** - Account, authentication, profile
2. **Case-Centric Data** - Investigation lifecycle, conversation, context
3. **Observability Data** - 7 types of uploaded machine data for analysis
4. **Knowledge Base Data** - User-uploaded runbooks and procedures

**Key Design Principles**:
- **Storage Polyglot**: PostgreSQL for transactional, Redis for sessions, ChromaDB for semantic search
- **Interface-Based**: All storage accessed through repository abstractions
- **Privacy-First**: PII redaction before persistence, encryption at rest
- **Performance-Optimized**: Hybrid schemas balance query performance with flexibility
- **Cloud-Native**: Designed for Kubernetes deployment with horizontal scaling

---

## Table of Contents

1. [Storage Technology Matrix](#1-storage-technology-matrix)
2. [User Information Storage](#2-user-information-storage)
3. [Case-Centric Data Storage](#3-case-centric-data-storage)
4. [Observability Data Storage](#4-observability-data-storage)
5. [Knowledge Base Data Storage](#5-knowledge-base-data-storage)
6. [Access Patterns & Interfaces](#6-access-patterns--interfaces)
7. [Data Retention & Lifecycle](#7-data-retention--lifecycle)
8. [Security & Compliance](#8-security--compliance)
9. [Scalability & Performance](#9-scalability--performance)
10. [Migration & Backup](#10-migration--backup)

---

## 1. Storage Technology Matrix

### 1.1 Technology Selection

| Data Category | Primary Storage | Secondary/Cache | Reasoning |
|--------------|----------------|-----------------|-----------|
| **User Information** | PostgreSQL | Redis (sessions) | ACID guarantees for auth, relational integrity |
| **Case Data** | PostgreSQL | Redis (state) | Transactional integrity for investigations, complex queries |
| **Observability Data** | PostgreSQL + S3 | - | Structured metadata in PG, raw artifacts in S3 |
| **Knowledge Base** | ChromaDB | PostgreSQL (metadata) | Semantic search for runbooks, vector embeddings |
| **Session State** | Redis | PostgreSQL (archive) | Sub-10ms access, TTL expiration, session resumption |

### 1.2 Storage Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                     APPLICATION LAYER                        │
│              (FastAPI + Service Layer)                       │
└────────────┬────────────────────────────────────────────────┘
             │
             ├──> UserRepository Interface
             │    └──> PostgreSQL (auth_db.users)
             │
             ├──> CaseRepository Interface
             │    └──> PostgreSQL (cases_db.* hybrid schema)
             │
             ├──> ISessionStore Interface
             │    ├──> Redis (primary, TTL-based)
             │    └──> PostgreSQL (archive, optional)
             │
             ├──> IVectorStore Interface (Knowledge)
             │    ├──> ChromaDB (user_kb_{user_id} collections)
             │    └──> PostgreSQL (document metadata)
             │
             └──> IStorageBackend Interface (Artifacts)
                  └──> S3 (raw uploaded files)

┌─────────────────────────────────────────────────────────────┐
│                  INFRASTRUCTURE LAYER                        │
├─────────────────────────────────────────────────────────────┤
│ PostgreSQL Clusters:                                        │
│   - auth_db: User accounts, roles, SSO                      │
│   - cases_db: Investigation data, evidence, hypotheses      │
│                                                              │
│ Redis Cluster:                                              │
│   - Session state (TTL: 30 min default)                     │
│   - Client indexes for session resumption                   │
│                                                              │
│ ChromaDB:                                                   │
│   - Per-user collections: user_kb_{user_id}                │
│   - BGE-M3 embeddings for semantic search                   │
│                                                              │
│ S3-Compatible Storage:                                      │
│   - Raw uploaded files: artifacts/{case_id}/{file_id}       │
│   - Lifecycle policy: 90 days default                       │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. User Information Storage

### 2.1 Schema Overview

**Database**: `auth_db` (PostgreSQL)
**Table**: `users`
**Repository**: `faultmaven/infrastructure/persistence/user_repository.py`

### 2.2 User Model

```python
class User(BaseModel):
    # Identity
    user_id: str              # Primary key, UUID
    username: str             # Unique, indexed
    email: EmailStr           # Unique, indexed

    # Authentication
    hashed_password: Optional[str]  # NULL for SSO-only users
    is_active: bool           # Account status

    # Profile
    display_name: str         # User-friendly name
    avatar_url: Optional[str] # Profile picture
    timezone: str             # Default: "UTC"
    locale: str               # Default: "en-US"

    # Email Verification
    is_email_verified: bool
    email_verified_at: Optional[datetime]

    # SSO Integration
    sso_provider: Optional[str]      # "google", "okta", "azure"
    sso_provider_id: Optional[str]   # External user ID

    # Timestamps
    created_at: datetime
    updated_at: datetime
    last_login_at: Optional[datetime]
    last_password_change_at: Optional[datetime]

    # Soft Delete
    deleted_at: Optional[datetime]   # NULL = active

    # Authorization (Development - in production use separate roles table)
    roles: List[str]          # ["user", "admin", "analyst"]
```

### 2.3 Storage Implementation

**Production**: `PostgreSQLUserRepository`
- ACID guarantees for authentication operations
- Indexed lookups on username, email (case-insensitive)
- Upsert pattern for atomic save operations
- Soft delete support (deleted_at timestamp)

**Development/Testing**: `InMemoryUserRepository`
- Dictionary-based storage with secondary indexes
- Fast iteration without database overhead
- Full interface compliance for testing

### 2.4 Access Patterns

```python
# Common operations (all async)
user = await user_repository.get(user_id)
user = await user_repository.get_by_username(username)
user = await user_repository.get_by_email(email)
users, total = await user_repository.list(limit=50, offset=0)
await user_repository.save(user)
deleted = await user_repository.delete(user_id)
```

### 2.5 Indexes

```sql
-- Primary Key
CREATE UNIQUE INDEX idx_users_pk ON users (user_id);

-- Authentication lookups
CREATE UNIQUE INDEX idx_users_username ON users (LOWER(username));
CREATE UNIQUE INDEX idx_users_email ON users (LOWER(email));

-- SSO lookups
CREATE INDEX idx_users_sso ON users (sso_provider, sso_provider_id)
  WHERE sso_provider IS NOT NULL;

-- Active users filter
CREATE INDEX idx_users_active ON users (is_active, deleted_at)
  WHERE deleted_at IS NULL;
```

---

## 3. Case-Centric Data Storage

### 3.1 Schema Overview

**Database**: `cases_db` (PostgreSQL)
**Architecture**: Hybrid Normalized Schema
**Repository**: `faultmaven/infrastructure/persistence/postgresql_hybrid_case_repository.py`

### 3.2 Hybrid Schema Design

**Philosophy**: "Normalize what you query, embed what you don't"

#### Main Table: `cases`

```sql
CREATE TABLE cases (
    -- Identity
    case_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,

    -- Lifecycle
    status TEXT NOT NULL,  -- consulting, investigating, resolved, closed
    closure_reason TEXT,

    -- Problem Definition
    title TEXT NOT NULL,
    description TEXT,

    -- Investigation Context (JSONB - low cardinality, flexible)
    current_phase TEXT,
    investigation_strategy TEXT,
    investigation_progress JSONB,  -- Milestone tracking
    consulting_data JSONB,          -- Pre-investigation context
    problem_verification JSONB,     -- Consolidated symptoms/scope
    working_conclusion JSONB,       -- Current hypothesis synthesis
    root_cause_conclusion JSONB,    -- Final diagnosis
    degraded_mode JSONB,            -- Partial functionality info
    escalation_state JSONB,         -- Escalation tracking
    documentation_data JSONB,       -- Post-mortem data
    path_selection JSONB,           -- Decision tracking

    -- Conversation Context
    conversation_history JSONB,     -- Full message thread
    turn_progress JSONB,            -- Per-turn state tracking

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    resolved_at TIMESTAMP WITH TIME ZONE,

    -- Indexes
    CONSTRAINT fk_user FOREIGN KEY (user_id) REFERENCES users(user_id)
);
```

#### Normalized Tables (High-Cardinality Data)

**1. Evidence Table**
```sql
CREATE TABLE evidence (
    evidence_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    content TEXT NOT NULL,
    source_type TEXT NOT NULL,
    form TEXT NOT NULL,
    raw_artifact_s3_key TEXT,  -- S3 reference
    preprocessed_summary TEXT,
    classification_dimensions JSONB,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT fk_case FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
);

-- Indexes
CREATE INDEX idx_evidence_case ON evidence (case_id, created_at DESC);
CREATE INDEX idx_evidence_source ON evidence (source_type);
CREATE INDEX idx_evidence_form ON evidence (form);
```

**2. Hypotheses Table**
```sql
CREATE TABLE hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,  -- exploring, testing, validated, invalidated
    confidence_level TEXT,
    reasoning TEXT,
    evidence_links JSONB,  -- Array of {evidence_id, relationship}
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT fk_case FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
);

-- Indexes
CREATE INDEX idx_hypotheses_case ON hypotheses (case_id, status);
CREATE INDEX idx_hypotheses_status ON hypotheses (status);
```

**3. Solutions Table**
```sql
CREATE TABLE solutions (
    solution_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL,  -- fix, mitigation, escalation, investigation
    status TEXT NOT NULL,     -- proposed, applying, applied, failed
    linked_hypothesis_id TEXT,
    steps JSONB,              -- Array of action steps
    verification_criteria JSONB,
    result_summary TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    applied_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT fk_case FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
);

-- Indexes
CREATE INDEX idx_solutions_case ON solutions (case_id, status);
CREATE INDEX idx_solutions_hypothesis ON solutions (linked_hypothesis_id);
```

**4. Case Messages Table**
```sql
CREATE TABLE case_messages (
    message_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    message_type TEXT NOT NULL,  -- user_query, agent_response, system_event, data_upload
    role TEXT NOT NULL,           -- user, assistant, system
    content TEXT NOT NULL,
    metadata JSONB,               -- Turn number, tool calls, etc.
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT fk_case FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
);

-- Indexes
CREATE INDEX idx_messages_case ON case_messages (case_id, created_at ASC);
CREATE INDEX idx_messages_type ON case_messages (message_type);
```

**5. Uploaded Files Table**
```sql
CREATE TABLE uploaded_files (
    file_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    data_type TEXT NOT NULL,  -- logs_and_errors, metrics, config, etc.
    size_bytes BIGINT NOT NULL,
    s3_key TEXT NOT NULL,
    preprocessing_status TEXT NOT NULL,  -- pending, processing, completed, failed
    preprocessing_summary TEXT,
    uploaded_at TIMESTAMP WITH TIME ZONE NOT NULL,
    processed_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT fk_case FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
);

-- Indexes
CREATE INDEX idx_uploaded_files_case ON uploaded_files (case_id, uploaded_at DESC);
CREATE INDEX idx_uploaded_files_type ON uploaded_files (data_type);
```

**6. Case Status Transitions Table** (Audit Trail)
```sql
CREATE TABLE case_status_transitions (
    transition_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    triggered_at TIMESTAMP WITH TIME ZONE NOT NULL,
    triggered_by TEXT NOT NULL,  -- user_id or 'system'
    reason TEXT NOT NULL,
    CONSTRAINT fk_case FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
);

-- Indexes
CREATE INDEX idx_transitions_case ON case_status_transitions (case_id, triggered_at DESC);
```

**7. Case Tags Table** (M:N Relationship)
```sql
CREATE TABLE case_tags (
    case_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (case_id, tag),
    CONSTRAINT fk_case FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
);

-- Indexes
CREATE INDEX idx_tags_tag ON case_tags (tag);
```

**8. Agent Tool Calls Table**
```sql
CREATE TABLE agent_tool_calls (
    call_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    input_params JSONB NOT NULL,
    output_result JSONB,
    status TEXT NOT NULL,  -- pending, success, failed
    error_message TEXT,
    called_at TIMESTAMP WITH TIME ZONE NOT NULL,
    completed_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT fk_case FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
);

-- Indexes
CREATE INDEX idx_tool_calls_case ON agent_tool_calls (case_id, called_at DESC);
CREATE INDEX idx_tool_calls_tool ON agent_tool_calls (tool_name, status);
```

### 3.3 Performance Characteristics

**Case Load**: ~10ms (single query with LEFT JOINs)
- Main case data + aggregated normalized tables
- JSON aggregation for collections (evidence, hypotheses, solutions)

**Evidence Filtering**: ~5ms (indexed queries)
- Filter by source_type, form, created_at
- Full-text search on preprocessed_summary

**Hypothesis Tracking**: ~3ms (status index lookup)
- Filter active hypotheses by status
- Traverse evidence links efficiently

**Message Thread Retrieval**: ~8ms
- Chronological ordering with pagination
- Metadata includes tool calls and turn context

### 3.4 Access Patterns

```python
# Core CRUD operations
case = await case_repository.get(case_id)
case = await case_repository.save(case)
cases = await case_repository.find_by_user(user_id)

# Specialized queries
active_hypotheses = await case_repository.get_active_hypotheses(case_id)
evidence_by_type = await case_repository.get_evidence_by_type(case_id, source_type)
conversation = await case_repository.get_conversation_thread(case_id)
```

### 3.5 Session State (Redis)

**Purpose**: Fast session state with TTL expiration

```python
# Redis keys
session:{session_id}                    # Session data (TTL: 30 min)
session_client:{user_id}:{client_id}   # Client-based session index (TTL: same as session)

# Session structure
{
    "session_id": "uuid",
    "user_id": "user_123",
    "client_id": "device_456",  # For session resumption
    "created_at": "ISO timestamp",
    "last_activity": "ISO timestamp",
    "data_uploads": ["file_id_1", "file_id_2"],
    "case_history": ["case_id_1", "case_id_2"]
}
```

**Interface**: `ISessionStore` (`redis_session_store.py`)

```python
# Session operations
session = await session_store.get(session_id)
await session_store.set(session_id, session_data, ttl=1800)
exists = await session_store.exists(session_id)
deleted = await session_store.delete(session_id)
extended = await session_store.extend_ttl(session_id, ttl=1800)

# Client-based resumption
session_id = await session_store.find_by_user_and_client(user_id, client_id)
await session_store.index_session_by_client(user_id, client_id, session_id, ttl=1800)
await session_store.remove_client_index(user_id, client_id)
```

---

## 4. Observability Data Storage

### 4.1 Data Type Classification

**7 Observability Data Types** (from preprocessing specification):

| Data Type | Description | Examples | Preprocessing Strategy |
|-----------|-------------|----------|------------------------|
| `LOGS_AND_ERRORS` | Event-based chronological text | Application logs, system logs, error logs | Crime Scene Extraction |
| `METRICS_AND_PERFORMANCE` | Time-series numeric data | CPU usage, memory, latency, throughput | Statistical Anomaly Detection |
| `STRUCTURED_CONFIG` | System configuration files | YAML, JSON, TOML, INI | Configuration Diff Analysis |
| `SOURCE_CODE` | Executable code | Python, JavaScript, Java, SQL | Code Pattern Analysis |
| `UNSTRUCTURED_TEXT` | Human-written documents | Incident reports, runbooks | Semantic Summarization |
| `VISUAL_EVIDENCE` | Screenshots, UI captures | PNG, JPG, PDF | OCR + Visual Analysis |
| `UNANALYZABLE` | Reference-only files | Binary blobs, encrypted files | Metadata only |

### 4.2 Storage Architecture

**Two-Tier Storage**:

1. **Metadata Storage** (PostgreSQL `uploaded_files` table)
   - File metadata: filename, size, type, upload timestamp
   - Processing status: pending → processing → completed/failed
   - Preprocessing summary (extracted insights, ~8KB text)
   - S3 reference key

2. **Raw Artifact Storage** (S3-Compatible)
   - Full raw file content (up to 10MB)
   - Path: `artifacts/{case_id}/{file_id}/{filename}`
   - Lifecycle: 90 days default, configurable per case
   - Encryption at rest (AES-256)

### 4.3 Preprocessing Pipeline

**Synchronous Pipeline** (user waits):
```
Upload → Validate (size ≤ 10MB) → Classify (data type)
→ Extract (type-specific) → Sanitize (PII/secrets)
→ Store Raw (S3) → Store Metadata (PostgreSQL) → Return Summary
```

**Async Background Pipeline** (optional):
```
Preprocessing Result → Chunk (512 tokens) → Embed (BGE-M3)
→ Store Vector DB (ChromaDB case_{case_id}) → Enable Semantic Search
```

### 4.4 Preprocessing Result Structure

```python
class PreprocessingResult(BaseModel):
    """Output of preprocessing pipeline"""
    file_id: str
    data_type: DataType

    # Extracted insights (goes to PostgreSQL)
    summary: str                    # High-level overview (~500 chars)
    full_extraction: str            # Detailed analysis (~8K chars)
    key_insights: List[str]         # Bullet points (3-10 items)

    # Security findings
    pii_detected: bool
    secrets_detected: bool
    sanitized_content: str          # PII-redacted version

    # Storage references
    s3_key: str                     # Raw file location
    preprocessing_time_ms: int

    # Classification metadata
    confidence_score: float         # Classification confidence
    alternative_types: List[str]    # Other possible types
```

### 4.5 Access Patterns

```python
# Upload and preprocess
result = await preprocessing_service.process_upload(
    file=uploaded_file,
    case_id=case_id,
    user_choice=ProcessingChoice.FAST_EXTRACTION
)

# Retrieve by file ID
file_metadata = await case_repository.get_uploaded_file(file_id)

# List files for case
files = await case_repository.list_uploaded_files(case_id)

# Download raw artifact
raw_content = await s3_storage.retrieve(s3_key)

# Semantic search (if vectorized)
results = await vector_store.search(
    case_id=case_id,
    query="database connection timeout errors",
    k=5
)
```

### 4.6 Size Limits & Performance

**File Size Limit**: 10 MB (10,485,760 bytes)
- Handles 95% of troubleshooting files
- Crime Scene Extraction: 200:1 compression (10 MB → 50 KB)
- Prevents timeout/resource issues

**Processing Time** (by data type):
- Logs: 0.5-5s (Crime Scene Extraction)
- Metrics: 1-3s (Statistical Analysis)
- Config: 0.5-2s (Diff Analysis)
- Code: 1-4s (Pattern Analysis)
- Text: 2-8s (Semantic Summarization)
- Visual: 3-10s (OCR + Analysis)

---

## 5. Knowledge Base Data Storage

### 5.1 Architecture Overview

**User-Scoped Persistent Storage**: Each user has their own knowledge base for runbooks, procedures, and documentation.

**Storage**: ChromaDB with per-user collections
**Collection Naming**: `user_kb_{user_id}`
**Implementation**: `faultmaven/infrastructure/persistence/user_kb_vector_store.py`

### 5.2 Storage Characteristics

**Permanent Storage**:
- Documents persist indefinitely (no TTL)
- User controls lifecycle through explicit deletion
- Grows with user's documented knowledge

**Semantic Search**:
- BGE-M3 embeddings for vector similarity
- Sub-second search for typical queries
- Relevance ranking by cosine similarity

### 5.3 Document Structure

```python
class KnowledgeDocument(BaseModel):
    """User knowledge base document"""
    document_id: str              # Unique identifier
    user_id: str                  # Owner

    # Content
    title: str                    # Document title
    content: str                  # Full text content
    document_type: str            # troubleshooting, configuration, runbook, etc.

    # Metadata
    metadata: Dict[str, Any] = {
        "author": str,            # Document creator
        "version": str,           # Version identifier
        "tags": List[str],        # Searchable tags
        "source_url": str,        # Original source
        "last_updated": str,      # ISO timestamp
        "difficulty": str,        # beginner, intermediate, advanced
        "category": str,          # Organizational category
    }

    # Timestamps
    created_at: datetime
    updated_at: datetime
```

### 5.4 ChromaDB Collection Metadata

```python
# Collection metadata (no TTL)
{
    "user_id": "user_123",
    "created_at": "2025-01-15T10:30:00Z",
    "type": "user_knowledge_base"
}
```

### 5.5 Access Patterns

```python
# Add documents to user KB
await user_kb_store.add_documents(
    user_id="user_123",
    documents=[
        {
            "id": "doc_001",
            "content": "Database timeout troubleshooting runbook...",
            "metadata": {"title": "DB Timeouts", "category": "database"}
        }
    ]
)

# Semantic search
results = await user_kb_store.search(
    user_id="user_123",
    query="how to handle connection pool exhaustion",
    k=5
)

# List all documents
documents = await user_kb_store.list_documents(user_id="user_123")

# Delete document
await user_kb_store.delete_document(
    user_id="user_123",
    document_id="doc_001"
)
```

### 5.6 Chunking Strategy

**Large Documents** (> 2000 chars):
- Automatic chunking into 512-token segments
- Overlap: 50 tokens between chunks
- Chunk metadata includes parent document ID
- Search returns relevant chunks with context

**Small Documents** (≤ 2000 chars):
- Stored as single chunk
- No segmentation overhead

### 5.7 Integration with Agent

**Tool**: `answer_from_user_kb` (agent tool)

```python
# Agent flow
1. User asks: "How do I handle database timeouts?"
2. Agent calls answer_from_user_kb tool
3. Tool performs semantic search on user's KB
4. Retrieves top 5 relevant chunks
5. Synthesis LLM generates answer from chunks
6. Agent provides contextual response
```

**Benefits**:
- Personalized knowledge for each user
- Learns from past troubleshooting
- No external dependency for known issues
- Privacy-preserving (user data stays isolated)

---

## 6. Access Patterns & Interfaces

### 6.1 Repository Pattern

All storage access follows the **Repository Pattern** with interface abstraction:

```python
# User Repository
class UserRepository(ABC):
    async def save(self, user: User) -> User
    async def get(self, user_id: str) -> Optional[User]
    async def get_by_username(self, username: str) -> Optional[User]
    async def get_by_email(self, email: str) -> Optional[User]
    async def list(self, limit: int, offset: int) -> tuple[List[User], int]
    async def delete(self, user_id: str) -> bool

# Case Repository
class CaseRepository(ABC):
    async def save(self, case: Case) -> Case
    async def get(self, case_id: str) -> Optional[Case]
    async def find_by_user(self, user_id: str) -> List[Case]
    async def list_active(self, user_id: str) -> List[Case]
    async def search(self, query: str, filters: Dict) -> List[Case]

# Session Store Interface
class ISessionStore(ABC):
    async def get(self, key: str) -> Optional[Dict]
    async def set(self, key: str, value: Dict, ttl: Optional[int]) -> None
    async def delete(self, key: str) -> bool
    async def exists(self, key: str) -> bool
    async def extend_ttl(self, key: str, ttl: Optional[int]) -> bool
    async def find_by_user_and_client(self, user_id: str, client_id: str) -> Optional[str]

# Vector Store Interface
class IVectorStore(ABC):
    async def add_documents(self, documents: List[Dict]) -> None
    async def search(self, query: str, k: int) -> List[Dict]
    async def delete_documents(self, ids: List[str]) -> None

# Storage Backend Interface
class IStorageBackend(ABC):
    async def store(self, key: str, data: Any) -> None
    async def retrieve(self, key: str) -> Optional[Any]
```

### 6.2 Dependency Injection

All repositories accessed through DI Container:

```python
from faultmaven.container import container

# Get repository instances
user_repo = container.get_user_repository()
case_repo = container.get_case_repository()
session_store = container.get_session_store()
user_kb_store = container.get_user_kb_vector_store()
```

### 6.3 Common Access Patterns

**Pattern 1: Case Creation with Initial Evidence**
```python
# Create case
case = Case(
    case_id=str(uuid4()),
    user_id=current_user.user_id,
    title="Database Connection Timeouts",
    status=CaseStatus.CONSULTING
)
await case_repo.save(case)

# Upload initial evidence
preprocessing_result = await preprocessing_service.process_upload(file, case.case_id)
evidence = Evidence.from_preprocessing(preprocessing_result)
case.add_evidence(evidence)
await case_repo.save(case)
```

**Pattern 2: Session Resumption**
```python
# Find existing session by client
session_id = await session_store.find_by_user_and_client(user_id, client_id)

if session_id:
    # Resume existing session
    session = await session_store.get(session_id)
    await session_store.extend_ttl(session_id, ttl=1800)
else:
    # Create new session
    new_session = create_new_session(user_id, client_id)
    await session_store.set(new_session.session_id, new_session.dict(), ttl=1800)
    await session_store.index_session_by_client(user_id, client_id, new_session.session_id, ttl=1800)
```

**Pattern 3: Knowledge Base Search with Fallback**
```python
# Search user KB first
kb_results = await user_kb_store.search(user_id, query, k=5)

if kb_results:
    # Synthesize answer from user's runbooks
    answer = await synthesis_llm.generate(query, kb_results)
else:
    # Fall back to global knowledge base or LLM
    answer = await global_knowledge.search(query)
```

**Pattern 4: Evidence-Driven Hypothesis Update**
```python
# Upload new evidence
evidence = await add_evidence(case_id, uploaded_file)

# Evaluate against active hypotheses
for hypothesis in case.get_active_hypotheses():
    evaluation = await evaluate_hypothesis(hypothesis, evidence)

    if evaluation.status == "VALIDATED":
        hypothesis.status = HypothesisStatus.VALIDATED
        hypothesis.add_evidence_link(evidence.evidence_id, "supports")
    elif evaluation.status == "INVALIDATED":
        hypothesis.status = HypothesisStatus.INVALIDATED
        hypothesis.add_evidence_link(evidence.evidence_id, "refutes")

await case_repo.save(case)
```

---

## 7. Data Retention & Lifecycle

### 7.1 Retention Policies

| Data Category | Retention Period | Cleanup Strategy |
|---------------|------------------|------------------|
| **User Accounts** | Indefinite (soft delete) | Soft delete after 30 days inactive (configurable) |
| **Active Cases** | Indefinite | User-controlled closure |
| **Resolved Cases** | 1 year default | Archive to cold storage after 90 days |
| **Session State** | 30 minutes (TTL) | Automatic Redis expiration |
| **Raw Artifacts** | 90 days default | S3 lifecycle policy |
| **User Knowledge Base** | Indefinite | User-controlled deletion |
| **Case Vector Store** | Case lifetime + 7 days | TTL-based cleanup after case closure |
| **Audit Logs** | 2 years | Archive to S3 after 90 days |

### 7.2 Lifecycle Management

**Case Lifecycle**:
```
CONSULTING (active)
  → INVESTIGATING (active)
    → RESOLVED (terminal, archived after 90 days)
    → CLOSED (terminal, archived after 30 days)
```

**Session Lifecycle**:
```
Created (TTL: 30 min)
  → Activity extends TTL
    → Expiration (automatic cleanup)
    → Explicit logout (immediate cleanup)
```

**Uploaded File Lifecycle**:
```
Upload → Preprocessing → S3 Storage (90 days)
  → Archive to Glacier (if case retained)
  → Permanent deletion (after retention period)
```

### 7.3 Automated Cleanup Jobs

**Daily Cleanup Tasks** (cron jobs):
- Expire old sessions (Redis TTL handles most)
- Archive resolved cases older than 90 days
- Delete raw artifacts past retention period
- Clean up orphaned vector store collections
- Soft delete inactive user accounts (if configured)

**Weekly Cleanup Tasks**:
- Vacuum PostgreSQL tables
- Reindex for performance
- Backup validation
- Storage usage reporting

---

## 8. Security & Compliance

### 8.1 Data Privacy

**PII Redaction**:
- All user input sanitized before LLM processing
- Presidio integration for advanced PII detection
- Fallback regex patterns when Presidio unavailable
- Configurable sensitivity levels (development vs. production)

**Encryption**:
- **At Rest**: AES-256 encryption for S3, PostgreSQL, Redis
- **In Transit**: TLS 1.3 for all network communication
- **Secrets Management**: Environment variables, not in code

### 8.2 Access Control

**User Data Isolation**:
- User can only access their own data
- Foreign key constraints enforce ownership
- Row-level security (RLS) in PostgreSQL (optional)

**Role-Based Access Control (RBAC)**:
- User roles: `user`, `admin`, `analyst`
- Admin: Full system access
- Analyst: Read-only case access for support
- User: Own data only

### 8.3 Audit Trail

**Immutable Audit Logs**:
- `case_status_transitions`: All status changes
- `agent_tool_calls`: All agent actions
- User authentication events
- Data access logs (who accessed what, when)

**Compliance**:
- GDPR: Right to deletion (soft delete + data export)
- SOC 2: Audit trails, encryption, access control
- HIPAA-ready: Additional PHI redaction if needed

---

## 9. Scalability & Performance

### 9.1 Horizontal Scaling

**PostgreSQL**:
- Read replicas for query load distribution
- Partitioning for large tables (cases, messages, evidence)
- Connection pooling (PgBouncer)

**Redis**:
- Cluster mode for high availability
- Sharding by session key
- Sentinel for automatic failover

**ChromaDB**:
- Per-user collections enable horizontal partitioning
- Collection-level isolation prevents hotspots

**S3**:
- Infinite horizontal scalability
- CDN for frequently accessed artifacts

### 9.2 Performance Optimization

**Query Optimization**:
- Indexes on all foreign keys
- Composite indexes for common filters
- Partial indexes for status-based queries
- JSON indexing for JSONB columns (GIN indexes)

**Caching Strategy**:
- Redis cache for frequently accessed cases
- CDN cache for static artifacts
- In-memory cache for user profiles

**Batch Operations**:
- Bulk evidence insertion
- Batch hypothesis evaluation
- Parallel preprocessing for multiple files

### 9.3 Performance Targets

| Operation | Target | Measured |
|-----------|--------|----------|
| User authentication | < 50ms | 30ms avg |
| Case load (full) | < 20ms | 10ms avg |
| Session get/set | < 5ms | 2ms avg |
| Evidence query | < 10ms | 5ms avg |
| KB semantic search | < 200ms | 150ms avg |
| File preprocessing | < 30s | 5s median, 25s p95 |

---

## 10. Migration & Backup

### 10.1 Database Migrations

**Tool**: Alembic (PostgreSQL schema migrations)

```bash
# Migration location
migrations/versions/

# Create migration
alembic revision --autogenerate -m "Add evidence classification"

# Apply migration
alembic upgrade head

# Rollback
alembic downgrade -1
```

**Migration Strategy**:
- Zero-downtime deployments with backward-compatible schemas
- Blue-green deployment for breaking changes
- Rollback plan for every migration

### 10.2 Backup & Recovery

**PostgreSQL Backups**:
- Continuous WAL archiving to S3
- Daily full backups
- Point-in-time recovery (PITR) capability
- 30-day backup retention

**Redis Persistence**:
- RDB snapshots every 15 minutes
- AOF (Append-Only File) for durability
- Backup to S3 daily

**S3 Versioning**:
- Object versioning enabled
- Cross-region replication for disaster recovery

**ChromaDB Backups**:
- Collection exports to S3 daily
- Metadata backup to PostgreSQL

### 10.3 Disaster Recovery

**RTO (Recovery Time Objective)**: < 1 hour
**RPO (Recovery Point Objective)**: < 15 minutes

**Recovery Procedures**:
1. **Database Failure**: Promote read replica to master (< 5 min)
2. **Redis Failure**: Automatic failover via Sentinel (< 1 min)
3. **S3 Failure**: Cross-region replication active (automatic)
4. **Complete Region Failure**: Restore from backups to new region (< 60 min)

---

## Appendix A: Schema Diagrams

### A.1 Complete Entity-Relationship Diagram

```
┌─────────────┐
│    users    │
└──────┬──────┘
       │ 1:N
       │
       ▼
┌─────────────┐         ┌──────────────────┐
│    cases    │────────▶│ case_status_     │
└──────┬──────┘ 1:N     │   transitions    │
       │                 └──────────────────┘
       │ 1:N
       ├──────────────────┬──────────────┬──────────────┬─────────────┐
       ▼                  ▼              ▼              ▼             ▼
┌──────────┐    ┌──────────────┐ ┌──────────┐  ┌─────────────┐ ┌──────────┐
│ evidence │    │ hypotheses   │ │ solutions│  │case_messages│ │uploaded_ │
│          │    │              │ │          │  │             │ │  files   │
└──────────┘    └──────────────┘ └──────────┘  └─────────────┘ └──────────┘

       │
       │ M:N
       ▼
┌──────────────┐
│  case_tags   │
└──────────────┘

       │
       │ 1:N
       ▼
┌──────────────────┐
│ agent_tool_calls │
└──────────────────┘
```

### A.2 Session Storage Schema

```
Redis Keys:
├── session:{session_id} → Session data (TTL: 30 min)
└── session_client:{user_id}:{client_id} → session_id (TTL: same)

Session Data Structure:
{
    "session_id": str,
    "user_id": str,
    "client_id": str,
    "created_at": timestamp,
    "last_activity": timestamp,
    "data_uploads": [file_ids],
    "case_history": [case_ids]
}
```

---

## Appendix B: Migration from Previous Architecture

### B.1 Redis-to-PostgreSQL Migration

**Completed**: Phase 0 cleanup and Phase 1 implementation

**Old Architecture** (Redis-based):
- All case data in Redis (cases, evidence, hypotheses)
- No relational integrity
- Manual JSON serialization
- No indexing support
- Memory-limited scalability

**New Architecture** (Hybrid PostgreSQL):
- ACID guarantees for transactional data
- Foreign key constraints
- Advanced querying (JOINs, filters, full-text search)
- Horizontal scalability via read replicas
- Cost-effective storage

**Migration Benefits**:
- 10x faster complex queries
- 100x more storage capacity
- ACID guarantees for data integrity
- Standard SQL tooling support
- Backup/recovery maturity

### B.2 Backward Compatibility

**Transition Period**:
- Repository abstraction ensures code compatibility
- Both storage backends supported temporarily
- Gradual migration with feature flags
- Rollback capability maintained

---

## Document Version History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2025-01-12 | FaultMaven Team | Initial comprehensive design |

## References

- Investigation Architecture Specification v2.0
- Data Preprocessing Design Specification v2.0
- Case Storage Design (docs/architecture/case-storage-design.md)
- User Storage Design (docs/architecture/user-storage-design.md)
- Evidence Architecture v1.1
