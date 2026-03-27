"""Knowledge Module Contracts

This module defines the public interfaces (contracts) for the Knowledge vertical module.
Other modules should import from here, not from infrastructure or domain directly.

Following the design in module-organization-design.md:
- Vertical modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication
"""

from typing import TYPE_CHECKING, Any, Optional, Protocol

if TYPE_CHECKING:
    from faultmaven.modules.knowledge.domain.models.suggestion import (
        KnowledgeSuggestion,
    )


# ============================================================
# Service Contracts
# ============================================================


class IKnowledgeService(Protocol):
    """Service interface for Knowledge business logic."""

    async def search(self, query: str, k: int = 5) -> list[dict]:
        """Perform semantic search on knowledge base."""
        ...

    async def add_document(self, document: dict) -> str:
        """Add a document to the knowledge base."""
        ...

    async def delete_document(self, document_id: str) -> bool:
        """Delete a document from the knowledge base."""
        ...

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        """Get a specific document by ID."""
        ...

    async def get_semantic_snippet(
        self, document_id: str, query: str, max_lines: int = 5
    ) -> dict[str, Any] | None:
        """Get semantically relevant snippet from a document."""
        ...


class IKnowledgeQuery(Protocol):
    """Read-only knowledge query interface."""

    async def search(self, query: str, k: int = 5) -> list[dict]:
        """Perform semantic search (read-only)."""
        ...


class IConversionService(Protocol):
    """Service interface for document-to-runbook conversion."""

    async def convert_document(
        self,
        file_path: Any,
        content_type: str,
        original_filename: str,
        scope: str,
        user_id: str,
        organization_id: str | None = None,
        team_id: str | None = None,
    ) -> Any:
        """Convert a document to one or more runbook drafts."""
        ...

    async def get_conversion(self, conversion_id: str, user_id: str) -> Any | None:
        """Get conversion job details."""
        ...

    async def verify_draft(
        self, conversion_id: str, draft_id: str, user_id: str, username: str
    ) -> Any | None:
        """Promote draft to verified status."""
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
        title_suggestion: str | None = None,
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
        organization_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List suggestions with filtering."""
        ...

    async def approve_suggestion(
        self,
        suggestion_id: str,
        reviewed_by: str,
        review_notes: str | None = None,
    ) -> dict[str, Any] | None:
        """Approve a suggestion and create a knowledge item."""
        ...

    async def reject_suggestion(
        self,
        suggestion_id: str,
        reviewed_by: str,
        rejection_reason: str,
        review_notes: str | None = None,
    ) -> bool:
        """Reject a suggestion."""
        ...


# ============================================================
# DTOs for cross-module communication
# ============================================================


# Re-export enums for external use

# ============================================================
# Note: Knowledge module uses infrastructure/vector/ for vector store
# The actual KnowledgeService implementation uses IVectorStore interface
# from infrastructure layer, which is correct for vertical modules.
# ============================================================
