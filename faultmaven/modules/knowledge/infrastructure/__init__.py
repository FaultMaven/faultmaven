"""Knowledge Infrastructure Layer.

This package contains infrastructure implementations for knowledge management:
- persistence: Repository implementations for knowledge items
"""

from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    KnowledgeItemRepository,
    DatabaseKnowledgeItemRepository,
    InMemoryKnowledgeItemRepository,
)

__all__ = [
    "KnowledgeItemRepository",
    "DatabaseKnowledgeItemRepository",
    "InMemoryKnowledgeItemRepository",
]
