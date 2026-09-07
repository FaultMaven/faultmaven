"""Sessionless Audit Repository.

Wrapper around PostgreSQLAuditRepository that creates a session per operation
via get_db_session(), following the same pattern as SessionlessTeamRepository.
This removes the need for a long-lived db_session in the DI container.
"""

from typing import Any, Dict, List, Optional

from faultmaven.infrastructure.persistence.audit_repository import (
    PostgreSQLAuditRepository,
)
from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.models.interfaces_user import (
    AuditCategory,
    AuditEventType,
    IAuditRepository,
    UserAuditLog,
)


class SessionlessAuditRepository(IAuditRepository):
    """Sessionless wrapper for the audit log repository.

    Creates a new database session for each operation using get_db_session().
    """

    async def log_event(
        self,
        user_id: str,
        event_type: AuditEventType,
        event_category: AuditCategory,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        session_id: Optional[str] = None,
        enterprise_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        success: bool = True,
    ) -> bool:
        """Log an audit event."""
        async with get_db_session() as session:
            repo = PostgreSQLAuditRepository(session)
            return await repo.log_event(
                user_id=user_id,
                event_type=event_type,
                event_category=event_category,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details,
                ip_address=ip_address,
                user_agent=user_agent,
                session_id=session_id,
                enterprise_id=enterprise_id,
                organization_id=organization_id,
                success=success,
            )

    async def get_user_audit_log(
        self, user_id: str, limit: int = 100, offset: int = 0
    ) -> List[UserAuditLog]:
        """Get audit log entries for a user."""
        async with get_db_session() as session:
            repo = PostgreSQLAuditRepository(session)
            return await repo.get_user_audit_log(user_id, limit, offset)

    async def get_enterprise_audit_log(
        self, enterprise_id: str, limit: int = 100, offset: int = 0
    ) -> List[UserAuditLog]:
        """Get audit log entries for an enterprise."""
        async with get_db_session() as session:
            repo = PostgreSQLAuditRepository(session)
            return await repo.get_enterprise_audit_log(enterprise_id, limit, offset)
