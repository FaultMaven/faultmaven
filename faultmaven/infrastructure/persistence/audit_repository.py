"""Audit Log Repository - SQLAlchemy ORM Implementation.

Implements ``IAuditRepository`` for the ``user_audit_log`` table. First wired
for the SSO just-in-time provisioning audit trail (ADR-015 PR 7); any future
security-relevant event (logins, role grants, shares) writes through the same
interface.

Tenancy posture: ``user_audit_log`` is RLS-tenanted (migration 018 keys its
policy on ``organization_id``). When the caller does not supply an
organization, the row is stamped with the current tenant-context org — the
same value the engine applies to the transaction as ``app.current_org_id`` —
so the INSERT satisfies the policy's WITH CHECK under the limited
``faultmaven_app`` role instead of writing a NULL that RLS rejects.

⚠️ ``TENANT_PROVIDER=multi`` precondition: an unauthenticated caller (e.g. the
SSO callback) leaves the tenant context at the standalone default, whose org
row does not exist under multi — the FK then fails and a fail-open caller
loses the entry. Correct org stamping on the SSO path is part of the deferred
WorkOS-org→FM-org mapping (ADR-015 D3); resolve it before flipping multi.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.config.tenant_context import get_current_org_id
from faultmaven.infrastructure.persistence.models import UserAuditLogModel
from faultmaven.models.interfaces_user import (
    AuditCategory,
    AuditEventType,
    IAuditRepository,
    UserAuditLog,
)

logger = logging.getLogger(__name__)

# Column bounds, enforced here so an oversized transport value (a hostile
# User-Agent header, a malformed forwarded address) degrades to a truncated
# audit field instead of failing the INSERT — and with it, the audited action.
_MAX_IP_LENGTH = 45  # String(45): full-length IPv6
_MAX_USER_AGENT_LENGTH = 512
_MAX_SESSION_ID_LENGTH = 64


def _model_to_domain(model: UserAuditLogModel) -> UserAuditLog:
    """Convert ORM model to domain object."""
    details: Optional[Dict[str, Any]] = None
    if model.details:
        try:
            details = json.loads(model.details)
        except (ValueError, TypeError):
            # A corrupt details blob must not make the audit trail unreadable.
            details = {"_unparsed": model.details}
    return UserAuditLog(
        audit_id=model.audit_id,
        user_id=model.user_id,
        event_type=AuditEventType(model.event_type),
        event_category=AuditCategory(model.event_category),
        resource_type=model.resource_type,
        resource_id=model.resource_id,
        details=details,
        ip_address=model.ip_address,
        user_agent=model.user_agent,
        session_id=model.session_id,
        organization_id=model.organization_id,
        event_at=model.created_at,
        success=model.success,
    )


class PostgreSQLAuditRepository(IAuditRepository):
    """SQLAlchemy ORM implementation of the audit log repository."""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

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
        organization_id: Optional[str] = None,
        success: bool = True,
    ) -> bool:
        """Persist one audit event. See module docstring for the org stamping."""
        model = UserAuditLogModel(
            user_id=user_id,
            organization_id=organization_id or get_current_org_id(),
            event_type=(
                event_type.value
                if isinstance(event_type, AuditEventType)
                else str(event_type)
            ),
            event_category=(
                event_category.value
                if isinstance(event_category, AuditCategory)
                else str(event_category)
            ),
            resource_type=resource_type,
            resource_id=resource_id,
            details=json.dumps(details, default=str) if details else None,
            ip_address=ip_address[:_MAX_IP_LENGTH] if ip_address else None,
            user_agent=user_agent[:_MAX_USER_AGENT_LENGTH] if user_agent else None,
            session_id=session_id[:_MAX_SESSION_ID_LENGTH] if session_id else None,
            success=success,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(model)
        await self.db.commit()
        return True

    async def get_user_audit_log(
        self, user_id: str, limit: int = 100, offset: int = 0
    ) -> List[UserAuditLog]:
        """Get audit log entries for a user, newest first."""
        stmt = (
            select(UserAuditLogModel)
            .where(UserAuditLogModel.user_id == user_id)
            .order_by(UserAuditLogModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        return [_model_to_domain(m) for m in result.scalars().all()]

    async def get_organization_audit_log(
        self, organization_id: str, limit: int = 100, offset: int = 0
    ) -> List[UserAuditLog]:
        """Get audit log entries for an organization, newest first."""
        stmt = (
            select(UserAuditLogModel)
            .where(UserAuditLogModel.organization_id == organization_id)
            .order_by(UserAuditLogModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        return [_model_to_domain(m) for m in result.scalars().all()]
