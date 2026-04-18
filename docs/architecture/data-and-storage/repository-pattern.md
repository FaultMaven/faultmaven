# Database Abstraction Layer Specification v2.2

**Document Purpose**: Define the pluggable storage architecture that enables FaultMaven to switch between storage backends via configuration without code changes, across multiple data types and storage technologies.

**Status**: ✅ Production Implementation
**Version**: 2.2.1
**Last Updated**: 2026-03-18
**Alignment**:

- Investigation Architecture v2.0 (Milestone-Based)
- Case Model Design v2.0
- Current Implementation (faultmaven/infrastructure/persistence/)

**Critical Updates**:

- ✅ Two-dimensional storage architecture (backend × data type)
- ✅ Multiple storage technologies (PostgreSQL/SQLite, Redis/FakeRedis, ChromaDB)
- ✅ Pluggable adapters for each data type
- ✅ Configuration-based backend selection per storage system
- ✅ 13-method `CaseRepository` interface
- ✅ `PostgreSQLHybridCaseRepository` is the sole PostgreSQL case-repository implementation (legacy `PostgreSQLCaseRepository` class removed)
- ✅ `InMemoryVectorStore` removed — ChromaDB `PersistentClient` is always available

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
Cached         │ Python dict  │ File-based   │ Redis        │
(Sessions)     │ ✅ Impl.     │ ⚠️ Future    │ ✅ Impl.     │
               │              │              │              │
Technology:    │ InMemory     │ File         │ Redis        │
               │ SessionStore │ SessionStore │ SessionStore │
───────────────┼──────────────┼──────────────┼──────────────┤
Vector         │     n/a      │ ChromaDB     │ ChromaDB     │
(Knowledge)    │              │ ✅ Impl.     │ ✅ Impl.     │
               │              │              │              │
Technology:    │     n/a      │ ChromaDB     │ ChromaDB     │
               │              │ (local)      │ (server)     │
└──────────────┴──────────────┴──────────────┴──────────────┘

Configuration Example:
  # Long-term data storage
  CASE_STORAGE_TYPE=inmemory       # or: postgres
  USER_STORAGE_TYPE=inmemory       # or: postgres

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

**Configuration**:
```bash
CASE_STORAGE_TYPE=inmemory   # Development
CASE_STORAGE_TYPE=sqlite     # Future: single-node
CASE_STORAGE_TYPE=postgres   # Production
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

**TTL Strategy** (distinct concepts — do not conflate):

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
    for the full interface (>30 methods spanning reports, checkpoints, standalone evidence,
    agent executions, and tool calls).

    Technology: Relational database (PostgreSQL/SQLite)

    Implementations:
    - SQLiteCaseRepository: Local file (single-node deployment, default)
    - PostgreSQLHybridCaseRepository: K8s PostgreSQL (production)
    - SessionlessCaseRepository: Sessionless variant
    """

    # Core CRUD (5 methods)
    @abstractmethod
    async def save(self, case: Case) -> Case:
        """Save or update a case. Returns the saved case."""
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
# No SESSION_STORAGE_TYPE needed — FakeRedis auto-selected when no Redis server available
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
- ⚠️ `FileSessionStore` - Sessions in local files (future)
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

**Use Case**: Production deployment, high availability

**Implementations**:
- ✅ `PostgreSQLHybridCaseRepository` - Cases in K8s PostgreSQL
- ✅ `RedisSessionStore` - Sessions in K8s Redis
- ✅ `ChromaDBVectorStore` - Vectors in K8s ChromaDB

**Configuration**:
```bash
# .env.production
CASE_STORAGE_TYPE=postgres
CASES_DB_HOST=postgres.faultmaven.local
CASES_DB_PORT=30432
CASES_DB_NAME=cases_db
CASES_DB_USER=case_service
CASES_DB_PASSWORD=${DB_PASSWORD}

SESSION_STORAGE_TYPE=redis
REDIS_HOST=redis.faultmaven.local
REDIS_PORT=6379

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
# Technology: PostgreSQL/SQLite
# Options: inmemory, sqlite (future), postgres

CASE_STORAGE_TYPE=postgres           # or: inmemory
USER_STORAGE_TYPE=postgres           # or: inmemory

# PostgreSQL Configuration (when TYPE=postgres)
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

**Production** (K8s microservices):
```bash
# .env.production
CASE_STORAGE_TYPE=postgres
USER_STORAGE_TYPE=postgres
SESSION_STORAGE_TYPE=redis
VECTOR_STORAGE_TYPE=chromadb

# All connection details from K8s ConfigMaps/Secrets
```

**Single-Node** (future - persistent local):
```bash
# .env.singlenode (future)
CASE_STORAGE_TYPE=sqlite
USER_STORAGE_TYPE=sqlite
SESSION_STORAGE_TYPE=file
VECTOR_STORAGE_TYPE=chromadb
CHROMADB_URL=http://localhost:8090
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

        if case_storage_type == "postgres":
            # PostgreSQL backend (production)
            cases_engine = create_async_engine(
                settings.database.cases_db_url,
                pool_size=10,
                max_overflow=20
            )
            session_factory = sessionmaker(cases_engine, class_=AsyncSession)
            self.case_repository = PostgreSQLHybridCaseRepository(session_factory())

        else:
            # In-memory backend (development)
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
        # VECTOR DATA: Vector Store (InMemory or ChromaDB)
        # ==========================================
        vector_storage_type = settings.database.vector_storage_type.lower()

        if vector_storage_type == "chromadb":
            # ChromaDB backend (production)
            from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
            self.vector_store = ChromaDBVectorStore()

        else:
            # ChromaDB always available (PersistentClient for local, HttpClient for cloud)
            # Client created by DI container via create_kb_chromadb_client() or create_evidence_chromadb_client()
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
CASE_STORAGE_TYPE=postgres
REDIS_HOST=redis.faultmaven.local
VECTOR_STORAGE_TYPE=chromadb
CHROMADB_URL=http://chromadb.faultmaven.local:30080
```

### Appendix C: Adding New Storage Backend

**Example**: Adding SQLite backend for cases

1. Implement `SQLiteCaseRepository` class:
   ```python
   class SQLiteCaseRepository(CaseRepository):
       """Implement the full CaseRepository interface using SQLite."""
   ```

2. Update `container.py`:
   ```python
   elif case_storage_type == "sqlite":
       self.case_repository = SQLiteCaseRepository(db_path)
   ```

3. Add configuration:
   ```bash
   CASE_STORAGE_TYPE=sqlite
   SQLITE_DB_PATH=data/faultmaven.db
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
