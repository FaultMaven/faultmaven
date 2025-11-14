# FaultMaven Data Storage Architecture v2.0

## Executive Summary

This document defines the comprehensive data storage architecture for FaultMaven's **12 data categories**, covering both **primary application data** and **operational infrastructure data**.

### Primary Application Data (7 categories)
1. **User Information** - Account, authentication, profile, SSO
2. **Case-Centric Data** - Investigation lifecycle, conversation, context, evidence
3. **Observability Data** - 8 types of uploaded machine data for analysis
4. **User Knowledge Base** - User-uploaded runbooks and procedures (permanent)
5. **Case Working Memory** - Temporary per-case vector store (ephemeral)
6. **Global Knowledge Base** - System-wide troubleshooting documentation (shared)
7. **Report & Analytics Data** - Generated reports, post-mortems, analytics

### Operational Infrastructure Data (5 categories)
8. **Job Queue State** - Async background job tracking
9. **ML Model Artifacts** - Confidence models, calibration data, feature metadata
10. **Protection System State** - Rate limiting, reputation scores, behavioral analysis
11. **Cache Data** - Multi-tier intelligent caching with pattern analysis
12. **System Operational Data** - Metrics, traces, logs, audit trails (optional)

**Key Design Principles**:
- **Storage Polyglot**: PostgreSQL for transactional, Redis for sessions, ChromaDB for semantic search
- **Interface-Based**: All storage accessed through repository abstractions
- **Privacy-First**: PII redaction before persistence, encryption at rest
- **Performance-Optimized**: Hybrid schemas balance query performance with flexibility
- **Cloud-Native**: Designed for Kubernetes deployment with horizontal scaling
- **Processing-Driven Types**: Data classified by processing method, not semantic meaning

---

## Table of Contents

### Core Data Storage
1. [Storage Technology Matrix](#1-storage-technology-matrix)
2. [User Information Storage](#2-user-information-storage)
3. [Case-Centric Data Storage](#3-case-centric-data-storage)
4. [Observability Data Storage](#4-observability-data-storage)
5. [User Knowledge Base Storage](#5-user-knowledge-base-storage)

### Extended Data Storage (New)
6. [Case Working Memory Storage](#6-case-working-memory-storage)
7. [Global Knowledge Base Storage](#7-global-knowledge-base-storage)
8. [Report & Analytics Data Storage](#8-report--analytics-data-storage)
9. [Job Queue State Storage](#9-job-queue-state-storage)
10. [ML Model Artifacts Storage](#10-ml-model-artifacts-storage)

### Operational Data Storage
11. [Protection System State Storage](#11-protection-system-state-storage)
12. [Cache Data Storage](#12-cache-data-storage)
13. [System Operational Data](#13-system-operational-data)

### Cross-Cutting Concerns
14. [Access Patterns & Interfaces](#14-access-patterns--interfaces)
15. [Data Retention & Lifecycle](#15-data-retention--lifecycle)
16. [Security & Compliance](#16-security--compliance)
17. [Scalability & Performance](#17-scalability--performance)
18. [Migration & Backup](#18-migration--backup)

---

## 1. Storage Technology Matrix

### 1.1 Complete Technology Selection

| Data Category | Primary Storage | Secondary/Cache | Lifecycle | Scope |
|--------------|----------------|-----------------|-----------|-------|
| **User Information** | PostgreSQL | - | Indefinite (soft delete) | Per-user |
| **Case Data** | PostgreSQL | Redis (state) | 1 year default | Per-case |
| **Observability Data** | PostgreSQL + S3 | - | 90 days | Per-case |
| **User Knowledge Base** | ChromaDB | PostgreSQL (metadata) | Indefinite | Per-user |
| **Case Working Memory** | ChromaDB | - | Case lifetime + 7 days | Per-case |
| **Global Knowledge Base** | ChromaDB | - | Indefinite | System-wide |
| **Report & Analytics** | Redis + ChromaDB | - | 90 days post-closure | Per-case/system |
| **Job Queue State** | Redis | - | 24 hours (TTL) | Per-job |
| **ML Model Artifacts** | File system | PostgreSQL (metadata) | 3 versions retained | System-wide |
| **Protection State** | Redis | PostgreSQL (archive) | Real-time + 30 days | Per-client |
| **Cache Data** | Multi-tier | - | Minutes to hours (TTL) | Various |
| **System Operational** | Time-series DB + S3 | - | 90 days - 1 year | System-wide |

### 1.2 Complete Storage Architecture Diagram

```
┌───────────────────────────────────────────────────────────────────────┐
│                       APPLICATION LAYER                                │
│                  (FastAPI + Service Layer)                            │
└────────────┬──────────────────────────────────────────────────────────┘
             │
             ├──> UserRepository Interface
             │    └──> PostgreSQL (auth_db.users)
             │
             ├──> CaseRepository Interface
             │    └──> PostgreSQL (cases_db.* hybrid schema - 10 tables)
             │
             ├──> ISessionStore Interface
             │    ├──> Redis (primary, TTL-based)
             │    └──> PostgreSQL (archive, optional)
             │
             ├──> IVectorStore Interface (3 implementations)
             │    ├──> UserKBVectorStore: ChromaDB (user_kb_{user_id})
             │    ├──> CaseVectorStore: ChromaDB (case_{case_id})
             │    └──> GlobalKBVectorStore: ChromaDB (global_kb)
             │
             ├──> IReportStore Interface
             │    ├──> Redis (metadata)
             │    └──> ChromaDB (content)
             │
             ├──> IJobService Interface
             │    └──> Redis (job:{job_id})
             │
             ├──> IGlobalConfidenceService Interface
             │    ├──> File system (model weights)
             │    └──> PostgreSQL (metadata)
             │
             ├──> ReputationEngine
             │    └──> Redis (reputation state)
             │
             ├──> IntelligentCache
             │    ├──> L1: In-memory (< 1ms)
             │    ├──> L2: Redis (< 5ms)
             │    └──> L3: PostgreSQL/S3 (< 20ms)
             │
             └──> IStorageBackend Interface (Artifacts)
                  └──> S3 (raw uploaded files)

┌───────────────────────────────────────────────────────────────────────┐
│                     INFRASTRUCTURE LAYER                               │
├───────────────────────────────────────────────────────────────────────┤
│ PostgreSQL Clusters:                                                  │
│   - auth_db: User accounts, roles, SSO                                │
│   - cases_db: Investigation data (10 tables), evidence, hypotheses    │
│                                                                        │
│ Redis Cluster:                                                        │
│   - Session state (session:{id}, TTL: 30 min)                         │
│   - Job queue (job:{id}, TTL: 24 hours)                               │
│   - Report metadata (case:{id}:reports)                               │
│   - Protection state (reputation, rate limits)                        │
│   - Cache L2 (multi-tier caching)                                     │
│                                                                        │
│ ChromaDB:                                                             │
│   - User KB: user_kb_{user_id} (permanent)                           │
│   - Case Working Memory: case_{case_id} (ephemeral)                  │
│   - Global KB: global_kb (shared)                                     │
│   - Report content storage                                            │
│                                                                        │
│ S3-Compatible Storage:                                                │
│   - Raw uploaded files: artifacts/{case_id}/{file_id}                 │
│   - ML model artifacts: models/{version}/*.pkl                        │
│   - Audit logs: logs/{year}/{month}/{day}/                            │
│   - Lifecycle policy: 90 days default                                 │
│                                                                        │
│ File System (Local/NFS):                                              │
│   - ML model weights: /var/lib/faultmaven/models/                     │
│   - Calibration data: /var/lib/faultmaven/calibration/                │
└───────────────────────────────────────────────────────────────────────┘
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
**Architecture**: Hybrid Normalized Schema (10 tables)
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

CREATE INDEX idx_solutions_case ON solutions (case_id, status);
CREATE INDEX idx_solutions_hypothesis ON solutions (linked_hypothesis_id);
```

**4-10. Additional Tables**: See full schema in Appendix A

### 3.3 Case Sharing

**Purpose**: Enable collaboration by sharing cases with specific users or teams

**Implementation**: See `migrations/002_add_case_sharing.sql`

#### 3.3.1 Case Participants Table

**Storage**: PostgreSQL (`case_participants` table)

```sql
CREATE TABLE case_participants (
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    user_id VARCHAR(20) NOT NULL,
    role participant_role NOT NULL DEFAULT 'viewer',  -- owner, collaborator, viewer
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    added_by VARCHAR(20),
    last_accessed_at TIMESTAMPTZ,
    metadata JSONB DEFAULT '{}'::jsonb,
    PRIMARY KEY (case_id, user_id)
);
```

#### 3.3.2 Access Control Model

| Role | Permissions |
|------|-------------|
| **owner** | Full control: read, write, delete, share, transfer ownership |
| **collaborator** | Read and write: view case, add evidence, propose hypotheses/solutions |
| **viewer** | Read-only: view case details, cannot modify |

#### 3.3.3 Sharing Mechanisms

**Individual User Sharing**:
```python
# Share case with specific user
await case_repository.share_case(
    case_id="case_abc123",
    target_user_id="user_bob",
    role=ParticipantRole.COLLABORATOR,
    sharer_user_id="user_alice"
)
```

**Team-Based Sharing** (requires `migrations/003_enterprise_user_schema.sql`):
```sql
-- Assign case to team for automatic team member access
UPDATE cases
SET team_id = 'team_sre_oncall'
WHERE case_id = 'case_abc123';

-- All team members automatically get access
-- Access level determined by team role and org permissions
```

**Organization-Wide Visibility**:
```sql
-- Cases belong to organization for tenant isolation
SELECT * FROM cases WHERE org_id = 'org_acme_corp';

-- Org members can view cases based on org role permissions
```

#### 3.3.4 Access Resolution Hierarchy

```
User access to case is granted if ANY of:
1. User is case owner (cases.user_id = user_id)
2. User is explicit participant (case_participants)
3. User is team member (cases.team_id → team_members)
4. User has org-level permissions (organization_members.role_id → permissions)
```

**SQL Function**:
```sql
-- Check if user can access case
SELECT user_can_access_case('user_alice', 'case_abc123');

-- Get user's role for case
SELECT get_user_case_role('user_alice', 'case_abc123');
-- Returns: 'owner' | 'collaborator' | 'viewer' | NULL
```

#### 3.3.5 Audit Trail

**Table**: `case_sharing_audit`

Tracks all sharing actions for compliance:
- Who shared the case (`action_by`)
- With whom (`target_user_id`)
- What role assigned (`new_role`)
- When (`action_at`)
- Action type (`shared`, `unshared`, `role_changed`)

```sql
SELECT * FROM case_sharing_audit
WHERE case_id = 'case_abc123'
ORDER BY action_at DESC;
```

#### 3.3.6 Views

**user_shared_cases**: All cases shared with each user
```sql
SELECT * FROM user_shared_cases
WHERE user_id = 'user_bob'
ORDER BY shared_at DESC;
```

**case_collaboration_summary**: Collaboration statistics per case
```sql
SELECT
    case_id,
    participant_count,
    collaborator_count,
    viewer_count
FROM case_collaboration_summary;
```

### 3.4 Performance Characteristics

- **Case Load**: ~10ms (single query with LEFT JOINs)
- **Evidence Filtering**: ~5ms (indexed queries)
- **Hypothesis Tracking**: ~3ms (status index lookup)
- **Message Thread**: ~8ms (chronological ordering)

### 3.5 Session State (Redis)

**Purpose**: Fast session state with TTL expiration

```python
# Redis keys
session:{session_id}                    # Session data (TTL: 30 min)
session_client:{user_id}:{client_id}   # Client-based session index

# Session structure
{
    "session_id": "uuid",
    "user_id": "user_123",
    "client_id": "device_456",
    "created_at": "ISO timestamp",
    "last_activity": "ISO timestamp",
    "data_uploads": ["file_id_1"],
    "case_history": ["case_id_1"]
}
```

---

## 4. Observability Data Storage

### 4.1 Data Type Classification (8 Types)

**Processing-Driven Classification**: Types defined by processing method and tool requirements, not semantic meaning.

| Type | Structure | Processing Method | Tools | Examples |
|------|-----------|-------------------|-------|----------|
| **LOGS_AND_ERRORS** | Timestamped events, errors, stack traces | Crime Scene Extraction, timeline reconstruction | Log parser, error pattern detector, stack trace analyzer | App logs, system logs, Sentry reports, crash dumps |
| **TRACE_DATA** | Hierarchical spans with parent-child relationships | Critical path analysis, service mapping, latency distribution | Span parser, relationship graph builder, bottleneck detector | Jaeger, Zipkin, OpenTelemetry, AWS X-Ray, distributed traces |
| **PROFILING_DATA** | Call stacks with resource attribution | Hotspot detection, flame graph generation, resource analysis | Profile parser (pprof/perf), call stack aggregator, flame graph generator | pprof, perf, VisualVM, heap dumps, CPU profiles |
| **METRICS_AND_PERFORMANCE** | Time-series numeric data | Statistical anomaly detection, trend analysis | Statistical analyzer, threshold checker, correlation detector | Prometheus, CSV timeseries, Grafana exports |
| **STRUCTURED_CONFIG** | Hierarchical key-value (YAML/JSON/TOML) | Config diff, schema validation | JSON/YAML parser, diff engine | YAML, JSON, TOML, INI, XML config files |
| **SOURCE_CODE** | Programming language syntax | AST parsing, pattern matching, static analysis | Language-specific parsers, static analyzer | Python, JavaScript, Java, SQL, Dockerfiles |
| **UNSTRUCTURED_TEXT** | Prose, documentation, markdown | Semantic summarization, NLP, entity extraction | LLM summarizer, markdown parser, entity extractor | Incident reports, README, API docs, runbooks |
| **VISUAL_EVIDENCE** | Images, screenshots, PDFs | OCR, visual analysis, layout detection | OCR engine, image processor | PNG, JPG, PDF screenshots |

### 4.2 Classification Logic

```python
class DataType(str, Enum):
    """8 purpose-driven data classifications for preprocessing pipeline."""

    # Event-based data
    LOGS_AND_ERRORS = "logs_and_errors"          # Includes crash reports, error dumps
    TRACE_DATA = "trace_data"                    # Distributed traces with span relationships

    # Performance data
    PROFILING_DATA = "profiling_data"            # CPU/memory profiles, flame graphs
    METRICS_AND_PERFORMANCE = "metrics_and_performance"  # Time-series metrics

    # Configuration and code
    STRUCTURED_CONFIG = "structured_config"      # JSON/YAML/TOML/INI
    SOURCE_CODE = "source_code"                  # Programming languages

    # Human-authored content
    UNSTRUCTURED_TEXT = "unstructured_text"      # Includes documentation

    # Visual content
    VISUAL_EVIDENCE = "visual_evidence"          # Images, screenshots


def classify_data_type(filename: str, content_sample: bytes, mime_type: str) -> DataType:
    """Multi-pass classification using pattern matching."""
    sample = content_sample[:5000].decode('utf-8', errors='ignore')

    # Check MIME type first
    if mime_type.startswith("image/"):
        return DataType.VISUAL_EVIDENCE

    # Check structured formats
    if filename.endswith(('.yaml', '.yml', '.json', '.toml', '.ini', '.xml')):
        return DataType.STRUCTURED_CONFIG

    if filename.endswith(('.py', '.js', '.java', '.go', '.rs', '.cpp', '.sql')):
        return DataType.SOURCE_CODE

    # Check trace data signatures
    trace_patterns = [
        r'"spans":\s*\[',           # OpenTelemetry/Jaeger
        r'"traceId":\s*"',          # Trace ID field
        r'"parentSpanId":\s*"',     # Span relationships
    ]
    if any(re.search(pattern, sample) for pattern in trace_patterns):
        return DataType.TRACE_DATA

    # Check profiling data signatures
    profiling_patterns = [
        r'^collapsed\s+stack',       # Collapsed stack format
        r'samples/count',            # pprof format
        r'CPU\s+profile',            # Profile header
    ]
    if any(re.search(pattern, sample) for pattern in profiling_patterns):
        return DataType.PROFILING_DATA

    # Check log patterns
    log_patterns = [
        r'\d{4}-\d{2}-\d{2}',               # Date stamps
        r'ERROR|FATAL|Exception',            # Error keywords
        r'Traceback|Stack\s+trace',          # Stack traces
    ]
    if any(re.search(pattern, sample) for pattern in log_patterns):
        return DataType.LOGS_AND_ERRORS

    # Check metrics patterns
    metrics_patterns = [
        r'\d+\.\d+,\d+\.\d+',              # CSV numeric data
        r'"value":\s*\d+',                  # JSON metrics
        r'cpu_usage|memory_usage|latency',  # Metric names
    ]
    if any(re.search(pattern, sample) for pattern in metrics_patterns):
        return DataType.METRICS_AND_PERFORMANCE

    # Check text/documentation patterns
    text_patterns = [
        r'^#+\s+',                          # Markdown headers
        r'```',                             # Code blocks
    ]
    if any(re.search(pattern, sample, re.MULTILINE) for pattern in text_patterns):
        return DataType.UNSTRUCTURED_TEXT

    return DataType.UNSTRUCTURED_TEXT  # Default
```

### 4.3 Processing Pipelines

#### **TRACE_DATA Processing Pipeline** (New)

```python
# Trace Analyzer Pipeline
Trace Data → Span Parser (OpenTelemetry/Jaeger format)
          → Relationship Graph Builder (DAG construction)
          → Critical Path Detector (longest span chain)
          → Service Dependency Mapper
          → Latency Distribution Calculator
          → Bottleneck Identifier
          → Summary Generation

# Output:
{
    "trace_id": "abc123",
    "total_duration_ms": 1247,
    "critical_path": [
        {"service": "api-gateway", "duration_ms": 45},
        {"service": "auth-service", "duration_ms": 892},
        {"service": "database", "duration_ms": 310}
    ],
    "bottlenecks": [
        {"service": "auth-service", "operation": "validate_token", "duration_ms": 892}
    ],
    "service_map": {
        "api-gateway": ["auth-service", "user-service"],
        "auth-service": ["database"]
    },
    "key_insights": [
        "Auth service taking 71% of total request time",
        "Database queries show N+1 pattern"
    ]
}
```

#### **PROFILING_DATA Processing Pipeline** (New)

```python
# Profile Analyzer Pipeline
Profile Data → Profile Parser (pprof/perf/collapsed stack)
            → Call Stack Aggregator
            → Hotspot Detector (top N functions by resource)
            → Flame Graph Generator
            → Memory Allocation Analyzer
            → CPU Time Attribution
            → Resource Optimization Recommendations

# Output:
{
    "profile_type": "cpu",
    "total_samples": 15420,
    "duration_seconds": 60,
    "hotspots": [
        {"function": "database.query_slow", "samples": 8234, "percentage": 53.4},
        {"function": "json.serialize", "samples": 2156, "percentage": 14.0}
    ],
    "flame_graph_url": "s3://artifacts/case_123/flame_graph.svg",
    "key_insights": [
        "53% of CPU time in database.query_slow - optimize query",
        "JSON serialization 14% - consider using msgpack"
    ],
    "recommendations": [
        "Add index on users.email column",
        "Use binary serialization for large payloads"
    ]
}
```

### 4.4 Storage Architecture

**Two-Tier Storage**:

1. **Metadata Storage** (PostgreSQL `uploaded_files` table)
   - File metadata: filename, size, type, upload timestamp
   - Processing status: pending → processing → completed/failed
   - Preprocessing summary (extracted insights, ~8KB text)
   - S3 reference key
   - Data type classification

2. **Raw Artifact Storage** (S3-Compatible)
   - Full raw file content (up to 10MB)
   - Path: `artifacts/{case_id}/{file_id}/{filename}`
   - Lifecycle: 90 days default, configurable per case
   - Encryption at rest (AES-256)

### 4.5 Preprocessing Result Structure

```python
class PreprocessingResult(BaseModel):
    """Output of preprocessing pipeline"""
    file_id: str
    data_type: DataType

    # Extracted insights (stored in PostgreSQL)
    summary: str                    # High-level overview (~500 chars)
    full_extraction: str            # Detailed analysis (~8K chars)
    key_insights: List[str]         # Bullet points (3-10 items)

    # Security findings
    pii_detected: bool
    secrets_detected: bool
    sanitized_content: str

    # Storage references
    s3_key: str
    preprocessing_time_ms: int

    # Classification metadata
    confidence_score: float
    alternative_types: List[str]
```

### 4.6 Size Limits & Performance

**File Size Limit**: 10 MB (10,485,760 bytes)

**Processing Time** (by data type):
- Logs: 0.5-5s (Crime Scene Extraction)
- Traces: 1-4s (Span graph analysis)
- Profiles: 2-8s (Flame graph generation)
- Metrics: 1-3s (Statistical analysis)
- Config: 0.5-2s (Diff analysis)
- Code: 1-4s (AST parsing)
- Text: 2-8s (Semantic summarization)
- Visual: 3-10s (OCR + analysis)

---

## 5. User Knowledge Base Storage

### 5.1 Architecture Overview

**Purpose**: User-scoped persistent storage for runbooks, procedures, documentation

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

### 5.4 Access Patterns

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

### 5.5 Knowledge Base Sharing

**Purpose**: Enable collaboration by sharing runbooks and documentation with users, teams, and organizations

**Implementation**: See `migrations/004_kb_sharing_infrastructure.sql`

#### 5.5.1 Architecture Change

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

#### 5.5.2 Document Metadata Table

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

#### 5.5.3 Sharing Mechanisms

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

#### 5.5.4 Access Control Model

| Permission | Capabilities |
|-----------|--------------|
| **read** | View document content, search, download |
| **write** | Read + edit content, update metadata, delete (if owner) |

**Owner Always Has Write**: Document owner always has write permission regardless of sharing settings.

#### 5.5.5 Access Resolution for Search

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

#### 5.5.6 Audit Trail

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

#### 5.5.7 Views

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

#### 5.5.8 ChromaDB Collection Strategy

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

## 6. Case Working Memory Storage

### 6.1 Architecture Overview

**Purpose**: Ephemeral session-specific RAG for temporary document storage during active troubleshooting

**Key Differences from User KB**:
- **Lifecycle**: Ephemeral (deleted when case closes)
- **Scope**: Case-specific collections (`case_{case_id}`)
- **TTL**: Tied to case lifecycle + 7 days cleanup
- **Use Case**: QA sub-agent for "What does this uploaded PDF say?"

**Storage**: ChromaDB
**Collection Naming**: `case_{case_id}`
**Implementation**: `faultmaven/infrastructure/persistence/case_vector_store.py`

### 6.2 Storage Characteristics

**Ephemeral Storage**:
- Collections created on-demand when first document added
- Automatically deleted when case closes or archives
- 7-day grace period after case closure for forensics
- No cross-case sharing

**Semantic Search**:
- Same BGE-M3 embeddings as User KB
- Case-scoped search (only within current case)
- Used by `answer_from_case_evidence` tool

### 6.3 Collection Metadata

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

### 6.4 Lifecycle Management

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

### 6.5 Access Patterns

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

## 7. Global Knowledge Base Storage

### 7.1 Architecture Overview

**Purpose**: System-wide troubleshooting documentation shared across ALL users

**Three Knowledge Systems**:
1. **Global KB** - System-wide best practices (THIS SECTION)
2. **User KB** - User's personal runbooks (Section 5)
3. **Case Working Memory** - Temporary case uploads (Section 6)

**Storage**: ChromaDB (shared collection)
**Collection Naming**: `global_kb` (single shared collection)
**Implementation**: `faultmaven/tools/global_kb_qa.py`

### 7.2 Storage Characteristics

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

### 7.3 Document Structure

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

### 7.4 Tool Integration

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

### 7.5 Access Control

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

### 7.6 Performance Optimization

**Caching Strategy**:
- 7-day cache TTL (global KB changes rarely)
- Pre-computed embeddings for fast search
- Popular articles cached in Redis L2

**Search Performance**:
- Sub-200ms typical query time
- Pre-warmed cache for common queries
- Batch embedding generation for updates

---

## 8. Report & Analytics Data Storage

### 8.1 Architecture Overview

**Purpose**: Generated case reports, post-mortems, analytics dashboards

**Storage**: Hybrid Redis (metadata) + ChromaDB (content)
**Implementation**: `faultmaven/infrastructure/persistence/redis_report_store.py`

### 8.2 Storage Architecture

**Two-Tier Design**:

1. **Metadata Storage** (Redis)
   - Report metadata, version tracking, timestamps
   - Fast lookups and filtering
   - Current report pointers

2. **Content Storage** (ChromaDB)
   - Full markdown report content
   - Enables similarity search for related reports
   - Automatic runbook indexing

### 8.3 Redis Key Schema

```python
# Redis keys
case:{case_id}:reports                    # Sorted set (all reports by timestamp)
report:{report_id}:metadata               # Hash (report metadata)
case:{case_id}:reports:{type}             # Sorted set (reports by type, version desc)
case:{case_id}:reports:current            # Hash (type → current report_id)

# Example metadata
{
    "report_id": "rpt_abc123",
    "case_id": "case_456",
    "report_type": "post_mortem",
    "version": 2,
    "status": "published",
    "created_at": "2025-01-15T10:30:00Z",
    "created_by": "user_789",
    "chromadb_doc_id": "doc_abc123"  # Reference to content
}
```

### 8.4 Report Types

```python
class ReportType(str, Enum):
    POST_MORTEM = "post_mortem"          # Case post-mortem analysis
    RUNBOOK = "runbook"                  # Auto-generated runbook (indexed)
    ANALYTICS_DASHBOARD = "analytics"    # Analytics summary
    INVESTIGATION_SUMMARY = "summary"    # Investigation timeline
    TREND_REPORT = "trend"               # Pattern analysis
```

### 8.5 Report Structure

```python
class CaseReport(BaseModel):
    report_id: str
    case_id: str
    report_type: ReportType
    version: int
    status: str  # draft, published, archived

    # Content
    title: str
    content: str  # Markdown format
    summary: str

    # Metadata
    created_by: str
    created_at: datetime
    updated_at: datetime

    # Runbook-specific
    runbook_source: Optional[str]  # "manual", "ai_generated"
    auto_indexed: bool = False     # Auto-added to runbook KB?
```

### 8.6 Automatic Runbook Indexing

**Feature**: Generated runbooks automatically indexed for similarity search

```python
# Runbook generation and indexing
async def generate_runbook(case: Case) -> CaseReport:
    # Generate runbook from case
    runbook = await runbook_generator.generate(case)

    # Store in Redis + ChromaDB
    report = CaseReport(
        report_type=ReportType.RUNBOOK,
        content=runbook.content,
        runbook_source="ai_generated"
    )
    await report_store.save(report)

    # Auto-index in runbook KB for similarity search
    if runbook_kb:
        await runbook_kb.index_document(
            document_id=report.report_id,
            content=report.content,
            metadata={"case_id": case.case_id, "auto_generated": True}
        )
        report.auto_indexed = True

    return report
```

### 8.7 Report Versioning

**Strategy**: Keep up to 5 versions per report type

```python
# Version management
async def save_report_version(case_id: str, report: CaseReport):
    # Get current version count
    versions = await report_store.list_versions(case_id, report.report_type)

    if len(versions) >= 5:
        # Delete oldest version
        oldest = versions[0]
        await report_store.delete(oldest.report_id)

    # Increment version
    report.version = len(versions) + 1
    await report_store.save(report)
```

### 8.8 Retention Policy

**TTL**: 90 days post-case-closure

```python
# Cleanup job
async def cleanup_expired_reports():
    expired_cases = await case_repository.find_closed_before(
        datetime.now(timezone.utc) - timedelta(days=90)
    )

    for case in expired_cases:
        reports = await report_store.list_by_case(case.case_id)
        for report in reports:
            await report_store.delete(report.report_id)
            logger.info(f"Deleted expired report: {report.report_id}")
```

---

## 9. Job Queue State Storage

### 9.1 Architecture Overview

**Purpose**: Async background job tracking for long-running operations

**Storage**: Redis with TTL (24 hours default)
**Implementation**: `faultmaven/infrastructure/jobs/job_service.py`

### 9.2 Job Types

**Common Job Types**:
- File preprocessing (large uploads)
- Report generation
- Batch analytics
- Vector embedding generation
- Case archival
- Knowledge base indexing

### 9.3 Redis Schema

```python
# Redis key
job:{job_id}

# Job structure
{
    "job_id": "job_abc123",
    "job_type": "preprocessing",
    "status": "running",  # pending, running, completed, failed, cancelled
    "payload": {
        "file_id": "file_456",
        "case_id": "case_789"
    },
    "progress": 45,       # 0-100 percentage
    "result": null,       # Populated on completion
    "error": null,        # Populated on failure
    "created_at": "2025-01-15T10:30:00Z",
    "updated_at": "2025-01-15T10:31:15Z",
    "ttl_seconds": 86400
}
```

### 9.4 API Workflow

**202 → Location → 303/200 Pattern**:

```python
# Step 1: Create async job (202 Accepted)
POST /api/data/upload
Response: 202 Accepted
Location: /api/jobs/job_abc123
Retry-After: 5

# Step 2: Poll job status (200 OK, in progress)
GET /api/jobs/job_abc123
Response: 200 OK
{
    "job_id": "job_abc123",
    "status": "running",
    "progress": 45
}
Retry-After: 5

# Step 3: Job complete (303 See Other)
GET /api/jobs/job_abc123
Response: 303 See Other
Location: /api/cases/case_789/evidence/ev_456

# Step 4: Get final result (200 OK)
GET /api/cases/case_789/evidence/ev_456
Response: 200 OK
{
    "evidence_id": "ev_456",
    "preprocessing_summary": "..."
}
```

### 9.5 Job Service Interface

```python
class IJobService(ABC):
    async def create_job(
        self,
        job_type: str,
        payload: Dict[str, Any] = None,
        ttl_seconds: Optional[int] = None
    ) -> str:
        """Create new job, returns job_id"""

    async def get_job(self, job_id: str) -> Optional[JobStatus]:
        """Retrieve current job status"""

    async def update_job_status(
        self,
        job_id: str,
        status: str,
        progress: Optional[int] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None
    ) -> bool:
        """Update job status and metadata"""
```

### 9.6 Cleanup & TTL

**Automatic Cleanup**: Redis TTL handles expiration
**Manual Cleanup**: Background job cleans completed jobs after 1 hour

```python
# Cleanup job (runs hourly)
async def cleanup_completed_jobs():
    completed_jobs = await job_service.find_completed_before(
        datetime.now(timezone.utc) - timedelta(hours=1)
    )

    for job_id in completed_jobs:
        await job_service.delete(job_id)
```

---

## 10. ML Model Artifacts Storage

### 10.1 Architecture Overview

**Purpose**: Machine learning model weights, calibration data, feature metadata

**Model Types**:
- **Confidence Scoring Model**: Calibrated confidence prediction
- **Classification Models**: Data type classification
- **Anomaly Detection Models**: Behavioral pattern detection

**Storage**: File system (model files) + PostgreSQL (metadata)
**Implementation**: `faultmaven/services/analytics/confidence_service.py`

### 10.2 Storage Architecture

**Two-Tier Design**:

1. **Model Files** (File System / S3)
   - Serialized model weights (`.pkl` files)
   - Calibration data (`.json` files)
   - Training metadata

2. **Model Metadata** (PostgreSQL)
   - Model versions, timestamps
   - Calibration metrics (ECE, Brier score)
   - Feature definitions
   - Training provenance

### 10.3 File System Layout

```
/var/lib/faultmaven/models/
├── confidence/
│   ├── conf-v1.2/
│   │   ├── model_weights.pkl         # Serialized sklearn model
│   │   ├── calibrated_model.pkl      # Calibrated version
│   │   ├── calibration_data.json     # Calibration curves
│   │   ├── feature_definitions.json  # Feature schemas
│   │   └── training_metadata.json    # Training provenance
│   ├── conf-v1.3/                    # New version
│   └── conf-v1.4/
├── classification/
│   └── data_classifier_v2.pkl
└── anomaly/
    └── behavioral_detector_v1.pkl
```

### 10.4 Model Metadata Schema

```python
class ModelMetadata(BaseModel):
    model_id: str                   # e.g., "conf-v1.2"
    model_type: str                 # "confidence", "classification", "anomaly"
    version: str                    # Semantic version

    # Training info
    training_date: datetime
    training_samples: int
    training_duration_seconds: int

    # Calibration metrics
    calibration_method: str         # "platt", "isotonic"
    ece_score: float                # Expected Calibration Error
    brier_score: float              # Brier score
    log_loss: float                 # Logarithmic loss

    # Feature metadata
    feature_count: int
    feature_definitions: Dict[str, Any]

    # File references
    model_file_path: str
    calibration_file_path: str

    # Deployment info
    deployed_at: Optional[datetime]
    is_active: bool

    created_at: datetime
    created_by: str
```

### 10.5 Model Versioning

**Strategy**: Keep 3 most recent versions

```sql
CREATE TABLE ml_models (
    model_id TEXT PRIMARY KEY,
    model_type TEXT NOT NULL,
    version TEXT NOT NULL,
    training_date TIMESTAMP WITH TIME ZONE NOT NULL,
    calibration_method TEXT NOT NULL,
    ece_score FLOAT,
    brier_score FLOAT,
    model_file_path TEXT NOT NULL,
    deployed_at TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    UNIQUE(model_type, version)
);

CREATE INDEX idx_ml_models_type_active ON ml_models (model_type, is_active);
CREATE INDEX idx_ml_models_deployed ON ml_models (deployed_at DESC);
```

### 10.6 Hot-Swapping Models

**Interface**: `IGlobalConfidenceService.update_model()`

```python
async def update_model(model_data: bytes, version: str) -> bool:
    """
    Hot-swap confidence model without service downtime.

    Process:
    1. Validate model data structure
    2. Backup current model state
    3. Load and validate new model
    4. Test with validation dataset
    5. Atomic switch to new model
    6. Update metadata
    7. Clean up old versions (keep 3)
    """

    # Backup current model
    current_model = await load_active_model()
    await backup_model(current_model)

    # Load new model
    new_model = deserialize_model(model_data)
    validate_model_structure(new_model)

    # Test new model
    test_results = await test_model(new_model)
    if test_results.ece_score > 0.1:  # Too high calibration error
        return False

    # Atomic switch
    global _active_model
    _active_model = new_model

    # Update metadata
    await model_repository.save(ModelMetadata(
        model_id=f"conf-{version}",
        version=version,
        ece_score=test_results.ece_score,
        is_active=True
    ))

    # Cleanup old versions
    await cleanup_old_versions(keep_count=3)

    return True
```

### 10.7 Model Calibration Data

**Calibration Files**: Store calibration curves for probability calibration

```json
// calibration_data.json
{
    "method": "platt",
    "calibration_curve": [
        {"predicted": 0.1, "actual": 0.08},
        {"predicted": 0.2, "actual": 0.18},
        {"predicted": 0.5, "actual": 0.52},
        {"predicted": 0.8, "actual": 0.83},
        {"predicted": 0.9, "actual": 0.91}
    ],
    "ece_score": 0.034,
    "training_samples": 15000,
    "validation_samples": 3000
}
```

---

## 11. Protection System State Storage

### 11.1 Architecture Overview

**Purpose**: Client protection, rate limiting, reputation scoring, behavioral analysis

**Storage**: Redis (real-time state) + PostgreSQL (archive)
**Implementation**: `faultmaven/infrastructure/protection/`

### 11.2 Data Types

**a) Reputation Scores**

```python
# Redis key: reputation:{client_id}
{
    "client_id": "session_abc123",
    "score": 85,
    "level": "normal",  # trusted, normal, suspicious, restricted, blocked
    "compliance_score": 90,
    "efficiency_score": 85,
    "stability_score": 80,
    "reliability_score": 90,
    "trend": "improving",
    "violations": [
        {"type": "rate_limit", "severity": "low", "timestamp": "..."}
    ],
    "last_updated": "2025-01-15T10:30:00Z",
    "ttl_seconds": 86400
}
```

**b) Rate Limit State**

```python
# Redis keys
rate_limit:{client_id}:{endpoint}:{window}

# Example: rate_limit:session_123:/api/queries:60s
{
    "requests": 45,
    "limit": 100,
    "window_start": "2025-01-15T10:30:00Z",
    "reset_at": "2025-01-15T10:31:00Z",
    "burst_allowance": 20
}
```

**c) Behavioral Patterns**

```python
# Redis key: behavior:{client_id}
{
    "client_id": "session_abc123",
    "request_frequency": 12.5,  # requests/minute
    "peak_hours": [9, 10, 14, 15],
    "anomaly_score": 0.12,
    "risk_level": "low",
    "fingerprint": "browser_chrome_v120_mac",
    "session_duration_avg_seconds": 1847,
    "last_analyzed": "2025-01-15T10:30:00Z"
}
```

### 11.3 Access Patterns

```python
# Reputation check (< 5ms requirement)
reputation = await reputation_engine.calculate_reputation(client_id)
if reputation.level == ReputationLevel.BLOCKED:
    raise AccessDeniedError()

# Rate limit check
allowed = await rate_limiter.check_rate_limit(
    client_id=client_id,
    endpoint="/api/queries",
    limit=100,
    window_seconds=60
)

# Behavioral analysis
profile = await behavioral_analyzer.analyze_client(client_id)
if profile.risk_level == "high":
    await protection_coordinator.trigger_enhanced_monitoring(client_id)
```

### 11.4 Archive Strategy

**PostgreSQL Archive** (30 days retention):

```sql
CREATE TABLE protection_events (
    event_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    event_type TEXT NOT NULL,  -- reputation_change, rate_limit, anomaly
    severity TEXT NOT NULL,     -- low, medium, high, critical
    details JSONB NOT NULL,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE INDEX idx_protection_client ON protection_events (client_id, timestamp DESC);
CREATE INDEX idx_protection_type ON protection_events (event_type, severity);
```

### 11.5 Performance Requirements

**Real-Time Checks**:
- Reputation lookup: < 5ms (Redis)
- Rate limit check: < 3ms (Redis)
- Behavioral analysis: < 10ms (Redis)

**Archival**:
- PostgreSQL writes: Async (non-blocking)
- Archive TTL: 30 days
- Used for forensics and trend analysis

---

## 12. Cache Data Storage

### 12.1 Architecture Overview

**Purpose**: Multi-tier intelligent caching with usage pattern analysis

**Storage**: L1 (in-memory) + L2 (Redis) + L3 (PostgreSQL/S3)
**Implementation**: `faultmaven/infrastructure/caching/intelligent_cache.py`

### 12.2 Multi-Tier Architecture

```
┌──────────────────────────────────────────────────────┐
│ L1 Cache (In-Memory)                                 │
│ - Hit time: < 1ms                                    │
│ - Size: 100MB                                        │
│ - TTL: 5 minutes                                     │
│ - Use: Hot data (user profiles, active cases)       │
└─────────────────────┬────────────────────────────────┘
                      │ L1 miss
                      ↓
┌──────────────────────────────────────────────────────┐
│ L2 Cache (Redis)                                     │
│ - Hit time: < 5ms                                    │
│ - Size: 10GB                                         │
│ - TTL: 1 hour                                        │
│ - Use: Warm data (LLM responses, KB results)        │
└─────────────────────┬────────────────────────────────┘
                      │ L2 miss
                      ↓
┌──────────────────────────────────────────────────────┐
│ L3 Cache (PostgreSQL/S3)                             │
│ - Hit time: < 20ms                                   │
│ - Size: Unlimited                                    │
│ - TTL: 24 hours                                      │
│ - Use: Cold data (computed aggregations, reports)   │
└──────────────────────────────────────────────────────┘
```

### 12.3 Cached Data Types

**LLM Responses** (most expensive to regenerate):
- Cache key: `llm:{model}:{prompt_hash}`
- TTL: 1 hour
- Tier: L2 (Redis)

**User Profiles** (frequent access):
- Cache key: `user:{user_id}:profile`
- TTL: 15 minutes
- Tier: L1 (in-memory)

**Case Summaries** (derived data):
- Cache key: `case:{case_id}:summary`
- TTL: 30 minutes
- Tier: L2 (Redis)

**Knowledge Base Results** (reusable):
- Cache key: `kb:{query_hash}:results`
- TTL: 2 hours
- Tier: L2 (Redis)

**Analytics Dashboards** (computed aggregations):
- Cache key: `analytics:{dashboard_id}:{date}`
- TTL: 24 hours
- Tier: L3 (PostgreSQL)

### 12.4 Cache Entry Structure

```python
@dataclass
class CacheEntry:
    key: str
    value: Any
    created_at: datetime
    last_accessed: datetime
    access_count: int = 0
    size_bytes: int = 0
    ttl_seconds: Optional[int] = None
    tags: Set[str] = field(default_factory=set)
    semantic_hash: Optional[str] = None  # For similarity-based cache hits
    priority_score: float = 1.0
```

### 12.5 Cache Analytics

**Usage Pattern Analysis**:

```python
@dataclass
class AccessPattern:
    key_pattern: str
    access_times: List[datetime]
    access_frequency: float             # requests/hour
    seasonal_pattern: Dict[str, float]  # hour → frequency
    user_distribution: Dict[str, int]   # user_hash → count
    effectiveness_score: float          # hit_rate * avg_time_saved
    recommended_ttl: int
    cache_tier: str  # L1, L2, L3
```

**Example Analytics**:
```json
{
    "key_pattern": "llm_response:*",
    "access_frequency": 142.5,
    "seasonal_pattern": {
        "09:00": 0.8,  // Peak morning usage
        "14:00": 0.3   // Low afternoon usage
    },
    "effectiveness_score": 0.87,
    "recommended_ttl": 3600,
    "cache_tier": "L2"
}
```

### 12.6 Eviction Strategies

**L1 (In-Memory)**: LRU with size limit
**L2 (Redis)**: TTL-based with LRU fallback
**L3 (PostgreSQL/S3)**: TTL-based only

**Priority-Based Eviction**:
- High priority: User profiles, active case data
- Medium priority: LLM responses, KB results
- Low priority: Analytics, computed aggregations

---

## 13. System Operational Data

### 13.1 Architecture Overview

**Purpose**: System health, performance metrics, security audits, distributed tracing

**Storage**: Time-series DB + S3 + PostgreSQL
**Scope**: Infrastructure observability (may be out of application scope)

### 13.2 Data Types

**a) Metrics** (Time-Series DB)
- Request latency (p50, p95, p99)
- Error rates by endpoint
- Throughput (requests/second)
- Resource usage (CPU, memory)
- Database query performance

**b) Distributed Traces** (Opik/Jaeger)
- Request flow across services
- Span timing and relationships
- Error propagation
- Service dependency mapping

**c) Application Logs** (S3 + PostgreSQL)
- Structured JSON logs
- Error logs with stack traces
- Audit logs (retention: 1 year)
- Debug logs (retention: 7 days)

**d) Security Audit Logs** (PostgreSQL)
- Authentication events
- Authorization failures
- Data access logs
- Configuration changes
- Admin actions

### 13.3 Storage Architecture

```
┌─────────────────────────────────────────────────┐
│ Prometheus (Time-Series)                        │
│ - Metrics retention: 30 days                    │
│ - Aggregations: 5m, 1h, 1d                      │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│ Opik / Jaeger (Distributed Tracing)             │
│ - Trace retention: 7 days                       │
│ - Sampling rate: 100% (production: 10%)         │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│ S3 (Long-Term Logs)                             │
│ - Application logs: 90 days                     │
│ - Audit logs: 1 year                            │
│ - Compressed, encrypted                          │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│ PostgreSQL (Security Audits)                    │
│ - Authentication events: 1 year                 │
│ - Data access logs: 1 year                      │
│ - Compliance-ready format                       │
└─────────────────────────────────────────────────┘
```

### 13.4 Optional Scope Note

**This category may be out of application architecture scope** and belong to DevOps/SRE infrastructure. Consider whether to document in separate observability guide.

---

## 14. Access Patterns & Interfaces

### 14.1 Repository Pattern

All storage access follows **Repository Pattern** with interface abstraction.

### 14.2 Interface Summary

```python
# User storage
class UserRepository(ABC):
    async def save(self, user: User) -> User
    async def get(self, user_id: str) -> Optional[User]
    async def get_by_username(self, username: str) -> Optional[User]

# Case storage
class CaseRepository(ABC):
    async def save(self, case: Case) -> Case
    async def get(self, case_id: str) -> Optional[Case]
    async def find_by_user(self, user_id: str) -> List[Case]

# Session storage
class ISessionStore(ABC):
    async def get(self, key: str) -> Optional[Dict]
    async def set(self, key: str, value: Dict, ttl: Optional[int]) -> None
    async def exists(self, key: str) -> bool

# Vector storage (3 implementations)
class IVectorStore(ABC):
    async def add_documents(self, documents: List[Dict]) -> None
    async def search(self, query: str, k: int) -> List[Dict]
    async def delete_documents(self, ids: List[str]) -> None

# Report storage
class IReportStore(ABC):
    async def save(self, report: CaseReport) -> CaseReport
    async def get(self, report_id: str) -> Optional[CaseReport]
    async def list_by_case(self, case_id: str) -> List[CaseReport]

# Job queue
class IJobService(ABC):
    async def create_job(self, job_type: str, payload: Dict) -> str
    async def get_job(self, job_id: str) -> Optional[JobStatus]
    async def update_job_status(self, job_id: str, status: str) -> bool

# ML models
class IGlobalConfidenceService(ABC):
    async def score_confidence(self, request: ConfidenceRequest) -> ConfidenceResponse
    async def get_model_info(self) -> Dict[str, Any]
    async def update_model(self, model_data: bytes, version: str) -> bool
```

### 14.3 Dependency Injection

```python
from faultmaven.container import container

# Get repository instances
user_repo = container.get_user_repository()
case_repo = container.get_case_repository()
session_store = container.get_session_store()
user_kb_store = container.get_user_kb_vector_store()
case_vector_store = container.get_case_vector_store()
global_kb_store = container.get_global_kb_vector_store()
report_store = container.get_report_store()
job_service = container.get_job_service()
confidence_service = container.get_confidence_service()
```

---

## 15. Data Retention & Lifecycle

### 15.1 Retention Policies

| Data Category | Retention Period | Cleanup Strategy |
|---------------|------------------|------------------|
| **User Accounts** | Indefinite (soft delete) | Soft delete after 30 days inactive (configurable) |
| **Active Cases** | Indefinite | User-controlled closure |
| **Resolved Cases** | 1 year default | Archive to cold storage after 90 days |
| **Session State** | 30 minutes (TTL) | Automatic Redis expiration |
| **Raw Artifacts** | 90 days default | S3 lifecycle policy |
| **User Knowledge Base** | Indefinite | User-controlled deletion |
| **Case Working Memory** | Case lifetime + 7 days | TTL-based cleanup after case closure |
| **Global Knowledge Base** | Indefinite | Admin-controlled updates |
| **Reports & Analytics** | 90 days post-closure | Automatic cleanup job |
| **Job Queue State** | 24 hours | Redis TTL expiration |
| **ML Model Artifacts** | 3 versions retained | Version-based cleanup |
| **Protection State** | Real-time + 30 days archive | Archive to PostgreSQL |
| **Cache Data** | Minutes to hours (TTL) | Multi-tier eviction |
| **Audit Logs** | 1 year | Archive to S3 after 90 days |

### 15.2 Automated Cleanup Jobs

**Daily Tasks**:
- Expire old sessions (Redis TTL handles most)
- Archive resolved cases older than 90 days
- Delete raw artifacts past retention period
- Clean up expired case vector store collections
- Delete expired job queue entries
- Vacuum PostgreSQL tables

**Weekly Tasks**:
- Reindex for performance
- Backup validation
- Storage usage reporting
- Model version cleanup

---

## 16. Security & Compliance

### 16.1 Data Privacy

**PII Redaction**:
- All user input sanitized before LLM processing
- Presidio integration for advanced PII detection
- Fallback regex patterns
- Configurable sensitivity levels

**Encryption**:
- **At Rest**: AES-256 for S3, PostgreSQL, Redis
- **In Transit**: TLS 1.3 for all network communication
- **Secrets**: Environment variables, HashiCorp Vault

### 16.2 Access Control

**User Data Isolation**:
- Row-level security (RLS) in PostgreSQL
- Foreign key constraints enforce ownership
- User can only access own data

**RBAC**:
- User roles: `user`, `admin`, `analyst`
- Admin: Full system access
- Analyst: Read-only case access
- User: Own data only

### 16.3 Audit Trail

**Immutable Logs**:
- `case_status_transitions`: All status changes
- `agent_tool_calls`: All agent actions
- `protection_events`: Security events
- User authentication events

**Compliance**:
- GDPR: Right to deletion, data export
- SOC 2: Audit trails, encryption, access control
- HIPAA-ready: Additional PHI redaction

---

## 17. Scalability & Performance

### 17.1 Horizontal Scaling

**PostgreSQL**:
- Read replicas for query distribution
- Table partitioning (cases, messages, evidence)
- Connection pooling (PgBouncer)

**Redis**:
- Cluster mode for high availability
- Sharding by key prefix
- Sentinel for automatic failover

**ChromaDB**:
- Per-user collections enable partitioning
- Collection-level isolation prevents hotspots

**S3**:
- Infinite horizontal scalability
- CDN for frequently accessed artifacts

### 17.2 Performance Targets

| Operation | Target | Measured |
|-----------|--------|----------|
| User authentication | < 50ms | 30ms avg |
| Case load (full) | < 20ms | 10ms avg |
| Session get/set | < 5ms | 2ms avg |
| Evidence query | < 10ms | 5ms avg |
| KB semantic search | < 200ms | 150ms avg |
| File preprocessing | < 30s | 5s median, 25s p95 |
| Reputation check | < 5ms | 3ms avg |
| Rate limit check | < 3ms | 1ms avg |
| Cache L1 hit | < 1ms | 0.5ms avg |
| Cache L2 hit | < 5ms | 3ms avg |

---

## 18. Migration & Backup

### 18.1 Database Migrations

**Tool**: Alembic (PostgreSQL schema migrations)

```bash
# Create migration
alembic revision --autogenerate -m "Add ML model metadata table"

# Apply migration
alembic upgrade head

# Rollback
alembic downgrade -1
```

### 18.2 Backup & Recovery

**PostgreSQL**:
- Continuous WAL archiving to S3
- Daily full backups
- Point-in-time recovery (PITR)
- 30-day backup retention

**Redis**:
- RDB snapshots every 15 minutes
- AOF (Append-Only File) for durability
- Daily backups to S3

**ChromaDB**:
- Collection exports to S3 daily
- Metadata backup to PostgreSQL

**S3**:
- Object versioning enabled
- Cross-region replication

### 18.3 Disaster Recovery

**RTO**: < 1 hour
**RPO**: < 15 minutes

**Recovery Procedures**:
1. Database failure → Promote read replica (< 5 min)
2. Redis failure → Sentinel failover (< 1 min)
3. S3 failure → Cross-region replication (automatic)
4. Complete region failure → Restore from backups (< 60 min)

---

## Appendix A: Complete Schema Diagrams

### A.1 PostgreSQL Schema (cases_db)

```
cases (main)
├── evidence (1:N)
├── hypotheses (1:N)
├── solutions (1:N)
├── case_messages (1:N)
├── uploaded_files (1:N)
├── case_status_transitions (1:N)
├── case_tags (M:N)
└── agent_tool_calls (1:N)
```

### A.2 Redis Key Namespaces

```
session:{session_id}                          # Session state
session_client:{user_id}:{client_id}         # Client index
job:{job_id}                                  # Job queue
case:{case_id}:reports                        # Report metadata
report:{report_id}:metadata                   # Report details
rate_limit:{client_id}:{endpoint}:{window}   # Rate limits
reputation:{client_id}                        # Reputation scores
behavior:{client_id}                          # Behavioral patterns
cache:{tier}:{key}                            # Cache entries
```

### A.3 ChromaDB Collections

```
user_kb_{user_id}    # Per-user knowledge base (permanent)
case_{case_id}       # Per-case working memory (ephemeral)
global_kb            # System-wide knowledge base (shared)
reports              # Report content storage
```

---

## Document Version History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2025-01-12 | FaultMaven Team | Initial design (4 categories) |
| 2.0 | 2025-01-12 | FaultMaven Team | Expanded to 12 categories, 8 data types, complete architecture |

## References

- Investigation Architecture Specification v2.0
- Data Preprocessing Design Specification v2.0
- Case Storage Design
- User Storage Design
- Evidence Architecture v1.1
- Agentic Framework Documentation
