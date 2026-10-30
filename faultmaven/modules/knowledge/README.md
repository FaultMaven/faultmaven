# Knowledge Module

**Module Type**: Vertical Slice
**Owner**: Knowledge Domain
**Dependencies**: Shared infrastructure (`faultmaven.services.base`, `faultmaven.exceptions`), Core DI container

## Overview

The Knowledge module owns all knowledge-related functionality for FaultMaven's RAG (Retrieval-Augmented Generation) system. This module demonstrates the vertical slice architecture pattern, where all layers of a feature domain (API, domain logic, infrastructure) are co-located.

## What This Module Owns

### Core Capabilities
- **Knowledge base search and retrieval**: Semantic search using vector embeddings and hybrid search combining semantic + full-text
- **Document embedding and vector search**: Integration with OpenAI embeddings and ChromaDB vector store
- **Knowledge ingestion and processing**: Document upload, parsing, chunking, and indexing
- **Knowledge item management**: CRUD operations for knowledge base items

### Technical Components
- **Vector Search**: ChromaDB integration for semantic similarity search
- **Embeddings**: OpenAI text-embedding-3-small (1536 dimensions)
- **Full-Text Search**: PostgreSQL/SQLite text search capabilities
- **Hybrid Search**: Weighted combination of semantic and text search results

## Architecture

This module follows the **vertical slice** pattern:

```
knowledge/
├── __init__.py            # docstring only: import from the modules below
├── contracts.py           # public interfaces and DTOs for other modules
├── taxonomy.py            # the runbook taxonomy: one enum per closed vocabulary
├── exceptions.py
├── api/
│   ├── routes.py          # /knowledge/* (documents, search, suggestions)
│   └── conversion_routes.py  # /knowledge/convert, drafts, runbooks/create
├── domain/
│   ├── services/
│   │   ├── knowledge_service.py   # KnowledgeService (search, upload, ingest)
│   │   ├── runbook_validator.py   # the ingestion gate
│   │   ├── suggestion_service.py  # case → runbook suggestions
│   │   └── conversion_service/    # document/case → runbook drafts
│   └── models/
│       └── knowledge_item.py      # KnowledgeItem domain model
└── infrastructure/
    └── persistence/
        └── knowledge_item_repository.py # Repository pattern
```

## Public API

### Imports

`faultmaven/modules/knowledge/__init__.py` holds no imports: every symbol is
imported from the module that defines it. Other modules import from
`contracts.py`, and read the runbook taxonomy from `taxonomy.py`.

### Services

#### KnowledgeService
High-level knowledge base management — search (`search_knowledge()`,
`search_documents()`), `upload_document()`, `list_documents()`,
`ingest_runbook()` and the document reads and deletes:

```python
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
```

The vector half of retrieval is `KnowledgeVectorStore`
(`faultmaven/infrastructure/knowledge/knowledge_vector_store.py`); query
embedding goes through `faultmaven/infrastructure/embedding_guard.py`.

### Models

#### KnowledgeItem
Core domain entity for knowledge items:

```python
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
)
```

#### KnowledgeItemType (Enum)
- `TROUBLESHOOTING_GUIDE`
- `ERROR_PATTERN`
- `SOLUTION_TEMPLATE`
- `API_DOCUMENTATION`
- `CONFIGURATION_GUIDE`
- `BEST_PRACTICE`
- `FAQ`
- `RUNBOOK`

#### The runbook taxonomy
`RunbookDomain`, `SymptomClass`, `RunbookSeverity`, `KnowledgeScope`,
`RunbookDifficulty` and `RunbookStatus` are defined once, in `taxonomy.py`; their
values are the spec's ([runbook-content-architecture.md §Taxonomy Schema](../../../docs/architecture/knowledge-and-ai/runbook-content-architecture.md#taxonomy-schema)):

```python
from faultmaven.modules.knowledge.taxonomy import KnowledgeScope, RunbookSeverity
```

### Infrastructure

#### KnowledgeItemRepository
Abstract repository interface with implementations:
- `DatabaseKnowledgeItemRepository`: SQLAlchemy database implementation
- `InMemoryKnowledgeItemRepository`: In-memory implementation for testing

```python
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    DatabaseKnowledgeItemRepository,
)
```

## Dependencies

### Allowed Dependencies
-  `faultmaven.services.base` (BaseService for common patterns)
-  `faultmaven.exceptions` (Domain exceptions)
-  `faultmaven.infrastructure.persistence.models` (ORM models)
-  `faultmaven.infrastructure.persistence.database` (Database session)
-  `faultmaven.core.container` (DI container)
-  Standard library and third-party packages (OpenAI, ChromaDB, FastAPI, SQLAlchemy)

### Forbidden Dependencies
- L Other modules (`case`, `evidence`, `agent`, `session`)
- L Cross-module imports that would create circular dependencies

Module boundaries are enforced by import-linter (see `.importlinter` config).

## API Endpoints

All endpoints are under `/api/v1/knowledge/`:

### Document Management
- `POST /knowledge/documents` - Upload document
- `GET /knowledge/documents` - List documents
- `GET /knowledge/documents/{id}` - Get document
- `PUT /knowledge/documents/{id}` - Update document
- `DELETE /knowledge/documents/{id}` - Delete document
- `POST /knowledge/documents/bulk-update` - Bulk update
- `POST /knowledge/documents/bulk-delete` - Bulk delete

### Search
- `POST /knowledge/search` - Search knowledge base

### Job Management
- `GET /knowledge/jobs/{id}` - Get job status

### Analytics
- `GET /knowledge/stats` - Get knowledge base statistics
- `GET /knowledge/analytics/search` - Get search analytics

## Usage Examples

### Search

```python
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

service = container.get(KnowledgeService)
# Hybrid only (dense vector + keyword-constrained recall, then rerank), and
# row-checked: a hit is kept only when its knowledge_items row is complete and
# published.
results = await service.search_knowledge(query="postgresql timeout", limit=10)
```

### Document Upload

```python
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

service = container.get(KnowledgeService)

result = await service.upload_document(
    content="Troubleshooting steps...",
    title="Database Connection Issues",
    document_type="troubleshooting_guide",
    tags=["database", "postgresql", "connection"],
    category="database",
)

print(f"Document {result['document_id']} uploaded, job {result['job_id']}")
```

## Testing

### Running Tests

```bash
# Run all knowledge module tests
pytest tests/ -k knowledge

# Run specific test suites
pytest tests/api/test_knowledge_endpoints.py
pytest tests/services/test_knowledge_service.py
pytest tests/unit/services/test_knowledge_search_service.py
```

### Test Coverage

The knowledge module has comprehensive test coverage:
- **Unit tests**: Service logic, domain models, repository patterns
- **Integration tests**: Database operations, API endpoints
- **Benchmarks**: Search performance, embedding generation

## Migration Guide

Import every symbol from the module that defines it — there is one canonical
path per symbol, and the package `__init__.py` re-exports nothing:

```python
from faultmaven.modules.knowledge.contracts import IKnowledgeService
from faultmaven.modules.knowledge.domain.models.knowledge_item import KnowledgeItem
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.taxonomy import RunbookSeverity
```

This is a breaking change, but since FaultMaven is pre-production, we're building it right from the start rather than maintaining backward compatibility shims.

## Future Enhancements

Potential improvements for the knowledge module:
- **Query expansion**: Automatically expand queries with synonyms/related terms
- **Re-ranking**: LLM-based re-ranking of search results
- **Multi-modal support**: Support for images, diagrams, code snippets
- **Federated search**: Search across multiple knowledge sources
- **Auto-tagging**: Automatic tag generation from content
- **Knowledge graphs**: Entity extraction and relationship mapping

## Pattern for Other Modules

This knowledge module serves as the **proof-of-concept** for the vertical slice architecture. Other modules (`case`, `evidence`, `agent`, `session`) should follow this pattern:

### Directory Structure
```
modules/{module_name}/
   api/                    # REST API layer
      __init__.py
      routes.py
   domain/                 # Business logic
      services/
         {service}.py
      models/
          {model}.py
   infrastructure/         # Infrastructure layer
      persistence/
          {repository}.py
   README.md
```

### Key Principles
1. **Vertical ownership**: Module owns API  Domain  Infrastructure for its feature
2. **Clear boundaries**: No imports from other modules
3. **Public API**: `contracts.py` for module consumers; `__init__.py` holds no imports
4. **No backward compatibility**: one canonical import path per symbol, no shims
5. **Documentation**: README.md explaining module scope and usage

## Support

For questions or issues related to the knowledge module:
- Review this README
- Check test cases for usage examples
- See the main FaultMaven documentation
- Contact the architecture team for guidance

---

**Last Updated**: 2026-01-01
**Module Version**: 1.0.0 (Phase 3, Week 16-18)
**Pattern Status**: Proof-of-Concept (to be replicated for other modules)
