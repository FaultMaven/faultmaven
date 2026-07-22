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

For the data-type × backend × deployment breakdown, see:

- **[README.md](./README.md)** — index-level summary
- **[repository-pattern.md](./repository-pattern.md)** §3 — full per-data-type detail

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
             │    └──> PostgreSQL (cases_db.* hybrid schema)
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
│   - cases_db: Investigation data, evidence, hypotheses, reports       │
│                                                                        │
│ Redis (real or FakeRedis for local deployment):                        │
│   - Session state (session:{id}, idle timeout 30 min / record TTL 24h)│
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
│   Scope isolation: faultmaven_kb queries use metadata filters        │
│   (scope/owner_id/team_id). See vector-storage.md §1.1, §4.2, §1.3  │
│   for collection design, scope-filter examples, and ingestion detail.│
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

Core repository interface signatures live in **[repository-pattern.md](./repository-pattern.md)**:

- `CaseRepository` (>30 methods; §4.1 illustrates the core 11) — §4.1
- `ISessionStore` — §4.2
- `IVectorStore` — §4.3

`UserRepository` is defined in `faultmaven/infrastructure/persistence/user_repository.py`. `IJobService` and `IGlobalConfidenceService` are defined in `faultmaven/models/interfaces.py`. These interfaces are not documented separately in repository-pattern.md.

Reports are persisted via the Case repository (see `modules/case/contracts.py` — the legacy `IReportStore` interface was removed when report storage migrated to PostgreSQL under TD-001).

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

For auth/session TTL detail (JWT lifetimes, session record TTL, inactivity timeout) see [schemas/user-schema.md §5.3](./schemas/user-schema.md#53-session-ttl-strategy).

| Data Category | Retention Period | Cleanup Strategy |
|---------------|------------------|------------------|
| **User Accounts** | Indefinite (soft delete) | Soft delete after 30 days inactive (configurable) |
| **Active Cases** | Indefinite | User-controlled closure |
| **Resolved Cases** | 1 year default | Archive to cold storage after 90 days |
| **Session State** | 30 min idle / 24 h record TTL | Automatic Redis expiration |
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

- `case_actions`: All case-level actions and status changes (ORM model `CaseActionModel`)
- `agent_tool_calls` / `agent_tool_calls_v2`: All agent actions
- `user_audit_log`: User authentication and administrative events

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

- Unified `faultmaven_kb` collection — isolation via scope/owner/team metadata filters (not per-user collections)
- HNSW index per collection; external ChromaDB server supported for horizontal scale

**S3**:
- Infinite horizontal scalability
- CDN for frequently accessed artifacts

### Performance Targets

Summary targets below; per-backend latency breakdown is in [repository-pattern.md §9.1](./repository-pattern.md#91-performance-by-data-type-and-backend).

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

This overview provides the high-level architecture story. See **[README.md](./README.md)** for the navigation index of all storage docs.

---

## Related Documentation

- **Security**: [../security/iam-design.md](../security/iam-design.md) - Identity and Access Management
- **AI/RAG**: [../knowledge-and-ai/knowledge-base-architecture.md](../knowledge-and-ai/knowledge-base-architecture.md) - RAG pipeline and embeddings
- **Investigation**: [../investigation-engine/milestone-based-investigation-framework.md](../investigation-engine/milestone-based-investigation-framework.md) - Case lifecycle
