# FaultMaven Data Storage Architecture

## Executive Summary

FaultMaven's storage architecture supports **12 data categories** across primary application data and operational infrastructure. The architecture follows a **storage polyglot** approach, selecting the optimal technology for each data type's access patterns and lifecycle requirements.

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

## Storage Technology Matrix

| Data Category | Primary Storage | Secondary/Cache | Lifecycle | Scope |
|--------------|----------------|-----------------|-----------|-------|
| **User Information** | PostgreSQL | - | Indefinite (soft delete) | Per-user |
| **Case Data** | PostgreSQL | Redis (state) | 1 year default | Per-case |
| **Observability Data** | PostgreSQL + S3 | - | 90 days | Per-case |
| **User Knowledge Base** | ChromaDB | PostgreSQL (metadata) | Indefinite | Per-user |
| **Case Working Memory** | ChromaDB | - | Case lifetime + 7 days | Per-case |
| **Global Knowledge Base** | ChromaDB | - | Indefinite | System-wide |
| **Report & Analytics** | PostgreSQL | - | Persistent (linked to case lifecycle) | Per-case |
| **Job Queue State** | Redis | - | 24 hours (TTL) | Per-job |
| **ML Model Artifacts** | File system | PostgreSQL (metadata) | 3 versions retained | System-wide |
| **Protection State** | Redis | PostgreSQL (archive) | Real-time + 30 days | Per-client |
| **Cache Data** | Multi-tier | - | Minutes to hours (TTL) | Various |
| **System Operational** | Time-series DB + S3 | - | 90 days - 1 year | System-wide |

---

## Storage Architecture Diagram

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
             ├──> IVectorStore Interface (global KB)
             │    └──> ChromaDBVectorStore: ChromaDB (faultmaven_kb)
             │
             ├──> CaseVectorStore (per-case evidence)
             │    └──> ChromaDB (case_{case_id}, dynamic collections)
             │
             ├──> ICaseRepository Interface (Report methods)
             │    └──> PostgreSQL (reports table, FK to cases)
             │         # TD-001: Migrated from Redis + ChromaDB (ephemeral)
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
│ Redis (real or FakeRedis for local deployment):                        │
│   - Session state (session:{id}, TTL: 30 min)                         │
│   - Job queue (job:{id}, TTL: 24 hours)                               │
│   - Report metadata now in PostgreSQL (reports table)                 │
│   - Protection state (reputation, rate limits)                        │
│   - Cache L2 (multi-tier caching)                                     │
│   - Token revocation (JTI tracking with TTL)                          │
│   - Request deduplication (content-hash with Lua scripts)             │
│   NOTE: Local deployment uses FakeRedis (in-process, full API parity) │
│                                                                        │
│ ChromaDB KB Instance (PersistentClient at data/chroma-kb/):           │
│   - faultmaven_kb: All KB documents (global/personal/team scope,     │
│     filtered by metadata: scope, owner_id, team_id)                  │
│   - faultmaven_runbooks: Runbook similarity search (report_type,     │
│     domain metadata — used for "this looks like runbook X")          │
│   - knowledge_items: Knowledge module items (organization_id,        │
│     item_type, category — used by KnowledgeSearchService)            │
│   Lifecycle: permanent — backed up, never wiped.                     │
│                                                                        │
│ ChromaDB Evidence Instance (PersistentClient at data/chroma-evidence/):│
│   - case_{case_id}: Per-case evidence (dynamic, ephemeral —         │
│     created on first upload, deleted on case close)                  │
│   Lifecycle: ephemeral — excluded from backups, safe to wipe.        │
│                                                                        │
│   Two separate ChromaDB clients created in DI container:             │
│   - kb_chromadb_client → data/chroma-kb/ (permanent KB data)         │
│   - evidence_chromadb_client → data/chroma-evidence/ (ephemeral)     │
│   Cloud: both use HttpClient to external ChromaDB server             │
│                                                                        │
│   Scope isolation on faultmaven_kb uses metadata filtering:          │
│   - global_kb_qa tool: {"scope": "global"}                           │
│   - user_kb_qa tool: {"scope": "personal", "owner_id": user_id}     │
│   - Team KB: {"scope": "team", "team_id": {"$in": team_ids}}        │
│                                                                        │
│   KB config layer returns logical names (global_kb, user_{id}_kb)    │
│   which map to faultmaven_kb with metadata filters, NOT separate     │
│   collections.                                                        │
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

## Access Patterns & Interfaces

All storage follows the **Repository Pattern** with interface abstraction for testability and flexibility.

### Interface Summary

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

# Vector storage (ChromaDB — shared client, multiple collections)
class IVectorStore(ABC):
    async def add_documents(self, documents: List[Dict]) -> None
    async def search(self, query: str, k: int) -> List[Dict]
    async def delete_documents(self, ids: List[str]) -> None

# Report storage (via Case repository - TD-001 migration)
# Reports are now stored via ICaseRepository methods (see Case module contracts)
# Legacy: IReportStore interface deprecated, use Case repository instead

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

### Dependency Injection

```python
from faultmaven.container import container

# Get service instances via DI container
case_repo = container.get_service("case_repository")
session_store = container.get_service("session_store")
vector_store = container.get_service("vector_store")        # ChromaDBVectorStore (faultmaven_kb)
case_vector_store = container.case_vector_store              # CaseVectorStore (case_{id} collections)
redis_client = container.redis_client                        # Real Redis or FakeRedis
kb_chromadb_client = container.kb_chromadb_client            # KB ChromaDB client (permanent)
evidence_chromadb_client = container.evidence_chromadb_client # Evidence ChromaDB client (ephemeral)
```

See [repository-pattern.md](./repository-pattern.md) for detailed abstraction layer specification.

---

## Data Retention & Lifecycle

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
| **Reports & Analytics** | Persistent (linked to case lifecycle) | Cascade delete with case (TD-001) |
| **Job Queue State** | 24 hours | Redis TTL expiration |
| **ML Model Artifacts** | 3 versions retained | Version-based cleanup |
| **Protection State** | Real-time + 30 days archive | Archive to PostgreSQL |
| **Cache Data** | Minutes to hours (TTL) | Multi-tier eviction |
| **Audit Logs** | 1 year | Archive to S3 after 90 days |

### Automated Cleanup Jobs

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

## Security & Compliance

### Data Privacy

**PII Redaction**:
- All user input sanitized before LLM processing
- Presidio integration for advanced PII detection
- Fallback regex patterns
- Configurable sensitivity levels

**Encryption**:
- **At Rest**: AES-256 for S3, PostgreSQL, Redis
- **In Transit**: TLS 1.3 for all network communication
- **Secrets**: Environment variables, HashiCorp Vault

### Access Control

**User Data Isolation**:
- Row-level security (RLS) in PostgreSQL
- Foreign key constraints enforce ownership
- User can only access own data

**RBAC**:
- User roles: `user`, `admin`, `analyst`
- Admin: Full system access
- Analyst: Read-only case access
- User: Own data only

### Audit Trail

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

## Scalability & Performance

### Horizontal Scaling

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

### Performance Targets

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

## Documentation Structure

This overview provides the high-level architecture. For detailed schema specifications, see:

- **[schemas/case-schema.md](./schemas/case-schema.md)** - Complete case data model (10 PostgreSQL tables)
- **[schemas/user-schema.md](./schemas/user-schema.md)** - User accounts, roles, and SSO integration
- **[schemas/knowledge-schema.md](./schemas/knowledge-schema.md)** - Vector storage for KB and working memory
- **[vector-storage.md](./vector-storage.md)** - ChromaDB implementation and operations
- **[repository-pattern.md](./repository-pattern.md)** - Storage abstraction layer specification
- **[sqlmodel-analysis.md](./sqlmodel-analysis.md)** - SQLModel ORM usage and patterns

For complete implementation details covering all 12 data categories, see [data-storage-design.md](./data-storage-design.md) (comprehensive reference).

---

## Related Documentation

- **Security**: [../security/iam-design.md](../security/iam-design.md) - Identity and Access Management
- **AI/RAG**: [../knowledge-and-ai/knowledge-base-architecture.md](../knowledge-and-ai/knowledge-base-architecture.md) - RAG pipeline and embeddings
- **Investigation**: [../investigation-engine/milestone-based-investigation-framework.md](../investigation-engine/milestone-based-investigation-framework.md) - Case lifecycle
