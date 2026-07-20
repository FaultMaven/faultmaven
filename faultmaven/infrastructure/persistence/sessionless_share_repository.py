"""Sessionless Share Repository.

Wrapper around ``PostgreSQLShareRepository`` that creates a session per operation
via ``get_db_session()``, following the same pattern as
``SessionlessTeamRepository``. Removes the need for a long-lived db_session in the
DI container.
"""

from typing import Dict, List, Optional

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.share_repository import (
    PostgreSQLShareRepository,
)
from faultmaven.models.interfaces_sharing import IShareRepository, ResourceShare


class SessionlessShareRepository(IShareRepository):
    """Sessionless wrapper for the share repository."""

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
        async with get_db_session() as session:
            repo = PostgreSQLShareRepository(session)
            return await repo.share(
                resource_type=resource_type,
                resource_id=resource_id,
                scope_type=scope_type,
                scope_id=scope_id,
                organization_id=organization_id,
                created_by=created_by,
            )

    async def unshare(
        self,
        *,
        resource_type: str,
        resource_id: str,
        scope_type: str,
        scope_id: str,
    ) -> bool:
        async with get_db_session() as session:
            repo = PostgreSQLShareRepository(session)
            return await repo.unshare(
                resource_type=resource_type,
                resource_id=resource_id,
                scope_type=scope_type,
                scope_id=scope_id,
            )

    async def list_scopes_for_resource(
        self, resource_type: str, resource_id: str
    ) -> List[ResourceShare]:
        async with get_db_session() as session:
            repo = PostgreSQLShareRepository(session)
            return await repo.list_scopes_for_resource(resource_type, resource_id)

    async def list_scopes_for_resources(
        self, resource_type: str, resource_ids: List[str]
    ) -> Dict[str, List[ResourceShare]]:
        async with get_db_session() as session:
            repo = PostgreSQLShareRepository(session)
            return await repo.list_scopes_for_resources(resource_type, resource_ids)

    async def list_resource_ids(
        self,
        *,
        resource_type: str,
        scope_type: str,
        scope_ids: List[str],
    ) -> List[str]:
        async with get_db_session() as session:
            repo = PostgreSQLShareRepository(session)
            return await repo.list_resource_ids(
                resource_type=resource_type,
                scope_type=scope_type,
                scope_ids=scope_ids,
            )

    async def delete_for_resource(self, resource_type: str, resource_id: str) -> int:
        async with get_db_session() as session:
            repo = PostgreSQLShareRepository(session)
            return await repo.delete_for_resource(resource_type, resource_id)

    async def delete_for_scope(self, scope_type: str, scope_id: str) -> int:
        async with get_db_session() as session:
            repo = PostgreSQLShareRepository(session)
            return await repo.delete_for_scope(scope_type, scope_id)

    async def count_shares_for_resource(
        self, resource_type: str, resource_id: str
    ) -> int:
        async with get_db_session() as session:
            repo = PostgreSQLShareRepository(session)
            return await repo.count_shares_for_resource(resource_type, resource_id)
