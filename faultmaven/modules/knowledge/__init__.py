"""Knowledge Module - Vertical Slice.

This module owns all knowledge-related functionality for the RAG system:
- Knowledge base search and retrieval (semantic + full-text)
- Document embedding and vector search
- Knowledge ingestion and processing
- Knowledge item management

Public API:
    From domain.services:
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

# API routes
from faultmaven.modules.knowledge.api.routes import router

# Domain models
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    EMBEDDING_DIMENSIONS,
    KnowledgeItem,
    KnowledgeItemType,
)

# Domain services
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

# Infrastructure persistence
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    DatabaseKnowledgeItemRepository,
    InMemoryKnowledgeItemRepository,
    KnowledgeItemRepository,
)

__all__ = [
    # Services
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
