# Data and Storage

This README is the canonical entry point for the data-and-storage doc set; `overview.md` covers the high-level architecture story.

Documentation for FaultMaven's persistence layer, database design, and storage architecture.

## Quick Start

**New to FaultMaven storage?** Start here:

1. **[overview.md](./overview.md)** - High-level storage architecture, technology matrix, and collection layout
2. **[schemas/](./schemas/)** - Detailed schema specifications for each data domain

## Documents

### Core Architecture

- **[evidence-file-storage.md](./evidence-file-storage.md)** - Where raw evidence blobs live
  - The FileStorageService → IFileStorageBackend seam (domain vs vendor)
  - Backend selection via `STORAGE_BACKEND` (filesystem / S3)
  - Storage-key layout, orphan-tracking sidecars, async discipline

- **[overview.md](./overview.md)** - Complete storage architecture overview
  - Three storage technologies: SQLite/PostgreSQL, ChromaDB, Redis/FakeRedis
  - ChromaDB collection layout (faultmaven_kb, faultmaven_runbooks, knowledge_items, case_{id})
  - Shared client injection pattern (Principle 5)
  - Access patterns, interfaces, and DI wiring
  - Data retention, security, and performance targets

### Schema Specifications

- **[schemas/case-schema.md](./schemas/case-schema.md)** - Case data model (hybrid normalized + JSONB)
  - Investigation lifecycle (INQUIRY → INVESTIGATING → RESOLVED → CLOSED)
  - Evidence, hypotheses, solutions, messages (normalized tables)
  - Multi-dialect support (SQLite + PostgreSQL)

- **[schemas/user-schema.md](./schemas/user-schema.md)** - User accounts, roles, and SSO integration
  - Enterprise SaaS schema (organizations, teams, RBAC)
  - 10 user-domain tables with Row-Level Security (`users`, `organizations`, `organization_members`, `roles`, `permissions`, `role_permissions`, `teams`, `team_members`, `user_audit_log`, `oauth_authorization_codes`)

- **[schemas/knowledge-schema.md](./schemas/knowledge-schema.md)** - Knowledge base storage
  - Unified KB collection (faultmaven_kb) with metadata-based scope filtering
  - Case working memory (ephemeral per-case collections)
  - Scope fields: scope, owner_id, team_id

### Implementation Guides

- **[vector-storage.md](./vector-storage.md)** - ChromaDB implementation and operations
  - Shared client pattern (PersistentClient local, HttpClient cloud)
  - Unified KB search with combined scope filter
  - Embedding model (BGE-M3), ingestion pipeline, query execution

- **[repository-pattern.md](./repository-pattern.md)** - Database abstraction layer
  - Two-dimensional architecture (data type × storage backend)
  - Repository interfaces and DI wiring
  - FakeRedis for sessions, ChromaDB PersistentClient for vectors

- **[er-diagram.md](./er-diagram.md)** - Entity-relationship diagram (auto-generated from SQLAlchemy models — authoritative table enumeration)

## Storage Technologies

High-level summary below. For the full per-data-type detail (data type × storage backend × deployment), see [repository-pattern.md §3](./repository-pattern.md#3-storage-technologies-by-data-type).

| Layer | Local Deployment | Cloud Deployment |
|-------|-----------------|------------------|
| **Relational** | SQLite (`data/faultmaven.db`) | PostgreSQL |
| **Vector (KB)** | ChromaDB PersistentClient (`data/chroma-kb/`) | ChromaDB HttpClient |
| **Vector (Evidence)** | ChromaDB PersistentClient (`data/chroma-evidence/`) | ChromaDB HttpClient |
| **Cache/Sessions** | FakeRedis (in-process) | Redis |
| **Blob Storage** | Local filesystem (`data/evidence/`) | S3 |

**Key Design Principles**:

- **Deployment Agnostic**: Same code, deployment-time selection via DI
- **No Dual Code Paths**: FakeRedis and PersistentClient are full-API replacements, not fallbacks
- **Interface-Based**: Repository pattern for testability
- **Shared Clients**: Two ChromaDB clients (KB + evidence, lifecycle-separated) and one Redis client, injected via DI

## Related Documentation

- **Security**: [../security/iam-design.md](../security/iam-design.md) - Identity and Access Management
- **AI/RAG**: [../knowledge-and-ai/knowledge-base-architecture.md](../knowledge-and-ai/knowledge-base-architecture.md) - RAG pipeline and embeddings
- **Investigation**: [../investigation-engine/milestone-based-investigation-framework.md](../investigation-engine/milestone-based-investigation-framework.md) - Case lifecycle

## Directory Structure

```text
data-and-storage/
├── README.md               # This file — master index
├── overview.md              # High-level architecture + collection layout
├── vector-storage.md        # ChromaDB implementation guide
├── repository-pattern.md    # Database abstraction layer spec
├── er-diagram.md            # ER diagram (auto-generated, authoritative table enumeration)
└── schemas/
    ├── case-schema.md       # Case data model (hybrid normalized)
    ├── user-schema.md       # User/org/team/RBAC schema
    └── knowledge-schema.md  # KB + case vector storage schemas
```
