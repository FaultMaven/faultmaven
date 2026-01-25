# Data and Storage

Documentation for FaultMaven's persistence layer, database design, and storage architecture.

## Quick Start

**New to FaultMaven storage?** Start here:

1. **[overview.md](./overview.md)** - High-level storage architecture and technology matrix
2. **[schemas/](./schemas/)** - Detailed schema specifications for each data domain

## Master Index

### Core Architecture

- **[overview.md](./overview.md)** - Complete storage architecture overview
  - 12 data categories (User, Case, Observability, Knowledge Base, etc.)
  - Storage technology matrix (PostgreSQL, Redis, ChromaDB, S3)
  - Architecture diagrams and access patterns
  - Security, compliance, scalability, and performance

### Schema Specifications

**[schemas/](./schemas/)** - Detailed schema documentation organized by domain

- **[schemas/user-schema.md](./schemas/user-schema.md)** - User accounts, roles, and SSO integration
  - Authentication (password, SSO providers)
  - Profile and preferences
  - Email verification
  - Audit trail

- **[schemas/case-schema.md](./schemas/case-schema.md)** - Complete case data model (10 PostgreSQL tables)
  - Investigation lifecycle (CONSULTING → INVESTIGATING → RESOLVED → CLOSED)
  - Conversation history and context
  - Evidence and hypotheses tracking
  - Status transitions and audit trail

- **[schemas/knowledge-schema.md](./schemas/knowledge-schema.md)** - Vector storage for KB and working memory
  - User Knowledge Base (permanent, shareable runbooks)
  - Case Working Memory (ephemeral per-case storage)
  - Global Knowledge Base (system-wide documentation)
  - ChromaDB collection architecture and sharing mechanisms

### Implementation Guides

- **[vector-storage.md](./vector-storage.md)** - ChromaDB implementation and operations
  - Physical architecture (deployment, embedding models)
  - Three vector storage systems (User KB, Case Working Memory, Global KB)
  - Document ingestion pipeline
  - Query execution flow and performance
  - Collection lifecycle management
  - API endpoints and operational procedures

- **[repository-pattern.md](./repository-pattern.md)** - Storage abstraction layer specification
  - Repository interfaces and dependency injection
  - Data access patterns
  - Testing and mocking strategies

- **[sqlmodel-analysis.md](./sqlmodel-analysis.md)** - SQLModel ORM usage and patterns
  - ORM patterns and best practices
  - Async SQLAlchemy integration
  - Performance considerations

### Legacy Reference

- **[data-storage-design.md](./data-storage-design.md)** - Comprehensive reference (2400 lines)
  - Complete implementation details for all 12 data categories
  - Migration history and technical decisions
  - **Note**: For new development, use the focused docs above

- **[async-vector-storage-implementation.md](./async-vector-storage-implementation.md)** - Implementation notes
  - Background processing with FastAPI BackgroundTasks
  - Map-reduce for large documents
  - **Note**: Consolidated into [vector-storage.md](./vector-storage.md)

## Purpose

This section covers how FaultMaven stores and retrieves data across:

- **Transactional Data**: PostgreSQL for user accounts, cases, evidence, reports
- **Session State**: Redis for ephemeral session and job tracking
- **Semantic Search**: ChromaDB for knowledge base and vector similarity
- **Blob Storage**: S3 for raw artifacts and large files
- **Caching**: Multi-tier intelligent caching (in-memory, Redis, PostgreSQL)

**Key Design Principles**:

- **Storage Polyglot**: Right technology for each data type
- **Interface-Based**: Repository pattern for testability
- **Privacy-First**: PII redaction and encryption
- **Performance-Optimized**: Hybrid schemas and caching
- **Cloud-Native**: Kubernetes-ready with horizontal scaling

## Related Documentation

- **Security**: [../security/iam-design.md](../security/iam-design.md) - Identity and Access Management
- **AI/RAG**: [../knowledge-and-ai/knowledge-base-architecture.md](../knowledge-and-ai/knowledge-base-architecture.md) - RAG pipeline and embeddings
- **Investigation**: [../investigation-engine/milestone-based-investigation-framework.md](../investigation-engine/milestone-based-investigation-framework.md) - Case lifecycle

## Documentation Organization

```text
data-and-storage/
├── README.md (this file)           # Master index
├── overview.md                     # High-level architecture
├── schemas/                        # Domain-specific schemas
│   ├── case-schema.md             # Case data model
│   ├── user-schema.md             # User data model
│   └── knowledge-schema.md        # Vector storage schemas
├── vector-storage.md              # ChromaDB implementation
├── repository-pattern.md          # Abstraction layer
├── sqlmodel-analysis.md           # ORM patterns
├── data-storage-design.md         # Legacy comprehensive reference
└── async-vector-storage-implementation.md  # Legacy implementation notes
```
