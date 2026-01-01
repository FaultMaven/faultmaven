"""Knowledge Item Model - Compatibility Shim.

DEPRECATED: This module has been moved to faultmaven.modules.knowledge.domain.models.knowledge_item

This file provides backward compatibility imports. New code should import from:
    from faultmaven.modules.knowledge import KnowledgeItem, KnowledgeItemType, EMBEDDING_DIMENSIONS
"""

# Backward compatibility - re-export from new location
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
    EMBEDDING_DIMENSIONS,
)

__all__ = ["KnowledgeItem", "KnowledgeItemType", "EMBEDDING_DIMENSIONS"]
