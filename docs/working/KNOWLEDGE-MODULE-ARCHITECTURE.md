# Knowledge Module Architecture

## Before: Horizontal Layers

```
faultmaven/
├── api/
│   └── v1/
│       └── routes/
│           └── knowledge.py          ❌ Mixed with other API routes
├── services/
│   ├── knowledge_search_service.py   ❌ Mixed with other services
│   ├── embedding_service.py          ❌ Mixed with other services
│   ├── vector_store_service.py       ❌ Mixed with other services
│   └── domain/
│       └── knowledge_service.py      ❌ Mixed with other services
├── models/
│   └── knowledge_item.py             ❌ Mixed with other models
└── infrastructure/
    └── persistence/
        └── knowledge_item_repository.py ❌ Mixed with other repos
```

**Problems**:
- Hard to see what belongs together
- Difficult to work on knowledge features without touching everything
- No clear module boundaries
- Can't easily extract or replace knowledge functionality

## After: Vertical Slice

```
faultmaven/
├── modules/
│   └── knowledge/                    ✅ All knowledge code in one place
│       ├── __init__.py              # Clean public API
│       ├── README.md                 # Module documentation
│       ├── api/                      # Knowledge API layer
│       │   ├── __init__.py
│       │   └── routes.py            # /knowledge/* endpoints
│       ├── domain/                   # Knowledge business logic
│       │   ├── __init__.py
│       │   ├── services/            # Domain services
│       │   │   ├── __init__.py
│       │   │   ├── search_service.py
│       │   │   ├── embedding_service.py
│       │   │   ├── vector_store_service.py
│       │   │   └── knowledge_service.py
│       │   └── models/              # Domain models
│       │       ├── __init__.py
│       │       └── knowledge_item.py
│       └── infrastructure/          # Knowledge infrastructure
│           ├── __init__.py
│           └── persistence/         # Persistence layer
│               ├── __init__.py
│               └── knowledge_item_repository.py
└── [old locations]                   ✅ Compatibility shims (zero breaking changes)
```

**Benefits**:
- ✅ Clear module boundaries
- ✅ Easy to work on knowledge features (everything in one place)
- ✅ Can extract/replace knowledge module independently
- ✅ Follows DDD/Clean Architecture principles
- ✅ Zero breaking changes (backward compatibility shims)

## Architecture Layers

```
┌─────────────────────────────────────────────────────────────────┐
│                         API Layer                                │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  FastAPI Routes: /knowledge/*                            │   │
│  │  - POST /documents (upload)                              │   │
│  │  - GET /documents (list)                                 │   │
│  │  - POST /search (search)                                 │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                       Domain Layer                               │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Services                                                 │   │
│  │  - KnowledgeSearchService (semantic + hybrid search)     │   │
│  │  - EmbeddingService (OpenAI integration)                 │   │
│  │  - VectorStoreService (ChromaDB operations)              │   │
│  │  - KnowledgeService (high-level management)              │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Models                                                   │   │
│  │  - KnowledgeItem (domain entity)                         │   │
│  │  - KnowledgeItemType (enum)                              │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                   Infrastructure Layer                           │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Persistence                                              │   │
│  │  - KnowledgeItemRepository (interface)                   │   │
│  │  - DatabaseKnowledgeItemRepository (SQLAlchemy)          │   │
│  │  - InMemoryKnowledgeItemRepository (testing)             │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  External Services                                        │   │
│  │  - OpenAI (embeddings)                                    │   │
│  │  - ChromaDB (vector store)                                │   │
│  │  - PostgreSQL/SQLite (metadata)                           │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## Data Flow: Semantic Search

```
User Request
    │
    ├── POST /knowledge/search {"query": "database timeout"}
    │
    ↓
API Layer (routes.py)
    │
    ├── Validate request
    ├── Extract parameters
    │
    ↓
Domain Layer (KnowledgeSearchService)
    │
    ├── 1. Generate embedding
    │   └── EmbeddingService.generate_embedding()
    │       └── OpenAI API call
    │
    ├── 2. Search vector store
    │   └── VectorStoreService.search_similar()
    │       └── ChromaDB query
    │
    ├── 3. Fetch full items
    │   └── KnowledgeItemRepository.get_by_id()
    │       └── Database query
    │
    ├── 4. Mark as retrieved
    │   └── KnowledgeItemRepository.update()
    │
    ↓
Response
    │
    └── [
          {"document_id": "...", "title": "...", "score": 0.95},
          {"document_id": "...", "title": "...", "score": 0.89},
          ...
        ]
```

## Module Dependencies

```
┌─────────────────────────────────────────────────────────────────┐
│              Knowledge Module (Vertical Slice)                   │
│                                                                  │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐                │
│  │    API     │  │   Domain   │  │    Infra   │                │
│  │   Layer    │→ │   Layer    │→ │   Layer    │                │
│  └────────────┘  └────────────┘  └────────────┘                │
│         │              │               │                         │
└─────────┼──────────────┼───────────────┼─────────────────────────┘
          │              │               │
          ↓              ↓               ↓
┌─────────────────────────────────────────────────────────────────┐
│                      Shared Infrastructure                       │
│  - BaseService (common service patterns)                        │
│  - Exceptions (domain exceptions)                               │
│  - Database (session management)                                │
│  - DI Container (service registration)                          │
└─────────────────────────────────────────────────────────────────┘
          ↓
┌─────────────────────────────────────────────────────────────────┐
│                      External Services                           │
│  - OpenAI (text-embedding-3-small)                              │
│  - ChromaDB (vector storage)                                    │
│  - PostgreSQL/SQLite (metadata)                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Module Boundaries Enforcement

```python
# ✅ ALLOWED: Import from module public API
from faultmaven.modules.knowledge import (
    KnowledgeSearchService,
    KnowledgeItem,
)

# ✅ ALLOWED: Import from shared infrastructure
from faultmaven.services.base import BaseService
from faultmaven.exceptions import KnowledgeBaseException

# ❌ FORBIDDEN: Import from other modules
from faultmaven.modules.case import CaseService  # ❌ Would create coupling
from faultmaven.modules.agent import AgentService  # ❌ Would create coupling

# ❌ FORBIDDEN: Import internal module details from outside
from faultmaven.modules.knowledge.domain.services.search_service import (
    KnowledgeSearchService  # ❌ Should use public API instead
)
```

Enforced by import-linter in `.importlinter` config.

## Replication Pattern for Other Modules

```
modules/{module_name}/
├── __init__.py             # Clean public API exports
├── README.md                # Module documentation
├── api/                     # REST API layer
│   ├── __init__.py
│   └── routes.py
├── domain/                  # Business logic
│   ├── __init__.py
│   ├── services/
│   │   ├── __init__.py
│   │   └── {service}.py
│   └── models/
│       ├── __init__.py
│       └── {model}.py
└── infrastructure/          # Infrastructure layer
    ├── __init__.py
    └── persistence/
        ├── __init__.py
        └── {repository}.py
```

Follow the knowledge module as the reference implementation.

---

**Status**: ✅ PROOF-OF-CONCEPT COMPLETE
**Next**: Replicate for Case, Evidence, Agent, Session modules
