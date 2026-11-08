# FaultMaven Data Storage Architecture

## Executive Summary

FaultMaven's storage architecture follows a **storage polyglot** approach, selecting the technology for each data type's access patterns and lifecycle. The list of stores, with what each holds, its owners, its tenant, its writer, its ends of life and its retention bound, is the **[store registry](./store-registry.md)** (Principle 13.1); this overview does not repeat it.

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

The diagram shows access paths. The stores themselves, and how each one's data ends, are in the [store registry](./store-registry.md).

```
┌───────────────────────────────────────────────────────────────────────┐
│                       APPLICATION LAYER                                │
│                  (FastAPI + Service Layer)                            │
└────────────┬──────────────────────────────────────────────────────────┘
             │
             ├──> UserRepository Interface
             │    └──> PostgreSQL (users table)
             │
             ├──> CaseRepository Interface
             │    └──> PostgreSQL (cases + case-domain tables, hybrid schema)
             │
             ├──> ISessionStore Interface
             │    ├──> Redis (primary, TTL-based)
             │    └──> PostgreSQL (archive, optional)
             │
             ├──> IKnowledgeVectorStore Interface (global KB)
             │    └──> KnowledgeVectorStore: ChromaDB (faultmaven_kb)
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
             └──> IStorageBackend Interface (Artifacts)
                  └──> S3 (raw uploaded files)

┌───────────────────────────────────────────────────────────────────────┐
│                     INFRASTRUCTURE LAYER                               │
├───────────────────────────────────────────────────────────────────────┤
│ PostgreSQL (one database; SQLite in standalone):                      │
│   - Users, organizations, teams, roles, SSO mappings                  │
│   - Cases, evidence, hypotheses, reports, knowledge items             │
│                                                                        │
│ Redis (real or FakeRedis for local deployment):                        │
│   - Session state (session:{id}, TTL 30 min past its last write)     │
│   - Job records (job:{id}, TTL 24 hours; nothing writes one today)    │
│   - Report metadata now in PostgreSQL (reports table)                 │
│   - Protection state (rate limit windows, dedup keys)                │
│   - Token revocation (JTI tracking with TTL)                          │
│   - Request deduplication (content-hash with Lua scripts)             │
│   NOTE: Local deployment uses FakeRedis (in-process, full API parity) │
│                                                                        │
│ ChromaDB KB Instance (PersistentClient at data/chroma-kb/):           │
│   - faultmaven_kb: the instance's ONLY collection. All KB documents  │
│     (filtered by metadata: scope, owner_id — team shares resolve to  │
│     an id allowlist at query time) AND runbooks, which have no       │
│     collection of their own and are told apart only by              │
│     document_type="runbook"                                          │
│   Lifecycle: permanent — backed up, never wiped.                     │
│                                                                        │
│ ChromaDB Evidence Instance (PersistentClient at data/chroma-evidence/):│
│   - case_{case_id}: Per-case evidence (dynamic, ephemeral —         │
│     created on first upload, deleted with the case)                  │
│   Lifecycle: ephemeral — excluded from backups, safe to wipe.        │
│                                                                        │
│   Two separate ChromaDB clients created in DI container:             │
│   - kb_chromadb_client → data/chroma-kb/ (permanent KB data)         │
│   - evidence_chromadb_client → data/chroma-evidence/ (ephemeral)     │
│   Cloud: both use HttpClient to external ChromaDB server             │
│                                                                        │
│   Scope isolation: faultmaven_kb queries use metadata filters        │
│   (scope/owner_id; team shares resolve to an id allowlist at query  │
│   time, never stored). See vector-storage.md §1.1, §4.2, §1.3       │
│   for collection design, scope-filter examples, and ingestion detail.│
│                                                                        │
│ Object storage (data/evidence/, S3 or Azure):                         │
│   - Uploaded files: {enterprise_id}/{case_id}/{date}/{uuid}_{name}    │
└───────────────────────────────────────────────────────────────────────┘
```

---

## Access Patterns & Interfaces

All storage follows the **Repository Pattern** with interface abstraction for testability and flexibility.

### Interface Summary

Core repository interface signatures live in **[repository-pattern.md](./repository-pattern.md)**:

- `CaseRepository` (>30 methods; §4.1 illustrates the core 11) — §4.1
- `ISessionStore` — §4.2
- `IKnowledgeVectorStore` — §4.3

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

Each store's end of life per owner kind and its retention bound are in the [store registry](./store-registry.md). For auth/session TTL detail (JWT lifetimes, the session TTL) see [schemas/user-schema.md §5.3](./schemas/user-schema.md#53-session-management).

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
| Rate limit check | < 3ms | 1ms avg |

---

## Documentation Structure

This overview provides the high-level architecture story. See **[README.md](./README.md)** for the navigation index of all storage docs.

---

## Related Documentation

- **Security**: [../security/iam-design.md](../security/iam-design.md) - Identity and Access Management
- **AI/RAG**: [../knowledge-and-ai/knowledge-base-architecture.md](../knowledge-and-ai/knowledge-base-architecture.md) - RAG pipeline and embeddings
- **Investigation**: [../investigation-engine/milestone-based-investigation-framework.md](../investigation-engine/milestone-based-investigation-framework.md) - Case lifecycle
