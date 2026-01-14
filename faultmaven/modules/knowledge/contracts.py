"""Knowledge Module Contracts

This module defines the public interfaces (contracts) for the Knowledge vertical module.
Other modules should import from here, not from infrastructure or domain directly.

Following the design in module-organization-design.md:
- Vertical modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication
"""

from abc import ABC
from typing import TYPE_CHECKING, List, Optional, Protocol

if TYPE_CHECKING:
    from faultmaven.modules.knowledge.domain.models.knowledge_item import KnowledgeItem


# ============================================================
# Service Contracts
# ============================================================


class IKnowledgeService(Protocol):
    """Service interface for Knowledge business logic."""

    async def search(self, query: str, k: int = 5) -> List[dict]:
        """Perform semantic search on knowledge base."""
        ...

    async def add_document(self, document: dict) -> str:
        """Add a document to the knowledge base."""
        ...

    async def delete_document(self, document_id: str) -> bool:
        """Delete a document from the knowledge base."""
        ...


class IKnowledgeQuery(Protocol):
    """Read-only knowledge query interface."""

    async def search(self, query: str, k: int = 5) -> List[dict]:
        """Perform semantic search (read-only)."""
        ...


# ============================================================
# Note: Knowledge module uses infrastructure/vector/ for vector store
# The actual KnowledgeService implementation uses IVectorStore interface
# from infrastructure layer, which is correct for vertical modules.
# ============================================================
