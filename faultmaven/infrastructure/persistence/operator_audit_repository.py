"""Operator access audit repository — SQLAlchemy ORM implementation.

Implements ``IOperatorAuditRepository`` for the ``operator_access_audit`` table
(ADR-012 D8/D9).

**Not tenant-scoped, deliberately.** ``user_audit_log`` is RLS-tenanted, so it
cannot express an access that spans every tenant. This table therefore carries a
nullable ``target_organization_id`` and no tenant policy, and does not stamp the
row from ``get_current_org_id()`` the way ``PostgreSQLAuditRepository`` does.

**Write failures propagate.** ``record_access`` does not swallow exceptions.
The caller is expected to record the access *before* serving the data and to
fail the request if the record could not be written: an operator read that
leaves no evidence is precisely what this table exists to prevent, so silently
degrading to "served but unaudited" would defeat it. This is the opposite of the
posture for incidental telemetry, and is intentional.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.models import OperatorAccessAuditModel
from faultmaven.models.interfaces_operator_audit import (
    IOperatorAuditRepository,
    OperatorAccessAudit,
    OperatorAction,
)

logger = logging.getLogger(__name__)

# Column bounds, so an oversized value degrades to a truncated audit field
# instead of failing the INSERT — which, being fail-closed, would fail the
# audited request itself.
_MAX_USERNAME_LENGTH = 255
_MAX_ID_LENGTH = 36
_MAX_DEPLOYMENT_MODE_LENGTH = 32


def _clip(value: Optional[str], limit: int) -> Optional[str]:
    """Truncate to a column bound, preserving None."""
    return value[:limit] if value else None


def _model_to_domain(model: OperatorAccessAuditModel) -> OperatorAccessAudit:
    """Convert ORM model to domain object."""
    details: Optional[Dict[str, Any]] = None
    if model.details:
        try:
            details = json.loads(model.details)
        except (ValueError, TypeError):
            # A corrupt blob must not make the whole trail unreadable.
            details = {"_unparsed": model.details}
    return OperatorAccessAudit(
        audit_id=model.audit_id,
        operator_user_id=model.operator_user_id,
        operator_username=model.operator_username,
        action=OperatorAction(model.action),
        created_at=model.created_at,
        target_organization_id=model.target_organization_id,
        target_case_id=model.target_case_id,
        reason=model.reason,
        grant_id=model.grant_id,
        expires_at=model.expires_at,
        deployment_mode=model.deployment_mode,
        details=details,
    )


class OperatorAuditRepository(IOperatorAuditRepository):
    """SQLAlchemy ORM implementation of the operator access audit repository."""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

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
        model = OperatorAccessAuditModel(
            operator_user_id=_clip(operator_user_id, _MAX_ID_LENGTH),
            operator_username=_clip(operator_username, _MAX_USERNAME_LENGTH),
            action=OperatorAction(action).value,
            target_organization_id=_clip(target_organization_id, _MAX_ID_LENGTH),
            target_case_id=_clip(target_case_id, _MAX_ID_LENGTH),
            reason=reason,
            grant_id=_clip(grant_id, _MAX_ID_LENGTH),
            expires_at=expires_at,
            deployment_mode=_clip(deployment_mode, _MAX_DEPLOYMENT_MODE_LENGTH),
            details=json.dumps(details, default=str) if details else None,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(model)
        await self.db.commit()

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
        filters = []
        if operator_user_id:
            filters.append(
                OperatorAccessAuditModel.operator_user_id == operator_user_id
            )
        if target_organization_id:
            filters.append(
                OperatorAccessAuditModel.target_organization_id
                == target_organization_id
            )
        if target_case_id:
            filters.append(OperatorAccessAuditModel.target_case_id == target_case_id)
        if action:
            filters.append(
                OperatorAccessAuditModel.action == OperatorAction(action).value
            )

        total = (
            await self.db.execute(
                select(func.count())
                .select_from(OperatorAccessAuditModel)
                .where(*filters)
            )
        ).scalar_one()
        page_stmt = (
            select(OperatorAccessAuditModel)
            .where(*filters)
            .order_by(
                OperatorAccessAuditModel.created_at.desc(),
                # Tiebreak on the monotonic PK: bulk access in the same
                # transaction shares a timestamp, and an unstable sort would
                # let paging repeat or skip rows.
                OperatorAccessAuditModel.audit_id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(page_stmt)
        return [_model_to_domain(m) for m in result.scalars().all()], total
