"""Knowledge Domain Layer.

This package contains domain logic for knowledge management:
- services: Domain services for knowledge operations
- models: Domain models and entities
"""

from faultmaven.modules.knowledge.domain.models import (
    EMBEDDING_DIMENSIONS,
    KnowledgeItem,
    KnowledgeItemType,
)
from faultmaven.modules.knowledge.domain.services import (
    EmbeddingService,
    KnowledgeSearchService,
    KnowledgeService,
    VectorStoreService,
)

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
]
