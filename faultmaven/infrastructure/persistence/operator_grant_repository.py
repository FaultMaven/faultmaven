"""Break-glass grant repository — SQLAlchemy ORM implementation.

Implements ``IOperatorGrantRepository`` over ``operator_access_grants``
(migration 036, ADR-012 D9).

**Not tenant-scoped, deliberately.** A grant is a record about the *operator*,
not about the tenant, and it is written by a session bound to the operator's own
organization while naming a different one. Applying RLS to it would make a grant
unreadable at exactly the moment it is needed.

**No update path beyond revocation and approval.** The justification columns are
pinned by database triggers, so an attempt to rewrite them fails at the engine
rather than at a code review.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.models import OperatorAccessGrantModel
from faultmaven.models.interfaces_operator_grant import (
    APPROVED_STATES,
    GrantApprovalState,
    IOperatorGrantRepository,
    OperatorAccessGrant,
)

logger = logging.getLogger(__name__)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Stamp UTC onto a naive timestamp read back from the database.

    SQLite has no timezone type, so every value returned from it is naive even
    though it was written as UTC. Left alone it reaches the API serialiser with
    no offset, and a client parses ``expires_at`` as *local* time — a break-glass
    window that reads hours off in either direction depending on where the
    operator sits.

    ``is_live`` compensates for exactly this internally, which is the tell that
    the normalisation belonged one layer down: fixing it here means every
    consumer, including the wire, sees an aware timestamp.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _model_to_domain(model: OperatorAccessGrantModel) -> OperatorAccessGrant:
    """Convert ORM model to domain object."""
    return OperatorAccessGrant(
        grant_id=model.grant_id,
        operator_user_id=model.operator_user_id,
        operator_username=model.operator_username,
        target_case_id=model.target_case_id,
        target_organization_id=model.target_organization_id,
        reason=model.reason,
        created_at=_as_utc(model.created_at),
        expires_at=_as_utc(model.expires_at),
        revoked_at=_as_utc(model.revoked_at),
        revoked_by=model.revoked_by,
        approval_state=GrantApprovalState(model.approval_state),
        approved_by=model.approved_by,
        approved_at=_as_utc(model.approved_at),
        deployment_mode=model.deployment_mode,
    )


class OperatorGrantRepository(IOperatorGrantRepository):
    """SQLAlchemy ORM implementation of the break-glass grant repository."""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def create_grant(self, grant: OperatorAccessGrant) -> OperatorAccessGrant:
        """Persist a new grant. Raises if it cannot be written."""
        model = OperatorAccessGrantModel(
            grant_id=grant.grant_id,
            operator_user_id=grant.operator_user_id,
            operator_username=grant.operator_username,
            target_case_id=grant.target_case_id,
            target_organization_id=grant.target_organization_id,
            reason=grant.reason,
            created_at=grant.created_at,
            expires_at=grant.expires_at,
            approval_state=GrantApprovalState(grant.approval_state).value,
            approved_by=grant.approved_by,
            approved_at=grant.approved_at,
            deployment_mode=grant.deployment_mode,
        )
        self.db.add(model)
        await self.db.commit()
        return grant

    async def get_grant(self, grant_id: str) -> Optional[OperatorAccessGrant]:
        """Fetch one grant by id, or None."""
        result = await self.db.execute(
            select(OperatorAccessGrantModel).where(
                OperatorAccessGrantModel.grant_id == grant_id
            )
        )
        model = result.scalar_one_or_none()
        return _model_to_domain(model) if model else None

    async def find_live_grant(
        self,
        operator_user_id: str,
        target_case_id: str,
        now: Optional[datetime] = None,
    ) -> Optional[OperatorAccessGrant]:
        """The operator's live grant over this case, or None (latest expiry wins).

        Liveness is filtered in SQL so an operator holding thousands of expired
        grants does not drag the whole history into memory on every read, then
        re-checked in Python via ``OperatorAccessGrant.is_live`` at the call
        site — the domain predicate stays the authority, and a divergence
        between the two can only ever fail closed.
        """
        moment = now or datetime.now(timezone.utc)
        approved = [s.value for s in APPROVED_STATES]
        result = await self.db.execute(
            select(OperatorAccessGrantModel)
            .where(
                OperatorAccessGrantModel.operator_user_id == operator_user_id,
                OperatorAccessGrantModel.target_case_id == target_case_id,
                OperatorAccessGrantModel.approval_state.in_(approved),
                OperatorAccessGrantModel.revoked_at.is_(None),
                OperatorAccessGrantModel.expires_at > moment,
            )
            .order_by(OperatorAccessGrantModel.expires_at.desc())
            .limit(1)
        )
        model = result.scalar_one_or_none()
        return _model_to_domain(model) if model else None

    async def list_grants(
        self,
        operator_user_id: Optional[str] = None,
        target_case_id: Optional[str] = None,
        target_organization_id: Optional[str] = None,
        live_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[OperatorAccessGrant], int]:
        """Query grants, newest first. Returns (page, total_matching)."""
        filters = []
        if operator_user_id:
            filters.append(
                OperatorAccessGrantModel.operator_user_id == operator_user_id
            )
        if target_case_id:
            filters.append(OperatorAccessGrantModel.target_case_id == target_case_id)
        if target_organization_id:
            filters.append(
                OperatorAccessGrantModel.target_organization_id
                == target_organization_id
            )
        if live_only:
            approved = [s.value for s in APPROVED_STATES]
            filters.extend(
                [
                    OperatorAccessGrantModel.approval_state.in_(approved),
                    OperatorAccessGrantModel.revoked_at.is_(None),
                    OperatorAccessGrantModel.expires_at > datetime.now(timezone.utc),
                ]
            )

        total = (
            await self.db.execute(
                select(func.count())
                .select_from(OperatorAccessGrantModel)
                .where(*filters)
            )
        ).scalar_one()
        result = await self.db.execute(
            select(OperatorAccessGrantModel)
            .where(*filters)
            .order_by(
                OperatorAccessGrantModel.created_at.desc(),
                # Tiebreak on the primary key: grants minted in the same
                # transaction share a timestamp, and an unstable sort would let
                # paging repeat or skip rows.
                OperatorAccessGrantModel.grant_id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return [_model_to_domain(m) for m in result.scalars().all()], total

    async def revoke_grant(
        self, grant_id: str, revoked_by: str
    ) -> Optional[OperatorAccessGrant]:
        """End a grant early. Returns the updated grant, or None if unknown.

        Idempotent: an already-revoked grant is returned untouched, so a second
        revoke cannot move ``revoked_at`` forward — that timestamp is the record
        of when access actually ended. Two concurrent first-revokes can race to
        write it, but both write ~the same instant and the grant is revoked
        either way, so the race has no security consequence.
        """
        result = await self.db.execute(
            select(OperatorAccessGrantModel).where(
                OperatorAccessGrantModel.grant_id == grant_id
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None

        if model.revoked_at is None:
            model.revoked_at = datetime.now(timezone.utc)
            model.revoked_by = revoked_by
            await self.db.commit()
            await self.db.refresh(model)

        return _model_to_domain(model)
