"""Sessionless Operator Audit Repository.

Wrapper around ``OperatorAuditRepository`` that creates a session per operation
via ``get_db_session()``, following ``SessionlessAuditRepository``. This keeps
the operator-governance write path off any long-lived session in the DI
container.

A session per operation also gives the audit row its **own transaction**, so a
later failure in the request cannot roll away the evidence that the access was
attempted.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.operator_audit_repository import (
    OperatorAuditRepository,
)
from faultmaven.models.interfaces_operator_audit import (
    IOperatorAuditRepository,
    OperatorAccessAudit,
    OperatorAction,
)


class SessionlessOperatorAuditRepository(IOperatorAuditRepository):
    """Sessionless wrapper for the operator access audit repository."""

    async def record_access(
        self,
        operator_user_id: Optional[str],
        action: OperatorAction,
        operator_username: Optional[str] = None,
        target_organization_id: Optional[str] = None,
        target_case_id: Optional[str] = None,
        reason: Optional[str] = None,
        grant_id: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        deployment_mode: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append one access record. Raises if it cannot be persisted."""
        async with get_db_session() as session:
            repo = OperatorAuditRepository(session)
            await repo.record_access(
                operator_user_id=operator_user_id,
                action=action,
                operator_username=operator_username,
                target_organization_id=target_organization_id,
                target_case_id=target_case_id,
                reason=reason,
                grant_id=grant_id,
                expires_at=expires_at,
                deployment_mode=deployment_mode,
                details=details,
            )

    async def list_access(
        self,
        operator_user_id: Optional[str] = None,
        target_organization_id: Optional[str] = None,
        target_case_id: Optional[str] = None,
        action: Optional[OperatorAction] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[OperatorAccessAudit], int]:
        """Query the trail, newest first. Returns (page, total_matching)."""
        async with get_db_session() as session:
            repo = OperatorAuditRepository(session)
            return await repo.list_access(
                operator_user_id=operator_user_id,
                target_organization_id=target_organization_id,
                target_case_id=target_case_id,
                action=action,
                limit=limit,
                offset=offset,
            )
