"""Knowledge Item Repository - Compatibility Shim.

DEPRECATED: This module has been moved to faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository

This file provides backward compatibility imports. New code should import from:
    from faultmaven.modules.knowledge import KnowledgeItemRepository, DatabaseKnowledgeItemRepository, InMemoryKnowledgeItemRepository
"""

# Backward compatibility - re-export from new location
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    KnowledgeItemRepository,
    DatabaseKnowledgeItemRepository,
    InMemoryKnowledgeItemRepository,
    KnowledgeItemRepositoryException,
)

__all__ = [
    "KnowledgeItemRepository",
    "DatabaseKnowledgeItemRepository",
    "InMemoryKnowledgeItemRepository",
    "KnowledgeItemRepositoryException",
]
