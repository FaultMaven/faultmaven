"""Knowledge Item Repository - Database and In-Memory Implementations.

This module provides knowledge item repository implementations for managing
knowledge base items in the RAG (Retrieval-Augmented Generation) system.

Features:
- Full CRUD operations for knowledge items
- Text search on title and content
- Tag-based filtering with match_all/match_any options
- Query items needing embedding generation
- Helpfulness score ranking
- Pagination support
- Support for both in-memory and database backends

Tenancy posture: unlike the case repositories (whose reads ignore the org
param and rely on PostgreSQL RLS alone, ADR-010), knowledge queries
deliberately keep their per-query ``organization_id`` predicates on the
org-owned tiers (personal | team, ADR-011/ADR-013) — defense-in-depth on top
of RLS. Do NOT remove them by analogy with the case module. Global-tier rows
are the org-free platform corpus (#770): they carry NO organization_id
(``knowledge_items_global_org_check``) and are readable by every tenant on
both paths — SQL via the org-free ``scope='global'`` visibility arm here plus
the RLS read exemption (``knowledge_items_tenant_read``, migration 033), and
vector via ``build_kb_scope_filter`` (``global ∪ owner ∪ team-share
allowlist``, owner ids globally unique, share ids resolved from RLS-scoped
SQL). Writes to the global tier are platform operations: RLS write policies
confine them to single-tenant sentinel sessions or the audited BYPASSRLS
maintenance path (``jobs/run.py kb_seed``), never tenant sessions. Methods
below that take only an ``organization_id`` (counts / org listings /
text+tag search) are org-scoped queries that exclude the org-free platform
tier; note they currently have NO production callers (the service uses only
get/create/update/delete/list_for_inventory) — wire the global arm in
explicitly if one is ever made live, or remove them.

Usage:
    from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
        DatabaseKnowledgeItemRepository,
        InMemoryKnowledgeItemRepository,
    )
    from faultmaven.infrastructure.persistence.database import get_db_session

    # Database usage
    async with get_db_session() as session:
        repo = DatabaseKnowledgeItemRepository(session)
        item = await repo.create(item)

    # In-memory usage (for testing)
    repo = InMemoryKnowledgeItemRepository()
    item = await repo.create(item)
"""

import json
import logging
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.models import (
    KnowledgeItemModel,
    ResourceShareModel,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
    KnowledgeScope,
)

logger = logging.getLogger(__name__)


class KnowledgeItemRepositoryException(Exception):
    """Exception raised when knowledge item repository operations fail."""

    pass


class KnowledgeItemRepository(ABC):
    """Abstract base class for knowledge item repositories."""

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    @abstractmethod
    async def create(self, item: KnowledgeItem) -> KnowledgeItem:
        """Create a new knowledge item.

        Args:
            item: Knowledge item to create

        Returns:
            Created knowledge item with timestamps

        Raises:
            ValueError: If item_id already exists
            KnowledgeItemRepositoryException: If creation fails
        """
        pass

    @abstractmethod
    async def get_by_id(self, item_id: str) -> Optional[KnowledgeItem]:
        """Get knowledge item by ID.

        Args:
            item_id: Item identifier

        Returns:
            Knowledge item if found, None otherwise
        """
        pass

    @abstractmethod
    async def update(self, item: KnowledgeItem) -> KnowledgeItem:
        """Update existing knowledge item.

        Args:
            item: Item with updated fields

        Returns:
            Updated item

        Raises:
            ValueError: If item not found
            KnowledgeItemRepositoryException: If update fails
        """
        pass

    @abstractmethod
    async def delete(self, item_id: str) -> bool:
        """Delete knowledge item by ID.

        Args:
            item_id: Item identifier

        Returns:
            True if deleted, False if not found
        """
        pass

    # =========================================================================
    # Query Operations
    # =========================================================================

    @abstractmethod
    async def list_by_organization_id(
        self,
        organization_id: str,
        item_type: Optional[KnowledgeItemType] = None,
        category: Optional[str] = None,
        is_published: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> List[KnowledgeItem]:
        """List knowledge items with filtering and pagination.

        Args:
            organization_id: Organization identifier
            item_type: Optional filter by item type
            category: Optional filter by category
            is_published: Filter by publication status (default True)
            limit: Maximum results to return
            offset: Offset for pagination

        Returns:
            List of items ordered by created_at DESC
        """
        pass

    @abstractmethod
    async def list_for_inventory(
        self,
        organization_id: str,
        user_id: Optional[str] = None,
        team_ids: Optional[List[str]] = None,
        item_type: Optional[KnowledgeItemType] = None,
    ) -> List[KnowledgeItem]:
        """List published items visible to a requester, RBAC enforced in-query.

        Powers the dashboard "Runbooks" inventory. Visibility (mirrors
        ``build_kb_scope_filter``):
          - global → visible to everyone in the org
          - owned → any item whose ``owner_id == user_id`` (an author always
            sees their own, regardless of scope)
          - team-shared → items shared to any of ``team_ids`` via the
            ``resource_shares`` table (ADR-013 §D4 / ADR-011 D3)

        Returns the full RBAC-visible set (ordered created_at DESC); the
        service applies tag/scope filtering and pagination over this already
        tenant-isolated set. RBAC lives here, NOT in a post-fetch Python
        filter, so a personal/team item can never leak across tenants.
        """
        pass

    @abstractmethod
    async def get_visible_by_id(
        self,
        item_id: str,
        organization_id: str,
        user_id: Optional[str] = None,
        team_ids: Optional[List[str]] = None,
    ) -> Optional[KnowledgeItem]:
        """Get an item by ID, scoped to what the requester may see (#867).

        Applies the same read-visibility rule as ``list_for_inventory``:
        global ∪ own-org owned ∪ own-org shared-to-my-teams. Returns None both
        for an absent id and for an id the requester cannot see — the two are
        indistinguishable by design, so the endpoint cannot be used as an
        existence oracle for other users' documents.

        Additionally the row must be **published or the requester's own**: for
        a built-in global runbook, unpublish IS the delete semantics
        (``KnowledgeService.delete_document`` keeps the row and drops the
        vectors), so serving an unpublished row by id would hand deleted
        content to any authenticated caller. The owner exemption keeps an
        author's own unpublished draft reachable by id.
        """
        pass

    @abstractmethod
    async def search_by_text(
        self,
        organization_id: str,
        query: str,
        item_type: Optional[KnowledgeItemType] = None,
        limit: int = 10,
    ) -> List[KnowledgeItem]:
        """Full-text search for knowledge items.

        Args:
            organization_id: Organization identifier
            query: Search query string
            item_type: Optional filter by item type
            limit: Maximum results to return

        Returns:
            List of matching items ordered by relevance
        """
        pass

    @abstractmethod
    async def search_by_tags(
        self,
        organization_id: str,
        tags: List[str],
        match_all: bool = False,
        limit: int = 50,
    ) -> List[KnowledgeItem]:
        """Search knowledge items by tags.

        Args:
            organization_id: Organization identifier
            tags: List of tags to search for
            match_all: If True, item must have all tags. If False, any tag matches.
            limit: Maximum results to return

        Returns:
            List of matching items ordered by created_at DESC
        """
        pass

    @abstractmethod
    async def get_items_without_embeddings(
        self,
        organization_id: str,
        limit: int = 100,
    ) -> List[KnowledgeItem]:
        """Get items that need embedding generation.

        Args:
            organization_id: Organization identifier
            limit: Maximum results to return

        Returns:
            List of items without embedding vectors
        """
        pass

    @abstractmethod
    async def count_by_organization_id(
        self,
        organization_id: str,
        item_type: Optional[KnowledgeItemType] = None,
    ) -> int:
        """Count knowledge items for an organization.

        Args:
            organization_id: Organization identifier
            item_type: Optional filter by item type

        Returns:
            Number of matching items
        """
        pass

    @abstractmethod
    async def get_most_helpful(
        self,
        organization_id: str,
        limit: int = 10,
    ) -> List[KnowledgeItem]:
        """Get most helpful items sorted by helpfulness score.

        Only includes items with at least some feedback (minimum threshold).

        Args:
            organization_id: Organization identifier
            limit: Maximum results to return

        Returns:
            List of items ordered by helpfulness score DESC
        """
        pass


class DatabaseKnowledgeItemRepository(KnowledgeItemRepository):
    """Database-backed knowledge item repository using SQLAlchemy ORM."""

    # Minimum feedback count to be included in helpfulness ranking
    MIN_FEEDBACK_THRESHOLD = 3

    def __init__(self, db_session: AsyncSession):
        """Initialize repository with database session.

        Args:
            db_session: SQLAlchemy async session for database operations
        """
        self.db = db_session

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    async def create(self, item: KnowledgeItem) -> KnowledgeItem:
        """Create new knowledge item record in the database."""
        try:
            # Convert domain model to ORM model. The TagsArray TypeDecorator
            # on `tags` handles cross-dialect serialization (list[str] →
            # text[] on PG, comma-sep TEXT on SQLite), so we pass the list
            # directly. embedding_vector is a plain Text column → JSON-encode.
            item_model = KnowledgeItemModel(
                item_id=item.item_id,
                organization_id=item.organization_id,
                scope=item.scope.value,
                owner_id=item.owner_id,
                title=item.title,
                content=item.content,
                item_type=item.item_type.value,
                category=item.category,
                tags=list(item.tags) if item.tags else None,
                embedding_model=item.embedding_model,
                embedding_vector=(
                    json.dumps(item.embedding_vector) if item.embedding_vector else None
                ),
                embedding_version=item.embedding_version,
                source_url=item.source_url,
                author=item.author,
                language=item.language,
                view_count=item.view_count,
                helpful_count=item.helpful_count,
                not_helpful_count=item.not_helpful_count,
                last_retrieved_at=item.last_retrieved_at,
                is_published=item.is_published,
                # Verification metadata. Coerce IntEnum → int for the column;
                # the IntEnum compares equal to its value but SQLAlchemy
                # serializes the int form more predictably across dialects.
                verification_level=int(item.verification_level),
                verification_reason=item.verification_reason,
                verified_by=item.verified_by,
                verified_at=item.verified_at,
                source_suggestion_id=item.source_suggestion_id,
                created_at=item.created_at,
                updated_at=item.updated_at,
                knowledge_metadata=json.dumps(item.metadata) if item.metadata else "{}",
            )

            self.db.add(item_model)
            await self.db.commit()
            await self.db.refresh(item_model)

            logger.debug(f"Created knowledge item {item.item_id}")
            return self._to_domain(item_model)

        except IntegrityError as e:
            await self.db.rollback()
            msg = str(e.orig if e.orig is not None else e).lower()
            if "foreign key" in msg or "violates foreign key" in msg:
                logger.error(f"Knowledge item {item.item_id} FK violation: {e.orig}")
                raise KnowledgeItemRepositoryException(
                    f"Failed to create knowledge item {item.item_id}: "
                    f"foreign key violation ({e.orig})"
                ) from e
            logger.error(f"Knowledge item {item.item_id} already exists")
            raise ValueError(f"Knowledge item {item.item_id} already exists") from e

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to create knowledge item: {e}")
            raise KnowledgeItemRepositoryException(
                f"Failed to create knowledge item: {e}"
            ) from e

    async def get_by_id(self, item_id: str) -> Optional[KnowledgeItem]:
        """Get knowledge item by ID."""
        try:
            stmt = select(KnowledgeItemModel).where(
                KnowledgeItemModel.item_id == item_id
            )
            result = await self.db.execute(stmt)
            item_model = result.scalar_one_or_none()

            if item_model is None:
                return None

            return self._to_domain(item_model)

        except Exception as e:
            logger.error(f"Failed to get knowledge item {item_id}: {e}")
            raise KnowledgeItemRepositoryException(
                f"Failed to get knowledge item {item_id}: {e}"
            ) from e

    async def update(self, item: KnowledgeItem) -> KnowledgeItem:
        """Update knowledge item fields."""
        try:
            item.updated_at = datetime.now(timezone.utc)

            stmt = (
                update(KnowledgeItemModel)
                .where(KnowledgeItemModel.item_id == item.item_id)
                .values(
                    scope=item.scope.value,
                    owner_id=item.owner_id,
                    title=item.title,
                    content=item.content,
                    item_type=item.item_type.value,
                    category=item.category,
                    tags=list(item.tags) if item.tags else None,
                    embedding_model=item.embedding_model,
                    embedding_vector=(
                        json.dumps(item.embedding_vector)
                        if item.embedding_vector
                        else None
                    ),
                    embedding_version=item.embedding_version,
                    source_url=item.source_url,
                    author=item.author,
                    language=item.language,
                    view_count=item.view_count,
                    helpful_count=item.helpful_count,
                    not_helpful_count=item.not_helpful_count,
                    last_retrieved_at=item.last_retrieved_at,
                    is_published=item.is_published,
                    verification_level=int(item.verification_level),
                    verification_reason=item.verification_reason,
                    verified_by=item.verified_by,
                    verified_at=item.verified_at,
                    source_suggestion_id=item.source_suggestion_id,
                    updated_at=item.updated_at,
                    knowledge_metadata=(
                        json.dumps(item.metadata) if item.metadata else "{}"
                    ),
                )
                .execution_options(synchronize_session=False)
            )

            result = await self.db.execute(stmt)

            if result.rowcount == 0:
                raise ValueError(f"Knowledge item {item.item_id} not found")

            await self.db.commit()
            logger.debug(f"Updated knowledge item {item.item_id}")
            return item

        except ValueError:
            raise

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to update knowledge item: {e}")
            raise KnowledgeItemRepositoryException(
                f"Failed to update knowledge item: {e}"
            ) from e

    async def delete(self, item_id: str) -> bool:
        """Delete knowledge item by ID."""
        try:
            stmt = delete(KnowledgeItemModel).where(
                KnowledgeItemModel.item_id == item_id
            )
            result = await self.db.execute(stmt)
            await self.db.commit()

            deleted = result.rowcount > 0
            if deleted:
                logger.debug(f"Deleted knowledge item {item_id}")
            return deleted

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to delete knowledge item {item_id}: {e}")
            raise KnowledgeItemRepositoryException(
                f"Failed to delete knowledge item {item_id}: {e}"
            ) from e

    # =========================================================================
    # Query Operations
    # =========================================================================

    async def list_by_organization_id(
        self,
        organization_id: str,
        item_type: Optional[KnowledgeItemType] = None,
        category: Optional[str] = None,
        is_published: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> List[KnowledgeItem]:
        """List knowledge items with filtering and pagination."""
        try:
            # Build query conditions
            conditions = [
                KnowledgeItemModel.organization_id == organization_id,
                KnowledgeItemModel.is_published == is_published,
            ]
            if item_type:
                conditions.append(KnowledgeItemModel.item_type == item_type.value)
            if category:
                conditions.append(KnowledgeItemModel.category == category)

            where_clause = and_(*conditions)

            stmt = (
                select(KnowledgeItemModel)
                .where(where_clause)
                .order_by(KnowledgeItemModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )

            result = await self.db.execute(stmt)
            item_models = result.scalars().all()

            return [self._to_domain(model) for model in item_models]

        except Exception as e:
            logger.error(
                f"Failed to list items for organization {organization_id}: {e}"
            )
            raise KnowledgeItemRepositoryException(
                f"Failed to list items for organization {organization_id}: {e}"
            ) from e

    @staticmethod
    def _inventory_visibility_clause(
        organization_id: str,
        user_id: Optional[str],
        team_ids: Optional[List[str]],
    ):
        """RBAC scope-visibility predicate for the inventory surface.

        Mirrors the vector-retrieval allowlist (``build_kb_scope_filter``):
        ``global`` (the org-free platform tier, visible to every tenant — #770)
        ∪ own-org items the requester ``owns`` (an author always sees their own)
        ∪ own-org items ``shared to any of the requester's teams`` via
        ``resource_shares``. The ``organization_id`` predicate guards only the
        org-owned arms (defense-in-depth on top of RLS for personal/team); the
        global arm is deliberately org-free — global rows carry no org
        (``knowledge_items_global_org_check``). The owner/team branches are
        added only when the requester has a user_id / team memberships, so an
        anonymous caller sees global content only. The share table is the single
        source of truth for team visibility (ADR-013 §D4 / ADR-011 D3).
        """
        org_owned = []
        if user_id:
            org_owned.append(KnowledgeItemModel.owner_id == user_id)
        if team_ids:
            shared_ids = select(ResourceShareModel.resource_id).where(
                ResourceShareModel.resource_type == "knowledge_item",
                ResourceShareModel.scope_type == "team",
                ResourceShareModel.scope_id.in_(list(team_ids)),
            )
            org_owned.append(KnowledgeItemModel.item_id.in_(shared_ids))

        visibility = [KnowledgeItemModel.scope == "global"]
        if org_owned:
            visibility.append(
                and_(
                    KnowledgeItemModel.organization_id == organization_id,
                    or_(*org_owned),
                )
            )
        return or_(*visibility)

    async def list_for_inventory(
        self,
        organization_id: str,
        user_id: Optional[str] = None,
        team_ids: Optional[List[str]] = None,
        item_type: Optional[KnowledgeItemType] = None,
    ) -> List[KnowledgeItem]:
        """List published items visible to a requester, RBAC enforced in-query."""
        try:
            conditions = [
                KnowledgeItemModel.is_published == True,  # noqa: E712
                self._inventory_visibility_clause(organization_id, user_id, team_ids),
            ]
            if item_type:
                conditions.append(KnowledgeItemModel.item_type == item_type.value)

            stmt = (
                select(KnowledgeItemModel)
                .where(and_(*conditions))
                .order_by(KnowledgeItemModel.created_at.desc())
            )
            result = await self.db.execute(stmt)
            item_models = result.scalars().all()
            return [self._to_domain(model) for model in item_models]

        except Exception as e:
            logger.error(
                f"Failed to list inventory for organization {organization_id}: {e}"
            )
            raise KnowledgeItemRepositoryException(
                f"Failed to list inventory for organization {organization_id}: {e}"
            ) from e

    async def get_visible_by_id(
        self,
        item_id: str,
        organization_id: str,
        user_id: Optional[str] = None,
        team_ids: Optional[List[str]] = None,
    ) -> Optional[KnowledgeItem]:
        """Get an item by ID, read-visibility enforced in-query (#867).

        RBAC lives in the WHERE clause, never in a post-fetch Python filter
        (same rationale as ``list_for_inventory``), so an invisible row is
        never materialised in the first place.

        The published-or-mine condition is applied on top of the scope
        predicate rather than inside ``_inventory_visibility_clause``, which
        ``list_for_inventory`` shares and which applies ``is_published``
        separately.
        """
        try:
            # Unpublish IS the delete semantics for built-in global runbooks
            # (``KnowledgeService.delete_document``), so an unpublished row
            # must not stay id-readable to anyone but its author.
            published_or_mine = [KnowledgeItemModel.is_published == True]  # noqa: E712
            if user_id:
                published_or_mine.append(KnowledgeItemModel.owner_id == user_id)

            stmt = select(KnowledgeItemModel).where(
                and_(
                    KnowledgeItemModel.item_id == item_id,
                    self._inventory_visibility_clause(
                        organization_id, user_id, team_ids
                    ),
                    or_(*published_or_mine),
                )
            )
            result = await self.db.execute(stmt)
            item_model = result.scalar_one_or_none()

            if item_model is None:
                return None

            return self._to_domain(item_model)

        except Exception as e:
            logger.error(f"Failed to get visible knowledge item {item_id}: {e}")
            raise KnowledgeItemRepositoryException(
                f"Failed to get visible knowledge item {item_id}: {e}"
            ) from e

    async def search_by_text(
        self,
        organization_id: str,
        query: str,
        item_type: Optional[KnowledgeItemType] = None,
        limit: int = 10,
    ) -> List[KnowledgeItem]:
        """Full-text search for knowledge items.

        Uses LIKE operator for SQLite compatibility.
        For PostgreSQL, could be enhanced with ts_vector/ts_query.
        """
        try:
            # Build query conditions
            search_pattern = f"%{query}%"
            conditions = [
                KnowledgeItemModel.organization_id == organization_id,
                KnowledgeItemModel.is_published == True,
                or_(
                    KnowledgeItemModel.title.ilike(search_pattern),
                    KnowledgeItemModel.content.ilike(search_pattern),
                ),
            ]
            if item_type:
                conditions.append(KnowledgeItemModel.item_type == item_type.value)

            where_clause = and_(*conditions)

            stmt = (
                select(KnowledgeItemModel)
                .where(where_clause)
                .order_by(KnowledgeItemModel.created_at.desc())
                .limit(limit)
            )

            result = await self.db.execute(stmt)
            item_models = result.scalars().all()

            return [self._to_domain(model) for model in item_models]

        except Exception as e:
            logger.error(
                f"Failed to search items for organization {organization_id}: {e}"
            )
            raise KnowledgeItemRepositoryException(
                f"Failed to search items for organization {organization_id}: {e}"
            ) from e

    async def search_by_tags(
        self,
        organization_id: str,
        tags: List[str],
        match_all: bool = False,
        limit: int = 50,
    ) -> List[KnowledgeItem]:
        """Search knowledge items by tags.

        For SQLite, fetches items and filters in memory.
        For PostgreSQL, could use JSONB operators.
        """
        try:
            if not tags:
                return []

            # Fetch published items for organization
            stmt = (
                select(KnowledgeItemModel)
                .where(
                    and_(
                        KnowledgeItemModel.organization_id == organization_id,
                        KnowledgeItemModel.is_published == True,
                    )
                )
                .order_by(KnowledgeItemModel.created_at.desc())
            )

            result = await self.db.execute(stmt)
            item_models = result.scalars().all()

            # Filter by tags in memory (SQLite compatible)
            matching_items = []
            for model in item_models:
                item_tags = list(model.tags) if model.tags else []
                if match_all:
                    if all(tag in item_tags for tag in tags):
                        matching_items.append(self._to_domain(model))
                else:
                    if any(tag in item_tags for tag in tags):
                        matching_items.append(self._to_domain(model))

                if len(matching_items) >= limit:
                    break

            return matching_items

        except Exception as e:
            logger.error(
                f"Failed to search by tags for organization {organization_id}: {e}"
            )
            raise KnowledgeItemRepositoryException(
                f"Failed to search by tags for organization {organization_id}: {e}"
            ) from e

    async def get_items_without_embeddings(
        self,
        organization_id: str,
        limit: int = 100,
    ) -> List[KnowledgeItem]:
        """Get items that need embedding generation."""
        try:
            stmt = (
                select(KnowledgeItemModel)
                .where(
                    and_(
                        KnowledgeItemModel.organization_id == organization_id,
                        KnowledgeItemModel.is_published == True,
                        or_(
                            KnowledgeItemModel.embedding_vector == None,
                            KnowledgeItemModel.embedding_vector == "",
                            KnowledgeItemModel.embedding_vector == "null",
                        ),
                    )
                )
                .order_by(KnowledgeItemModel.created_at.asc())
                .limit(limit)
            )

            result = await self.db.execute(stmt)
            item_models = result.scalars().all()

            return [self._to_domain(model) for model in item_models]

        except Exception as e:
            logger.error(
                f"Failed to get items without embeddings for organization {organization_id}: {e}"
            )
            raise KnowledgeItemRepositoryException(
                f"Failed to get items without embeddings: {e}"
            ) from e

    async def count_by_organization_id(
        self,
        organization_id: str,
        item_type: Optional[KnowledgeItemType] = None,
    ) -> int:
        """Count knowledge items for an organization."""
        try:
            conditions = [KnowledgeItemModel.organization_id == organization_id]
            if item_type:
                conditions.append(KnowledgeItemModel.item_type == item_type.value)

            where_clause = and_(*conditions)

            stmt = (
                select(func.count()).select_from(KnowledgeItemModel).where(where_clause)
            )
            result = await self.db.execute(stmt)
            return result.scalar() or 0

        except Exception as e:
            logger.error(
                f"Failed to count items for organization {organization_id}: {e}"
            )
            raise KnowledgeItemRepositoryException(
                f"Failed to count items for organization {organization_id}: {e}"
            ) from e

    async def get_most_helpful(
        self,
        organization_id: str,
        limit: int = 10,
    ) -> List[KnowledgeItem]:
        """Get most helpful items sorted by helpfulness score.

        Filters items with at least MIN_FEEDBACK_THRESHOLD votes.
        """
        try:
            # Fetch all published items with enough feedback
            stmt = select(KnowledgeItemModel).where(
                and_(
                    KnowledgeItemModel.organization_id == organization_id,
                    KnowledgeItemModel.is_published == True,
                    (
                        KnowledgeItemModel.helpful_count
                        + KnowledgeItemModel.not_helpful_count
                    )
                    >= self.MIN_FEEDBACK_THRESHOLD,
                )
            )

            result = await self.db.execute(stmt)
            item_models = result.scalars().all()

            # Convert to domain models and sort by helpfulness score
            items = [self._to_domain(model) for model in item_models]
            items.sort(key=lambda x: x.get_helpfulness_score(), reverse=True)

            return items[:limit]

        except Exception as e:
            logger.error(
                f"Failed to get most helpful items for organization {organization_id}: {e}"
            )
            raise KnowledgeItemRepositoryException(
                f"Failed to get most helpful items: {e}"
            ) from e

    # =========================================================================
    # Model Conversion Helpers
    # =========================================================================

    def _to_domain(self, model: KnowledgeItemModel) -> KnowledgeItem:
        """Convert SQLAlchemy model to domain model."""
        # tags is handled by the TagsArray TypeDecorator — already a list
        # (or None) on read. embedding_vector/metadata are JsonBlob (Text on
        # SQLite, JSONB on PostgreSQL); the parse helpers handle BOTH a
        # JSON-string and an already-decoded value, returning a fresh copy.
        tags = list(model.tags) if model.tags else []
        embedding_vector = self._parse_json_list(model.embedding_vector)
        metadata = self._parse_json_dict(model.knowledge_metadata)

        return KnowledgeItem(
            item_id=model.item_id,
            organization_id=model.organization_id,
            # Coerce the DB-side string into the typed enum at the boundary.
            # KnowledgeItem.scope is now strongly typed; raw strings would
            # raise from __post_init__.
            scope=KnowledgeScope(model.scope),
            owner_id=model.owner_id,
            title=model.title,
            content=model.content,
            item_type=KnowledgeItemType(model.item_type),
            category=model.category,
            tags=tags,
            embedding_model=model.embedding_model,
            embedding_vector=embedding_vector if embedding_vector else None,
            embedding_version=model.embedding_version,
            source_url=model.source_url,
            author=model.author,
            language=model.language,
            view_count=model.view_count,
            helpful_count=model.helpful_count,
            not_helpful_count=model.not_helpful_count,
            last_retrieved_at=self._ensure_tz_aware(model.last_retrieved_at),
            is_published=model.is_published,
            verification_level=model.verification_level,
            verification_reason=model.verification_reason,
            verified_by=model.verified_by,
            verified_at=self._ensure_tz_aware(model.verified_at),
            source_suggestion_id=model.source_suggestion_id,
            created_at=self._ensure_tz_aware(model.created_at),
            updated_at=self._ensure_tz_aware(model.updated_at),
            metadata=metadata,
        )

    def _parse_json_list(self, value: Optional[str]) -> List:
        """Parse JSON string to list."""
        if not value:
            return []
        try:
            result = json.loads(value)
            return result if isinstance(result, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def _parse_json_dict(self, value: Any) -> Optional[Dict]:
        """Parse a stored JSON value to a dict.

        Robust to both representations of the ``JsonBlob`` column: a JSON
        *string* (SQLite TEXT) and an already-decoded ``dict`` (PostgreSQL JSONB
        returns the deserialized object — without a dict branch, ``json.loads``
        on it raises ``TypeError`` and the value is silently lost).

        Returns a FRESH copy in both branches: ``json.loads`` is naturally fresh,
        and the dict branch ``deepcopy``s so a caller mutating the result cannot
        alias/dirty the session-bound ORM attribute (PG-only aliasing bug). The
        dict check precedes the falsy check so an empty ``{}`` round-trips to
        ``{}`` (matching the string path), not ``None``.
        """
        if isinstance(value, dict):
            return deepcopy(value)
        if not value:
            return None
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None

    def _ensure_tz_aware(self, dt: Optional[datetime]) -> Optional[datetime]:
        """Ensure datetime is timezone-aware (UTC if naive)."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt


class InMemoryKnowledgeItemRepository(KnowledgeItemRepository):
    """In-memory knowledge item repository for testing."""

    # Minimum feedback count to be included in helpfulness ranking
    MIN_FEEDBACK_THRESHOLD = 3

    def __init__(self):
        """Initialize empty storage."""
        self._items: Dict[str, KnowledgeItem] = {}

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    async def create(self, item: KnowledgeItem) -> KnowledgeItem:
        """Create new knowledge item record in memory."""
        if item.item_id in self._items:
            raise ValueError(f"Knowledge item {item.item_id} already exists")

        # Store a deep copy to prevent mutation
        self._items[item.item_id] = deepcopy(item)
        return deepcopy(item)

    async def get_by_id(self, item_id: str) -> Optional[KnowledgeItem]:
        """Get knowledge item by ID."""
        item = self._items.get(item_id)
        if item is None:
            return None
        return deepcopy(item)

    async def update(self, item: KnowledgeItem) -> KnowledgeItem:
        """Update knowledge item fields."""
        if item.item_id not in self._items:
            raise ValueError(f"Knowledge item {item.item_id} not found")

        item.updated_at = datetime.now(timezone.utc)
        self._items[item.item_id] = deepcopy(item)
        return deepcopy(item)

    async def delete(self, item_id: str) -> bool:
        """Delete knowledge item by ID."""
        if item_id not in self._items:
            return False
        del self._items[item_id]
        return True

    # =========================================================================
    # Query Operations
    # =========================================================================

    async def list_by_organization_id(
        self,
        organization_id: str,
        item_type: Optional[KnowledgeItemType] = None,
        category: Optional[str] = None,
        is_published: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> List[KnowledgeItem]:
        """List knowledge items with filtering and pagination."""
        items = [
            i
            for i in self._items.values()
            if i.organization_id == organization_id and i.is_published == is_published
        ]

        if item_type:
            items = [i for i in items if i.item_type == item_type]

        if category:
            items = [i for i in items if i.category == category]

        # Sort by created_at descending
        items.sort(key=lambda x: x.created_at, reverse=True)

        # Apply pagination
        paginated = items[offset : offset + limit]

        return [deepcopy(i) for i in paginated]

    @staticmethod
    def _inventory_visible(item, organization_id, user_id) -> bool:
        scope = item.scope.value if hasattr(item.scope, "value") else str(item.scope)
        if scope == "global":
            # Org-free platform tier (#770) — visible regardless of caller org.
            return True
        # Owner sees their own org's items (any scope). Team visibility lives in
        # the share table (resource_shares), which this in-memory fallback does
        # not model — team-shared items authored by others are not surfaced here.
        return (
            item.organization_id == organization_id
            and bool(user_id)
            and item.owner_id == user_id
        )

    async def list_for_inventory(
        self,
        organization_id: str,
        user_id: Optional[str] = None,
        team_ids: Optional[List[str]] = None,
        item_type: Optional[KnowledgeItemType] = None,
    ) -> List[KnowledgeItem]:
        """List published items visible to a requester, RBAC enforced.

        ``team_ids`` is accepted for interface parity with the DB repository but
        unused: the in-memory fallback does not model the share table, so it
        surfaces only global + owner-authored items.
        """
        items = [
            i
            for i in self._items.values()
            if i.is_published
            and (item_type is None or i.item_type == item_type)
            and self._inventory_visible(i, organization_id, user_id)
        ]
        items.sort(key=lambda x: x.created_at, reverse=True)
        return [deepcopy(i) for i in items]

    async def get_visible_by_id(
        self,
        item_id: str,
        organization_id: str,
        user_id: Optional[str] = None,
        team_ids: Optional[List[str]] = None,
    ) -> Optional[KnowledgeItem]:
        """Get an item by ID if the requester may see it (#867).

        ``team_ids`` is accepted for interface parity with the DB repository
        but unused, for the same reason as ``list_for_inventory``: this
        fallback does not model the share table, so it resolves global +
        owner-authored items only.

        Mirrors the SQL rule: the row must be published or the requester's
        own — unpublish is the delete semantics for built-in global runbooks.
        """
        item = self._items.get(item_id)
        if item is None or not self._inventory_visible(item, organization_id, user_id):
            return None
        if not item.is_published and not (user_id and item.owner_id == user_id):
            return None
        return deepcopy(item)

    async def search_by_text(
        self,
        organization_id: str,
        query: str,
        item_type: Optional[KnowledgeItemType] = None,
        limit: int = 10,
    ) -> List[KnowledgeItem]:
        """Full-text search for knowledge items."""
        query_lower = query.lower()
        items = [
            i
            for i in self._items.values()
            if (
                i.organization_id == organization_id
                and i.is_published
                and (query_lower in i.title.lower() or query_lower in i.content.lower())
            )
        ]

        if item_type:
            items = [i for i in items if i.item_type == item_type]

        # Sort by created_at descending
        items.sort(key=lambda x: x.created_at, reverse=True)

        return [deepcopy(i) for i in items[:limit]]

    async def search_by_tags(
        self,
        organization_id: str,
        tags: List[str],
        match_all: bool = False,
        limit: int = 50,
    ) -> List[KnowledgeItem]:
        """Search knowledge items by tags."""
        if not tags:
            return []

        matching_items = []
        for item in self._items.values():
            if item.organization_id != organization_id or not item.is_published:
                continue

            if match_all:
                if item.has_all_tags(tags):
                    matching_items.append(item)
            else:
                if item.has_any_tags(tags):
                    matching_items.append(item)

        # Sort by created_at descending
        matching_items.sort(key=lambda x: x.created_at, reverse=True)

        return [deepcopy(i) for i in matching_items[:limit]]

    async def get_items_without_embeddings(
        self,
        organization_id: str,
        limit: int = 100,
    ) -> List[KnowledgeItem]:
        """Get items that need embedding generation."""
        items = [
            i
            for i in self._items.values()
            if (
                i.organization_id == organization_id
                and i.is_published
                and not i.has_embedding()
            )
        ]

        # Sort by created_at ascending (oldest first)
        items.sort(key=lambda x: x.created_at)

        return [deepcopy(i) for i in items[:limit]]

    async def count_by_organization_id(
        self,
        organization_id: str,
        item_type: Optional[KnowledgeItemType] = None,
    ) -> int:
        """Count knowledge items for an organization."""
        items = [
            i for i in self._items.values() if i.organization_id == organization_id
        ]

        if item_type:
            items = [i for i in items if i.item_type == item_type]

        return len(items)

    async def get_most_helpful(
        self,
        organization_id: str,
        limit: int = 10,
    ) -> List[KnowledgeItem]:
        """Get most helpful items sorted by helpfulness score."""
        items = [
            i
            for i in self._items.values()
            if (
                i.organization_id == organization_id
                and i.is_published
                and i.get_total_feedback() >= self.MIN_FEEDBACK_THRESHOLD
            )
        ]

        # Sort by helpfulness score descending
        items.sort(key=lambda x: x.get_helpfulness_score(), reverse=True)

        return [deepcopy(i) for i in items[:limit]]

    # =========================================================================
    # Testing Helpers
    # =========================================================================

    def clear(self) -> None:
        """Clear all data (for testing)."""
        self._items.clear()

    def delete_items_for_organization(self, organization_id: str) -> int:
        """Delete all items for an organization.

        Returns:
            Number of items deleted
        """
        to_delete = [
            item_id
            for item_id, item in self._items.items()
            if item.organization_id == organization_id
        ]

        for item_id in to_delete:
            del self._items[item_id]

        return len(to_delete)
