"""Knowledge Domain Models.

This package contains domain models for knowledge management:
- KnowledgeItem: Core domain entity for knowledge items
- KnowledgeItemType: Enum for knowledge item types
- EMBEDDING_DIMENSIONS: Standard embedding dimensions
"""

from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    EMBEDDING_DIMENSIONS,
    KnowledgeItem,
    KnowledgeItemType,
)

__all__ = [
    "KnowledgeItem",
    "KnowledgeItemType",
    "EMBEDDING_DIMENSIONS",
]
