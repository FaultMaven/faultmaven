"""KB Document Repository - PostgreSQL Implementation.

Implements IKBDocumentRepository for KB document metadata and sharing.
Works in conjunction with UserKBVectorStore for vector embeddings.
"""

from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from faultmaven.models.interfaces_kb import (
    IKBDocumentRepository,
    KBDocument,
    KBDocumentType,
    KBVisibility,
    KBSharePermission
)

logger = logging.getLogger(__name__)


class PostgreSQLKBDocumentRepository(IKBDocumentRepository):
    """PostgreSQL implementation of KB document repository.

    Manages KB document metadata and sharing permissions.
    Vector embeddings stored separately in ChromaDB via UserKBVectorStore.
    """

    def __init__(self, db_session: AsyncSession):
        """Initialize repository with database session.

        Args:
            db_session: SQLAlchemy async session
        """
        self.db = db_session

    async def create_document(self, doc: KBDocument) -> KBDocument:
        """Create KB document metadata."""
        query = text("""
            INSERT INTO kb_documents (
                doc_id, owner_user_id, org_id, title, description, document_type,
                chromadb_collection, chromadb_doc_count, visibility, tags,
                file_size, original_filename, content_type, storage_path,
                metadata, created_at, updated_at
            ) VALUES (
                :doc_id, :owner_user_id, :org_id, :title, :description, :document_type,
                :chromadb_collection, :chromadb_doc_count, :visibility, :tags,
                :file_size, :original_filename, :content_type, :storage_path,
                :metadata::jsonb, :created_at, :updated_at
            )
            RETURNING doc_id
        """)

        await self.db.execute(query, {
            "doc_id": doc.doc_id,
            "owner_user_id": doc.owner_user_id,
            "org_id": doc.org_id,
            "title": doc.title,
            "description": doc.description,
            "document_type": doc.document_type.value,
            "chromadb_collection": doc.chromadb_collection,
            "chromadb_doc_count": doc.chromadb_doc_count,
            "visibility": doc.visibility.value,
            "tags": doc.tags or [],
            "file_size": doc.file_size,
            "original_filename": doc.original_filename,
            "content_type": doc.content_type,
            "storage_path": doc.storage_path,
            "metadata": doc.metadata or {},
            "created_at": doc.created_at,
            "updated_at": doc.updated_at
        })
        await self.db.commit()

        logger.info(f"Created KB document: {doc.doc_id} ({doc.title})")
        return doc

    async def get_document(self, doc_id: str) -> Optional[KBDocument]:
        """Get KB document by ID."""
        query = text("""
            SELECT doc_id, owner_user_id, org_id, title, description, document_type,
                   chromadb_collection, chromadb_doc_count, visibility, tags,
                   file_size, original_filename, content_type, storage_path,
                   metadata, created_at, updated_at, deleted_at
            FROM kb_documents
            WHERE doc_id = :doc_id AND deleted_at IS NULL
        """)

        result = await self.db.execute(query, {"doc_id": doc_id})
        row = result.fetchone()

        if not row:
            return None

        return self._row_to_document(row)

    async def update_document(self, doc: KBDocument) -> bool:
        """Update KB document metadata."""
        doc.updated_at = datetime.now(timezone.utc)

        query = text("""
            UPDATE kb_documents
            SET title = :title,
                description = :description,
                document_type = :document_type,
                visibility = :visibility,
                tags = :tags,
                chromadb_doc_count = :chromadb_doc_count,
                metadata = :metadata::jsonb,
                updated_at = :updated_at
            WHERE doc_id = :doc_id AND deleted_at IS NULL
        """)

        result = await self.db.execute(query, {
            "doc_id": doc.doc_id,
            "title": doc.title,
            "description": doc.description,
            "document_type": doc.document_type.value,
            "visibility": doc.visibility.value,
            "tags": doc.tags or [],
            "chromadb_doc_count": doc.chromadb_doc_count,
            "metadata": doc.metadata or {},
            "updated_at": doc.updated_at
        })
        await self.db.commit()

        return result.rowcount > 0

    async def delete_document(self, doc_id: str) -> bool:
        """Soft delete KB document."""
        query = text("""
            UPDATE kb_documents
            SET deleted_at = :deleted_at
            WHERE doc_id = :doc_id AND deleted_at IS NULL
        """)

        result = await self.db.execute(query, {
            "doc_id": doc_id,
            "deleted_at": datetime.now(timezone.utc)
        })
        await self.db.commit()

        return result.rowcount > 0

    async def list_user_documents(
        self,
        user_id: str,
        include_shared: bool = False
    ) -> List[KBDocument]:
        """List KB documents owned by user."""
        if include_shared:
            query = text("""
                SELECT DISTINCT d.doc_id, d.owner_user_id, d.org_id, d.title, d.description,
                       d.document_type, d.chromadb_collection, d.chromadb_doc_count,
                       d.visibility, d.tags, d.file_size, d.original_filename,
                       d.content_type, d.storage_path, d.metadata, d.created_at,
                       d.updated_at, d.deleted_at
                FROM kb_documents d
                LEFT JOIN kb_document_shares ds ON d.doc_id = ds.doc_id
                LEFT JOIN kb_document_team_shares dts ON d.doc_id = dts.doc_id
                LEFT JOIN team_members tm ON dts.team_id = tm.team_id
                WHERE (d.owner_user_id = :user_id OR ds.shared_with_user_id = :user_id OR tm.user_id = :user_id)
                  AND d.deleted_at IS NULL
                ORDER BY d.created_at DESC
            """)
        else:
            query = text("""
                SELECT doc_id, owner_user_id, org_id, title, description, document_type,
                       chromadb_collection, chromadb_doc_count, visibility, tags,
                       file_size, original_filename, content_type, storage_path,
                       metadata, created_at, updated_at, deleted_at
                FROM kb_documents
                WHERE owner_user_id = :user_id AND deleted_at IS NULL
                ORDER BY created_at DESC
            """)

        result = await self.db.execute(query, {"user_id": user_id})
        rows = result.fetchall()

        return [self._row_to_document(row) for row in rows]

    async def list_accessible_documents(self, user_id: str) -> List[KBDocument]:
        """List all KB documents user can access (own + shared with them)."""
        return await self.list_user_documents(user_id, include_shared=True)

    async def search_documents(
        self,
        query: str,
        user_id: Optional[str] = None,
        document_type: Optional[KBDocumentType] = None,
        tags: Optional[List[str]] = None,
        limit: int = 20
    ) -> List[KBDocument]:
        """Search KB documents by text."""
        # Simplified search - full-text search on title and description
        sql_query = text("""
            SELECT doc_id, owner_user_id, org_id, title, description, document_type,
                   chromadb_collection, chromadb_doc_count, visibility, tags,
                   file_size, original_filename, content_type, storage_path,
                   metadata, created_at, updated_at, deleted_at
            FROM kb_documents
            WHERE deleted_at IS NULL
              AND (title ILIKE :query OR description ILIKE :query)
            ORDER BY created_at DESC
            LIMIT :limit
        """)

        result = await self.db.execute(sql_query, {
            "query": f"%{query}%",
            "limit": limit
        })
        rows = result.fetchall()

        return [self._row_to_document(row) for row in rows]

    # Sharing operations

    async def share_with_user(
        self,
        doc_id: str,
        shared_with_user_id: str,
        permission: KBSharePermission,
        shared_by: str
    ) -> bool:
        """Share document with specific user."""
        query = text("""
            SELECT share_kb_document_with_user(
                :doc_id,
                :shared_with_user_id,
                :permission::kb_share_permission,
                :shared_by
            )
        """)

        await self.db.execute(query, {
            "doc_id": doc_id,
            "shared_with_user_id": shared_with_user_id,
            "permission": permission.value,
            "shared_by": shared_by
        })
        await self.db.commit()

        logger.info(f"Shared KB document {doc_id} with user {shared_with_user_id}")
        return True

    async def unshare_with_user(
        self,
        doc_id: str,
        user_id: str,
        unshared_by: str
    ) -> bool:
        """Unshare document from user."""
        query = text("""
            SELECT unshare_kb_document_from_user(
                :doc_id,
                :user_id,
                :unshared_by
            )
        """)

        await self.db.execute(query, {
            "doc_id": doc_id,
            "user_id": user_id,
            "unshared_by": unshared_by
        })
        await self.db.commit()

        return True

    async def share_with_team(
        self,
        doc_id: str,
        team_id: str,
        permission: KBSharePermission,
        shared_by: str
    ) -> bool:
        """Share document with team."""
        query = text("""
            SELECT share_kb_document_with_team(
                :doc_id,
                :team_id,
                :permission::kb_share_permission,
                :shared_by
            )
        """)

        await self.db.execute(query, {
            "doc_id": doc_id,
            "team_id": team_id,
            "permission": permission.value,
            "shared_by": shared_by
        })
        await self.db.commit()

        logger.info(f"Shared KB document {doc_id} with team {team_id}")
        return True

    async def unshare_with_team(
        self,
        doc_id: str,
        team_id: str,
        unshared_by: str
    ) -> bool:
        """Unshare document from team."""
        # Use SQL function from migration 004
        query = text("""
            DELETE FROM kb_document_team_shares
            WHERE doc_id = :doc_id AND team_id = :team_id
        """)

        result = await self.db.execute(query, {"doc_id": doc_id, "team_id": team_id})
        await self.db.commit()

        return result.rowcount > 0

    async def share_with_organization(
        self,
        doc_id: str,
        org_id: str,
        permission: KBSharePermission,
        shared_by: str
    ) -> bool:
        """Share document with entire organization."""
        query = text("""
            INSERT INTO kb_document_org_shares (doc_id, org_id, permission, shared_by, shared_at)
            VALUES (:doc_id, :org_id, :permission::kb_share_permission, :shared_by, :shared_at)
            ON CONFLICT (doc_id, org_id) DO UPDATE
            SET permission = EXCLUDED.permission
        """)

        await self.db.execute(query, {
            "doc_id": doc_id,
            "org_id": org_id,
            "permission": permission.value,
            "shared_by": shared_by,
            "shared_at": datetime.now(timezone.utc)
        })
        await self.db.commit()

        logger.info(f"Shared KB document {doc_id} with organization {org_id}")
        return True

    async def unshare_with_organization(
        self,
        doc_id: str,
        org_id: str,
        unshared_by: str
    ) -> bool:
        """Unshare document from organization."""
        query = text("""
            DELETE FROM kb_document_org_shares
            WHERE doc_id = :doc_id AND org_id = :org_id
        """)

        result = await self.db.execute(query, {"doc_id": doc_id, "org_id": org_id})
        await self.db.commit()

        return result.rowcount > 0

    async def list_document_shares(self, doc_id: str) -> Dict[str, Any]:
        """List all shares for a document."""
        # Get user shares
        user_query = text("""
            SELECT shared_with_user_id, permission, shared_at, shared_by
            FROM kb_document_shares
            WHERE doc_id = :doc_id
        """)
        user_result = await self.db.execute(user_query, {"doc_id": doc_id})
        user_shares = user_result.fetchall()

        # Get team shares
        team_query = text("""
            SELECT team_id, permission, shared_at, shared_by
            FROM kb_document_team_shares
            WHERE doc_id = :doc_id
        """)
        team_result = await self.db.execute(team_query, {"doc_id": doc_id})
        team_shares = team_result.fetchall()

        # Get org shares
        org_query = text("""
            SELECT org_id, permission, shared_at, shared_by
            FROM kb_document_org_shares
            WHERE doc_id = :doc_id
        """)
        org_result = await self.db.execute(org_query, {"doc_id": doc_id})
        org_shares = org_result.fetchall()

        return {
            "user_shares": [dict(row._mapping) for row in user_shares],
            "team_shares": [dict(row._mapping) for row in team_shares],
            "org_shares": [dict(row._mapping) for row in org_shares]
        }

    async def user_can_access_document(
        self,
        user_id: str,
        doc_id: str
    ) -> bool:
        """Check if user has access to document."""
        query = text("""
            SELECT user_can_access_kb_document(:user_id, :doc_id)
        """)

        result = await self.db.execute(query, {"user_id": user_id, "doc_id": doc_id})
        row = result.fetchone()

        return bool(row[0]) if row else False

    async def get_user_document_permission(
        self,
        user_id: str,
        doc_id: str
    ) -> Optional[KBSharePermission]:
        """Get user's permission level for document."""
        query = text("""
            SELECT get_user_kb_document_permission(:user_id, :doc_id)
        """)

        result = await self.db.execute(query, {"user_id": user_id, "doc_id": doc_id})
        row = result.fetchone()

        if row and row[0]:
            return KBSharePermission(row[0])
        return None

    def _row_to_document(self, row) -> KBDocument:
        """Convert database row to KBDocument model."""
        return KBDocument(
            doc_id=row.doc_id,
            owner_user_id=row.owner_user_id,
            org_id=row.org_id,
            title=row.title,
            description=row.description,
            document_type=KBDocumentType(row.document_type),
            chromadb_collection=row.chromadb_collection,
            chromadb_doc_count=row.chromadb_doc_count,
            visibility=KBVisibility(row.visibility),
            tags=row.tags or [],
            file_size=row.file_size,
            original_filename=row.original_filename,
            content_type=row.content_type,
            storage_path=row.storage_path,
            metadata=row.metadata or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
            deleted_at=row.deleted_at
        )
