"""Operator access auditing — shared API-layer policy (ADR-012 D8/D9).

Lives outside any one route module because the policy is not route-specific:
the cloud list-metadata split (#814) and the break-glass content path (#815)
record through the same helper, and the fail-closed decision below must hold
identically for all of them rather than being re-derived per handler.

Scope note: this records operator access to tenant **case** data — the
metadata/content boundary D8/D9 governs. Other operator-gated endpoints (user
administration, LLM configuration, Global KB authoring) are not tenant-content
reads and do not write here; durable audit for cross-tenant user administration
is tracked separately.
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import HTTPException, status
from starlette.requests import Request

from faultmaven.models.interfaces_operator_audit import (
    IOperatorAuditRepository,
    OperatorAction,
)
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser

logger = logging.getLogger(__name__)


async def get_operator_audit_repository(request: Request) -> IOperatorAuditRepository:
    """Get the operator audit repository from app.state (Composition Root)."""
    repo = getattr(request.app.state, "operator_audit_repository", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Operator audit trail not available",
        )
    return repo


async def record_operator_access(
    audit_repo: IOperatorAuditRepository,
    operator: AuthenticatedUser,
    action: OperatorAction,
    deployment_mode: str,
    target_organization_id: Optional[str] = None,
    target_case_id: Optional[str] = None,
    reason: Optional[str] = None,
    grant_id: Optional[str] = None,
    expires_at: Optional[datetime] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Record one operator access, or refuse the request.

    Call this BEFORE serving the data. A failure here becomes a 503, not a
    logged warning: degrading to "served but unaudited" would silently remove
    the control, and an access with no row behind it is the exact failure a
    compliance reviewer is looking for.

    ``reason``/``grant_id``/``expires_at`` carry break-glass provenance (#815)
    and are None for ambient access. They are denormalised onto the row rather
    than left as a reference to the grant, so the evidence of an access stays
    complete and readable even if the grant row is later lost.
    """
    try:
        await audit_repo.record_access(
            operator_user_id=operator.user_id,
            action=action,
            operator_username=operator.email,
            target_organization_id=target_organization_id,
            target_case_id=target_case_id,
            reason=reason,
            grant_id=grant_id,
            expires_at=expires_at,
            deployment_mode=deployment_mode,
            details=details,
        )
    except Exception as exc:
        logger.error(
            "operator_access_audit_write_failed",
            extra={
                "operator_user_id": operator.user_id,
                "action": action.value,
                "error": str(exc),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Operator access could not be recorded in the audit trail; "
                "the request was refused rather than served unaudited."
            ),
        ) from exc
