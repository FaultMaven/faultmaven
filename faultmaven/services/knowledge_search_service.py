"""Knowledge Search Service - Compatibility Shim.

DEPRECATED: This module has been moved to faultmaven.modules.knowledge.domain.services.search_service

This file provides backward compatibility imports. New code should import from:
    from faultmaven.modules.knowledge import KnowledgeSearchService
"""

# Backward compatibility - re-export from new location
from faultmaven.modules.knowledge.domain.services.search_service import KnowledgeSearchService

__all__ = ["KnowledgeSearchService"]
