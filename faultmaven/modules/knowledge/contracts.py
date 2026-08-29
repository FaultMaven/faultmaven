"""Knowledge Module Contracts

This module defines the public interfaces (contracts) for the Knowledge vertical module.
Other modules should import from here, not from infrastructure or domain directly.

Following the design in module-organization-design.md:
- Vertical modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication
"""

from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

if TYPE_CHECKING:
    from faultmaven.modules.knowledge.domain.models.knowledge_item import KnowledgeItem
    from faultmaven.modules.knowledge.domain.models.suggestion import (
        KnowledgeSuggestion,
    )


# ============================================================
# Service Contracts
# ============================================================


class IKnowledgeService(Protocol):
    """Service interface for Knowledge business logic.

    KB retrieval during investigation is handled by the kb_qa tool
    (via the tool-augmented generation loop), not by this interface.
    The tool path provides proper scope filtering via ToolContext.
    See: modules/agent/tools/kb_qa.py, kb_tool_adapter.py.
    """

    async def delete_document(self, document_id: str) -> bool:
        """Delete a document from the knowledge base."""
        ...

    async def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific document by ID — the TRUSTED, unscoped load.

        Has no actor and applies no visibility rule. It backs the write-policy
        check and internal ingestion, so it must keep reaching rows the caller
        could not list. Never answer an actor-facing read from it.
        """
        ...

    async def get_document_visible(
        self,
        document_id: str,
        user: Optional[Any] = None,
        team_ids: Optional[list] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get a document by ID scoped to what the requester may see (#867).

        The ACTOR-FACING counterpart of :meth:`get_document`: global ∪ own ∪
        shared-to-my-teams, published-or-mine. Returns None both for an absent
        id and for one the requester cannot see, so the two are
        indistinguishable. Implementations that cannot evaluate the rule must
        return None (fail closed), never fall back to the unscoped load.
        """
        ...

    async def get_semantic_snippet(
        self, document_id: str, query: str, max_lines: int = 5
    ) -> Optional[Dict[str, Any]]:
        """Get semantically relevant snippet from a document."""
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
        organization_id: Optional[str] = None,
        team_id: Optional[str] = None,
    ) -> Any:
        """Convert a document to one or more runbook drafts."""
        ...

    async def get_conversion(self, conversion_id: str, user_id: str) -> Optional[Any]:
        """Get conversion job details."""
        ...

    async def verify_draft(
        self, conversion_id: str, draft_id: str, user_id: str, username: str
    ) -> Optional[Any]:
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
        title_suggestion: Optional[str] = None,
    ) -> "KnowledgeSuggestion":
        """Extract knowledge from a case into a suggestion."""
        ...

    async def get_suggestion(
        self, suggestion_id: str
    ) -> Optional["KnowledgeSuggestion"]:
        """Get a suggestion by ID — UNSCOPED trusted load (no actor)."""
        ...

    async def get_suggestion_visible(
        self, suggestion_id: str, *, organization_id: str
    ) -> Optional["KnowledgeSuggestion"]:
        """Get a suggestion by ID, scoped to the actor's tenant (None if out of scope)."""
        ...

    async def list_suggestions(
        self,
        organization_id: str,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List one organization's suggestions (org REQUIRED, fail-closed)."""
        ...

    async def approve_suggestion(
        self,
        suggestion_id: str,
        reviewed_by: str,
        review_notes: Optional[str] = None,
        *,
        organization_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Approve a suggestion and create a knowledge item."""
        ...

    async def reject_suggestion(
        self,
        suggestion_id: str,
        reviewed_by: str,
        rejection_reason: str,
        review_notes: Optional[str] = None,
        *,
        organization_id: str,
    ) -> bool:
        """Reject a suggestion."""
        ...


# ============================================================
# Repository Contracts
# ============================================================


# ``SuggestionConcurrencyError`` — raised by every implementation of the
# interface below — lives in ``modules/knowledge/exceptions``, NOT here.
#
# Measured, not preferred: ``contracts`` re-exports the domain models, so any
# module importing from it acquires a path to ``domain``. import-linter's layers
# contract follows indirect chains, so an infrastructure module importing this
# exception from here would report ``infrastructure -> contracts -> domain`` and
# need contract 4 to exempt the whole contracts hop — a wider hole than the
# single model import ``knowledge_item_repository`` already has exempted.
# ``exceptions`` imports nothing from ``domain``, so the exception reaches every
# layer with no chain at all.
from faultmaven.modules.knowledge.exceptions import (  # noqa: E402,F401
    SuggestionConcurrencyError,
)


class ISuggestionRepository(Protocol):
    """Persistence interface for knowledge suggestions (#1227).

    Declared here rather than in ``infrastructure`` for the reason
    ``ICaseRepository`` is: it is what ``SuggestionService`` depends on, and a
    domain service that imported a concrete repository would pull the whole ORM
    graph in at module-import time and pin itself to one implementation.

    **Every read returns a DETACHED COPY.** Mutating what a read handed you
    changes nothing until you ``save`` it. That is not a choice an
    implementation may make differently — it is what a sessionless,
    session-per-operation database repository does, and an implementation that
    returned its own live object would let a caller forget a ``save`` and still
    appear correct.

    **Every write is optimistically locked** on ``KnowledgeSuggestion.version``.
    Because reads are detached, two callers can hold the same row at version N;
    without the check the second write replays its stale snapshot over the
    first and silently reverts a concurrent decision.
    """

    async def save(self, suggestion: "KnowledgeSuggestion") -> "KnowledgeSuggestion":
        """Insert a new suggestion, or update an existing one in place.

        The update is conditional on ``suggestion.version`` still matching what
        is stored; on success the stored version is bumped and the returned
        copy carries the new value.

        Raises:
            SuggestionConcurrencyError: the row moved since it was loaded.
        """
        ...

    async def get(self, suggestion_id: str) -> Optional["KnowledgeSuggestion"]:
        """Load one suggestion by id — UNSCOPED (the trusted internal load)."""
        ...

    async def get_for_organization(
        self, suggestion_id: str, organization_id: str
    ) -> Optional["KnowledgeSuggestion"]:
        """Load one suggestion by id, scoped to ``organization_id``.

        ``None`` both for an absent id and for one owned by another tenant, so
        the two are indistinguishable to the caller.
        """
        ...

    async def list_for_organization(
        self,
        organization_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> "Tuple[List[KnowledgeSuggestion], int]":
        """Return one page of an organization's suggestions and the total count.

        Newest first, by ``created_at``.
        """
        ...

    async def count_for_organization(
        self,
        organization_id: str,
        *,
        statuses: Optional["Sequence[SuggestionStatus]"] = None,
    ) -> int:
        """Count an organization's suggestions, optionally in given statuses."""
        ...

    @property
    def is_durable(self) -> bool:
        """Does this store survive a restart and share rows across processes?

        Reported on ``GET /admin/config/status``. An implementation states it
        about itself; the composition root is responsible for not building a
        durable-claiming store over an ephemeral database.
        """
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
    KnowledgeSuggestion,
    PIIScanStatus,
    SuggestionStatus,
)

# ============================================================
# Note: Knowledge module uses infrastructure/vector/ for vector store
# The actual KnowledgeService implementation uses IVectorStore interface
# from infrastructure layer, which is correct for vertical modules.
# ============================================================
