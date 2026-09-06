"""Sessionless break-glass grant repository.

Wrapper around ``OperatorGrantRepository`` that creates a session per operation
via ``get_db_session()``, mirroring ``SessionlessOperatorAuditRepository``. This
keeps the operator-governance path off any long-lived session in the DI
container, and gives each grant write its own transaction so a later failure in
the request cannot roll away the record that access was granted.
"""

from datetime import datetime
from typing import List, Optional

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.operator_grant_repository import (
    OperatorGrantRepository,
)
from faultmaven.models.interfaces_operator_grant import (
    IOperatorGrantRepository,
    OperatorAccessGrant,
)


class SessionlessOperatorGrantRepository(IOperatorGrantRepository):
    """Sessionless wrapper for the break-glass grant repository."""

    async def create_grant(self, grant: OperatorAccessGrant) -> OperatorAccessGrant:
        """Persist a new grant. Raises if it cannot be written."""
        async with get_db_session() as session:
            return await OperatorGrantRepository(session).create_grant(grant)

    async def get_grant(self, grant_id: str) -> Optional[OperatorAccessGrant]:
        """Fetch one grant by id, or None."""
        async with get_db_session() as session:
            return await OperatorGrantRepository(session).get_grant(grant_id)

    async def find_live_grant(
        self,
        operator_user_id: str,
        target_case_id: str,
        now: Optional[datetime] = None,
    ) -> Optional[OperatorAccessGrant]:
        """The operator's live grant over this case, or None."""
        async with get_db_session() as session:
            return await OperatorGrantRepository(session).find_live_grant(
                operator_user_id=operator_user_id,
                target_case_id=target_case_id,
                now=now,
            )

    async def list_grants(
        self,
        operator_user_id: Optional[str] = None,
        target_case_id: Optional[str] = None,
        target_enterprise_id: Optional[str] = None,
        live_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[OperatorAccessGrant], int]:
        """Query grants, newest first. Returns (page, total_matching)."""
        async with get_db_session() as session:
            return await OperatorGrantRepository(session).list_grants(
                operator_user_id=operator_user_id,
                target_case_id=target_case_id,
                target_enterprise_id=target_enterprise_id,
                live_only=live_only,
                limit=limit,
                offset=offset,
            )

    async def revoke_grant(
        self, grant_id: str, revoked_by: str
    ) -> Optional[OperatorAccessGrant]:
        """End a grant early. Returns the updated grant, or None if unknown."""
        async with get_db_session() as session:
            return await OperatorGrantRepository(session).revoke_grant(
                grant_id=grant_id, revoked_by=revoked_by
            )
