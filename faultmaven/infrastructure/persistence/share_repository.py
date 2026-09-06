"""Share Repository — SQLAlchemy ORM implementation.

Implements ``IShareRepository`` over the polymorphic ``resource_shares`` table
(ADR-013 §D4/D4a + ADR-011 D2/D3). This is the single source of truth for
resource→scope visibility beyond ownership; retrieval builds its "shared-to-my-
teams" allowlist arm from ``list_resource_ids``.

Referential integrity for the polymorphic ``resource_id``/``scope_id`` is
application-enforced here (no DB FK is possible against multiple target tables).
Resource-delete cascade is driven by callers via ``delete_for_resource``; shares
to a soft-deleted team are already unreachable (``list_all_user_team_ids`` filters
``teams.deleted_at``), so ``delete_for_scope`` is hygiene for hard deletes.
"""

import logging
import uuid
from typing import Dict, List, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.db_compat import dialect_insert
from faultmaven.infrastructure.persistence.models import ResourceShareModel
from faultmaven.models.interfaces_sharing import IShareRepository, ResourceShare

logger = logging.getLogger(__name__)


def _model_to_domain(m: ResourceShareModel) -> ResourceShare:
    """Convert ORM model to domain object."""
    return ResourceShare(
        share_id=m.share_id,
        resource_type=m.resource_type,
        resource_id=m.resource_id,
        scope_type=m.scope_type,
        scope_id=m.scope_id,
        enterprise_id=m.enterprise_id,
        organization_id=m.organization_id,
        created_by=m.created_by,
        created_at=m.created_at,
    )


class PostgreSQLShareRepository(IShareRepository):
    """SQLAlchemy ORM implementation of the resource-share repository."""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def _get(
        self, resource_type: str, resource_id: str, scope_type: str, scope_id: str
    ) -> Optional[ResourceShare]:
        stmt = select(ResourceShareModel).where(
            ResourceShareModel.resource_type == resource_type,
            ResourceShareModel.resource_id == resource_id,
            ResourceShareModel.scope_type == scope_type,
            ResourceShareModel.scope_id == scope_id,
        )
        row = (await self.db.execute(stmt)).scalar_one_or_none()
        return _model_to_domain(row) if row else None

    async def share(
        self,
        *,
        resource_type: str,
        resource_id: str,
        scope_type: str,
        scope_id: str,
        enterprise_id: str,
        organization_id: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> ResourceShare:
        """Share a resource to a scope (idempotent upsert).

        ``enterprise_id`` is the isolation key the read allowlist matches on;
        ``organization_id`` is billing attribution and matches nothing.
        """
        stmt = dialect_insert(self.db, ResourceShareModel).values(
            share_id=str(uuid.uuid4()),
            resource_type=resource_type,
            resource_id=resource_id,
            scope_type=scope_type,
            scope_id=scope_id,
            enterprise_id=enterprise_id,
            organization_id=organization_id,
            created_by=created_by,
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["resource_type", "resource_id", "scope_type", "scope_id"],
        )
        await self.db.execute(stmt)
        await self.db.commit()

        # Return the row (the one just created, or the pre-existing one on
        # conflict). The unique key guarantees exactly one.
        existing = await self._get(resource_type, resource_id, scope_type, scope_id)
        assert existing is not None  # just inserted or already present
        logger.info(
            "Shared %s:%s to %s:%s",
            resource_type,
            resource_id,
            scope_type,
            scope_id,
        )
        return existing

    async def unshare(
        self,
        *,
        resource_type: str,
        resource_id: str,
        scope_type: str,
        scope_id: str,
    ) -> bool:
        """Remove one share association."""
        stmt = delete(ResourceShareModel).where(
            ResourceShareModel.resource_type == resource_type,
            ResourceShareModel.resource_id == resource_id,
            ResourceShareModel.scope_type == scope_type,
            ResourceShareModel.scope_id == scope_id,
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return (result.rowcount or 0) > 0

    async def list_scopes_for_resource(
        self, resource_type: str, resource_id: str
    ) -> List[ResourceShare]:
        """All scopes a single resource is shared to."""
        stmt = select(ResourceShareModel).where(
            ResourceShareModel.resource_type == resource_type,
            ResourceShareModel.resource_id == resource_id,
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return [_model_to_domain(r) for r in rows]

    async def list_scopes_for_resources(
        self, resource_type: str, resource_ids: List[str]
    ) -> Dict[str, List[ResourceShare]]:
        """Scopes for MANY resources in one query (batch DTO enrichment)."""
        if not resource_ids:
            return {}
        stmt = select(ResourceShareModel).where(
            ResourceShareModel.resource_type == resource_type,
            ResourceShareModel.resource_id.in_(resource_ids),
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        result: Dict[str, List[ResourceShare]] = {}
        for r in rows:
            result.setdefault(r.resource_id, []).append(_model_to_domain(r))
        return result

    async def list_resource_ids(
        self,
        *,
        resource_type: str,
        scope_type: str,
        scope_ids: List[str],
        enterprise_id: str,
    ) -> List[str]:
        """Resource ids of ``resource_type`` shared to ANY of ``scope_ids``.

        Tenant-scoped: only shares stamped with ``enterprise_id`` are allowlist
        entries. Fail-closed on a falsy tenant — no query, no ids.

        The match is the ENTERPRISE, not the organization (ADR-017 D4): that is
        what lets one team span two cost centres while still refusing anything
        from outside the wall.
        """
        if not scope_ids or not enterprise_id:
            return []
        stmt = (
            select(ResourceShareModel.resource_id)
            .where(
                ResourceShareModel.resource_type == resource_type,
                ResourceShareModel.scope_type == scope_type,
                ResourceShareModel.scope_id.in_(scope_ids),
                # The share row must belong to the requesting tenant. Without
                # it, a row stamped with a foreign enterprise would grant
                # vector- and list-allowlist visibility across tenants — the
                # same predicate #871 added to the inventory clause's share
                # sub-select.
                ResourceShareModel.enterprise_id == enterprise_id,
            )
            .distinct()
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return list(rows)

    async def delete_for_resource(self, resource_type: str, resource_id: str) -> int:
        """Delete all shares of a resource (resource-delete cascade)."""
        stmt = delete(ResourceShareModel).where(
            ResourceShareModel.resource_type == resource_type,
            ResourceShareModel.resource_id == resource_id,
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount or 0

    async def delete_for_scope(self, scope_type: str, scope_id: str) -> int:
        """Delete all shares to a scope (team/org-delete cascade)."""
        stmt = delete(ResourceShareModel).where(
            ResourceShareModel.scope_type == scope_type,
            ResourceShareModel.scope_id == scope_id,
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount or 0

    async def count_shares_for_resource(
        self, resource_type: str, resource_id: str
    ) -> int:
        """Number of scopes a resource is shared to (feeds the scope enum)."""
        stmt = (
            select(func.count())
            .select_from(ResourceShareModel)
            .where(
                ResourceShareModel.resource_type == resource_type,
                ResourceShareModel.resource_id == resource_id,
            )
        )
        return (await self.db.execute(stmt)).scalar() or 0
