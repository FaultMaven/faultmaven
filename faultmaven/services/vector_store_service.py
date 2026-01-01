"""Vector Store Service - Compatibility Shim.

DEPRECATED: This module has been moved to faultmaven.modules.knowledge.domain.services.vector_store_service

This file provides backward compatibility imports. New code should import from:
    from faultmaven.modules.knowledge import VectorStoreService
"""

# Backward compatibility - re-export from new location
from faultmaven.modules.knowledge.domain.services.vector_store_service import VectorStoreService

__all__ = ["VectorStoreService"]
