# FaultMaven Architectural Design Principles

**Version**: 1.0
**Date**: 2026-01-05
**Status**: Active
**Related Documents**:

- [ADR-001: Monolith Evolution Strategy](decisions/ADR-001-MONOLITH-EVOLUTION-STRATEGY.md)
- [Import Linter Baseline](IMPORT-LINTER-BASELINE.md)
- [Platform Evolution Strategy](../FAULTMAVEN_PLATFORM_EVOLUTION_STRATEGY.md)

---

## Executive Summary

This document defines the **core architectural design principles** that guide FaultMaven's evolution from a battle-tested monolith to a modern, modular architecture.

**Key Principles**:

1. **Deployment Agnostic Architecture** - Infrastructure as deployment-time decisions, not code-time constraints
2. **Vertical Slicing** - Domain-based modules over horizontal layers
3. **Interface-Based Design** - Protocols and ABCs for all external dependencies
4. **Dependency Injection** - Service composition via DI container
5. **Architectural Boundary Enforcement** - Import-linter for compile-time safety
6. **Test Safety Net** - Zero regressions via comprehensive test suite
7. **Incremental Refactoring** - Evolutionary architecture over big rewrites

---

## Table of Contents

1. [Design Principle 1: Deployment Agnostic Architecture](#1-deployment-agnostic-architecture)
2. [Design Principle 2: Vertical Slicing](#2-vertical-slicing)
3. [Design Principle 3: Interface-Based Design](#3-interface-based-design)
4. [Design Principle 4: Dependency Injection](#4-dependency-injection)
5. [Design Principle 5: Architectural Boundary Enforcement](#5-architectural-boundary-enforcement)
6. [Design Principle 6: Test Safety Net](#6-test-safety-net)
7. [Design Principle 7: Incremental Refactoring](#7-incremental-refactoring)

---

## 1. Deployment Agnostic Architecture

### Principle

> **"Infrastructure choices are deployment-time decisions, not code-time constraints."**

FaultMaven Core must remain **agnostic to where it runs** (local dev, Docker, Kubernetes, serverless, bare metal). Infrastructure differences are expressed via **provider selection** and **configuration injection**, not code branching.

### Key Rules

| Rule | What it means in practice |
|------|----------------------------|
| ✅ **Single codebase & artifact** | One repository and one build artifact runs everywhere |
| ✅ **Business logic stays neutral** | No deployment-specific branching in services/domain/API handlers |
| ✅ **Provider selection is explicit** | Provider factories / composition root choose implementations from env selectors |
| ✅ **Operational neutrality** | Metrics/tracing/jobs are exposed as hooks/entrypoints; runtime decides exporters/schedulers |
| ❌ **No separate "local"/"cloud" packages** | No parallel app trees like `faultmaven/local/` or `faultmaven/cloud/` |
| ❌ **No infra coupling in business logic** | No direct vendor calls (S3/Pinecone/Redis) from core services; use interfaces |

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Application Code                              │
│                                                                  │
│  Business logic uses interfaces only (no deployment branching)   │
│  • CaseService calls TenantProvider (organization context)       │
│  • KnowledgeService calls VectorStore (vector search)            │
│  • EvidenceService calls StorageBackend (file storage)           │
└─────────────────────────────────────────────────────────────────┘
                              ▲
                              │ depends on interfaces
                              │
┌─────────────────────────────────────────────────────────────────┐
│                   Composition Root                               │
│                                                                  │
│  Loads settings once (settings-only env reads), wires providers  │
│  • TENANT_PROVIDER=single → SingleTenantProvider                │
│  • STORAGE_BACKEND=s3 → S3StorageBackend                        │
│  • VECTOR_BACKEND=chroma → ChromaVectorStore                    │
└─────────────────────────────────────────────────────────────────┘
```

### Provider Examples

**Local preset** (Local Deployment):

```bash
# Zero-config defaults (local dev / self-host)
CONFIG_PRESET=local

# Optional explicit selectors (advanced / diagnostics)
DATABASE_URL=sqlite+aiosqlite:///./faultmaven.db
TENANT_PROVIDER=single
STORAGE_BACKEND=filesystem
VECTOR_BACKEND=chroma
METRICS_ENABLED=false
METRICS_EXPORTER=none
TRACING_ENABLED=false
```

**Enterprise preset** (Production Deployment):

```bash
# Full infrastructure stack
CONFIG_PRESET=enterprise
DATABASE_URL=postgresql+asyncpg://user:pass@postgres:5432/faultmaven
SESSION_STORAGE_TYPE=redis
VECTOR_STORAGE_TYPE=chromadb
STORAGE_BACKEND=s3
TENANT_PROVIDER=multi
OPIK_ENABLED=true
METRICS_ENABLED=true
METRICS_EXPORTER=prometheus_http
PROTECTION_ENABLED=true
```

**Key Insight**: Both configurations run the **SAME codebase** with **ZERO conditional logic** in business services.

**Related Document**: Deployment-agnostic architecture specifications (Enterprise internal documentation).

---

## 2. Vertical Slicing

### Principle

> **"Organize code by domain capability (vertical slices), not technical layer (horizontal slices)."**

Instead of organizing code by technical concern (controllers, services, repositories), organize by **domain capability** (auth, session, case, knowledge, evidence, agent).

### Before: Horizontal Layering (Monolith)

```
faultmaven/
├── api/v1/routes/           # All API routes together
├── services/                # All services together
├── infrastructure/          # All infrastructure together
├── models/                  # All models together
└── utils/                   # All utilities together
```

**Problem**: Changes to a single feature (e.g., "add case sharing") require touching files across 4-5 directories.

### After: Vertical Slicing (Modular Monolith)

```
faultmaven/
├── modules/
│   ├── auth/                # Authentication module
│   │   ├── api/             # Auth API endpoints
│   │   ├── domain/          # Auth business logic
│   │   └── infrastructure/  # Auth persistence
│   │
│   ├── case/                # Case management module
│   │   ├── api/             # Case API endpoints
│   │   ├── domain/          # Case business logic
│   │   └── infrastructure/  # Case persistence
│   │
│   ├── knowledge/           # Knowledge base module
│   │   ├── api/             # KB API endpoints
│   │   ├── domain/          # KB business logic (search, RAG)
│   │   └── infrastructure/  # KB persistence (ChromaDB)
│   │
│   └── ...                  # Evidence, Session, Agent modules
│
└── core/                    # Shared infrastructure
    ├── container.py         # DI container
    ├── interfaces/          # Shared protocols
    └── providers/           # Infrastructure providers
```

**Benefit**: Changes to a single feature (e.g., "add case sharing") touch files within **one module directory**.

### Benefits

1. **Reduced Cognitive Load**: Developers only need to understand one domain at a time
2. **Clear Ownership**: Each module has clear boundaries and responsibilities
3. **Independent Development**: Teams can work on different modules in parallel
4. **Easier Testing**: Test all aspects of a feature within one module
5. **Simplified Debugging**: Feature-related code is co-located

**Related Documents**:

- [ADR-001: Monolith Evolution Strategy](decisions/ADR-001-MONOLITH-EVOLUTION-STRATEGY.md)
- [Knowledge Module Architecture](../working/KNOWLEDGE-MODULE-ARCHITECTURE.md)

---

## 3. Interface-Based Design

### Principle

> **"Depend on abstractions (interfaces), not concrete implementations."**

All external dependencies (LLM providers, databases, vector stores, file storage) are accessed through **Protocol** (structural typing) or **ABC** (abstract base class) interfaces.

### Key Interfaces

| Interface | Purpose | Implementations |
|-----------|---------|--------------------|
| `ILLMProvider` | LLM integration | OpenAI, Anthropic, Fireworks, Gemini, Groq, HuggingFace, Local |
| `IVectorStore` | Vector search | ChromaDB, InMemory, Pinecone (future) |
| `ISessionStore` | Session management | Redis, InMemory, Memcached (future) |
| `IStorageBackend` | File storage | S3, Azure Blob, Local Filesystem |
| `TenantProvider` | Multi-tenancy | SingleTenant, MultiTenant |
| `ICaseRepository` | Case persistence | PostgreSQL Hybrid, InMemory |
| `IUserRepository` | User persistence | PostgreSQL, InMemory |

### Example: IVectorStore Protocol

```python
from typing import Protocol, List, Dict, Any

class IVectorStore(Protocol):
    """Interface for vector storage backends."""

    async def add_documents(
        self,
        collection_name: str,
        documents: List[str],
        metadatas: List[Dict[str, Any]],
        ids: List[str]
    ) -> None:
        """Add documents to vector store."""
        ...

    async def search(
        self,
        collection_name: str,
        query_text: str,
        n_results: int = 5
    ) -> List[Dict[str, Any]]:
        """Search for similar documents."""
        ...
```

**Implementations**:

```python
# Community Edition
class InMemoryVectorStore:
    """Implements IVectorStore without external dependencies."""
    async def add_documents(...): ...
    async def search(...): ...

# Enterprise Edition
class ChromaDBVectorStore:
    """Implements IVectorStore using ChromaDB."""
    async def add_documents(...): ...
    async def search(...): ...
```

**Business Service** (deployment-agnostic):

```python
class KnowledgeSearchService:
    def __init__(self, vector_store: IVectorStore):
        self.vector_store = vector_store  # Accepts any IVectorStore implementation

    async def search_knowledge_base(self, query: str) -> List[Document]:
        results = await self.vector_store.search("kb_collection", query)
        return [self._parse_result(r) for r in results]
```

### Benefits

1. **Testability**: Mock implementations for unit tests
2. **Flexibility**: Swap implementations without changing business logic
3. **Deployment Agnostic**: Same code works with different providers
4. **Type Safety**: Static type checking via `mypy`

**Related Document**: [Interface-Based Design](interface-based-design.md)

---

## 4. Dependency Injection

### Principle

> **"Services receive dependencies via constructor injection, not direct instantiation."**

Services **do not create their own dependencies**. Dependencies are **injected** via constructor parameters and managed by a **DI Container**.

### Before: Direct Instantiation (Anti-Pattern)

```python
# ❌ ANTI-PATTERN: Service creates its own dependencies
class KnowledgeSearchService:
    def __init__(self, knowledge_repo):
        self.embedding_service = EmbeddingService()  # Direct instantiation
        self.vector_store = ChromaDBVectorStore()     # Direct instantiation
        self.knowledge_repo = knowledge_repo
```

**Problems**:

- ❌ Tight coupling to concrete implementations
- ❌ Hard to test (can't mock dependencies)
- ❌ Violates Deployment Agnostic Architecture (hardcoded to ChromaDB)
- ❌ Service-to-service import violations (import-linter fails)

### After: Dependency Injection (Correct Pattern)

```python
# ✅ CORRECT: Dependencies injected via constructor
class KnowledgeSearchService:
    def __init__(
        self,
        knowledge_repo: IKnowledgeRepository,
        embedding_service: IEmbeddingService,
        vector_store: IVectorStore,
    ):
        self.knowledge_repo = knowledge_repo
        self.embedding_service = embedding_service
        self.vector_store = vector_store
```

**Benefits**:

- ✅ Loose coupling (depends on interfaces)
- ✅ Testable (inject mocks)
- ✅ Deployment-agnostic (any IVectorStore works)
- ✅ No service-to-service imports (import-linter passes)

### DI Container

**Registration** (at application startup):

```python
# faultmaven/core/service_factories.py
from faultmaven.core.container import ServiceContainer

def register_services(config: AppConfig):
    # Register vector store based on configuration
    if config.vector_storage_type == "chromadb":
        ServiceContainer.register(IVectorStore, ChromaDBVectorStore)
    else:
        ServiceContainer.register(IVectorStore, InMemoryVectorStore)

    # Register embedding service
    ServiceContainer.register(IEmbeddingService, EmbeddingService)

    # Register knowledge search service
    ServiceContainer.register(KnowledgeSearchService, KnowledgeSearchService)
```

**Consumption** (in API handlers):

```python
# faultmaven/modules/knowledge/api/routes.py
from faultmaven.core.container import ServiceContainer

@router.get("/knowledge/search")
async def search_knowledge(query: str):
    # Get service from DI container
    search_service = ServiceContainer.get(KnowledgeSearchService)
    results = await search_service.search_knowledge_base(query)
    return results
```

**Related Document**: [Dependency Injection System](dependency-injection-system.md)

---

## 5. Architectural Boundary Enforcement

### Principle

> **"Architectural rules must be enforced at build time, not code review time."**

FaultMaven uses **import-linter** to automatically detect and prevent architectural violations. This ensures that clean architecture boundaries are **enforced by the compiler**, not just documented in guidelines.

### Enforced Contracts

| Contract | Rule | Status |
|----------|------|--------|
| **Service Independence** | Services cannot import other services directly | ✅ Enforced |
| **Layer Separation** | Services cannot import from API layer | ✅ Enforced |
| **Model Isolation** | Models cannot import from service layer | ✅ Enforced |

### Contract 1: Service Independence

**Rule**: Services should not directly import from each other. Dependencies must go through DI container.

**Why**: Prevents tight coupling between services, enables independent testing and deployment.

**Before** (Violation):

```python
# ❌ VIOLATION: Direct service-to-service import
from faultmaven.services.embedding_service import EmbeddingService

class KnowledgeSearchService:
    def __init__(self):
        self.embedding_service = EmbeddingService()  # Violation!
```

**After** (Compliant):

```python
# ✅ COMPLIANT: Dependency injection via container
from faultmaven.core.container import ServiceContainer

class KnowledgeSearchService:
    def __init__(self, embedding_service=None):
        if embedding_service is None:
            embedding_service = ServiceContainer.get(IEmbeddingService)
        self.embedding_service = embedding_service
```

### Contract 2: Layer Separation

**Rule**: Service layer must not import from API layer.

**Why**: Prevents circular dependencies. API depends on services, not vice versa.

**Enforcement**:

```ini
# .importlinter
[importlinter:contract:2]
name = Services cannot import API layer
type = forbidden
source_modules =
    faultmaven.services
forbidden_modules =
    faultmaven.api
```

### Contract 3: Model Isolation

**Rule**: Model classes (data structures, DTOs, entities) must not import service layer.

**Why**: Keeps models as pure data structures without business logic dependencies.

### Build-Time Enforcement

```bash
# CI/CD pipeline runs import-linter on every PR
lint-imports

# Output on success:
✅ All 3 contracts KEPT (0 violations)

# Output on failure:
❌ Contract "Service Independence" BROKEN (2 violations)
  faultmaven/services/knowledge_search_service.py imports:
    faultmaven.services.embedding_service
```

**CI/CD Integration**:

```yaml
# .github/workflows/ci.yml
- name: Check architectural boundaries
  run: |
    pip install import-linter
    lint-imports
```

**Related Document**: [Import Linter Baseline](IMPORT-LINTER-BASELINE.md)

---

## 6. Test Safety Net

### Principle

> **"Never merge code that decreases test coverage or breaks existing tests."**

A comprehensive test suite is essential for safe refactoring. Tests act as a safety net that catches regressions immediately, enabling aggressive architectural changes with confidence.

### Test Categories Required

1. **Unit Tests**
   - Service layer logic
   - Domain models
   - Infrastructure providers

2. **Integration Tests**
   - API endpoint behavior
   - Database interactions
   - External service integration

3. **Performance Tests**
   - Response time benchmarks
   - Resource usage limits
   - Scalability thresholds

4. **Security Tests**
   - Authentication and authorization
   - Data protection
   - Input validation

### Test-Driven Refactoring Process

1. **Baseline**: Run full test suite (all tests must pass)
2. **Refactor**: Perform code changes (move files, update imports, restructure)
3. **Verify**: Run full test suite (all tests must still pass)
4. **Coverage**: Check test coverage (must maintain or improve)
5. **Commit**: Only commit if all tests pass and coverage is maintained

### Quality Gates

- ✅ All tests must pass before merging
- ✅ Test coverage cannot decrease
- ✅ No regressions allowed
- ✅ CI/CD pipeline enforces all rules

**Related Document**: [Testing Guide](testing-guide.md)

---

## 7. Incremental Refactoring

### Principle

> **"Prefer incremental refactoring over big rewrites."**

Evolve the architecture incrementally rather than attempting a complete rewrite. This approach reduces risk, preserves working code, and maintains business value delivery.

### Why Big Rewrites Fail

Industry research shows that 80% of rewrites fail or take 2-3x longer than expected because:

- **Lost Knowledge**: Rewrites miss critical edge cases captured in original code
- **Discovery Tax**: Teams rediscover already-solved problems
- **Stalled Delivery**: Business value delivery stops during rewrite
- **Test Loss**: Existing test suites become obsolete

### The Incremental Approach

**Key Insight**: Most refactoring involves **moving code** and **updating references**, not rewriting logic.

**Benefits**:

1. **Lower Risk**: Changes are small, isolated, and reversible
2. **Continuous Delivery**: Business value continues throughout refactoring
3. **Preserved Knowledge**: Tests and edge case handling remain intact
4. **Faster Feedback**: Issues discovered quickly in small increments

### Refactoring Process

1. **Identify**: Choose a small, well-bounded area to refactor
2. **Plan**: Design the target structure
3. **Move**: Relocate code using version control (e.g., `git mv` to preserve history)
4. **Update**: Fix references and imports
5. **Test**: Verify all tests pass
6. **Document**: Update relevant documentation
7. **Merge**: Integrate changes
8. **Repeat**: Continue with next increment

### Git History Preservation

Always use version control move operations (`git mv`) rather than copy-delete to preserve:

- File history and blame information
- Author contributions
- Evolution and context of changes

**Related Document**: [ADR-001: Monolith Evolution Strategy](decisions/ADR-001-MONOLITH-EVOLUTION-STRATEGY.md)

---

## References

### Core Documents

- **[ADR-001: Monolith Evolution Strategy](decisions/ADR-001-MONOLITH-EVOLUTION-STRATEGY.md)** - Strategic decision to evolve vs. rewrite
- **[Platform Evolution Strategy](../FAULTMAVEN_PLATFORM_EVOLUTION_STRATEGY.md)** - Detailed implementation roadmap
- **[Import Linter Baseline](IMPORT-LINTER-BASELINE.md)** - Architectural boundary enforcement configuration

### Supporting Documents

- **[Dependency Injection System](dependency-injection-system.md)** - DI Container implementation details
- **[Interface-Based Design](interface-based-design.md)** - Protocol and ABC patterns
- **[Testing Guide](testing-guide.md)** - Test strategy and coverage targets
- **[Knowledge Module Architecture](../working/KNOWLEDGE-MODULE-ARCHITECTURE.md)** - Example of vertical slice implementation

---

**Last Updated**: 2026-01-05
**Document Owner**: Engineering Leadership
**Status**: Active Design Principles
