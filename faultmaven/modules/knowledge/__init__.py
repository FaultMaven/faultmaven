"""Knowledge Module - Vertical Slice.

This module owns all knowledge-related functionality for the RAG system:
- Knowledge base search and retrieval (semantic + full-text)
- Document embedding and vector search
- Knowledge ingestion and processing
- Knowledge item management

Public API:
    From domain.services:
        - KnowledgeSearchService: Main service for knowledge search operations
        - EmbeddingService: Document embedding service (OpenAI)
        - VectorStoreService: Vector database operations (ChromaDB)
        - KnowledgeService: Knowledge base management service

    From domain.models:
        - KnowledgeItem: Domain model for knowledge items
        - KnowledgeItemType: Enum for knowledge item types
        - EMBEDDING_DIMENSIONS: Standard embedding vector dimensions

    From infrastructure.persistence:
        - KnowledgeItemRepository: Abstract repository interface
        - DatabaseKnowledgeItemRepository: Database implementation
        - InMemoryKnowledgeItemRepository: In-memory implementation

    From api:
        - router: FastAPI router for /knowledge/* endpoints
"""

# Domain services
from faultmaven.modules.knowledge.domain.services.search_service import (
    KnowledgeSearchService,
)
from faultmaven.modules.knowledge.domain.services.embedding_service import (
    EmbeddingService,
)
from faultmaven.modules.knowledge.domain.services.vector_store_service import (
    VectorStoreService,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

# Domain models
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
    EMBEDDING_DIMENSIONS,
)

# Infrastructure persistence
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    KnowledgeItemRepository,
    DatabaseKnowledgeItemRepository,
    InMemoryKnowledgeItemRepository,
)

# API routes
from faultmaven.modules.knowledge.api.routes import router

__all__ = [
    # Services
    "KnowledgeSearchService",
    "EmbeddingService",
    "VectorStoreService",
    "KnowledgeService",
    # Models
    "KnowledgeItem",
    "KnowledgeItemType",
    "EMBEDDING_DIMENSIONS",
    # Infrastructure
    "KnowledgeItemRepository",
    "DatabaseKnowledgeItemRepository",
    "InMemoryKnowledgeItemRepository",
    # API
    "router",
]
