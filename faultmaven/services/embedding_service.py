"""Embedding Service - Compatibility Shim.

DEPRECATED: This module has been moved to faultmaven.modules.knowledge.domain.services.embedding_service

This file provides backward compatibility imports. New code should import from:
    from faultmaven.modules.knowledge import EmbeddingService
"""

# Backward compatibility - re-export from new location
from faultmaven.modules.knowledge.domain.services.embedding_service import EmbeddingService

__all__ = ["EmbeddingService"]
