# FaultMaven Deployment Strategy v2.0

## Executive Summary

This document defines the deployment strategy for FaultMaven, supporting two distinct deployment scenarios:
- **Local Deployment**: Single-user, self-hosted, free tier
- **Cloud Deployment**: Multi-user, managed SaaS, subscription-based

Both deployments share **FaultMaven Core** - the investigation engine - while differing in infrastructure providers and feature availability.

**Version**: 2.0
**Date**: 2025-12-31
**Status**: Approved Design

---

## Table of Contents

1. [Architecture Philosophy](#1-architecture-philosophy)
2. [FaultMaven Core Definition](#2-faultmaven-core-definition)
3. [Deployment Scenarios](#3-deployment-scenarios)
4. [Infrastructure Providers](#4-infrastructure-providers)
5. [Knowledge Base Architecture](#5-knowledge-base-architecture)
6. [Data Model](#6-data-model)
7. [Implementation Strategy](#7-implementation-strategy)
8. [Gap Analysis](#8-gap-analysis)
9. [Implementation Roadmap](#9-implementation-roadmap)

---

## 1. Architecture Philosophy

### Core Principle

> **Two separate systems sharing one core engine.**

| Aspect | Local Deployment | Cloud Deployment |
|--------|------------------|------------------|
| **Analogy** | Computer NOT connected to Internet | Computer connected to Internet |
| **Runtime** | App runs on user's machine | App runs on managed infrastructure |
| **Data** | Stored locally | Stored in cloud |
| **Users** | Single user, isolated | Multiple users, can share |
| **Knowledge** | User builds their own KB | Global KB + Org KB + User KB |
| **Price** | Free | Subscription |

### What They Share

```
┌─────────────────────────────────────────────────────────────────┐
│                      FaultMaven Core                             │
│                                                                  │
│  The investigation engine - identical in both deployments        │
│                                                                  │
│  • Case Management                                               │
│  • Session Management (conversation state)                       │
│  • Evidence Processing (parsers, analyzers)                      │
│  • Knowledge Base Engine (RAG pipeline, embeddings, search)      │
│  • Agent Orchestration (OODA framework, LLM integration)         │
│  • Reporting                                                     │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### What They Don't Share

- Infrastructure providers (database, storage, vector, cache)
- User management and authentication systems
- Organization and sharing features (Cloud only)
- Global Knowledge Base (Cloud only)

---

## 2. FaultMaven Core Definition

### Core Components

| Component | Description | Key Interfaces |
|-----------|-------------|----------------|
| **Case Management** | Create, update, close investigation cases | `CaseRepository`, `CaseService` |
| **Session Management** | Conversation context, state persistence | `SessionRepository`, `ISessionStore` |
| **Evidence Processing** | Upload, parse, analyze logs/screenshots/configs | `EvidenceArtifactRepository`, `IPreprocessor` |
| **Knowledge Base Engine** | RAG pipeline, semantic search, embeddings | `IVectorStore`, `KnowledgeItemRepository` |
| **Agent Orchestration** | LLM integration, tool execution, OODA framework | `ILLMProvider`, `BaseTool` |
| **Reporting** | Generate investigation reports | `IReportService` |

### Core Interfaces (Already Exist)

The codebase already defines comprehensive interfaces in `faultmaven/models/interfaces.py`:

```python
# Infrastructure interfaces (already implemented)
class ILLMProvider(ABC):           # LLM abstraction
class IVectorStore(ABC):           # Vector database abstraction
class ISessionStore(ABC):          # Session storage abstraction
class ISanitizer(ABC):             # PII redaction abstraction
class ITracer(ABC):                # Observability abstraction
class IConfiguration(ABC):         # Configuration abstraction
class IStorageBackend(ABC):        # File storage abstraction

# Data processing interfaces (already implemented)
class IDataClassifier(ABC):        # Data type classification
class ILogProcessor(ABC):          # Log analysis
class IPreprocessor(ABC):          # Data preprocessing
class IKnowledgeIngester(ABC):     # KB document ingestion
```

### Core Services (Already Exist)

Located in `faultmaven/services/`:

- `case_service.py` - Case lifecycle management
- `investigation_session_service.py` - Investigation sessions
- `evidence_artifact_service.py` - Evidence handling
- `knowledge_search_service.py` - KB search (RAG)
- `vector_store_service.py` - Vector operations
- `agent_orchestration_service.py` - Agent coordination

---

## 3. Deployment Scenarios

### 3.1 Local Deployment

```
┌─────────────────────────────────────────────────────────────────┐
│                     LOCAL DEPLOYMENT                             │
│                                                                  │
│  User installs FaultMaven locally                                │
│  • No internet required (except for LLM API calls)               │
│  • No account on FaultMaven Cloud                                │
│  • Completely isolated                                           │
│                                                                  │
│  What they get:                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ FaultMaven Core (full functionality)                     │    │
│  │ • Create cases, investigate, use agents                  │    │
│  │ • Upload and analyze evidence                            │    │
│  │ • Build personal knowledge base (starts empty)           │    │
│  │ • Generate reports                                       │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│  What they DON'T get:                                            │
│  • Global KB (provider's curated knowledge)                      │
│  • Sharing with anyone                                           │
│  • Organization features                                         │
│  • Managed infrastructure                                        │
│                                                                  │
│  Storage Layout:                                                 │
│  └── ./data/                                                     │
│      ├── faultmaven.db      (SQLite)                            │
│      ├── evidence/          (uploaded files)                     │
│      └── chroma/            (vector embeddings)                  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**Configuration** (`.env`):
```bash
# Infrastructure
DATABASE_URL=sqlite+aiosqlite:///./data/faultmaven.db
STORAGE_BACKEND=local
STORAGE_PATH=./data/evidence
VECTOR_BACKEND=chroma
CHROMA_PATH=./data/chroma
CACHE_BACKEND=memory

# LLM (user provides their own key)
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-...
```

### 3.2 Cloud Deployment

```
┌─────────────────────────────────────────────────────────────────┐
│                     CLOUD DEPLOYMENT                             │
│                                                                  │
│  User creates account on FaultMaven Cloud                        │
│  • Internet connected                                            │
│  • Managed infrastructure                                        │
│  • Connected to ecosystem                                        │
│                                                                  │
│  What they get:                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ FaultMaven Core (same as local)                          │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│  PLUS:                                                           │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ Global KB (read-only, provider-curated)                  │    │
│  │ • Common troubleshooting patterns                        │    │
│  │ • Technology-specific runbooks                           │    │
│  │ • Best practices, known issues                           │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ Organization Features (optional)                         │    │
│  │ • Create or join organization                            │    │
│  │ • Organization KB (shared knowledge)                     │    │
│  │ • Share cases with org members                           │    │
│  │ • Share cases with external users                        │    │
│  │ • Team collaboration                                     │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│  Storage: Managed cloud infrastructure                           │
│  • PostgreSQL (cases, sessions, users, orgs)                     │
│  • S3 (evidence files)                                           │
│  • Pinecone/pgvector (KB embeddings)                             │
│  • Redis (sessions, cache)                                       │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**Configuration** (environment/secrets):
```bash
# Infrastructure
DATABASE_URL=postgresql+asyncpg://user:pass@postgres:5432/faultmaven
STORAGE_BACKEND=s3
S3_BUCKET=faultmaven-evidence
AWS_REGION=us-east-1
VECTOR_BACKEND=pinecone
PINECONE_API_KEY=...
PINECONE_INDEX=faultmaven-kb
CACHE_BACKEND=redis
REDIS_URL=redis://redis:6379

# Global KB
GLOBAL_KB_ENABLED=true
GLOBAL_KB_INDEX=faultmaven-global-kb

# LLM (platform key)
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-...
```

---

## 4. Infrastructure Providers

### Provider Matrix

| Interface | Local Provider | Cloud Provider | Status |
|-----------|---------------|----------------|--------|
| `IDatabase` | SQLite | PostgreSQL | PostgreSQL exists, SQLite needed |
| `IStorage` | LocalFileStorage | S3Storage | Local exists, S3 needed |
| `IVectorStore` | ChromaDB | Pinecone | ChromaDB exists, Pinecone needed |
| `ICache` | InMemoryCache | RedisCache | Both exist |
| `ILLMProvider` | Same | Same | Exists (multiple providers) |

### 4.1 Database Layer

**Interface**: Already abstracted through SQLAlchemy ORM

**Current State**:
- PostgreSQL: Fully implemented (`database.py`, `database_*_repository.py`)
- SQLite: NOT implemented (needed for local)

**Implementation Strategy**:
```python
# Database URL determines dialect automatically via SQLAlchemy
# Local:  sqlite+aiosqlite:///./data/faultmaven.db
# Cloud:  postgresql+asyncpg://user:pass@host:port/db

# SQLite-specific configuration needed:
if database_url.startswith("sqlite"):
    connect_args = {
        "timeout": 30,
        "check_same_thread": False,
    }
    # Enable WAL mode for better concurrency
```

### 4.2 Storage Layer

**Interface**: `IStorageBackend` (exists in `models/interfaces.py`)

**Current State**:
- LocalFileStorage: Partially implemented (`file_storage_service.py`)
- S3Storage: NOT implemented

**Implementation Strategy**:
```python
# faultmaven/infrastructure/storage/base.py
class IStorageBackend(ABC):
    async def store(self, key: str, data: bytes, content_type: str) -> str: ...
    async def retrieve(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    async def get_url(self, key: str, expiry: int = 3600) -> str: ...

# faultmaven/infrastructure/storage/local.py
class LocalStorageBackend(IStorageBackend):
    def __init__(self, base_path: str = "./data/evidence"): ...

# faultmaven/infrastructure/storage/s3.py
class S3StorageBackend(IStorageBackend):
    def __init__(self, bucket: str, region: str): ...
```

### 4.3 Vector Layer

**Interface**: `IVectorStore` (exists in `models/interfaces.py`)

**Current State**:
- ChromaDB: Fully implemented (`chromadb.py`, `chromadb_store.py`)
- Pinecone: NOT implemented

**Implementation Strategy**:
```python
# faultmaven/infrastructure/vector/pinecone_store.py
class PineconeVectorStore(IVectorStore):
    def __init__(self, api_key: str, index_name: str): ...

    async def add_documents(self, documents: List[Dict]) -> None: ...
    async def search(self, query: str, k: int = 5) -> List[Dict]: ...
    async def delete_documents(self, ids: List[str]) -> None: ...
```

### 4.4 Cache Layer

**Interface**: `ISessionStore` (exists in `models/interfaces.py`)

**Current State**:
- InMemory: Implemented (`inmemory_session_store.py`)
- Redis: Implemented (`redis_session_store.py`)

**No additional work needed.**

---

## 5. Knowledge Base Architecture

### 5.1 Knowledge Scope Model

| Scope | Local | Cloud | Description |
|-------|-------|-------|-------------|
| **Global** | ❌ None | ✅ Provider-built | Curated by FaultMaven, read-only for users |
| **Organization** | ❌ No orgs | ✅ Shared in org | Created by org members, shared within org |
| **User** | ✅ Starts empty | ✅ Private | User builds their own, private by default |

### 5.2 Data Model

```python
class KnowledgeScope(Enum):
    GLOBAL = "global"           # Provider-curated (Cloud only)
    ORGANIZATION = "organization"  # Shared within org (Cloud only)
    USER = "user"               # Private to user

@dataclass
class KnowledgeItem:
    item_id: str
    content: str
    embeddings: List[float]

    # Scoping
    scope: KnowledgeScope
    scope_id: Optional[str]     # null for global, org_id or user_id

    # Metadata
    created_by: str
    created_at: datetime

    # Promotion tracking (user → org)
    promoted_from_user_id: Optional[str]
    promoted_at: Optional[datetime]
```

### 5.3 Search Logic

```python
async def search_knowledge(
    user_id: str,
    org_id: Optional[str],      # None for local deployment
    query: str,
    include_global: bool = True  # False for local deployment
) -> List[KnowledgeItem]:
    """Search knowledge base with proper scoping."""

    scopes = []

    # Always include user's private KB
    scopes.append((KnowledgeScope.USER, user_id))

    # Include org KB if user belongs to org (Cloud only)
    if org_id:
        scopes.append((KnowledgeScope.ORGANIZATION, org_id))

    # Include global KB (Cloud only)
    if include_global:
        scopes.append((KnowledgeScope.GLOBAL, None))

    return await vector_store.search(query, filters={"scope": scopes})
```

### 5.4 KB Promotion (Cloud Only)

Users can promote their private KB items to organizational scope:

```python
async def promote_to_org_kb(
    item_id: str,
    user_id: str,
    org_id: str,
) -> None:
    """Move a user's KB item to organizational scope."""
    item = await kb_repo.get(item_id)

    # Verify ownership
    if item.scope != KnowledgeScope.USER or item.scope_id != user_id:
        raise PermissionDenied("Can only promote own KB items")

    # Verify org membership
    if not await org_repo.is_member(user_id, org_id):
        raise PermissionDenied("Must be org member to promote")

    # Promote
    item.scope = KnowledgeScope.ORGANIZATION
    item.scope_id = org_id
    item.promoted_from_user_id = user_id
    item.promoted_at = datetime.utcnow()

    await kb_repo.update(item)
```

---

## 6. Data Model

### 6.1 Case Ownership

```python
class OwnerType(Enum):
    USER = "user"               # Individual owns it
    ORGANIZATION = "organization"  # Org owns it (Cloud only)

class CaseVisibility(Enum):
    PRIVATE = "private"         # Only owner can see
    ORG = "org"                 # All org members can see (Cloud only)
    SHARED = "shared"           # Specific users can see (Cloud only)

@dataclass
class Case:
    case_id: str
    title: str
    description: str
    status: CaseStatus

    # Ownership
    owner_type: OwnerType       # LOCAL: always USER
    owner_id: str               # LOCAL: user_id, CLOUD: user_id or org_id
    created_by: str             # user_id who created it

    # Sharing (Cloud only)
    visibility: CaseVisibility  # LOCAL: always PRIVATE
    shared_with: List[str]      # LOCAL: always empty
```

### 6.2 Access Control

```python
async def can_access_case(
    user_id: str,
    user_org_id: Optional[str],  # None for local
    case: Case
) -> bool:
    """Determine if user can access a case."""

    # Owner always has access
    if case.owner_type == OwnerType.USER and case.owner_id == user_id:
        return True

    # Org member can access org-owned cases (Cloud only)
    if case.owner_type == OwnerType.ORGANIZATION:
        if case.owner_id == user_org_id:
            return True

    # Check explicit sharing (Cloud only)
    if case.visibility == CaseVisibility.SHARED:
        if user_id in case.shared_with:
            return True

    return False
```

---

## 7. Implementation Strategy

### 7.1 Module Structure

```
faultmaven/
│
├── core/                           # FaultMaven Core (shared by both)
│   ├── cases/
│   │   ├── models.py
│   │   ├── service.py
│   │   └── repository.py
│   │
│   ├── sessions/
│   │   ├── models.py
│   │   ├── service.py
│   │   └── repository.py
│   │
│   ├── evidence/
│   │   ├── models.py
│   │   ├── service.py
│   │   └── processors/
│   │
│   ├── knowledge/
│   │   ├── models.py
│   │   ├── service.py
│   │   ├── scopes.py               # NEW: Scope-aware search
│   │   └── repository.py
│   │
│   ├── agents/
│   │   ├── orchestrator.py
│   │   ├── tools/
│   │   └── llm/
│   │
│   └── interfaces/                 # Core interfaces
│       ├── database.py
│       ├── storage.py
│       ├── vector.py
│       └── cache.py
│
├── infrastructure/                 # Provider implementations
│   ├── database/
│   │   ├── sqlite.py               # NEW: SQLite support
│   │   └── postgres.py             # Existing
│   │
│   ├── storage/
│   │   ├── local.py                # Existing (needs interface alignment)
│   │   └── s3.py                   # NEW: S3 support
│   │
│   ├── vector/
│   │   ├── chroma.py               # Existing
│   │   └── pinecone.py             # NEW: Pinecone support
│   │
│   ├── cache/
│   │   ├── memory.py               # Existing
│   │   └── redis.py                # Existing
│   │
│   └── llm/                        # Existing (shared)
│       ├── anthropic.py
│       ├── openai.py
│       └── fireworks.py
│
├── local/                          # Local deployment package
│   ├── __main__.py                 # Entry point
│   ├── app.py                      # FastAPI app (core routes only)
│   ├── auth.py                     # Local single-user auth
│   └── config.py                   # Local configuration
│
├── cloud/                          # Cloud deployment package
│   ├── app.py                      # FastAPI app (full routes)
│   ├── auth.py                     # Cloud auth (OAuth, JWT)
│   ├── config.py                   # Cloud configuration
│   │
│   ├── organizations/              # Cloud-only
│   │   ├── models.py
│   │   ├── service.py
│   │   └── routes.py
│   │
│   ├── sharing/                    # Cloud-only
│   │   ├── models.py
│   │   ├── service.py
│   │   └── routes.py
│   │
│   ├── global_kb/                  # Cloud-only
│   │   ├── service.py
│   │   └── admin.py
│   │
│   └── billing/                    # Cloud-only
│       ├── models.py
│       └── service.py
│
└── api/                            # Shared API components
    ├── routes/                     # Core routes (used by both)
    │   ├── cases.py
    │   ├── sessions.py
    │   ├── evidence.py
    │   └── knowledge.py
    └── middleware/
```

### 7.2 Provider Factory Pattern

Leverage existing `repository_factory.py` pattern:

```python
# faultmaven/infrastructure/provider_factory.py

from enum import Enum
from typing import Protocol

class StorageBackend(str, Enum):
    LOCAL = "local"
    S3 = "s3"

class VectorBackend(str, Enum):
    CHROMA = "chroma"
    PINECONE = "pinecone"

def get_storage_backend(settings: Settings) -> IStorageBackend:
    """Factory for storage backend based on configuration."""
    backend = settings.storage_backend

    if backend == StorageBackend.LOCAL:
        from .storage.local import LocalStorageBackend
        return LocalStorageBackend(settings.storage_path)

    elif backend == StorageBackend.S3:
        from .storage.s3 import S3StorageBackend
        return S3StorageBackend(
            bucket=settings.s3_bucket,
            region=settings.aws_region
        )

    raise ValueError(f"Unknown storage backend: {backend}")

def get_vector_backend(settings: Settings) -> IVectorStore:
    """Factory for vector backend based on configuration."""
    backend = settings.vector_backend

    if backend == VectorBackend.CHROMA:
        from .vector.chroma import ChromaDBVectorStore
        return ChromaDBVectorStore(path=settings.chroma_path)

    elif backend == VectorBackend.PINECONE:
        from .vector.pinecone import PineconeVectorStore
        return PineconeVectorStore(
            api_key=settings.pinecone_api_key,
            index_name=settings.pinecone_index
        )

    raise ValueError(f"Unknown vector backend: {backend}")
```

### 7.3 Configuration Schema

```python
# faultmaven/config/settings.py (additions)

class InfrastructureSettings(BaseSettings):
    """Infrastructure provider configuration."""

    # Database
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/faultmaven.db",
        description="Database connection URL"
    )

    # Storage
    storage_backend: str = Field(
        default="local",
        description="Storage backend: local, s3"
    )
    storage_path: str = Field(
        default="./data/evidence",
        description="Local storage path (when storage_backend=local)"
    )
    s3_bucket: Optional[str] = Field(
        default=None,
        description="S3 bucket name (when storage_backend=s3)"
    )
    aws_region: str = Field(
        default="us-east-1",
        description="AWS region for S3"
    )

    # Vector
    vector_backend: str = Field(
        default="chroma",
        description="Vector backend: chroma, pinecone"
    )
    chroma_path: str = Field(
        default="./data/chroma",
        description="ChromaDB storage path (when vector_backend=chroma)"
    )
    pinecone_api_key: Optional[str] = Field(
        default=None,
        description="Pinecone API key (when vector_backend=pinecone)"
    )
    pinecone_index: Optional[str] = Field(
        default=None,
        description="Pinecone index name"
    )

    # Cache
    cache_backend: str = Field(
        default="memory",
        description="Cache backend: memory, redis"
    )
    redis_url: Optional[str] = Field(
        default=None,
        description="Redis URL (when cache_backend=redis)"
    )

    # Knowledge Base
    global_kb_enabled: bool = Field(
        default=False,
        description="Enable global KB (Cloud only)"
    )
    global_kb_index: Optional[str] = Field(
        default=None,
        description="Global KB index name"
    )
```

---

## 8. Gap Analysis

### 8.1 Summary Matrix

| Component | Target Design | Current State | Gap Level | Priority |
|-----------|---------------|---------------|-----------|----------|
| **SQLite Support** | Required for local | Not implemented | 🔴 High | P0 |
| **S3 Storage** | Required for cloud | Not implemented | 🟡 Medium | P1 |
| **Pinecone Vector** | Required for cloud | Not implemented | 🟡 Medium | P1 |
| **KB Scoping** | Global/Org/User | No scope concept | 🔴 High | P0 |
| **Core/Deployment Split** | Separate packages | Mixed code | 🟡 Medium | P1 |
| **Provider Factory** | Unified factory | Per-repo factories | 🟢 Low | P2 |
| **Local Auth** | Simplified single-user | Full JWT auth | 🟡 Medium | P1 |

### 8.2 Detailed Gap Analysis

#### GAP-001: SQLite Database Support

**Current**: PostgreSQL only via `asyncpg`
**Target**: SQLite for local, PostgreSQL for cloud

**Work Required**:
1. Add `aiosqlite` dependency
2. Test SQLAlchemy models with SQLite dialect
3. Add SQLite-specific connection configuration (WAL mode, timeout)
4. Verify Alembic migrations work with SQLite
5. Test concurrent access behavior

**Files Affected**:
- `faultmaven/infrastructure/persistence/database.py`
- `requirements.txt`
- `alembic/env.py`

**Estimated Effort**: 2-3 days

---

#### GAP-002: S3 Storage Provider

**Current**: Local filesystem via `file_storage_service.py`
**Target**: S3 for cloud deployments

**Work Required**:
1. Create `IStorageBackend` protocol (extract from existing interface)
2. Implement `S3StorageBackend` class using `boto3`/`aioboto3`
3. Add presigned URL generation
4. Add factory for backend selection
5. Update evidence service to use abstraction

**Files to Create**:
- `faultmaven/infrastructure/storage/base.py`
- `faultmaven/infrastructure/storage/s3.py`
- `faultmaven/infrastructure/storage/factory.py`

**Files to Modify**:
- `faultmaven/services/file_storage_service.py`
- `faultmaven/services/evidence_artifact_service.py`

**Estimated Effort**: 3-4 days

---

#### GAP-003: Pinecone Vector Provider

**Current**: ChromaDB only
**Target**: Pinecone for cloud scale

**Work Required**:
1. Implement `PineconeVectorStore` conforming to `IVectorStore`
2. Handle score normalization (Pinecone uses cosine similarity)
3. Add namespace support for multi-tenant isolation
4. Add factory for backend selection
5. Test embedding compatibility

**Files to Create**:
- `faultmaven/infrastructure/vector/pinecone_store.py`
- `faultmaven/infrastructure/vector/factory.py`

**Files to Modify**:
- `faultmaven/services/vector_store_service.py`
- `faultmaven/services/knowledge_search_service.py`

**Estimated Effort**: 3-4 days

---

#### GAP-004: Knowledge Base Scoping

**Current**: No scope concept in KB items
**Target**: Global/Organization/User scoping

**Work Required**:
1. Add `scope` and `scope_id` to KnowledgeItem model
2. Update KB repository to filter by scope
3. Add scope-aware search logic
4. Add promotion endpoint (user → org)
5. Update UI to display scope badges

**Files to Modify**:
- `faultmaven/models/interfaces_kb.py`
- `faultmaven/infrastructure/persistence/knowledge_item_repository.py`
- `faultmaven/services/knowledge_search_service.py`
- `faultmaven/api/v1/routes/knowledge.py`

**Database Migration**: Add `scope`, `scope_id` columns

**Estimated Effort**: 4-5 days

---

#### GAP-005: Core/Deployment Separation

**Current**: All code in single package structure
**Target**: Separate `local/` and `cloud/` packages

**Work Required**:
1. Create `faultmaven/local/` package with entry point
2. Create `faultmaven/cloud/` package with entry point
3. Move organization routes to `cloud/organizations/`
4. Create simplified auth for local deployment
5. Create separate FastAPI app factories

**Approach**: Gradual extraction, not big-bang refactor

**Estimated Effort**: 5-7 days

---

#### GAP-006: Case Ownership Model

**Current**: Cases have `created_by` but no `owner_type` or sharing
**Target**: Full ownership and sharing model

**Work Required**:
1. Add `owner_type`, `owner_id`, `visibility`, `shared_with` to Case model
2. Update case repository with ownership filters
3. Add sharing endpoints (Cloud only)
4. Update access control middleware

**Files to Modify**:
- `faultmaven/models/case.py`
- `faultmaven/infrastructure/persistence/case_repository.py`
- `faultmaven/services/case_service.py`
- `faultmaven/api/v1/routes/case.py`

**Database Migration**: Add ownership columns to cases table

**Estimated Effort**: 3-4 days

---

## 9. Implementation Roadmap

### Phase 1: Local Deployment Foundation (Weeks 1-2)

**Goal**: Enable local deployment with SQLite

| Task | Description | Effort | Dependencies |
|------|-------------|--------|--------------|
| 1.1 | Add SQLite support to database layer | 2d | None |
| 1.2 | Test/fix Alembic migrations for SQLite | 1d | 1.1 |
| 1.3 | Add KB scoping model (scope, scope_id) | 2d | None |
| 1.4 | Create local entry point (`faultmaven/local/`) | 2d | 1.1, 1.3 |
| 1.5 | Simplified local auth (no org context) | 1d | 1.4 |

**Deliverable**: `python -m faultmaven.local` runs with SQLite + ChromaDB

---

### Phase 2: Cloud Provider Integration (Weeks 3-4)

**Goal**: Add cloud-scale providers

| Task | Description | Effort | Dependencies |
|------|-------------|--------|--------------|
| 2.1 | Implement S3StorageBackend | 3d | None |
| 2.2 | Implement PineconeVectorStore | 3d | None |
| 2.3 | Create provider factory pattern | 2d | 2.1, 2.2 |
| 2.4 | Update services to use factories | 2d | 2.3 |

**Deliverable**: Cloud deployment uses S3 + Pinecone via configuration

---

### Phase 3: Knowledge Base Enhancement (Weeks 5-6)

**Goal**: Full KB scoping with Global/Org/User

| Task | Description | Effort | Dependencies |
|------|-------------|--------|--------------|
| 3.1 | Scope-aware KB search | 2d | 1.3 |
| 3.2 | KB promotion endpoint (user → org) | 2d | 3.1 |
| 3.3 | Global KB infrastructure (Cloud) | 3d | 2.2 |
| 3.4 | KB admin tools for provider curation | 3d | 3.3 |

**Deliverable**: Full KB scoping working in Cloud

---

### Phase 4: Case Ownership & Sharing (Weeks 7-8)

**Goal**: Full case ownership and sharing model

| Task | Description | Effort | Dependencies |
|------|-------------|--------|--------------|
| 4.1 | Add ownership model to cases | 2d | None |
| 4.2 | Sharing endpoints (Cloud only) | 3d | 4.1 |
| 4.3 | Access control middleware | 2d | 4.1 |
| 4.4 | UI updates for sharing | 3d | 4.2 |

**Deliverable**: Cases can be shared in Cloud deployment

---

### Phase 5: Deployment Separation (Weeks 9-10)

**Goal**: Clean separation of Local and Cloud packages

| Task | Description | Effort | Dependencies |
|------|-------------|--------|--------------|
| 5.1 | Extract cloud-only routes | 2d | All previous |
| 5.2 | Create `faultmaven/cloud/` package | 2d | 5.1 |
| 5.3 | Docker images for both deployments | 2d | 5.2 |
| 5.4 | Documentation and guides | 2d | 5.3 |

**Deliverable**: Two deployable artifacts (local, cloud)

---

## Appendix A: Migration Path (Local → Cloud)

When a local user wants to migrate to Cloud:

```python
async def migrate_local_to_cloud(
    local_db_path: str,
    cloud_account: CloudAccount,
) -> MigrationResult:
    """
    Migrate local deployment data to cloud.

    1. Export local data
    2. Upload to cloud storage
    3. Remap ownership to cloud user
    4. User's KB becomes their cloud user KB
    """

    # Export from SQLite
    cases = await export_cases(local_db_path)
    knowledge = await export_knowledge(local_db_path)
    evidence = await export_evidence("./data/evidence")

    # Import to cloud
    new_user_id = cloud_account.user_id

    for case in cases:
        case.owner_type = OwnerType.USER
        case.owner_id = new_user_id
        case.visibility = CaseVisibility.PRIVATE
        await cloud_case_repo.create(case)

    for item in knowledge:
        item.scope = KnowledgeScope.USER
        item.scope_id = new_user_id
        await cloud_kb_repo.create(item)

    # Upload evidence files to S3
    await s3_storage.upload_batch(evidence, prefix=f"users/{new_user_id}/")

    return MigrationResult(
        cases_migrated=len(cases),
        kb_items=len(knowledge),
        files_uploaded=len(evidence),
    )
```

---

## Appendix B: Feature Availability Matrix

| Feature | Local | Cloud Free | Cloud Pro | Cloud Enterprise |
|---------|-------|------------|-----------|------------------|
| Case Management | ✅ | ✅ | ✅ | ✅ |
| Evidence Upload | ✅ | ✅ | ✅ | ✅ |
| Agent Investigation | ✅ | ✅ | ✅ | ✅ |
| User KB | ✅ | ✅ | ✅ | ✅ |
| Global KB | ❌ | ✅ | ✅ | ✅ |
| Organizations | ❌ | ❌ | ✅ | ✅ |
| Org KB | ❌ | ❌ | ✅ | ✅ |
| Case Sharing | ❌ | ❌ | ✅ | ✅ |
| Team Collaboration | ❌ | ❌ | ✅ | ✅ |
| SSO/SAML | ❌ | ❌ | ❌ | ✅ |
| Custom KB Import | ❌ | ❌ | ✅ | ✅ |
| API Access | ❌ | ❌ | ✅ | ✅ |
| Priority Support | ❌ | ❌ | ❌ | ✅ |

---

## Document History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2025-12-30 | Solutions Architect | Initial draft |
| 2.0 | 2025-12-31 | Claude | Complete redesign based on review feedback |

---

**END OF DOCUMENT**
