"""Operator access audit — domain types and repository interface (ADR-012 D8/D9).

The durable, queryable record of platform-operator access to tenant data, kept
distinct from ``user_audit_log`` (which is RLS-tenanted and therefore cannot
express a cross-tenant event; see ``interfaces_user.IAuditRepository``).

The write path is deliberately narrow: callers state *what kind* of access
happened and *what it targeted*, and the repository stamps the rest. There is
no update or delete operation on this interface, and the database refuses both
(migration 035) — an operator must not be able to alter the record of their own
access.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class OperatorAction(str, Enum):
    """What an operator did that this table has to remember.

    Two are the metadata/content boundary D8/D9 governs. ``LIST`` is ambient
    metadata (ids, org, state, timestamps, counts — never titles).
    ``CONTENT_OPEN`` is tenant content: title, transcript, evidence. Title
    counts as content because it is user free-text and leaks.

    ``ROLE_GRANTED`` / ``ROLE_REVOKED`` are not data access — they record
    changes to *who is an operator*. They live here because ``platform_admin``
    is deployment-scoped (ADR-012 D9) and so has no organization to stamp it
    with; see migration 042 and ``cli._operator_role_audit`` for why the
    RLS-tenanted ``user_audit_log`` cannot hold them.

    So read this enum as "operator events", of which data access is two.
    """

    LIST = "list"
    CONTENT_OPEN = "content_open"
    ROLE_GRANTED = "role_granted"
    ROLE_REVOKED = "role_revoked"


@dataclass
class OperatorAccessAudit:
    """One recorded operator access."""

    audit_id: int
    operator_user_id: Optional[str]
    operator_username: Optional[str]
    action: OperatorAction
    created_at: datetime
    # None = the access spanned all tenants (a cross-tenant list).
    target_organization_id: Optional[str] = None
    # None = the access was not scoped to a single case.
    target_case_id: Optional[str] = None
    # Break-glass provenance (#815); None for ambient access.
    reason: Optional[str] = None
    grant_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    deployment_mode: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


class IOperatorAuditRepository(ABC):
    """Append-only persistence for operator access records."""

    @abstractmethod
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
        """Append one access record.

        Implementations MUST let write failures propagate. Callers record the
        access before serving the data and fail the request if this raises; an
        implementation that swallowed the error would silently convert the
        control into "served but unaudited", which is the outcome this table
        exists to prevent.
        """

    @abstractmethod
    async def list_access(
        self,
        operator_user_id: Optional[str] = None,
        target_organization_id: Optional[str] = None,
        target_case_id: Optional[str] = None,
        action: Optional[OperatorAction] = None,
        grant_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[OperatorAccessAudit], int]:
        """Query the trail, newest first. Returns (page, total_matching).

        ``grant_id`` answers "everything read under this break-glass grant" —
        the query that takes a reviewer from one justification to the full set
        of accesses it authorised, and the reason migration 036 indexes the
        column.
        """
