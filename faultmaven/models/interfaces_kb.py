"""Knowledge base document sharing interfaces.

This module defines the interface contracts for KB document metadata and sharing,
following FaultMaven's interface-based dependency injection pattern.

Implemented by:
- PostgreSQLKBDocumentRepository
- UserKBVectorStore (enhanced with sharing support)
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional, Dict, Any
from enum import Enum

from pydantic import BaseModel, Field


# ============================================================================
# Enums
# ============================================================================

class KBVisibility(str, Enum):
    """Document visibility scope."""
    PRIVATE = "private"
    SHARED = "shared"
    TEAM = "team"
    ORGANIZATION = "organization"


class KBDocumentType(str, Enum):
    """Knowledge base document categories."""
    RUNBOOK = "runbook"
    PROCEDURE = "procedure"
    DOCUMENTATION = "documentation"
    TROUBLESHOOTING_GUIDE = "troubleshooting_guide"
    BEST_PRACTICES = "best_practices"
    INCIDENT_POSTMORTEM = "incident_postmortem"
    ARCHITECTURE_DIAGRAM = "architecture_diagram"
    OTHER = "other"


class KBSharePermission(str, Enum):
    """Permission level for shared documents."""
    READ = "read"
    WRITE = "write"


# ============================================================================
# Models
# ============================================================================

class KBDocument(BaseModel):
    """Knowledge base document metadata."""
    doc_id: str
    owner_user_id: str
    org_id: Optional[str] = None

    title: str
    description: Optional[str] = None
    document_type: KBDocumentType = KBDocumentType.OTHER

    chromadb_collection: str  # Which ChromaDB collection stores this
    chromadb_doc_count: int = 0  # Number of chunks in ChromaDB

    visibility: KBVisibility = KBVisibility.PRIVATE
    tags: List[str] = Field(default_factory=list)

    file_size: Optional[int] = None
    original_filename: Optional[str] = None
    content_type: Optional[str] = None
    storage_path: Optional[str] = None

    metadata: Dict[str, Any] = Field(default_factory=dict)

    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None


class KBDocumentShare(BaseModel):
    """Individual user sharing for KB document."""
    doc_id: str
    shared_with_user_id: str
    permission: KBSharePermission = KBSharePermission.READ
    shared_at: datetime
    shared_by: str
    last_accessed_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class KBDocumentTeamShare(BaseModel):
    """Team-based sharing for KB document."""
    doc_id: str
    team_id: str
    permission: KBSharePermission = KBSharePermission.READ
    shared_at: datetime
    shared_by: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class KBDocumentOrgShare(BaseModel):
    """Organization-wide sharing for KB document."""
    doc_id: str
    org_id: str
    permission: KBSharePermission = KBSharePermission.READ
    shared_at: datetime
    shared_by: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# Repository Interface
# ============================================================================

class IKBDocumentRepository(ABC):
    """Interface for KB document metadata and sharing persistence operations."""

    @abstractmethod
    async def create_document(self, doc: KBDocument) -> KBDocument:
        """Create KB document metadata.

        Args:
            doc: KB document metadata to create

        Returns:
            Created document with generated ID
        """
        pass

    @abstractmethod
    async def get_document(self, doc_id: str) -> Optional[KBDocument]:
        """Get KB document by ID.

        Args:
            doc_id: Document identifier

        Returns:
            Document if found, None otherwise
        """
        pass

    @abstractmethod
    async def update_document(self, doc: KBDocument) -> bool:
        """Update KB document metadata.

        Args:
            doc: Document with updates

        Returns:
            True if update was successful
        """
        pass

    @abstractmethod
    async def delete_document(self, doc_id: str) -> bool:
        """Soft delete KB document.

        Args:
            doc_id: Document identifier

        Returns:
            True if deletion was successful
        """
        pass

    @abstractmethod
    async def list_user_documents(
        self,
        user_id: str,
        include_shared: bool = False
    ) -> List[KBDocument]:
        """List KB documents owned by user.

        Args:
            user_id: User identifier
            include_shared: If True, include documents shared with user

        Returns:
            List of documents
        """
        pass

    @abstractmethod
    async def list_accessible_documents(self, user_id: str) -> List[KBDocument]:
        """List all KB documents user can access (own + shared with them).

        Args:
            user_id: User identifier

        Returns:
            List of accessible documents
        """
        pass

    @abstractmethod
    async def search_documents(
        self,
        query: str,
        user_id: Optional[str] = None,
        document_type: Optional[KBDocumentType] = None,
        tags: Optional[List[str]] = None,
        limit: int = 20
    ) -> List[KBDocument]:
        """Search KB documents by text.

        Args:
            query: Search query
            user_id: Optional user filter (only their accessible documents)
            document_type: Optional document type filter
            tags: Optional tags filter
            limit: Maximum results

        Returns:
            List of matching documents
        """
        pass

    # Sharing operations

    @abstractmethod
    async def share_with_user(
        self,
        doc_id: str,
        shared_with_user_id: str,
        permission: KBSharePermission,
        shared_by: str
    ) -> bool:
        """Share document with specific user.

        Args:
            doc_id: Document identifier
            shared_with_user_id: User to share with
            permission: Permission level (read/write)
            shared_by: User performing the share

        Returns:
            True if share was successful
        """
        pass

    @abstractmethod
    async def unshare_with_user(
        self,
        doc_id: str,
        user_id: str,
        unshared_by: str
    ) -> bool:
        """Unshare document from user.

        Args:
            doc_id: Document identifier
            user_id: User to unshare from
            unshared_by: User performing the unshare

        Returns:
            True if unshare was successful
        """
        pass

    @abstractmethod
    async def share_with_team(
        self,
        doc_id: str,
        team_id: str,
        permission: KBSharePermission,
        shared_by: str
    ) -> bool:
        """Share document with team.

        Args:
            doc_id: Document identifier
            team_id: Team to share with
            permission: Permission level (read/write)
            shared_by: User performing the share

        Returns:
            True if share was successful
        """
        pass

    @abstractmethod
    async def unshare_with_team(
        self,
        doc_id: str,
        team_id: str,
        unshared_by: str
    ) -> bool:
        """Unshare document from team.

        Args:
            doc_id: Document identifier
            team_id: Team to unshare from
            unshared_by: User performing the unshare

        Returns:
            True if unshare was successful
        """
        pass

    @abstractmethod
    async def share_with_organization(
        self,
        doc_id: str,
        org_id: str,
        permission: KBSharePermission,
        shared_by: str
    ) -> bool:
        """Share document with entire organization.

        Args:
            doc_id: Document identifier
            org_id: Organization to share with
            permission: Permission level (read/write)
            shared_by: User performing the share

        Returns:
            True if share was successful
        """
        pass

    @abstractmethod
    async def unshare_with_organization(
        self,
        doc_id: str,
        org_id: str,
        unshared_by: str
    ) -> bool:
        """Unshare document from organization.

        Args:
            doc_id: Document identifier
            org_id: Organization to unshare from
            unshared_by: User performing the unshare

        Returns:
            True if unshare was successful
        """
        pass

    @abstractmethod
    async def list_document_shares(self, doc_id: str) -> Dict[str, Any]:
        """List all shares for a document.

        Args:
            doc_id: Document identifier

        Returns:
            Dictionary with:
            - user_shares: List[KBDocumentShare]
            - team_shares: List[KBDocumentTeamShare]
            - org_shares: List[KBDocumentOrgShare]
        """
        pass

    @abstractmethod
    async def user_can_access_document(
        self,
        user_id: str,
        doc_id: str
    ) -> bool:
        """Check if user has access to document.

        Args:
            user_id: User identifier
            doc_id: Document identifier

        Returns:
            True if user can access document
        """
        pass

    @abstractmethod
    async def get_user_document_permission(
        self,
        user_id: str,
        doc_id: str
    ) -> Optional[KBSharePermission]:
        """Get user's permission level for document.

        Args:
            user_id: User identifier
            doc_id: Document identifier

        Returns:
            Permission level (read/write) or None if no access
        """
        pass
