"""Break-glass grants — domain types and repository interface (ADR-012 D8/D9).

A grant is one operator's time-boxed license to read **one case's** content in
Cloud. See ``docs/architecture/security/break-glass-content-access.md`` for why
the model is shaped this way; the durable record of accesses *taken* under a
grant lives in ``interfaces_operator_audit``.

The repository surface is deliberately narrow: create, read, list, revoke. There
is no update, and the database pins the justification columns (migration 036) —
an operator can end their own access early, but cannot rewrite why they took it
or how long they were allowed to keep it.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional


class GrantApprovalState(str, Enum):
    """Where a grant sits in the approval state machine.

    ``AUTO_APPROVED`` is what an operator's own grant is created as today: the
    control is reason + TTL + an immutable audit trail rather than a second
    party's consent. ``PENDING``/``APPROVED``/``DENIED`` are the seam for the
    customer-initiated posture ADR-012 D9 calls the ideal — declared here so
    adding it is a transition in this machine rather than a schema change, and
    so the liveness predicate below already accounts for every state.
    """

    AUTO_APPROVED = "auto_approved"
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


#: The states in which a grant authorises anything at all. Anything outside this
#: set is not live regardless of its expiry.
APPROVED_STATES = frozenset(
    {GrantApprovalState.AUTO_APPROVED, GrantApprovalState.APPROVED}
)


@dataclass
class OperatorAccessGrant:
    """One operator's break-glass license over one case."""

    grant_id: str
    operator_user_id: str
    target_case_id: str
    target_enterprise_id: str
    reason: str
    created_at: datetime
    expires_at: datetime
    operator_username: Optional[str] = None
    revoked_at: Optional[datetime] = None
    revoked_by: Optional[str] = None
    approval_state: GrantApprovalState = GrantApprovalState.AUTO_APPROVED
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    deployment_mode: Optional[str] = None

    def is_live(self, now: Optional[datetime] = None) -> bool:
        """Whether this grant authorises a content read right now.

        The single definition of liveness. Approval state, revocation and expiry
        are three independent ways for a grant to stop authorising, and every
        caller must apply all three — so no caller gets to spell out its own
        subset.
        """
        moment = now or datetime.now(timezone.utc)
        if self.approval_state not in APPROVED_STATES:
            return False
        if self.revoked_at is not None:
            return False
        # Rows read back from SQLite come without a tzinfo. Treat a naive
        # timestamp as the UTC it was written as, rather than letting the
        # comparison raise and take down an authorization check.
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > moment


class IOperatorGrantRepository(ABC):
    """Persistence for break-glass grants."""

    @abstractmethod
    async def create_grant(self, grant: OperatorAccessGrant) -> OperatorAccessGrant:
        """Persist a new grant. Raises if it cannot be written."""

    @abstractmethod
    async def get_grant(self, grant_id: str) -> Optional[OperatorAccessGrant]:
        """Fetch one grant by id, or None."""

    @abstractmethod
    async def find_live_grant(
        self,
        operator_user_id: str,
        target_case_id: str,
        now: Optional[datetime] = None,
    ) -> Optional[OperatorAccessGrant]:
        """The operator's live grant over this case, or None.

        Where several are live — an operator may hold overlapping grants after
        re-justifying — the one expiring **last** wins, so the access is
        attributed to the window that actually still covers it.
        """

    @abstractmethod
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

    @abstractmethod
    async def revoke_grant(
        self, grant_id: str, revoked_by: str
    ) -> Optional[OperatorAccessGrant]:
        """End a grant early. Returns the updated grant, or None if unknown.

        Idempotent: revoking an already-revoked grant leaves the original
        ``revoked_at`` in place rather than moving the record of when access
        actually ended.
        """
