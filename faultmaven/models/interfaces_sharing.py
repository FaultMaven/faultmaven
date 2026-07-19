"""Resource-sharing interfaces (polymorphic share association).

ADR-013 §D4/D4a + ADR-011 D2/D3. The share table is the single source of truth
for "who can see this resource" beyond its owner: a resource
(``case`` / ``knowledge_item`` / ``conversion_job``) is shared to a scope — v1
only a ``team``; ``organization`` is reserved for the D4a org-level extension.
Retrieval resolves the visible-id allowlist from these rows in SQL
(``build_kb_scope_filter``), never from ChromaDB metadata.

Implemented by ``PostgreSQLShareRepository`` (+ ``SessionlessShareRepository``).
Consumed by the knowledge and case modules through this core contract so neither
reaches into persistence infrastructure directly (import-linter).
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

# Allowed vocabularies — mirror the DB CHECK sets on ``resource_shares``. The
# full sets are admitted from day one so the D4a org-share extension is a new
# row, not a schema migration; v1 application code only writes ``scope_type``
# ``team``.
RESOURCE_TYPES = ("case", "knowledge_item", "conversion_job")
SCOPE_TYPES = ("team", "organization")


class ResourceShare(BaseModel):
    """One share-association row (a resource made visible to a scope)."""

    share_id: str
    resource_type: str
    resource_id: str
    scope_type: str
    scope_id: str
    organization_id: str
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None


class IShareRepository(ABC):
    """Persistence for polymorphic resource shares (ADR-013 §D4)."""

    @abstractmethod
    async def share(
        self,
        *,
        resource_type: str,
        resource_id: str,
        scope_type: str,
        scope_id: str,
        organization_id: str,
        created_by: Optional[str] = None,
    ) -> ResourceShare:
        """Share a resource to a scope (idempotent).

        Re-sharing the same ``(resource, scope)`` is a no-op that returns the
        existing row (upsert on the ``uq_resource_shares_target`` unique key).
        """

    @abstractmethod
    async def unshare(
        self,
        *,
        resource_type: str,
        resource_id: str,
        scope_type: str,
        scope_id: str,
    ) -> bool:
        """Remove one share association. Returns True if a row was deleted."""

    @abstractmethod
    async def list_scopes_for_resource(
        self, resource_type: str, resource_id: str
    ) -> List[ResourceShare]:
        """All scopes a single resource is shared to."""

    @abstractmethod
    async def list_resource_ids(
        self,
        *,
        resource_type: str,
        scope_type: str,
        scope_ids: List[str],
    ) -> List[str]:
        """Resource ids of ``resource_type`` shared to ANY of ``scope_ids``.

        The allowlist direction (scope(s) -> resources) used to build the
        "shared-to-my-teams" arm of a principal's visible-id set. Deduplicated;
        empty ``scope_ids`` returns ``[]`` without a query.
        """

    @abstractmethod
    async def delete_for_resource(self, resource_type: str, resource_id: str) -> int:
        """Delete all shares of a resource (resource-delete cascade). Row count."""

    @abstractmethod
    async def delete_for_scope(self, scope_type: str, scope_id: str) -> int:
        """Delete all shares to a scope (team/org-delete cascade). Row count."""

    @abstractmethod
    async def count_shares_for_resource(
        self, resource_type: str, resource_id: str
    ) -> int:
        """Number of scopes a resource is shared to.

        Feeds the derived ``scope`` enum invariant (0 shares => ``personal``,
        >=1 => ``team``) maintained by the single share writer.
        """
