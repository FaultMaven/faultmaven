# Database Abstraction Layer Specification v2.5

**Document Purpose**: Define the pluggable storage architecture that enables FaultMaven to switch between storage backends via configuration without code changes, across multiple data types and storage technologies.

**Status**: ✅ Production Implementation
**Version**: 2.5.0
**Last Updated**: 2026-04-25
**Alignment**:

- Investigation Architecture v2.0 (Milestone-Based)
- Case Model Design v2.0
- Current Implementation (faultmaven/modules/case/infrastructure/)

**Critical Updates**:

- ✅ Two-dimensional storage architecture (backend × data type)
- ✅ Multiple storage technologies (PostgreSQL/SQLite, Redis/FakeRedis, ChromaDB)
- ✅ Pluggable adapters for each data type
- ✅ Configuration-based backend selection per storage system
- ✅ 13-method `CaseRepository` interface
- ✅ `PostgreSQLHybridCaseRepository` is the sole PostgreSQL case-repository implementation (legacy `PostgreSQLCaseRepository` class removed)
- ✅ `InMemoryVectorStore` removed — ChromaDB `PersistentClient` is always available
- ✅ **(v2.3, 2026-04-24)** `save(case)` is **purely additive** — the aggregate save no longer performs `DELETE ... NOT IN (in_memory_ids)` on sibling tables. Intentional deletion is explicit via scoped repository methods. See [§4.1.1 Aggregate save semantics](#411-aggregate-save-semantics).
- ✅ **(v2.4, 2026-04-24)** `save(case)` enforces **optimistic concurrency control** via a `version` column on the `cases` table. Read-modify-write paths use the `update_case_with_retry` helper; turn submission surfaces 409 Conflict on mismatch rather than retrying (LLM calls are non-idempotent). See [§4.1.2 Optimistic concurrency control](#412-optimistic-concurrency-control).
- ✅ **(v2.5, 2026-04-25)** Case repository hierarchy **consolidated**. The duplicate hierarchy at `faultmaven/infrastructure/persistence/{case_repository,database_case_repository}.py` was deleted; the canonical types live at `faultmaven/modules/case/infrastructure/{case_repository,sqlite_case_repository,postgresql_hybrid_case_repository,sessionless_case_repository}.py`. The redundant generic `DatabaseCaseRepository` (incomplete in the new hierarchy, ORM-merge-based in the old) was also removed — production paths use `SessionlessCaseRepository` (auto-session) or `get_repository_for_session(session)` (existing session). The `Evidence.da_invocation_count` Pydantic field was removed at the same time — it had no DB column and never round-tripped.

> **Reality check (2026-04-18)**: the examples below occasionally use values like `CASE_STORAGE_TYPE=postgres` or `CASE_STORAGE_TYPE=sqlite` as shorthand for deployment modes. **Those values are not recognized by the code.** The actual selector at [repository_factory.py:62-63](../../../faultmaven/infrastructure/persistence/repository_factory.py#L62-L63) accepts only `inmemory` or `database`. When `CASE_STORAGE_TYPE=database`, the SQL dialect (SQLite vs PostgreSQL) is determined by `DATABASE_URL` — see `sqlite+aiosqlite://...` vs `postgresql+asyncpg://...`. `SessionlessCaseRepository` detects the dialect at runtime and routes to `SQLiteCaseRepository` or `PostgreSQLHybridCaseRepository` accordingly. For the schema-level policy that goes with this runtime routing, see [Deployment-Aware Schema Strategy](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md) (internal) which defines Tier 1 (both dialects) vs Tier 2 (PostgreSQL augmentations).

---

> **v2.1 locked design (2026-04-19)**: the storage redesign removes several repositories and methods documented further down in this doc. Treat the list below as authoritative when there is a conflict with later sections (which will be brought into line as time permits):
>
> | Repository / method | Status | Reason |
> | --- | --- | --- |
> | `EvidenceService` (`modules/evidence/.../evidence_service.py`) | **DELETED** | Standalone-evidence path is functionally a black hole — accepts uploads, never read by the investigation engine. See [Deployment-Aware Schema Strategy](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md) §7.2. |
> | `APIEvidenceArtifactService` (`modules/evidence/.../evidence_artifact_service.py`) | **DELETED** | Same. Wrote to `evidence_artifacts` (now deleted). |
> | `EvidenceArtifactRepository` (`infrastructure/persistence/evidence_artifact_repository.py`) | **DELETED** | Targets the deleted `evidence_artifacts` table. |
> | `ICaseRepository` methods: `create_standalone_evidence`, `get_standalone_evidence`, `list_standalone_evidence`, `delete_standalone_evidence`, `link_standalone_evidence_to_case`, `set_primary_evidence` | **REMOVED from contract** | The single-table evidence model has `case_id NOT NULL` FK. There is no "standalone" concept — evidence is always created in a case context. New methods: `add_evidence(case_id, evidence)`, `get_evidence(case_id, evidence_id)`, `list_evidence_for_case(case_id)`, `delete_evidence(case_id, evidence_id)`. |
> | `SessionRepository` / `DatabaseSessionRepository` (auth `sessions` SQL table) | **DELETED** | Per [case-and-session-concepts.md](../case-and-session/case-and-session-concepts.md) v2.1, sessions are Redis-only. The SQL `sessions` table is anti-pattern. Auth sessions live in `RedisSessionStore` (FakeRedis local, real Redis cloud). |
> | `PostgreSQLKBDocumentRepository` (`infrastructure/persistence/kb_document_repository.py`) | **DELETED** | Orphan — queries `kb_documents`, `kb_document_shares`, `kb_document_team_shares` tables that never existed in `models.py` or migrations. |
> | `agent_tool_calls` v1 ORM model (`AgentToolCallModel`) and `CaseModel.tool_calls` relationship | **DELETED** | Zero functional usage. v2 (`agent_tool_calls_v2`) is renamed to canonical `agent_tool_calls`. |
> | `EvidenceStorageAdapter.store_file()` fake-`standalone-{uuid}` case_id path | **DELETED** | Was scaffolding for the deleted standalone API. |
>
> **What stays**: the case-tied data submission path (`POST /api/v1/cases/{case_id}/turns`) is unaffected. It writes through the milestone engine to the `evidence` table directly via `case.evidence` (no `EvidenceService` involvement). Agent tools (`search_file`, `read_file`, `list_evidence`, `vectorize_file`) keep their **Path 2** code (case-embedded evidence lookup); the **Path 1** standalone fallback is removed.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Two-Dimensional Storage Architecture](#2-two-dimensional-storage-architecture)
3. [Storage Technologies by Data Type](#3-storage-technologies-by-data-type)
4. [Repository Pattern by Data Type](#4-repository-pattern-by-data-type)
5. [Storage Backend Options](#5-storage-backend-options)
6. [Configuration Management](#6-configuration-management)
7. [Error Handling Strategy](#7-error-handling-strategy)
8. [Testing Strategy](#8-testing-strategy)
9. [Performance Considerations](#9-performance-considerations)
10. [Appendices](#10-appendices)

---

## 1. Executive Summary

### 1.1 Purpose

The Database Abstraction Layer (DAL) provides a **dual-dimensional pluggable storage architecture**:

**Dimension 1 - Data Types** (What we store):
- Long-term persistent data (cases, users)
- Cached ephemeral data (sessions, temporary state)
- Vector embeddings (knowledge base, semantic search)

**Dimension 2 - Storage Backends** (Where we store):
- In-memory (Python modules) - Development/testing
- Local files (SQLite, JSON) - Single-node deployment (future)
- Microservices (K8s cluster) - Production distributed systems

### 1.2 Design Objectives

1. **Data Type Separation**: Different storage technologies for different data types
2. **Backend Flexibility**: Each storage technology can run on different backends
3. **Configuration-Based**: Switch backends via `.env` without code changes
4. **Clean Abstraction**: Business logic never depends on storage implementation

### 1.3 Two-Dimensional Architecture

```
                      STORAGE BACKENDS (Dimension 2)
                      ↓              ↓              ↓
DATA TYPES     ┌──────────────┬──────────────┬──────────────┐
(Dimension 1)  │  In-Memory   │ Local Files  │ Microservices│
               │  (Python)    │ (Filesystem) │   (K8s)      │
───────────────┼──────────────┼──────────────┼──────────────┤
Long-term      │ Python dict  │ SQLite file  │ PostgreSQL   │
(Cases/Users)  │ ✅ Impl.     │ ✅ Impl.     │ ✅ Impl.     │
               │              │              │              │
Technology:    │ InMemory     │ SQLite       │ PostgreSQL   │
               │ Repository   │ Repository   │ Repository   │
───────────────┼──────────────┼──────────────┼──────────────┤
Cached         │ FakeRedis    │ FakeRedis    │ Redis        │
(Sessions)     │ ✅ Impl.     │ ✅ Impl.     │ ✅ Impl.     │
               │              │              │              │
Technology:    │ Redis        │ Redis        │ Redis        │
               │ SessionStore │ SessionStore │ SessionStore │
               │ (FakeRedis)  │ (FakeRedis)  │ (real Redis) │
───────────────┼──────────────┼──────────────┼──────────────┤
Vector         │     n/a      │ ChromaDB     │ ChromaDB     │
(Knowledge)    │              │ ✅ Impl.     │ ✅ Impl.     │
               │              │              │              │
Technology:    │     n/a      │ ChromaDB     │ ChromaDB     │
               │              │ (local)      │ (server)     │
└──────────────┴──────────────┴──────────────┴──────────────┘

Configuration Example:
  # Long-term data storage — actual selector values: "inmemory" | "database"
  CASE_STORAGE_TYPE=database       # "inmemory" for dev/tests
  USER_STORAGE_TYPE=database       # "inmemory" for dev/tests

  # SQL dialect (SQLite vs PostgreSQL) is routed from DATABASE_URL:
  # DATABASE_URL=sqlite+aiosqlite:///./data/faultmaven.db        # local
  # DATABASE_URL=postgresql+asyncpg://user:pass@host/faultmaven  # cloud

  # Cached data storage (Redis or FakeRedis — auto-selected)
  # REDIS_HOST=redis.local        # set to use real Redis

  # Vector data storage (ChromaDB only — PersistentClient or HttpClient)
  VECTOR_STORAGE_TYPE=chromadb     # set CHROMADB_URL to use external server
```

### 1.4 Key Design Principles

| Principle | Implementation |
|-----------|----------------|
| **Separation by Data Type** | Different storage technologies for different data requirements |
| **Pluggable Backends** | Each storage technology has multiple backend options |
| **Independent Configuration** | Each data type configured separately |
| **Technology-Appropriate** | PostgreSQL for relational, Redis for caching, ChromaDB for vectors |
| **Abstraction Layers** | Repository/Store interfaces hide implementation details |

---

## 2. Two-Dimensional Storage Architecture

### 2.1 Architectural Overview

```
┌─────────────────────────────────────────────────────────────┐
│                  Application Layer                          │
│         (Agent, Services, API Endpoints)                    │
└───────────────────────┬─────────────────────────────────────┘
                        │
     ┌──────────────────┼──────────────────┐
     │                  │                  │
     ▼                  ▼                  ▼
┌────────────┐   ┌────────────┐   ┌────────────────┐
│ LONG-TERM  │   │  CACHED    │   │    VECTOR      │
│   DATA     │   │   DATA     │   │     DATA       │
│            │   │            │   │                │
│ Cases      │   │ Sessions   │   │ Knowledge Base │
│ Users      │   │ Temp State │   │ Embeddings     │
│ Evidence   │   │ Messages   │   │ Semantic Index │
└─────┬──────┘   └─────┬──────┘   └────────┬───────┘
      │                │                     │
      │ Repository     │ Store              │ Store
      │ Interface      │ Interface          │ Interface
      │                │                     │
      ▼                ▼                     ▼
┌──────────────┐ ┌──────────────┐  ┌─────────────────┐
│ CaseRepo     │ │ SessionStore │  │  VectorStore    │
│ (Abstract)   │ │ (Abstract)   │  │  (Abstract)     │
└──────┬───────┘ └──────┬───────┘  └────────┬────────┘
       │                │                     │
  ┌────┴────┐      ┌────┴────┐          ┌────┴────┐
  │         │      │         │          │         │
  ▼         ▼      ▼         ▼          ▼         ▼
InMemory  PostgreSQL  InMemory  Redis   Local   Server
 Repo      Repo        Store     Store   ChromaDB ChromaDB
```

### 2.2 Why Two Dimensions?

**Dimension 1: Data Type** (chooses storage technology)
- Different data has different requirements
- Relational data → PostgreSQL (ACID, complex queries)
- Ephemeral data → Redis (fast, TTL, pub/sub)
- Vector data → ChromaDB (similarity search, embeddings)

**Dimension 2: Storage Backend** (chooses deployment model)
- Development → In-memory (fast, no setup)
- Single-node → Local files (persistent, simple)
- Production → Microservices (distributed, HA)

**Example**: Case data (long-term) can be stored:
- In Python dict (InMemory backend)
- In local SQLite file (Local Files backend)
- In K8s PostgreSQL cluster (Microservices backend)

But session data (cached) should use Redis technology regardless of backend.

---

## 3. Storage Technologies by Data Type

### 3.1 Long-Term Persistent Data (Cases, Users, Evidence)

> **Current implementation vs target design**: The `SessionlessCaseRepository` with runtime dialect routing described below (and in §5.2, §6.3, and Appendix A) is the **target design**. The current wiring in `container.py` and `repository_factory.py` uses `DatabaseCaseRepository` — a generic ORM-backed repository that works on both SQLite and PostgreSQL via portable SQLAlchemy queries, with no dialect-specific code paths. The `RepositoryRegistry` consolidation (which would wire `SessionlessCaseRepository` end-to-end) was deferred as scope creep in the locked storage redesign 2026-04 (per strategy doc §12 decision #14). Readers should treat the "Runtime Dialect Detection" and `SessionlessCaseRepository` passages as documentation of the target state, not the live wiring.

**Requirements**:
- Permanent storage
- ACID transactions
- Complex relational queries
- Historical data preservation

**Technology**: **PostgreSQL** (relational database)

**Storage Backends**:

| Backend | Implementation | Status | Use Case |
|---------|----------------|--------|----------|
| In-Memory | `InMemoryCaseRepository` | ✅ Implemented | Development, testing |
| Local Files | `SQLiteCaseRepository` | ✅ Implemented | Single-node, local deployment |
| Microservices | `PostgreSQLHybridCaseRepository` | ✅ Implemented | Production K8s |

**Configuration** (`inmemory` and `database` are the only supported values):
```bash
CASE_STORAGE_TYPE=inmemory   # Development / unit tests (process-local dict)
CASE_STORAGE_TYPE=database   # Local or Cloud — dialect routed from DATABASE_URL
#   DATABASE_URL=sqlite+aiosqlite:///./data/faultmaven.db        → SQLiteCaseRepository
#   DATABASE_URL=postgresql+asyncpg://user:pass@host/faultmaven  → PostgreSQLHybridCaseRepository
```

**Data Includes**:
- Cases (investigations)
- Evidence records
- Hypotheses
- Solutions
- Turn history
- Status transitions

---

### 3.2 Cached Ephemeral Data (Sessions, Temporary State)

**Requirements**:
- Fast read/write
- TTL (time-to-live) support
- Ephemeral (acceptable data loss)
- Key-value access patterns

**Technology**: **Redis** (in-memory key-value store)

**Storage Backends**:

| Backend                | Implementation                        | Status         | Use Case             |
|------------------------|---------------------------------------|----------------|----------------------|
| FakeRedis (in-process) | `RedisSessionStore` + `fakeredis`     | ✅ Implemented | Local deployment     |
| Redis (external)       | `RedisSessionStore` + `redis.asyncio` | ✅ Implemented | Cloud/K8s deployment |

**Architecture**: A single `RedisSessionStore` implementation works with both real Redis and FakeRedis. The central client factory (`redis_client.py:get_async_redis_client()`) returns the appropriate client based on whether a real Redis server is available. No dual code paths.

**Configuration**:
```bash
# No config needed for local (FakeRedis auto-selected)
# For cloud: provide Redis connection details
REDIS_HOST=redis.example.com   # Triggers real Redis client
REDIS_PORT=6379
```

**Data Includes**:
- User sessions
- Investigation state (current turn, pending requests)
- Temporary caches
- Rate limiting data
- Real-time message queues

**TTL Strategy** (distinct concepts — do not conflate; see [schemas/user-schema.md §5.3](./schemas/user-schema.md#53-session-ttl-strategy) for the canonical auth/session domain reference):

- **JWT access token**: 60 min default (`JWT_ACCESS_TOKEN_EXPIRY`)
- **JWT refresh token**: 7 days default (`JWT_REFRESH_TOKEN_EXPIRY`)
- **Session record TTL (Redis)**: 24 h default (`SessionSettings.session_ttl_hours`)
- **Session inactivity timeout**: 30 min default (`SessionSettings.timeout_minutes`)
- Investigation state: 7 days
- Temporary caches: 1 hour

---

### 3.3 Vector Embeddings Data (Knowledge Base, Semantic Search)

**Requirements**:

- Vector similarity search
- Embedding storage (1024-dim vectors — BGE-M3)
- Semantic queries
- RAG (Retrieval-Augmented Generation) support

**Technology**: **ChromaDB** (vector database)

**Storage Backends**:

| Backend | Implementation | Status | Use Case |
|---------|----------------|--------|----------|
| PersistentClient (in-process) | `ChromaDBVectorStore` | ✅ Implemented | Local deployment |
| HttpClient (external server) | `ChromaDBVectorStore` | ✅ Implemented | Cloud/K8s deployment |

> **Note**: `InMemoryVectorStore` has been removed. ChromaDB `PersistentClient` is always
> available (chromadb is a base dependency), so no fallback is needed — same principle as FakeRedis.

**Configuration**:
```bash
# Vector storage type selection
VECTOR_STORAGE_TYPE=chromadb   # Production (true semantic embeddings)

# ChromaDB configuration (when TYPE=chromadb)
CHROMADB_URL=http://chromadb.faultmaven.local:30080
CHROMADB_API_KEY=your_chromadb_token_here
CHROMADB_COLLECTION=faultmaven_kb
```

**Data Includes**:

- Knowledge base documents
- Document embeddings (BGE-M3, 1024 dimensions, multilingual)
- Evidence summaries (for semantic search)
- Historical solution patterns
- Troubleshooting playbooks

**Note**: ChromaDB is used for all deployments. `PersistentClient` runs in-process for local deployment; `HttpClient` connects to an external ChromaDB server for cloud/K8s. There is no in-memory alternative — `InMemoryVectorStore` was removed (see §3.3 note).

---

## 4. Repository Pattern by Data Type

### 4.1 Case Repository Interface (Long-Term Data)

**File**: `faultmaven/infrastructure/persistence/case_repository.py`

**Abstract Interface**:
```python
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from faultmaven.models.case import Case, CaseStatus


class CaseRepository(ABC):
    """
    Abstract repository for Case persistence.
    SIMPLIFIED FOR ILLUSTRATION — see faultmaven/modules/case/infrastructure/case_repository.py
    for the full interface (>30 methods spanning reports, checkpoints, evidence,
    agent executions, and tool calls).
    Note (v2.1): the prior "standalone evidence" methods (create/get/list/delete/link)
    are removed in the locked design — evidence is always created in a case context.

    Technology: Relational database (PostgreSQL/SQLite)

    Implementations:
    - SQLiteCaseRepository: Local file (single-node deployment, default)
    - PostgreSQLHybridCaseRepository: K8s PostgreSQL (production)
    - SessionlessCaseRepository: Sessionless variant
    """

    # Core CRUD (5 methods)
    @abstractmethod
    async def save(self, case: Case) -> Case:
        """Save or update a case. Returns the saved case.

        Semantics: purely additive for all owned sub-collections
        (messages, evidence, hypotheses, solutions, uploaded_files).
        Rows absent from the in-memory case are NOT removed — see
        §4.1.1. Use the explicit scoped `delete_*` methods for
        intentional removal.
        """
        ...

    @abstractmethod
    async def get(self, case_id: str) -> Optional[Case]:
        """Get a case by ID. Returns None if not found."""
        ...

    @abstractmethod
    async def list(
        self,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> tuple[List[Case], int]:
        """List cases with optional filters. Returns (cases, total_count)."""
        ...

    @abstractmethod
    async def delete(self, case_id: str) -> bool:
        """Delete a case by ID. Returns True if deleted, False if not found."""
        ...

    @abstractmethod
    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> tuple[List[Case], int]:
        """Full-text search cases. Returns (cases, total_count)."""
        ...

    # Message Management (2 methods)
    @abstractmethod
    async def add_message(self, case_id: str, message_dict: dict) -> bool:
        """Add a message to a case. Returns True if successful."""
        ...

    @abstractmethod
    async def get_messages(
        self,
        case_id: str,
        limit: Optional[int] = None,
        offset: int = 0
    ) -> List[dict]:
        """Get messages for a case with optional pagination."""
        ...

    # Activity Tracking (1 method)
    @abstractmethod
    async def update_activity_timestamp(self, case_id: str) -> bool:
        """Update last_activity_at for a case. Returns True if successful."""
        ...

    # Analytics (1 method)
    @abstractmethod
    async def get_analytics(self, case_id: str) -> Dict[str, Any]:
        """Get analytics/statistics for a case."""
        ...

    # Maintenance (1 method)
    @abstractmethod
    async def cleanup_expired(
        self,
        days_inactive: int = 90,
        limit: int = 100
    ) -> int:
        """Cleanup expired/inactive cases. Returns count of deleted cases."""
        ...

    # Transaction Support (1 method)
    @abstractmethod
    async def begin_transaction(self):
        """Begin a database transaction context. Returns transaction context manager."""
        ...

    # NOT SHOWN (see canonical interface):
    #   - Report ops: save_report, get_report, list_reports_for_case, ...
    #   - Checkpoint ops: save_checkpoint, get_checkpoint, list_checkpoints, ...
    #   - Standalone evidence ops
    #   - Agent execution + tool-call ops
```

**Illustrated above: 11 methods** (5 CRUD + 2 messages + 4 specialized). The full interface adds report, checkpoint, evidence, and agent-execution operations — see the canonical `case_repository.py` for the complete contract.

---

### 4.1.1 Aggregate save semantics

`save(case)` persists the case aggregate. For each owned sub-collection — `messages`, `evidence`, `hypotheses`, `solutions`, `uploaded_files` — the per-collection `_upsert_*` helper is **purely additive**: it inserts new rows and updates existing rows keyed by primary ID, but does NOT delete rows absent from the in-memory list.

**Why this matters**. The in-memory `Case` object is a working snapshot, not the canonical truth for which rows should exist. Callers that save a `Case` they loaded earlier (foreground turn handlers, background vectorization tasks, DA tracking) cannot be trusted to hold the latest list of children — between their `get()` and `save()`, another concurrent writer may have added rows. Historically these helpers ran `DELETE FROM <table> WHERE case_id = ? AND <pk> NOT IN (in_memory_ids)`, which silently truncated those newer rows.

**Rule**: **never use `save(case)` as a channel for intentional deletion.** If you need to remove a row, call the explicit scoped method that states the intent in its signature:

| Entity | Explicit removal |
| --- | --- |
| Evidence | `delete_evidence(case_id, evidence_id)` |
| Uploaded file | `delete_uploaded_file(case_id, file_id)` |
| Hypothesis | `IHypothesisRepository.delete_hypothesis(hypothesis_id, organization_id)` |
| Solution | `ISolutionRepository.delete_solution(solution_id, organization_id)` |
| Message | — (append-only log; no domain operation intentionally removes messages) |

**Scoped UPDATE methods**. For background tasks that need to persist a single-field change on one row, prefer scoped methods over aggregate save:

| Method | Purpose |
| --- | --- |
| `update_evidence_vectorized(case_id, evidence_id, vectorized)` | Flip the `vectorized` flag after BGE-M3 encode completes. |
| `update_activity_timestamp(case_id)` | Refresh `cases.updated_at` without re-serializing the aggregate. |

Scoped methods:

- Touch one column on one row; blast radius is the intended field.
- Are safe to call from a fire-and-forget task holding a stale `Case` snapshot — the stale snapshot is never consulted during the write.
- Make the intent visible in the signature, so code review can catch misuse.

**Historical context**. Prior to v2.3, `save(case)` performed a DELETE-then-upsert on every owned sub-collection. A fire-and-forget background task (`_vectorize_evidence`) that captured a `Case` snapshot at turn-2 time and saved it ~34s later (after BGE-M3 encoding of a 171 KB log finished) truncated messages from turns 3–6 that had been persisted in the meantime. The fix removed the DELETE clauses and introduced `update_evidence_vectorized`; the broader pattern — "aggregate save must be additive" — applies to every `_upsert_*` helper.

---

### 4.1.2 Optimistic concurrency control

`save(case)` enforces **optimistic concurrency control (OCC)** on the Case aggregate via a `version` column on the `cases` table. Every successful aggregate save bumps `version` by 1; the save only succeeds if the caller's in-memory `case.version` still matches the DB row.

**Why**. Aggregate saves are read-modify-write: a caller loads a Case, mutates it, saves. Between the load and the save, another writer (another turn, a background task, a status transition, a peer replica in K8s) can commit changes. Without OCC that second save silently last-writer-wins. OCC turns that silent loss into a loud `StaleCaseException` the caller must handle.

**Why not pessimistic locks**. Pessimistic locking requires either DB-level row locks held across the LLM call (which takes tens of seconds and would serialize all turns) or an application-level lock (which only works within a single process — useless under K8s). OCC adds one integer column, costs nothing on the uncontended path, and works correctly across replicas.

**Protocol**:

1. `repository.get(case_id)` returns a `Case` whose `version` field matches the DB row.
2. Caller mutates the `Case` in memory. The `version` field is not touched by the caller.
3. `repository.save(case)` issues `UPDATE cases SET ..., version = :n+1 WHERE case_id = :id AND version = :n`.
4. On `rowcount == 0`, the save probes the DB to distinguish "no row" (fresh insert, goes to version=1) from "row exists at different version" (raises `StaleCaseException`).
5. On success, the passed-in `case.version` is mutated to the new version so subsequent in-flight saves within the same flow work without reloading.

**Scoped updates don't bump version**. `update_evidence_vectorized`, `delete_evidence`, `delete_uploaded_file`, `update_activity_timestamp` all operate on child tables only. They do not interact with `cases.version`. This is intentional — they're the safe channel for background-task writes that shouldn't invalidate a concurrent turn's save.

**Handling conflicts at the caller**. Two patterns, chosen per use case:

1. **Retry** (for idempotent mutations) — use `update_case_with_retry(repo, case_id, mutator, max_attempts=3)` from `faultmaven/modules/case/utils/retry.py`. The helper loads a fresh Case per attempt, invokes the mutator against that fresh state, and saves. On `StaleCaseException` it reloads and retries; after `max_attempts` exhausted it re-raises. Current users: `case_service.update_case`, `investigation_service.close_case`.

2. **Surface 409 Conflict** (for non-idempotent or expensive operations) — the `/turns` endpoint takes this path. LLM turns are expensive (seconds to minutes), non-idempotent (tool calls trigger external side effects, background vectorization fires, tokens are consumed), so silently retrying is wrong. The endpoint translates `StaleCaseException` to HTTP 409 with `x-error-code: CASE_VERSION_CONFLICT` and `x-expected-version` / `x-actual-version` headers; the client reloads case state and decides whether to resubmit.

**Why the retry-vs-409 split matters**. A decorator-style `@retry_on_stale_case` is attractive for DRY but elides a question the caller must answer: "Is re-running this function against fresh state safe, or is it side-effecting?" Making the choice explicit at the call site (helper call vs. exception translation) prevents accidental retries of LLM turns.

**Example — service-layer retry**:

```python
from faultmaven.modules.case.utils import update_case_with_retry

async def update_case(self, case_id: str, updates: dict) -> bool:
    async def apply(case: Case) -> None:
        for key, value in updates.items():
            if key in ALLOWED_FIELDS:
                setattr(case, key, value)

    await update_case_with_retry(self.repository, case_id, apply)
    return True
```

**Example — endpoint 409 handling**:

```python
try:
    response = await investigation_service.process_turn(...)
except StaleCaseException as e:
    raise HTTPException(
        status_code=409,
        detail="Case state changed while processing this turn. "
               "Reload the case and resubmit if still applicable.",
        headers={
            "x-error-code": "CASE_VERSION_CONFLICT",
            "x-expected-version": str(e.expected_version),
            "x-actual-version": str(e.actual_version),
        },
    )
```

**Follow-up work out of scope for v2.4**:

- Per-entity OCC on `evidence`, `hypotheses`, `solutions` (each aggregate gets its own `version`). Today those tables are sub-collections of Case; a future DDD carve-up would make them stand-alone aggregates with their own concurrency guarantees — background tasks could then lock individual Evidence rows without contending on `cases.version`.
- Explicit `CaseMessages` event-stream semantics (no OCC needed — already append-only at the domain level).

---

### 4.2 Session Store Interface (Cached Data)

**File**: `faultmaven/infrastructure/persistence/session_store.py` (or similar)

**Abstract Interface**:
```python
from abc import ABC, abstractmethod
from typing import Optional
from datetime import timedelta


class SessionStore(ABC):
    """
    Abstract store for session data.

    Technology: Key-value store (Redis)

    Implementations:
    - RedisSessionStore + FakeRedis: Local deployment
    - RedisSessionStore + real Redis: Cloud/K8s deployment
    """

    @abstractmethod
    async def set(
        self,
        key: str,
        value: str,
        ttl: Optional[timedelta] = None
    ) -> bool:
        """Store value with optional TTL."""
        pass

    @abstractmethod
    async def get(self, key: str) -> Optional[str]:
        """Retrieve value by key."""
        pass

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Delete value by key."""
        pass

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Check if key exists."""
        pass

    @abstractmethod
    async def expire(self, key: str, ttl: timedelta) -> bool:
        """Set/update TTL on key."""
        pass

    @abstractmethod
    async def keys(self, pattern: str) -> List[str]:
        """Find keys matching pattern."""
        pass
```

---

### 4.3 Vector Store Interface (Vector Data)

**File**: `faultmaven/infrastructure/persistence/vector_store.py` (or similar)

**Abstract Interface**:
```python
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional


class VectorStore(ABC):
    """
    Abstract store for vector embeddings.

    Technology: Vector database (ChromaDB)

    Implementations:
    - ChromaDB (local mode): Local persistent storage
    - ChromaDB (server mode): Client-server K8s deployment
    """

    @abstractmethod
    async def add_documents(
        self,
        documents: List[str],
        embeddings: List[List[float]],
        metadatas: List[Dict[str, Any]],
        ids: List[str]
    ) -> bool:
        """Add documents with embeddings to collection."""
        pass

    @abstractmethod
    async def query(
        self,
        query_embedding: List[float],
        n_results: int = 10,
        where: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Query for similar documents."""
        pass

    @abstractmethod
    async def get(self, ids: List[str]) -> Dict[str, Any]:
        """Retrieve documents by IDs."""
        pass

    @abstractmethod
    async def delete(self, ids: List[str]) -> bool:
        """Delete documents by IDs."""
        pass

    @abstractmethod
    async def update(
        self,
        ids: List[str],
        documents: Optional[List[str]] = None,
        embeddings: Optional[List[List[float]]] = None,
        metadatas: Optional[List[Dict]] = None
    ) -> bool:
        """Update existing documents."""
        pass
```

---

## 5. Storage Backend Options

### 5.1 In-Memory Backend (Python Modules)

**Use Case**: Development, testing, rapid prototyping

**Implementations**:

- ✅ `InMemoryCaseRepository` - Cases in Python dict (database dimension)
- ✅ `RedisSessionStore` + FakeRedis - Sessions via in-process Redis (cache dimension)
- ✅ ChromaDB `PersistentClient` - Vector storage (always available, no fallback needed)

> **Note**: Session/cache storage no longer uses a separate `InMemorySessionStore`.
> All deployments use `RedisSessionStore` backed by either real Redis (cloud) or
> FakeRedis (local). This eliminates dual code paths across 9 Redis-dependent subsystems.

**Configuration**:

```bash
# .env.development
CASE_STORAGE_TYPE=inmemory
USER_STORAGE_TYPE=inmemory
# Sessions: FakeRedis auto-selected when REDIS_HOST is unset (no config needed)
VECTOR_STORAGE_TYPE=chromadb   # Local PersistentClient at data/chroma-kb/
```

**Characteristics**:

- ✅ Zero setup for SQL/cache dimensions
- ✅ Microsecond operations for in-memory case/user repositories
- ✅ Perfect for tests
- ✅ 100% Redis API parity (FakeRedis supports Lua scripts, pipelines, sorted sets)
- ❌ Case/user data lost on restart (vector data persists via ChromaDB PersistentClient)
- ❌ Single process only

---

### 5.2 Local Files Backend (Filesystem)

**Use Case**: Single-node deployment, local development, self-hosted

**Implementations**:
- ✅ `SQLiteCaseRepository` - Cases in SQLite file (SQLite-compatible SQL)
- ✅ `RedisSessionStore` + FakeRedis - Sessions (in-process, no external server required)
- ✅ ChromaDB local mode - Vector storage with persistence

**Configuration**:
```bash
# .env.local (self-hosted deployment)
DATABASE_URL=sqlite+aiosqlite:///./data/faultmaven.db

# Sessions: FakeRedis auto-selected (no config needed)
# To use external Redis: set REDIS_HOST=redis.local

VECTOR_STORAGE_TYPE=chromadb   # PersistentClient on disk
```

**Characteristics**:
- ✅ Persistent local storage
- ✅ No external dependencies (PostgreSQL not required)
- ✅ Single file database (SQLite)
- ✅ SQLite-compatible SQL (no PostgreSQL-specific features)
- ❌ Limited concurrency (SQLite limitation)
- ❌ Not distributed

**Status**: ✅ **Fully Implemented** (PR #120)

**Implementation Details**:

- **`SQLiteCaseRepository`** (1,450 lines) - Complete SQLite-compatible repository
  - ✅ All 13 repository methods implemented
  - ✅ 8 integration tests passing with real SQLite database
  - ✅ Hybrid normalized schema (cases + 6 related tables)
  - ✅ **Functional parity** with PostgreSQL (same features, different SQL implementation)

**SQLite-Compatible SQL Patterns** (24 PostgreSQL features replaced):

- **Type Casts**: No `::jsonb` → plain parameter binding with JSON strings
- **JSON Functions**: No `jsonb_build_object()` → separate queries + Python dict construction
- **Aggregates**: No `FILTER (WHERE ...)` → `CASE WHEN ... END` expressions
- **Full-Text Search**: No `to_tsvector/ts_rank` → `LIKE '%term%'` pattern matching
- **Array Operations**: No `= ALL` / `!= ALL` → explicit `IN (...)` clauses
- **Timestamps**: SQLite returns strings → `datetime.fromisoformat()` parsing

**Runtime Dialect Detection**:

- `SessionlessCaseRepository` auto-detects database dialect from SQLAlchemy session
- `sqlite` dialect → instantiates `SQLiteCaseRepository`
- `postgresql` dialect → instantiates `PostgreSQLHybridCaseRepository`
- Zero configuration needed - works automatically based on `DATABASE_URL`

---

### 5.3 Microservices Backend (Kubernetes)

> **Current implementation vs target design**: The `SessionlessCaseRepository` wiring shown in §6.3 is the target design. The current live container wires `DatabaseCaseRepository` directly — a generic ORM repository that handles both SQLite and PostgreSQL without dialect-specific code paths. Dialect routing via `SessionlessCaseRepository` is deferred (strategy doc §12 #14). The production PostgreSQL path works correctly in both the current and target configurations.

**Use Case**: Production deployment, high availability

**Implementations**:
- ✅ `PostgreSQLHybridCaseRepository` - Cases in K8s PostgreSQL
- ✅ `RedisSessionStore` - Sessions in K8s Redis
- ✅ `ChromaDBVectorStore` - Vectors in K8s ChromaDB

**Configuration**:
```bash
# .env.production
CASE_STORAGE_TYPE=database
DATABASE_URL=postgresql+asyncpg://case_service:${DB_PASSWORD}@postgres.faultmaven.local:30432/cases_db
# (Legacy per-service vars retained for backwards compatibility)
CASES_DB_HOST=postgres.faultmaven.local
CASES_DB_PORT=30432
CASES_DB_NAME=cases_db
CASES_DB_USER=case_service
CASES_DB_PASSWORD=${DB_PASSWORD}

REDIS_HOST=redis.faultmaven.local
REDIS_PORT=6379
# Note: sessions are always Redis-backed. There is no SESSION_STORAGE_TYPE selector.
# REDIS_HOST / REDIS_URL configures the Redis backend; FakeRedis is used in-process when REDIS_HOST is unset.

VECTOR_STORAGE_TYPE=chromadb
CHROMADB_URL=http://chromadb.faultmaven.local:30080
CHROMADB_API_KEY=${CHROMADB_TOKEN}
CHROMADB_COLLECTION=faultmaven_kb
```

**Characteristics**:
- ✅ Distributed, HA
- ✅ ACID transactions (PostgreSQL)
- ✅ High concurrency
- ✅ Replication support
- ✅ Production-grade

---

## 6. Configuration Management

### 6.1 Configuration Matrix

**Complete Configuration Example** (all storage systems):

```bash
# ===========================================
# LONG-TERM DATA (Cases, Users, Evidence)
# ===========================================
# Technology: PostgreSQL (cloud) / SQLite (local) — routed from DATABASE_URL
# Selector values: "inmemory" | "database"

CASE_STORAGE_TYPE=database           # or: inmemory
USER_STORAGE_TYPE=database           # or: inmemory

# DATABASE_URL determines SQL dialect at runtime:
DATABASE_URL=postgresql+asyncpg://case_service:${DB_PASSWORD}@postgres.faultmaven.local:30432/cases_db
# For local:
# DATABASE_URL=sqlite+aiosqlite:///./data/faultmaven.db

# (Legacy per-service DB vars still read by some code paths)
CASES_DB_HOST=postgres.faultmaven.local
CASES_DB_PORT=30432
CASES_DB_NAME=cases_db
CASES_DB_USER=case_service
CASES_DB_PASSWORD=secure_password

USERS_DB_HOST=postgres.faultmaven.local
USERS_DB_PORT=30432
USERS_DB_NAME=users_db
USERS_DB_USER=user_service
USERS_DB_PASSWORD=secure_password

# ===========================================
# CACHED DATA (Sessions, Temp State)
# ===========================================
# Technology: Redis (real or FakeRedis for local)
# FakeRedis auto-selected when no REDIS_HOST configured

# Redis Configuration (set for cloud deployment)
REDIS_HOST=redis.faultmaven.local
REDIS_PORT=6379
REDIS_PASSWORD=secure_password
REDIS_DB=0

# JWT lifetimes (minutes)
JWT_ACCESS_TOKEN_EXPIRY=60            # Access token: 60 min (default)
JWT_REFRESH_TOKEN_EXPIRY=10080        # Refresh token: 7 days
# Session record TTL: 24h (SessionSettings.session_ttl_hours)
# Session inactivity timeout: 30 min (SessionSettings.timeout_minutes)

# ===========================================
# VECTOR DATA (Knowledge Base, Embeddings)
# ===========================================
# Technology: ChromaDB (PersistentClient local, HttpClient external)

VECTOR_STORAGE_TYPE=chromadb         # set CHROMADB_URL for external server

# ChromaDB Configuration (when TYPE=chromadb)
CHROMADB_URL=http://chromadb.faultmaven.local:30080
CHROMADB_API_KEY=secure_token
CHROMADB_COLLECTION=faultmaven_kb
```

### 6.2 Environment-Specific Configurations

**Development** (fast iteration, no setup):

```bash
# .env.development
CASE_STORAGE_TYPE=inmemory
USER_STORAGE_TYPE=inmemory
# Sessions: FakeRedis auto-selected (no config needed)
VECTOR_STORAGE_TYPE=chromadb     # Local PersistentClient
```

**Production / Cloud** (K8s microservices):
```bash
# .env.production
CASE_STORAGE_TYPE=database
USER_STORAGE_TYPE=database
DATABASE_URL=postgresql+asyncpg://user:pass@postgres.faultmaven.local/faultmaven
REDIS_HOST=redis.faultmaven.local   # triggers real Redis; omit for FakeRedis
VECTOR_STORAGE_TYPE=chromadb
CHROMADB_URL=http://chromadb.faultmaven.local:30080
# All connection details from K8s ConfigMaps/Secrets
```

**Local / Single-Node** (persistent local — SQLite + FakeRedis):
```bash
# .env.local
CASE_STORAGE_TYPE=database
USER_STORAGE_TYPE=database
DATABASE_URL=sqlite+aiosqlite:///./data/faultmaven.db
# Sessions: FakeRedis auto-selected when REDIS_HOST is unset (no external Redis required)
VECTOR_STORAGE_TYPE=chromadb                # Local ChromaDB PersistentClient at data/chroma-kb/
# CHROMADB_URL unset → PersistentClient mode
```

### 6.3 Dependency Injection (container.py)

**File**: `faultmaven/container.py`

```python
class Container:
    """Dependency injection container."""

    def __init__(self):
        settings = Settings()  # Load from .env

        # ==========================================
        # LONG-TERM DATA: Case Repository
        # ==========================================
        case_storage_type = settings.database.case_storage_type.lower()

        if case_storage_type == "database":
            # Database backend — dialect (SQLite / PostgreSQL) is routed from DATABASE_URL
            # by SessionlessCaseRepository at request time. No explicit dialect branch here.
            engine = create_async_engine(
                settings.database.database_url,
                pool_size=10,
                max_overflow=20,
            )
            session_factory = sessionmaker(engine, class_=AsyncSession)
            self.case_repository = SessionlessCaseRepository(session_factory)

        else:  # "inmemory"
            # In-memory backend (development / unit tests)
            self.case_repository = InMemoryCaseRepository()

        # ==========================================
        # CACHED DATA: Session Store
        # ==========================================
        # Single code path: get_async_redis_client() returns real Redis
        # or FakeRedis based on availability. No branching needed.
        from faultmaven.infrastructure.redis_client import get_async_redis_client
        redis_client = await get_async_redis_client(
            host=settings.database.redis_host,
            port=settings.database.redis_port,
        )
        self.session_store = RedisSessionStore(redis_client)

        # ==========================================
        # VECTOR DATA: Vector Store (ChromaDB only)
        # ==========================================
        # InMemoryVectorStore is removed — ChromaDB PersistentClient is always available.
        # Client created by DI container via create_kb_chromadb_client() or create_evidence_chromadb_client().
        from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
        self.vector_store = ChromaDBVectorStore(client=chromadb_client)
```

**Key Points**:
- ✅ Each data type configured independently
- ✅ Each can use different backend
- ✅ Business logic receives abstract interfaces
- ✅ Zero code changes when switching backends

---

## 7. Error Handling Strategy

### 7.1 Repository Error Patterns

All repository implementations must handle errors consistently:

**Error Categories**:

| Error Type | When It Occurs | Repository Behavior | Caller Responsibility |
|------------|----------------|---------------------|---------------------- |
| **Connection Failure** | DB unreachable, network timeout | Raise `ConnectionError` | Retry with exponential backoff |
| **Constraint Violation** | Unique/FK/CHECK constraint fails | Raise `IntegrityError` | Validate before save or show user error |
| **Not Found** | Resource doesn't exist | Return `None` (get) or `False` (delete) | Handle gracefully, don't treat as error |
| **Transaction Conflict** | Concurrent modification detected | Raise `ConcurrencyError` | Retry with optimistic locking |
| **Invalid Input** | Malformed data, type mismatch | Raise `ValueError` | Validate at service layer |
| **Timeout** | Query exceeds time limit | Raise `TimeoutError` | Log and retry or fail gracefully |

**Example Implementation**:

```python
from typing import Optional
from sqlalchemy.exc import IntegrityError, OperationalError
from asyncio import TimeoutError

class PostgreSQLHybridCaseRepository(CaseRepository):
    async def save(self, case: Case) -> Case:
        try:
            # Attempt save
            result = await self.db.execute(insert_query, case.dict())
            await self.db.commit()
            return case

        except IntegrityError as e:
            await self.db.rollback()
            if "unique constraint" in str(e).lower():
                raise ValueError(f"Case {case.case_id} already exists")
            elif "foreign key" in str(e).lower():
                raise ValueError(f"Referenced entity not found: {e}")
            else:
                raise ValueError(f"Data integrity violation: {e}")

        except OperationalError as e:
            await self.db.rollback()
            if "connection" in str(e).lower():
                raise ConnectionError(f"Database connection failed: {e}")
            else:
                raise

        except TimeoutError:
            await self.db.rollback()
            raise TimeoutError(f"Query timed out saving case {case.case_id}")

        except Exception as e:
            await self.db.rollback()
            # Log unexpected errors
            logger.error(f"Unexpected error saving case: {e}", exc_info=True)
            raise

    async def get(self, case_id: str) -> Optional[Case]:
        """Returns None if not found - NOT an error."""
        try:
            result = await self.db.fetch_one(select_query, {"case_id": case_id})
            return Case(**result) if result else None
        except Exception as e:
            logger.error(f"Error fetching case {case_id}: {e}")
            raise
```

### 7.2 Transaction Rollback Patterns

**Auto-Rollback on Error**:

- All write operations (`save`, `delete`, `add_message`) must rollback on error
- Read operations (`get`, `list`) don't need rollback (no changes made)

**Transaction Context Manager**:
```python
async def transfer_case_ownership(
    repo: CaseRepository,
    case_id: str,
    new_user_id: str
):
    """Example of explicit transaction management."""
    async with repo.begin_transaction():
        # Multiple operations in one transaction
        case = await repo.get(case_id)
        if not case:
            raise ValueError(f"Case {case_id} not found")

        case.user_id = new_user_id
        case.updated_at = datetime.utcnow()

        await repo.save(case)
        await repo.add_message(case_id, {
            "type": "system",
            "content": f"Ownership transferred to {new_user_id}"
        })

        # Automatic commit on success, rollback on exception
```

### 7.3 Retry Strategy

**When to Retry**:
- ✅ Transient connection failures
- ✅ Deadlocks / lock timeouts
- ✅ Temporary network issues
- ❌ Constraint violations (permanent failures)
- ❌ Invalid input (won't succeed on retry)

**Retry Implementation** (Service Layer):
```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    reraise=True
)
async def save_case_with_retry(repo: CaseRepository, case: Case) -> Case:
    """Retry transient failures, fail fast on permanent errors."""
    return await repo.save(case)
```

### 7.4 Error Logging

**What to Log**:

- ✅ All exceptions (with stack trace)
- ✅ Connection failures (for monitoring)
- ✅ Constraint violations (for debugging)
- ✅ Slow queries (> 100ms)
- ❌ Not Found results (normal operation)

**Logging Pattern**:
```python
import logging
logger = logging.getLogger(__name__)

async def save(self, case: Case) -> Case:
    try:
        result = await self._execute_save(case)
        logger.debug(f"Saved case {case.case_id}")
        return result
    except IntegrityError as e:
        logger.warning(f"Integrity violation saving case {case.case_id}: {e}")
        raise ValueError(f"Data integrity violation: {e}")
    except Exception as e:
        logger.error(f"Failed to save case {case.case_id}: {e}", exc_info=True)
        raise
```

---

## 8. Testing Strategy

### 8.1 Contract Tests by Data Type

**Pattern**: All implementations of the same interface must pass identical tests.

**Case Repository Contract Tests**:
```python
class CaseRepositoryContractTests(ABC):
    """All CaseRepository implementations must pass these tests."""

    @pytest.mark.asyncio
    async def test_save_and_get(self, repository):
        case = create_sample_case()
        saved = await repository.save(case)
        retrieved = await repository.get(saved.case_id)
        assert retrieved.case_id == saved.case_id

    # ... per-method tests for the full repository interface (see canonical case_repository.py)


class TestInMemoryCaseRepository(CaseRepositoryContractTests):
    @pytest.fixture
    def repository(self):
        return InMemoryCaseRepository()


class TestPostgreSQLHybridCaseRepository(CaseRepositoryContractTests):
    @pytest.fixture
    def repository(self):
        return PostgreSQLHybridCaseRepository(test_db_session)
```

**Session Store Contract Tests**:
```python
class SessionStoreContractTests(ABC):
    """All SessionStore implementations must pass these tests."""

    @pytest.mark.asyncio
    async def test_set_and_get(self, store):
        await store.set("key1", "value1")
        value = await store.get("key1")
        assert value == "value1"

    # ... TTL tests, expiration, pattern matching, etc.


class TestFakeRedisSessionStore(SessionStoreContractTests):
    @pytest.fixture
    def store(self):
        import fakeredis.aioredis as fakeredis_aio
        return RedisSessionStore(fakeredis_aio.FakeRedis(decode_responses=True))


class TestRealRedisSessionStore(SessionStoreContractTests):
    @pytest.fixture
    def store(self):
        return RedisSessionStore(test_redis_client)
```

### 8.2 Multi-Backend Integration Tests

Test that services work correctly with **any combination** of backends:

```python
@pytest.mark.parametrize("case_backend,session_backend", [
    ("inmemory", "fakeredis"),
    ("inmemory", "redis"),
    ("postgres", "fakeredis"),
    ("postgres", "redis"),
])
@pytest.mark.asyncio
async def test_case_service_all_combinations(case_backend, session_backend):
    """Test CaseService works with all storage backend combinations."""

    case_repo = get_case_repository(case_backend)
    session_store = get_session_store(session_backend)

    service = CaseService(case_repo, session_store)

    # Same test logic works regardless of backends!
    case = await service.create_case(user_id="user_1", title="Test")
    assert case.case_id is not None
```

---

## 9. Performance Considerations

### 9.1 Performance by Data Type and Backend

**Long-Term Data (Cases)** - PostgreSQL Technology:

| Operation | InMemory | PostgreSQL | SQLite (Future) |
|-----------|----------|------------|-----------------|
| save() | 10 μs | 2-5 ms | 1-3 ms |
| get() | 5 μs | 1-2 ms | 0.5-1 ms |
| list(100) | 100 μs | 10-20 ms | 5-10 ms |
| search() | 1-5 ms | 10-30 ms* | 5-15 ms |

*With GIN index on JSONB columns

**Cached Data (Sessions)** - Redis Technology:

| Operation | InMemory | Redis | File (Future) |
|-----------|----------|-------|---------------|
| set() | 1 μs | 0.5-1 ms | 1-5 ms |
| get() | 1 μs | 0.5-1 ms | 1-5 ms |
| delete() | 1 μs | 0.5-1 ms | 1-5 ms |
| TTL operations | 1 μs | 1-2 ms | 5-10 ms |

**Vector Data (Embeddings)** - ChromaDB Technology:

| Operation | Local ChromaDB | Server ChromaDB |
|-----------|----------------|-----------------|
| add_documents() | 50-200 ms | 100-500 ms |
| query(n=10) | 10-50 ms | 20-100 ms |
| get() | 5-20 ms | 10-50 ms |

### 9.2 Optimization Strategies

**PostgreSQL (Long-Term Data)**:
```sql
-- Required indexes
CREATE INDEX idx_cases_user_id ON cases(user_id);
CREATE INDEX idx_cases_status ON cases(status);
CREATE INDEX idx_cases_activity ON cases(last_activity_at DESC);
CREATE INDEX idx_cases_messages_gin ON cases USING GIN (messages);

-- Connection pooling (already configured)
pool_size=10, max_overflow=20
```

**Redis (Cached Data)**:
```python
# Connection pooling
redis_client = aioredis.from_url(
    redis_url,
    max_connections=20
)

# TTL strategy (see §3.2 for the full breakdown)
session_record: 24 h      # SessionSettings.session_ttl_hours
idle_timeout:   30 min    # SessionSettings.timeout_minutes
jwt_refresh:    7 days    # JWT_REFRESH_TOKEN_EXPIRY
temp_state:     1 hour
```

**ChromaDB (Vector Data)**:
```python
# Batch operations for efficiency
await vector_store.add_documents(
    documents=batch_docs,    # Process in batches of 100
    embeddings=batch_embeds
)
```

---

## 10. Appendices

### Appendix A: Complete Storage Matrix

**Current Implementation Status**:

| Data Type | Technology | InMemory | Local Files (SQLite) | Microservices (PostgreSQL) |
|-----------|-----------|----------|---------------------|---------------------------|
| **Cases** | PostgreSQL/SQLite | ✅ Impl. | ✅ Impl. | ✅ Impl. |
| **Users** | PostgreSQL/SQLite | ✅ Impl. | ✅ Impl. | ✅ Impl. |
| **Sessions** | Redis / FakeRedis | ✅ Impl. (FakeRedis) | ✅ Impl. (FakeRedis) | ✅ Impl. (Redis) |
| **Knowledge** | ChromaDB | n/a (removed) | ✅ Impl. (PersistentClient) | ✅ Impl. (HttpClient) |

**Repository Selection Logic** (SessionlessCaseRepository):
- Dialect detected at runtime from database session
- `sqlite` dialect → `SQLiteCaseRepository` (SQLite-compatible SQL)
- `postgresql` dialect → `PostgreSQLHybridCaseRepository` (optimized PostgreSQL SQL)

### Appendix B: Migration Scenarios

**Development → Production**:
```bash
# Change configuration only (no code changes)

# FROM (Development — FakeRedis auto-selected for sessions):
CASE_STORAGE_TYPE=inmemory
VECTOR_STORAGE_TYPE=chromadb     # Local PersistentClient

# TO (Production — real Redis for sessions, external ChromaDB):
CASE_STORAGE_TYPE=database
DATABASE_URL=postgresql+asyncpg://user:pass@postgres.faultmaven.local/faultmaven
REDIS_HOST=redis.faultmaven.local
VECTOR_STORAGE_TYPE=chromadb
CHROMADB_URL=http://chromadb.faultmaven.local:30080
```

### Appendix C: Adding a New SQL Dialect

The repository layer already supports SQLite and PostgreSQL via runtime dialect detection — there is **no separate selector value** to add. To support a third dialect (e.g., MySQL):

1. Implement `<Dialect>CaseRepository` class with dialect-appropriate SQL:
   ```python
   class MySQLCaseRepository(CaseRepository):
       """Implement the full CaseRepository interface using MySQL-compatible SQL."""
   ```

2. Update `SessionlessCaseRepository` to route the new dialect:
   ```python
   if dialect_name == "mysql":
       return MySQLCaseRepository(session)
   ```

3. Configuration uses the same selector; the dialect is selected by `DATABASE_URL`:
   ```bash
   CASE_STORAGE_TYPE=database
   DATABASE_URL=mysql+aiomysql://user:pass@host/faultmaven
   ```

4. Pass contract tests:
   ```python
   class TestSQLiteCaseRepository(CaseRepositoryContractTests):
       ...
   ```

**No changes required** in services, agents, or business logic! ✅

---

## Summary

**FaultMaven Storage Architecture** = **Two Dimensions**:

**Dimension 1 - Data Types** (3 types):

- Long-term data → PostgreSQL or SQLite technology
- Cached data → Redis (real or FakeRedis)
- Vector data → ChromaDB (`PersistentClient` local, `HttpClient` cloud)

**Dimension 2 - Storage Backends** (3 options):

- In-memory → Development/testing (case/user repositories only)
- Local files (SQLite + ChromaDB `PersistentClient` + FakeRedis) → Single-node, self-hosted deployment
- Microservices (PostgreSQL + external Redis + external ChromaDB) → Production K8s

**Current Status**:

- ✅ InMemory backend implemented for case/user data types
- ✅ SQLite backend implemented for Case Repository (local deployment)
- ✅ PostgreSQL backend implemented for Case Repository (cloud deployment)
- ✅ Automatic dialect detection in SessionlessCaseRepository
- ✅ ChromaDB is the sole vector backend (`InMemoryVectorStore` removed)
- ✅ FakeRedis is the sole in-process session backend (no separate `InMemorySessionStore`)

**Key Benefits**:
- ✅ Technology-appropriate storage for each data type
- ✅ Flexible backend deployment options
- ✅ Independent configuration per data type
- ✅ Zero code changes when switching backends
- ✅ True deployment-agnostic architecture (SQLite + PostgreSQL both work)

---

**Document Version**: 2.2.1
**Last Updated**: 2026-03-18
**Status**: ✅ Accurately reflects current implementation with fully implemented SQLite support (PR #120). Dead PostgreSQLCaseRepository references replaced with PostgreSQLHybridCaseRepository.
