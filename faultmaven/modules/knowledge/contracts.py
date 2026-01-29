"""Knowledge Module Contracts

This module defines the public interfaces (contracts) for the Knowledge vertical module.
Other modules should import from here, not from infrastructure or domain directly.

Following the design in module-organization-design.md:
- Vertical modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication
"""

from abc import ABC
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol

if TYPE_CHECKING:
    from faultmaven.modules.knowledge.domain.models.knowledge_item import KnowledgeItem
    from faultmaven.modules.knowledge.domain.models.suggestion import (
        KnowledgeSuggestion,
    )


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

    async def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific document by ID."""
        ...

    async def get_semantic_snippet(
        self, document_id: str, query: str, max_lines: int = 5
    ) -> Optional[Dict[str, Any]]:
        """Get semantically relevant snippet from a document."""
        ...


class IKnowledgeQuery(Protocol):
    """Read-only knowledge query interface."""

    async def search(self, query: str, k: int = 5) -> List[dict]:
        """Perform semantic search (read-only)."""
        ...


class ISuggestionService(Protocol):
    """Service interface for Knowledge Suggestion management."""

    async def extract_knowledge_from_case(
        self,
        case_id: str,
        organization_id: str,
        extracted_by: str,
        include_messages: bool = True,
        include_evidence: bool = True,
        title_suggestion: Optional[str] = None,
    ) -> "KnowledgeSuggestion":
        """Extract knowledge from a case into a suggestion."""
        ...

    async def get_suggestion(
        self, suggestion_id: str
    ) -> Optional["KnowledgeSuggestion"]:
        """Get a suggestion by ID."""
        ...

    async def list_suggestions(
        self,
        organization_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List suggestions with filtering."""
        ...

    async def approve_suggestion(
        self,
        suggestion_id: str,
        reviewed_by: str,
        review_notes: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Approve a suggestion and create a knowledge item."""
        ...

    async def reject_suggestion(
        self,
        suggestion_id: str,
        reviewed_by: str,
        rejection_reason: str,
        review_notes: Optional[str] = None,
    ) -> bool:
        """Reject a suggestion."""
        ...


# ============================================================
# DTOs for cross-module communication
# ============================================================


# Re-export enums for external use
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItemType,
    VerificationLevel,
)
from faultmaven.modules.knowledge.domain.models.suggestion import (
    PIIScanStatus,
    SuggestionStatus,
)

# ============================================================
# Note: Knowledge module uses infrastructure/vector/ for vector store
# The actual KnowledgeService implementation uses IVectorStore interface
# from infrastructure layer, which is correct for vertical modules.
# ============================================================
