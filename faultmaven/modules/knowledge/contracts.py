"""
Knowledge Module Public Contracts

This file defines the public interface of the knowledge module.
Other modules MUST only import from this file, never from
domain/ or infrastructure/ directories.

Contracts include:
- DTOs: Data transfer objects for cross-module communication
- Protocols: Interface definitions for dependency injection
- Exceptions: Re-exported domain exceptions
- Enums: Public enumerations needed by other modules
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


# ============================================
# Public Enumerations
# ============================================


class KnowledgeItemTypeDTO(str, Enum):
    """Public representation of knowledge item types."""

    TROUBLESHOOTING_GUIDE = "troubleshooting_guide"
    ERROR_PATTERN = "error_pattern"
    SOLUTION_TEMPLATE = "solution_template"
    API_DOCUMENTATION = "api_documentation"
    CONFIGURATION_GUIDE = "configuration_guide"
    BEST_PRACTICE = "best_practice"
    FAQ = "faq"
    RUNBOOK = "runbook"


# ============================================
# Data Transfer Objects (DTOs)
# ============================================


@dataclass(frozen=True)
class KnowledgeItemDTO:
    """Public representation of a knowledge item.

    This is the cross-module representation of a knowledge base item.
    Does not expose internal details like embedding vectors.
    """

    item_id: str
    organization_id: str
    title: str
    content: str
    item_type: KnowledgeItemTypeDTO
    category: Optional[str] = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    source_url: Optional[str] = None
    author: Optional[str] = None
    language: str = "en"
    is_published: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class KnowledgeItemSummaryDTO:
    """Lightweight knowledge item summary for list views.

    Contains minimal information for efficient listing.
    """

    item_id: str
    title: str
    item_type: KnowledgeItemTypeDTO
    category: Optional[str] = None
    is_published: bool = True


@dataclass(frozen=True)
class SearchResultDTO:
    """Result from a knowledge base search.

    Contains the matched item and relevance score.
    """

    item: KnowledgeItemDTO
    relevance_score: float
    snippet: Optional[str] = None  # Highlighted content snippet


@dataclass(frozen=True)
class SearchQueryDTO:
    """Parameters for a knowledge base search.

    Encapsulates search criteria for semantic or keyword search.
    """

    query: str
    organization_id: str
    item_types: Optional[tuple[KnowledgeItemTypeDTO, ...]] = None
    categories: Optional[tuple[str, ...]] = None
    tags: Optional[tuple[str, ...]] = None
    limit: int = 10
    min_relevance: float = 0.0


@dataclass(frozen=True)
class DocumentIngestionResultDTO:
    """Result of document ingestion operation.

    Returned after processing and indexing a document.
    """

    item_id: str
    title: str
    chunk_count: int
    processing_time_ms: int
    success: bool
    error_message: Optional[str] = None


# ============================================
# Service Protocols (Interfaces)
# ============================================


@runtime_checkable
class IKnowledgeQuery(Protocol):
    """Read-only knowledge operations for cross-module use.

    Provides read access to knowledge base for other modules.
    """

    async def get_item(self, item_id: str) -> Optional[KnowledgeItemDTO]:
        """Get knowledge item by ID.

        Args:
            item_id: The item's unique identifier

        Returns:
            KnowledgeItemDTO if found, None otherwise
        """
        ...

    async def get_items_by_ids(self, item_ids: list[str]) -> list[KnowledgeItemDTO]:
        """Bulk item lookup - prevents N+1 queries.

        Args:
            item_ids: List of item IDs to fetch

        Returns:
            List of KnowledgeItemDTO objects (may be fewer than requested)
        """
        ...

    async def search(self, query: SearchQueryDTO) -> list[SearchResultDTO]:
        """Semantic search in knowledge base.

        Args:
            query: Search parameters

        Returns:
            List of SearchResultDTO objects ordered by relevance
        """
        ...

    async def list_items(
        self,
        organization_id: str,
        item_type: Optional[KnowledgeItemTypeDTO] = None,
        category: Optional[str] = None,
        published_only: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[KnowledgeItemSummaryDTO]:
        """List knowledge items with optional filtering.

        Args:
            organization_id: The organization's unique identifier
            item_type: Optional type filter
            category: Optional category filter
            published_only: Only return published items
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of KnowledgeItemSummaryDTO objects
        """
        ...


@runtime_checkable
class IKnowledgeCommand(Protocol):
    """Write operations for knowledge management.

    Provides write access to knowledge base. Use with caution from other modules.
    """

    async def ingest_document(
        self,
        organization_id: str,
        title: str,
        content: str,
        item_type: KnowledgeItemTypeDTO,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        source_url: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> DocumentIngestionResultDTO:
        """Ingest a document into the knowledge base.

        Args:
            organization_id: The organization's unique identifier
            title: Document title
            content: Document content
            item_type: Type of knowledge item
            category: Optional category
            tags: Optional tags
            source_url: Optional source URL
            metadata: Optional additional metadata

        Returns:
            DocumentIngestionResultDTO with ingestion results
        """
        ...

    async def update_item(
        self,
        item_id: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> KnowledgeItemDTO:
        """Update a knowledge item.

        Args:
            item_id: The item's unique identifier
            title: Optional new title
            content: Optional new content
            category: Optional new category
            tags: Optional new tags

        Returns:
            Updated KnowledgeItemDTO
        """
        ...

    async def delete_item(self, item_id: str) -> bool:
        """Delete a knowledge item.

        Args:
            item_id: The item's unique identifier

        Returns:
            True if deleted, False if not found
        """
        ...

    async def publish_item(self, item_id: str) -> KnowledgeItemDTO:
        """Publish a knowledge item (make visible).

        Args:
            item_id: The item's unique identifier

        Returns:
            Updated KnowledgeItemDTO
        """
        ...

    async def unpublish_item(self, item_id: str) -> KnowledgeItemDTO:
        """Unpublish a knowledge item (hide from searches).

        Args:
            item_id: The item's unique identifier

        Returns:
            Updated KnowledgeItemDTO
        """
        ...


# ============================================
# Re-exports for convenience
# ============================================

# Export exceptions from module exceptions
from faultmaven.modules.knowledge.exceptions import (
    ChunkingError,
    DocumentIngestionError,
    DocumentNotFoundError,
    IndexingError,
    KnowledgeBaseAccessError,
    KnowledgeException,
    SearchError,
)

__all__ = [
    # Enums
    "KnowledgeItemTypeDTO",
    # DTOs
    "KnowledgeItemDTO",
    "KnowledgeItemSummaryDTO",
    "SearchResultDTO",
    "SearchQueryDTO",
    "DocumentIngestionResultDTO",
    # Protocols
    "IKnowledgeQuery",
    "IKnowledgeCommand",
    # Exceptions
    "KnowledgeException",
    "DocumentNotFoundError",
    "DocumentIngestionError",
    "SearchError",
    "ChunkingError",
    "KnowledgeBaseAccessError",
    "IndexingError",
]
