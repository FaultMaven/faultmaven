# Database Abstraction Layer Specification v2.1

**Document Purpose**: Define the pluggable storage architecture that enables FaultMaven to switch between storage backends via configuration without code changes, across multiple data types and storage technologies.

**Status**: ✅ Production Implementation
**Version**: 2.1.0
**Last Updated**: 2025-11-08
**Alignment**:
- Investigation Architecture v2.0 (Milestone-Based)
- Case Model Design v2.0
- Current Implementation (faultmaven/infrastructure/persistence/)

**Critical Updates in v2.1**:
- ✅ Two-dimensional storage architecture (backend × data type)
- ✅ Multiple storage technologies (PostgreSQL, Redis, ChromaDB)
- ✅ Pluggable adapters for each data type
- ✅ Configuration-based backend selection per storage system
- ✅ 13-method CaseRepository interface (not 7)
- ✅ Accurate configuration variable names

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Two-Dimensional Storage Architecture](#2-two-dimensional-storage-architecture)
3. [Storage Technologies by Data Type](#3-storage-technologies-by-data-type)
4. [Repository Pattern by Data Type](#4-repository-pattern-by-data-type)
5. [Storage Backend Options](#5-storage-backend-options)
6. [Configuration Management](#6-configuration-management)
7. [Testing Strategy](#7-testing-strategy)
8. [Performance Considerations](#8-performance-considerations)
9. [Appendices](#9-appendices)

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
(Cases/Users)  │ ✅ Impl.     │ ⚠️ Future    │ ✅ Impl.     │
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
Vector         │ Python dict  │ ChromaDB     │ ChromaDB     │
(Knowledge)    │ ✅ Impl.     │ ✅ Impl.     │ ✅ Impl.     │
               │              │              │              │
Technology:    │ InMemory     │ ChromaDB     │ ChromaDB     │
               │ VectorStore  │ (local)      │ (server)     │
└──────────────┴──────────────┴──────────────┴──────────────┘

Configuration Example:
  # Long-term data storage
  CASE_STORAGE_TYPE=inmemory       # or: postgres
  USER_STORAGE_TYPE=inmemory       # or: postgres

  # Cached data storage
  SESSION_STORAGE_TYPE=inmemory    # or: redis

  # Vector data storage
  VECTOR_STORAGE_TYPE=inmemory     # or: chromadb
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
| Local Files | `SQLiteCaseRepository` | ⚠️ Future | Single-node, offline |
| Microservices | `PostgreSQLCaseRepository` | ✅ Implemented | Production K8s |

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

| Backend | Implementation | Status | Use Case |
|---------|----------------|--------|----------|
| In-Memory | `InMemorySessionStore` | ✅ Implemented | Development, testing |
| Local Files | `FileSessionStore` | ⚠️ Future | Single-node, offline |
| Microservices | `RedisSessionStore` | ✅ Implemented | Production K8s |

**Configuration**:
```bash
SESSION_STORAGE_TYPE=inmemory  # Development
SESSION_STORAGE_TYPE=file      # Future: single-node
SESSION_STORAGE_TYPE=redis     # Production
```

**Data Includes**:
- User sessions
- Investigation state (current turn, pending requests)
- Temporary caches
- Rate limiting data
- Real-time message queues

**TTL Strategy**:
- Sessions: 30 days
- Investigation state: 7 days
- Temporary caches: 1 hour

---

### 3.3 Vector Embeddings Data (Knowledge Base, Semantic Search)

**Requirements**:
- Vector similarity search
- Embedding storage (768-dim vectors)
- Semantic queries
- RAG (Retrieval-Augmented Generation) support

**Technology**: **ChromaDB** (vector database)

**Storage Backends**:

| Backend | Implementation | Status | Use Case |
|---------|----------------|--------|----------|
| In-Memory | `InMemoryVectorStore` | ✅ Implemented | Development, testing |
| Local Files | `ChromaDB (persistent)` | ✅ Implemented | Development, single-node |
| Microservices | `ChromaDB (client-server)` | ✅ Implemented | Production K8s |

**Configuration**:
```bash
# Vector storage type selection
VECTOR_STORAGE_TYPE=inmemory   # Development/testing (simple word-based)
VECTOR_STORAGE_TYPE=chromadb   # Production (true semantic embeddings)

# ChromaDB configuration (when TYPE=chromadb)
CHROMADB_URL=http://chromadb.faultmaven.local:30080
CHROMADB_API_KEY=your_chromadb_token_here
CHROMADB_COLLECTION=faultmaven_kb
```

**Data Includes**:
- Knowledge base documents
- Document embeddings (BGE-M3, 768 dimensions)
- Evidence summaries (for semantic search)
- Historical solution patterns
- Troubleshooting playbooks

**Note**: For production semantic search, use ChromaDB. For development/testing without external dependencies, use InMemory (simple word-based similarity).

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

    Technology: Relational database (PostgreSQL/SQLite)

    Implementations:
    - InMemoryCaseRepository: RAM (development)
    - PostgreSQLCaseRepository: K8s PostgreSQL (production)
    - SQLiteCaseRepository: Local file (future)
    """

    # Core CRUD (5 methods)
    @abstractmethod
    async def save(self, case: Case) -> Case: ...

    @abstractmethod
    async def get(self, case_id: str) -> Optional[Case]: ...

    @abstractmethod
    async def list(...) -> tuple[List[Case], int]: ...

    @abstractmethod
    async def delete(self, case_id: str) -> bool: ...

    @abstractmethod
    async def search(...) -> tuple[List[Case], int]: ...

    # Message Management (2 methods)
    @abstractmethod
    async def add_message(self, case_id: str, message_dict: dict) -> bool: ...

    @abstractmethod
    async def get_messages(...) -> List[dict]: ...

    # Activity Tracking (1 method)
    @abstractmethod
    async def update_activity_timestamp(self, case_id: str) -> bool: ...

    # Analytics (1 method)
    @abstractmethod
    async def get_analytics(self, case_id: str) -> Dict[str, Any]: ...

    # Maintenance (1 method)
    @abstractmethod
    async def cleanup_expired(...) -> int: ...

    # Session Association (1 method)
    @abstractmethod
    async def find_by_session(...) -> tuple[List[Case], int]: ...

    # Transaction Support (1 method)
    async def begin_transaction(self): ...
```

**Total: 13 methods**

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
    - InMemorySessionStore: RAM (development)
    - RedisSessionStore: K8s Redis (production)
    - FileSessionStore: Local file (future)
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
- ✅ `InMemoryCaseRepository` - Cases in Python dict
- ✅ `InMemorySessionStore` - Sessions in Python dict
- ✅ `InMemoryVectorStore` - Simple word-based similarity (no embeddings)

**Configuration**:
```bash
# .env.development
CASE_STORAGE_TYPE=inmemory
USER_STORAGE_TYPE=inmemory
SESSION_STORAGE_TYPE=inmemory
VECTOR_STORAGE_TYPE=inmemory
```

**Characteristics**:
- ✅ Zero setup
- ✅ Microsecond operations
- ✅ Perfect for tests
- ❌ Data lost on restart
- ❌ Single process only

---

### 5.2 Local Files Backend (Filesystem)

**Use Case**: Single-node deployment, offline development

**Implementations** (future):
- ⚠️ `SQLiteCaseRepository` - Cases in SQLite file
- ⚠️ `FileSessionStore` - Sessions in local files
- ⚠️ Future: ChromaDB local mode could replace in-memory for persistence

**Configuration** (future):
```bash
# .env.singlenode
CASE_STORAGE_TYPE=sqlite
SQLITE_DB_PATH=data/faultmaven.db

SESSION_STORAGE_TYPE=file
SESSION_FILE_PATH=data/sessions/

VECTOR_STORAGE_TYPE=chromadb
CHROMADB_URL=http://localhost:8000
```

**Characteristics**:
- ✅ Persistent local storage
- ✅ No external dependencies
- ✅ Single file database (SQLite)
- ❌ Limited concurrency
- ❌ Not distributed

**Status**: ⚠️ **Planned - Can be added as needed**

---

### 5.3 Microservices Backend (Kubernetes)

**Use Case**: Production deployment, high availability

**Implementations**:
- ✅ `PostgreSQLCaseRepository` - Cases in K8s PostgreSQL
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
# Technology: Redis
# Options: inmemory, file (future), redis

SESSION_STORAGE_TYPE=redis           # or: inmemory

# Redis Configuration (when TYPE=redis)
REDIS_HOST=redis.faultmaven.local
REDIS_PORT=6379
REDIS_PASSWORD=secure_password
REDIS_DB=0
REDIS_TTL_SESSIONS=2592000          # 30 days

# ===========================================
# VECTOR DATA (Knowledge Base, Embeddings)
# ===========================================
# Technology: ChromaDB (production) or InMemory (dev)
# Options: inmemory, chromadb

VECTOR_STORAGE_TYPE=chromadb         # or: inmemory

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
SESSION_STORAGE_TYPE=inmemory
VECTOR_STORAGE_TYPE=inmemory
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
CHROMADB_URL=http://localhost:8000
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
            self.case_repository = PostgreSQLCaseRepository(session_factory())

        else:
            # In-memory backend (development)
            self.case_repository = InMemoryCaseRepository()

        # ==========================================
        # CACHED DATA: Session Store
        # ==========================================
        session_storage_type = settings.cache.session_storage_type.lower()

        if session_storage_type == "redis":
            # Redis backend (production)
            redis_client = aioredis.from_url(
                settings.cache.redis_url,
                encoding="utf-8",
                decode_responses=True
            )
            self.session_store = RedisSessionStore(redis_client)

        else:
            # In-memory backend (development)
            self.session_store = InMemorySessionStore()

        # ==========================================
        # VECTOR DATA: Vector Store (InMemory or ChromaDB)
        # ==========================================
        vector_storage_type = settings.database.vector_storage_type.lower()

        if vector_storage_type == "chromadb":
            # ChromaDB backend (production)
            from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
            self.vector_store = ChromaDBVectorStore()

        else:
            # In-memory backend (development)
            from faultmaven.infrastructure.persistence.inmemory_vector_store import InMemoryVectorStore
            self.vector_store = InMemoryVectorStore()
```

**Key Points**:
- ✅ Each data type configured independently
- ✅ Each can use different backend
- ✅ Business logic receives abstract interfaces
- ✅ Zero code changes when switching backends

---

## 7. Testing Strategy

### 7.1 Contract Tests by Data Type

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

    # ... 13 methods × multiple test scenarios


class TestInMemoryCaseRepository(CaseRepositoryContractTests):
    @pytest.fixture
    def repository(self):
        return InMemoryCaseRepository()


class TestPostgreSQLCaseRepository(CaseRepositoryContractTests):
    @pytest.fixture
    def repository(self):
        return PostgreSQLCaseRepository(test_db_session)
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


class TestInMemorySessionStore(SessionStoreContractTests):
    @pytest.fixture
    def store(self):
        return InMemorySessionStore()


class TestRedisSessionStore(SessionStoreContractTests):
    @pytest.fixture
    def store(self):
        return RedisSessionStore(test_redis_client)
```

### 7.2 Multi-Backend Integration Tests

Test that services work correctly with **any combination** of backends:

```python
@pytest.mark.parametrize("case_backend,session_backend", [
    ("inmemory", "inmemory"),
    ("inmemory", "redis"),
    ("postgres", "inmemory"),
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

## 8. Performance Considerations

### 8.1 Performance by Data Type and Backend

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

### 8.2 Optimization Strategies

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

# TTL strategy
sessions: 30 days
temp_state: 1 hour
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

## 9. Appendices

### Appendix A: Complete Storage Matrix

**Current Implementation Status**:

| Data Type | Technology | InMemory | Local Files | Microservices |
|-----------|-----------|----------|-------------|---------------|
| **Cases** | PostgreSQL/SQLite | ✅ Impl. | ⚠️ Future | ✅ Impl. |
| **Users** | PostgreSQL/SQLite | ✅ Impl. | ⚠️ Future | ✅ Impl. |
| **Sessions** | Redis | ✅ Impl. | ⚠️ Future | ✅ Impl. |
| **Knowledge** | InMemory/ChromaDB | ✅ Impl. | ⚠️ Future | ✅ Impl. |

### Appendix B: Migration Scenarios

**Development → Production**:
```bash
# Change configuration only (no code changes)

# FROM (Development):
CASE_STORAGE_TYPE=inmemory
SESSION_STORAGE_TYPE=inmemory
VECTOR_STORAGE_TYPE=inmemory

# TO (Production):
CASE_STORAGE_TYPE=postgres
SESSION_STORAGE_TYPE=redis
VECTOR_STORAGE_TYPE=chromadb
```

**Hybrid Deployment** (possible):
```bash
# Cases in production DB, sessions still in-memory for testing
CASE_STORAGE_TYPE=postgres
SESSION_STORAGE_TYPE=inmemory
VECTOR_STORAGE_TYPE=chromadb
```

### Appendix C: Adding New Storage Backend

**Example**: Adding SQLite backend for cases

1. Implement `SQLiteCaseRepository` class:
   ```python
   class SQLiteCaseRepository(CaseRepository):
       """Implement all 13 methods using SQLite."""
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
- Long-term data → PostgreSQL technology
- Cached data → Redis technology
- Vector data → ChromaDB technology

**Dimension 2 - Storage Backends** (3 options):
- In-memory → Development/testing
- Local files → Single-node (future)
- Microservices → Production K8s

**Current Status**:
- ✅ InMemory + Microservices backends implemented
- ✅ All three data types supported
- ⚠️ Local Files backend planned for future

**Key Benefits**:
- ✅ Technology-appropriate storage for each data type
- ✅ Flexible backend deployment options
- ✅ Independent configuration per data type
- ✅ Zero code changes when switching backends

---

**Document Version**: 2.1.0
**Last Updated**: 2025-11-08
**Status**: ✅ Accurately reflects current implementation and design objectives
