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
    KnowledgeService,
)

__all__ = [
    # Services
    "KnowledgeService",
    # Models
    "KnowledgeItem",
    "KnowledgeItemType",
    "EMBEDDING_DIMENSIONS",
]
