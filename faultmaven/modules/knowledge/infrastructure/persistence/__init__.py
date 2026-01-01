"""Knowledge Persistence Layer.

This package contains repository implementations for knowledge items:
- KnowledgeItemRepository: Abstract repository interface
- DatabaseKnowledgeItemRepository: SQLAlchemy database implementation
- InMemoryKnowledgeItemRepository: In-memory implementation for testing
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
