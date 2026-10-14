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

Tenancy posture: unlike the case repositories (whose reads ignore the tenant
param and rely on PostgreSQL RLS alone, ADR-010), knowledge queries
deliberately keep their per-query ``enterprise_id`` predicates on the
tenant-owned tiers (personal | team, ADR-011/ADR-013) — defense-in-depth on top
of RLS. Do NOT remove them by analogy with the case module. Global-tier rows
are the platform corpus (#770). They carry an ``enterprise_id`` like every other
row — the column is NOT NULL and the platform tier's is the STANDALONE
enterprise, which is exactly what the RLS global-write arm compares against;
what they carry no ORGANIZATION for is billing
(``knowledge_items_global_org_check``). They are readable by every tenant on
both paths — SQL via the tenant-free ``scope='global'`` visibility arm here plus
the RLS read exemption (``knowledge_items_tenant_read``, migration 033), and
vector via ``build_kb_scope_filter`` (``global ∪ owner ∪ team-share
allowlist``, owner ids globally unique, share ids resolved from RLS-scoped
SQL). Writes to the global tier are platform operations: RLS write policies
confine them to single-tenant sentinel sessions or the audited BYPASSRLS
maintenance path (``jobs/run.py kb_seed``), never tenant sessions. Methods
below that take only an ``enterprise_id`` (counts / org listings /
text+tag search) are org-scoped queries that exclude the org-free platform
tier; note they currently have NO production callers (the service uses only
get/create/update_fields/delete/list_for_inventory) — wire the global arm in
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
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.models import (
    KnowledgeItemModel,
    ResourceShareModel,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
)
from faultmaven.modules.knowledge.taxonomy import KnowledgeScope
from faultmaven.utils.serialization import decode_json_blob

logger = logging.getLogger(__name__)


class KnowledgeItemRepositoryException(Exception):
    """Exception raised when knowledge item repository operations fail."""

    pass


#: Domain field → column value, serialised exactly as ``create`` writes it, for
#: every field a write after creation may set. Identity and tenancy
#: (``item_id``, ``enterprise_id``, ``organization_id``, ``scope``,
#: ``owner_id``, ``created_at``) are not here: no write changes them.
#: ``is_published`` is here so a retirement can be written on its own; a
#: content edit simply does not name it.
_TARGETED_FIELDS = {
    "title": lambda v: ("title", v),
    "content": lambda v: ("content", v),
    "category": lambda v: ("category", v),
    "tags": lambda v: ("tags", list(v) if v else None),
    "item_type": lambda v: ("item_type", KnowledgeItemType(v).value),
    "is_published": lambda v: ("is_published", bool(v)),
    "metadata": lambda v: ("knowledge_metadata", json.dumps(v) if v else "{}"),
    "embedding_vector": lambda v: ("embedding_vector", json.dumps(v) if v else None),
    "embedding_model": lambda v: ("embedding_model", v),
    "embedding_version": lambda v: ("embedding_version", v),
    "view_count": lambda v: ("view_count", int(v)),
    "helpful_count": lambda v: ("helpful_count", int(v)),
    "not_helpful_count": lambda v: ("not_helpful_count", int(v)),
    "last_retrieved_at": lambda v: ("last_retrieved_at", v),
}


def _column_values(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Column values for a targeted write; rejects what it does not know."""
    if not fields:
        raise ValueError("A targeted update needs at least one field")
    unknown = sorted(set(fields) - set(_TARGETED_FIELDS))
    if unknown:
        raise ValueError(f"Fields not writable by a targeted update: {unknown}")
    return dict(_TARGETED_FIELDS[name](value) for name, value in fields.items())


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
    async def update_fields(
        self,
        item_id: str,
        fields: Dict[str, Any],
        *,
        commit: bool = True,
        touch: bool = True,
    ) -> bool:
        """Write ONLY ``fields`` on one row; every other column is left as the
        database holds it (#1966). The only write after creation.

        There is deliberately no full-row write: one would put back whatever
        it read for every column it did not mean to change, reverting a
        concurrent writer's change — ``is_published`` above all, which is how
        a built-in runbook is retired. The statement is a single targeted
        ``UPDATE``, so on PostgreSQL it waits for a concurrent writer's row
        lock and applies to the row as that writer left it, and on SQLite it
        waits for the database's writer lock.

        Args:
            item_id: Item identifier
            fields: Domain field name → new value: the content and
                classification fields, ``is_published``, ``metadata``, the
                embedding fields and the usage counters (``_TARGETED_FIELDS``),
                serialised exactly as :meth:`create` writes them.
            commit: ``False`` leaves the write uncommitted in the open
                transaction, for :meth:`commit` or :meth:`rollback` to settle.
            touch: Stamp ``updated_at`` with the current time. ``False`` keeps
                the stored value (bookkeeping writes the document's readers
                should not see as an edit).

        Returns:
            Whether a row matched. Zero rows — absent, deleted concurrently, or
            refused by an RLS write policy (which a PostgreSQL ``UPDATE``
            reports as zero rows) — is reported, never raised.

        Raises:
            ValueError: an unknown field name, or no fields
            KnowledgeItemRepositoryException: If the write fails
        """
        pass

    @abstractmethod
    async def delete(self, item_id: str, *, commit: bool = True) -> bool:
        """Delete knowledge item by ID.

        Args:
            item_id: Item identifier
            commit: ``False`` leaves the delete uncommitted in the open
                transaction, for :meth:`commit` or :meth:`rollback` to settle.

        Returns:
            True if a row matched (deleted, or deleted pending the commit),
            False if none did: absent, or refused by an RLS write policy.
        """
        pass

    @abstractmethod
    async def commit(self) -> None:
        """Make the writes issued with ``commit=False`` durable.

        Raises:
            KnowledgeItemRepositoryException: If the commit fails. The writes
                did not land unless the failure came after the database
                committed (a connection dropped mid-acknowledgement), which a
                caller cannot tell from here.
        """
        pass

    @abstractmethod
    async def rollback(self) -> None:
        """Discard the writes issued with ``commit=False``."""
        pass

    @abstractmethod
    async def may_update(self) -> bool:
        """Whether the session's role may UPDATE ``knowledge_items``.

        Not every role that runs the KB bootstrap can: under
        ``TENANT_PROVIDER=multi`` the ``kb_seed`` job connects as the
        maintenance role, which holds INSERT and DELETE on this table and
        deliberately no UPDATE. A pass asks ONCE and shapes what it issues by
        the answer, instead of issuing statements the role is refused (#1966).
        """
        pass

    @abstractmethod
    async def read_is_published(self, item_id: str, *, lock: bool) -> Optional[bool]:
        """Whether ``item_id`` is published (``None``: no such row) (#1966).

        For a caller that has just written vectors for a row read earlier and
        must know whether a delete or unpublish landed meanwhile. ``lock=True``
        reads AFTER any writer still holding the row has finished — including
        one whose row change is issued but not yet committed — and needs a
        role that :meth:`may_update`; the caller rolls its session back
        afterwards, which releases the lock. ``lock=False`` is a plain read of
        the last committed state.
        """
        pass

    # =========================================================================
    # Query Operations
    # =========================================================================

    @abstractmethod
    async def list_for_inventory(
        self,
        enterprise_id: str,
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
        enterprise_id: str,
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
    async def visible_item_ids(
        self,
        item_ids: List[str],
        enterprise_id: str,
        user_id: Optional[str] = None,
        team_ids: Optional[List[str]] = None,
    ) -> Set[str]:
        """The subset of ``item_ids`` the requester may read, in ONE query (#1919).

        The batched form of :meth:`get_visible_by_id`, with exactly its rule
        (global ∪ own-org owned ∪ own-org shared-to-my-teams, published or
        mine): an id is in the result iff ``get_visible_by_id`` would return
        its row. Absent ids and invisible ids are both left out. Backs the
        per-viewer gate on stored runbook excerpts (``gate_kb_sources``), which
        checks a whole response's ids at once.
        """
        pass

    # ``search_by_text`` lived here until #1288. It was an org-scoped LIKE over
    # title+content, and it had no caller: the two that look like callers pass
    # ``query_text``/``scope_filter``/``top_k``/``min_similarity`` to
    # ``RunbookKnowledgeBase.search_by_text``, a signature this one cannot
    # accept. It was also stale against the schema — the ``enterprise_id``
    # predicate carried no global arm, and global rows hold no org (#770), so
    # adopting it would have made every platform-tier runbook invisible, and it
    # saw none of the ``resource_shares`` team visibility ``list_for_inventory``
    # enforces in-query. ``POST /documents/search`` now scores title+content
    # over ``list_for_inventory``'s rows instead.

    @abstractmethod
    async def search_by_tags(
        self,
        enterprise_id: str,
        tags: List[str],
        match_all: bool = False,
        limit: int = 50,
    ) -> List[KnowledgeItem]:
        """Search knowledge items by tags.

        Args:
            enterprise_id: Enterprise identifier
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
        enterprise_id: str,
        limit: int = 100,
    ) -> List[KnowledgeItem]:
        """Get items that need embedding generation.

        Args:
            enterprise_id: Enterprise identifier
            limit: Maximum results to return

        Returns:
            List of items without embedding vectors
        """
        pass

    @abstractmethod
    async def get_most_helpful(
        self,
        enterprise_id: str,
        limit: int = 10,
    ) -> List[KnowledgeItem]:
        """Get most helpful items sorted by helpfulness score.

        Only includes items with at least some feedback (minimum threshold).

        Args:
            enterprise_id: Enterprise identifier
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
                enterprise_id=item.enterprise_id,
                # Billing attribution (ADR-017 D2), stamped alongside the
                # isolation key and never read for visibility. ``None`` is an
                # ordinary answer — an account may be in no organization — and
                # is the only permitted one for a global row, which the domain
                # model's own validator enforces before this write.
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

    async def update_fields(
        self,
        item_id: str,
        fields: Dict[str, Any],
        *,
        commit: bool = True,
        touch: bool = True,
    ) -> bool:
        """Targeted update (see the base class)."""
        values = _column_values(fields)
        values["updated_at"] = (
            datetime.now(timezone.utc) if touch else KnowledgeItemModel.updated_at
        )
        try:
            stmt = (
                update(KnowledgeItemModel)
                .where(KnowledgeItemModel.item_id == item_id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            result = await self.db.execute(stmt)
            if commit:
                await self.db.commit()
            return result.rowcount > 0

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to update knowledge item {item_id}: {e}")
            raise KnowledgeItemRepositoryException(
                f"Failed to update knowledge item {item_id}: {e}"
            ) from e

    async def delete(self, item_id: str, *, commit: bool = True) -> bool:
        """Delete knowledge item by ID (see the base class for ``commit``)."""
        try:
            stmt = delete(KnowledgeItemModel).where(
                KnowledgeItemModel.item_id == item_id
            )
            result = await self.db.execute(stmt)
            if commit:
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

    async def commit(self) -> None:
        """Commit the session's open transaction."""
        try:
            await self.db.commit()
        except Exception as e:
            logger.error(f"Failed to commit knowledge item changes: {e}")
            raise KnowledgeItemRepositoryException(
                f"Failed to commit knowledge item changes: {e}"
            ) from e

    async def rollback(self) -> None:
        """Roll back the session's open transaction."""
        await self.db.rollback()

    async def may_update(self) -> bool:
        """``has_table_privilege`` on PostgreSQL; SQLite has no roles."""
        if self.db.get_bind().dialect.name == "sqlite":
            return True
        return bool(
            (
                await self.db.execute(
                    text("SELECT has_table_privilege(current_user, :t, 'UPDATE')"),
                    {"t": KnowledgeItemModel.__table__.fullname},
                )
            ).scalar()
        )

    async def read_is_published(self, item_id: str, *, lock: bool) -> Optional[bool]:
        """Read of ``is_published`` (see the base class).

        With ``lock``: PostgreSQL ``SELECT ... FOR SHARE`` waits for an
        uncommitted ``UPDATE``/``DELETE`` of the row, then reads its outcome
        (it needs UPDATE privilege). SQLite ignores that clause, and under WAL
        a reader sees the last committed state while a writer still holds its
        lock, so the read is preceded by a no-op write of the row: it must take
        the database's writer lock first, which waits (``busy_timeout``) for
        the held change to commit. The no-op write changes nothing
        (``updated_at`` included) and is discarded by the caller's rollback.
        """
        where = KnowledgeItemModel.item_id == item_id
        query = select(KnowledgeItemModel.is_published).where(where)
        if lock:
            if self.db.get_bind().dialect.name == "sqlite":
                await self.db.execute(
                    update(KnowledgeItemModel)
                    .where(where)
                    .values(
                        is_published=KnowledgeItemModel.is_published,
                        updated_at=KnowledgeItemModel.updated_at,
                    )
                    .execution_options(synchronize_session=False)
                )
            query = query.with_for_update(read=True)
        found = (await self.db.execute(query)).first()
        return None if found is None else bool(found[0])

    # =========================================================================
    # Query Operations
    # =========================================================================

    @staticmethod
    def _inventory_visibility_clause(
        enterprise_id: str,
        user_id: Optional[str],
        team_ids: Optional[List[str]],
    ):
        """RBAC scope-visibility predicate for the inventory surface.

        Mirrors the vector-retrieval allowlist (``build_kb_scope_filter``), and
        the mirror is exact on the arm that matters: the share sub-select below
        and the vector path's id resolver
        (``IShareRepository.list_resource_ids``, reached via
        ``resolve_shared_kb_ids``) apply the same
        ``resource_shares.enterprise_id == enterprise_id`` predicate, so a
        share row stamped with a foreign org grants visibility on neither
        surface. The visibility rule is
        ``global`` (the org-free platform tier, visible to every tenant — #770)
        ∪ own-org items the requester ``owns`` (an author always sees their own)
        ∪ own-org items ``shared to any of the requester's teams`` via
        ``resource_shares``. The ``enterprise_id`` predicate guards only the
        org-owned arms (defense-in-depth on top of RLS for personal/team); the
        global arm is deliberately org-free — global rows carry no org
        (``knowledge_items_global_org_check``). The owner/team branches are
        added only when the requester has a user_id / team memberships, so an
        anonymous caller sees global content only. The share table is the single
        source of truth for team visibility (ADR-013 §D4 / ADR-011 D3).
        """
        tenant_owned = []
        if user_id:
            tenant_owned.append(KnowledgeItemModel.owner_id == user_id)
        if team_ids:
            shared_ids = select(ResourceShareModel.resource_id).where(
                ResourceShareModel.resource_type == "knowledge_item",
                ResourceShareModel.scope_type == "team",
                ResourceShareModel.scope_id.in_(list(team_ids)),
                # The share row must belong to the same ENTERPRISE as the item
                # it grants. Without it a row stamped with a foreign enterprise
                # would grant visibility — the one arm of this clause that could
                # reach across tenants on its own. Deliberately NOT matched on
                # the organization: a team may span cost centres (ADR-017 D4).
                ResourceShareModel.enterprise_id == enterprise_id,
            )
            tenant_owned.append(KnowledgeItemModel.item_id.in_(shared_ids))

        visibility = [KnowledgeItemModel.scope == "global"]
        if tenant_owned:
            visibility.append(
                and_(
                    KnowledgeItemModel.enterprise_id == enterprise_id,
                    or_(*tenant_owned),
                )
            )
        return or_(*visibility)

    async def list_for_inventory(
        self,
        enterprise_id: str,
        user_id: Optional[str] = None,
        team_ids: Optional[List[str]] = None,
        item_type: Optional[KnowledgeItemType] = None,
    ) -> List[KnowledgeItem]:
        """List published items visible to a requester, RBAC enforced in-query."""
        try:
            conditions = [
                KnowledgeItemModel.is_published == True,  # noqa: E712
                self._inventory_visibility_clause(enterprise_id, user_id, team_ids),
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
                f"Failed to list inventory for enterprise {enterprise_id}: {e}"
            )
            raise KnowledgeItemRepositoryException(
                f"Failed to list inventory for enterprise {enterprise_id}: {e}"
            ) from e

    async def get_visible_by_id(
        self,
        item_id: str,
        enterprise_id: str,
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
                    self._inventory_visibility_clause(enterprise_id, user_id, team_ids),
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

    async def visible_item_ids(
        self,
        item_ids: List[str],
        enterprise_id: str,
        user_id: Optional[str] = None,
        team_ids: Optional[List[str]] = None,
    ) -> Set[str]:
        """The readable subset of ``item_ids``: one SELECT of ids, same rule as
        ``get_visible_by_id`` (scope predicate plus published-or-mine)."""
        ids = sorted({i for i in item_ids if i})
        if not ids:
            return set()
        try:
            published_or_mine = [KnowledgeItemModel.is_published == True]  # noqa: E712
            if user_id:
                published_or_mine.append(KnowledgeItemModel.owner_id == user_id)

            stmt = select(KnowledgeItemModel.item_id).where(
                and_(
                    KnowledgeItemModel.item_id.in_(ids),
                    self._inventory_visibility_clause(enterprise_id, user_id, team_ids),
                    or_(*published_or_mine),
                )
            )
            result = await self.db.execute(stmt)
            return set(result.scalars().all())

        except Exception as e:
            logger.error(f"Failed to resolve visible knowledge item ids: {e}")
            raise KnowledgeItemRepositoryException(
                f"Failed to resolve visible knowledge item ids: {e}"
            ) from e

    async def search_by_tags(
        self,
        enterprise_id: str,
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

            # Fetch published items for the enterprise
            stmt = (
                select(KnowledgeItemModel)
                .where(
                    and_(
                        KnowledgeItemModel.enterprise_id == enterprise_id,
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
                f"Failed to search by tags for enterprise {enterprise_id}: {e}"
            )
            raise KnowledgeItemRepositoryException(
                f"Failed to search by tags for enterprise {enterprise_id}: {e}"
            ) from e

    async def get_items_without_embeddings(
        self,
        enterprise_id: str,
        limit: int = 100,
    ) -> List[KnowledgeItem]:
        """Get items that need embedding generation."""
        try:
            stmt = (
                select(KnowledgeItemModel)
                .where(
                    and_(
                        KnowledgeItemModel.enterprise_id == enterprise_id,
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
                f"Failed to get items without embeddings for enterprise {enterprise_id}: {e}"
            )
            raise KnowledgeItemRepositoryException(
                f"Failed to get items without embeddings: {e}"
            ) from e

    async def get_most_helpful(
        self,
        enterprise_id: str,
        limit: int = 10,
    ) -> List[KnowledgeItem]:
        """Get most helpful items sorted by helpfulness score.

        Filters items with at least MIN_FEEDBACK_THRESHOLD votes.
        """
        try:
            # Fetch all published items with enough feedback
            stmt = select(KnowledgeItemModel).where(
                and_(
                    KnowledgeItemModel.enterprise_id == enterprise_id,
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
                f"Failed to get most helpful items for enterprise {enterprise_id}: {e}"
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
            enterprise_id=model.enterprise_id,
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
        """Parse a stored ``JsonBlob`` value to a dict, as a FRESH object.

        Thin over :func:`decode_json_blob` (fm#1107) — this used to be one of
        three near-copies of that decode. What is repository-specific is the
        ``copy``: ``_to_domain`` hands the result out, so the dict branch must
        not alias the session-bound ORM attribute (a caller mutating it would
        dirty the row — PostgreSQL-only, invisible on SQLite). ``None`` for
        absent/undecodable; ``{}`` survives as ``{}``.
        """
        return decode_json_blob(value, copy=True)

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
        # Writes issued with ``commit=False``, keyed by item id: the item to
        # store, or ``None`` for a delete. Reads see committed state only, as a
        # second database session would.
        self._pending: Dict[str, Optional[KnowledgeItem]] = {}

    def _exists(self, item_id: str) -> bool:
        """Whether the open transaction would see ``item_id``."""
        if item_id in self._pending:
            return self._pending[item_id] is not None
        return item_id in self._items

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

    async def update_fields(
        self,
        item_id: str,
        fields: Dict[str, Any],
        *,
        commit: bool = True,
        touch: bool = True,
    ) -> bool:
        """Targeted update (see the base class)."""
        _column_values(fields)  # the same validation as the database repository
        if not self._exists(item_id):
            return False
        pending = self._pending.get(item_id)
        target = deepcopy(pending if pending is not None else self._items[item_id])
        for name, value in fields.items():
            if name == "item_type":
                value = KnowledgeItemType(value)
            setattr(target, name, deepcopy(value))
        if touch:
            target.updated_at = datetime.now(timezone.utc)
        if commit:
            self._items[item_id] = target
        else:
            self._pending[item_id] = target
        return True

    async def delete(self, item_id: str, *, commit: bool = True) -> bool:
        """Delete knowledge item by ID (see the base class for ``commit``)."""
        if not self._exists(item_id):
            return False
        if commit:
            del self._items[item_id]
        else:
            self._pending[item_id] = None
        return True

    async def commit(self) -> None:
        """Apply the writes issued with ``commit=False``."""
        for item_id, item in self._pending.items():
            if item is None:
                self._items.pop(item_id, None)
            else:
                self._items[item_id] = item
        self._pending.clear()

    async def rollback(self) -> None:
        """Discard the writes issued with ``commit=False``."""
        self._pending.clear()

    async def may_update(self) -> bool:
        """No roles in memory."""
        return True

    async def read_is_published(self, item_id: str, *, lock: bool) -> Optional[bool]:
        """Committed ``is_published`` (no concurrent writers to wait for)."""
        item = self._items.get(item_id)
        return None if item is None else bool(item.is_published)

    # =========================================================================
    # Query Operations
    # =========================================================================

    @staticmethod
    def _inventory_visible(item, enterprise_id, user_id) -> bool:
        scope = item.scope.value if hasattr(item.scope, "value") else str(item.scope)
        if scope == "global":
            # Org-free platform tier (#770) — visible regardless of caller org.
            return True
        # Owner sees their own org's items (any scope). Team visibility lives in
        # the share table (resource_shares), which this in-memory fallback does
        # not model — team-shared items authored by others are not surfaced here.
        return (
            item.enterprise_id == enterprise_id
            and bool(user_id)
            and item.owner_id == user_id
        )

    async def list_for_inventory(
        self,
        enterprise_id: str,
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
            and self._inventory_visible(i, enterprise_id, user_id)
        ]
        items.sort(key=lambda x: x.created_at, reverse=True)
        return [deepcopy(i) for i in items]

    async def get_visible_by_id(
        self,
        item_id: str,
        enterprise_id: str,
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
        if item is None or not self._inventory_visible(item, enterprise_id, user_id):
            return None
        if not item.is_published and not (user_id and item.owner_id == user_id):
            return None
        return deepcopy(item)

    async def visible_item_ids(
        self,
        item_ids: List[str],
        enterprise_id: str,
        user_id: Optional[str] = None,
        team_ids: Optional[List[str]] = None,
    ) -> Set[str]:
        """The readable subset of ``item_ids``, by ``get_visible_by_id``'s rule
        (``team_ids`` unused, as there: this fallback models no share table)."""
        visible = set()
        for item_id in {i for i in item_ids if i}:
            if await self.get_visible_by_id(item_id, enterprise_id, user_id, team_ids):
                visible.add(item_id)
        return visible

    async def search_by_tags(
        self,
        enterprise_id: str,
        tags: List[str],
        match_all: bool = False,
        limit: int = 50,
    ) -> List[KnowledgeItem]:
        """Search knowledge items by tags."""
        if not tags:
            return []

        matching_items = []
        for item in self._items.values():
            if item.enterprise_id != enterprise_id or not item.is_published:
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
        enterprise_id: str,
        limit: int = 100,
    ) -> List[KnowledgeItem]:
        """Get items that need embedding generation."""
        items = [
            i
            for i in self._items.values()
            if (
                i.enterprise_id == enterprise_id
                and i.is_published
                and not i.has_embedding()
            )
        ]

        # Sort by created_at ascending (oldest first)
        items.sort(key=lambda x: x.created_at)

        return [deepcopy(i) for i in items[:limit]]

    async def get_most_helpful(
        self,
        enterprise_id: str,
        limit: int = 10,
    ) -> List[KnowledgeItem]:
        """Get most helpful items sorted by helpfulness score."""
        items = [
            i
            for i in self._items.values()
            if (
                i.enterprise_id == enterprise_id
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
        self._pending.clear()
