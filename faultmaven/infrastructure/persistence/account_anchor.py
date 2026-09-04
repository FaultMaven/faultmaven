"""The one place an account's enterprise anchor is read and moved (#1045 D8 R2).

``users.enterprise_id`` says which enterprise owns an account. Two callers change
it — the SSO login and the operator command — and before this module they each
had their own mover with their own rule, which is how an unscoped login came to
be able to drag a company-anchored account back onto a personal tenant. There is
one rule now, it lives here, and both callers ask it rather than restating it.

The rule, in one sentence: **an anchor may be set when it is absent, and may
only ever move toward a company enterprise.**

* **absent → anything** is a *set*, not a move: an account anchored to nothing
  is what a ``--next-login fresh-tenant`` retirement leaves and what a
  freshly-provisioned personal tenant fills in. Nothing is being taken away.
* **a retired personal enterprise → a company enterprise** is the move a
  retired subject makes when a company invites them.
* **the subject's own live personal enterprise → a company enterprise** is the
  switch #1320 shipped, kept unchanged.
* **anything → a personal enterprise, when the anchor is already set**, is
  refused. That direction has no legitimate caller: it is what the reverse-move
  defect did, turning a half-finished re-anchor into a silent demotion back into
  a tenant the account had left.
* **a live company enterprise → anywhere else** is refused, as it always was.
  Moving an account between companies is an operator migration, never an
  implicit consequence of an IdP claim.

Liveness and "is this a retired personal enterprise?" are read from **typed
columns** — ``enterprises.deleted_at`` and ``enterprises.personal_tenant_retirement``
— never from a settings blob. ``enterprises`` carries no ``organization_id`` and
migration 018 does not enrol it, so these reads answer on the unauthenticated SSO
callback with no tenant bound, which is exactly where the login needs them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

import structlog

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.models import EnterpriseModel

logger = structlog.get_logger(__name__)


class AnchorKind(str, Enum):
    """What an account's current anchor is, as the typed columns report it."""

    #: The account is anchored to nothing. May provision, may be set.
    ABSENT = "absent"
    #: A live enterprise — a company, or a personal tenant still in service.
    LIVE = "live"
    #: Soft-deleted and carrying an operator's retirement policy: a personal
    #: tenant an operator retired.
    RETIRED_PERSONAL = "retired_personal"
    #: Soft-deleted with no retirement policy: a company that was removed. Not a
    #: retirement this feature performed, and deliberately not treated as one.
    DELETED = "deleted"
    #: The anchor names an enterprise row that does not exist. A data fault, and
    #: reported as one rather than folded into "absent" — the two have different
    #: causes and different remedies.
    DANGLING = "dangling"


@dataclass(frozen=True)
class AnchorState:
    """An account's anchor, resolved."""

    kind: AnchorKind
    enterprise_id: Optional[str]
    retirement_policy: Optional[str]

    @property
    def releases_provisioning(self) -> bool:
        """Whether an org-less login may provision a fresh personal tenant.

        Exactly one state does: no anchor at all. Everything else — a live
        anchor, a retired one, a dangling one — refuses, so the permissive
        answer can never come from an unreadable or unexpected value.
        """
        return self.kind is AnchorKind.ABSENT


async def read_anchor(enterprise_id: Optional[str]) -> AnchorState:
    """Resolve an account's anchor from the typed columns."""
    if not enterprise_id:
        return AnchorState(AnchorKind.ABSENT, None, None)
    async with get_db_session() as session:
        enterprise = await session.get(EnterpriseModel, enterprise_id)
        if enterprise is None:
            return AnchorState(AnchorKind.DANGLING, enterprise_id, None)
        policy = enterprise.personal_tenant_retirement
        if enterprise.deleted_at is None:
            return AnchorState(AnchorKind.LIVE, enterprise_id, policy)
        kind = AnchorKind.RETIRED_PERSONAL if policy else AnchorKind.DELETED
        return AnchorState(kind, enterprise_id, policy)


def move_is_permitted(current: AnchorState, *, destination_is_personal: bool) -> bool:
    """The rule, as one expression with no I/O. See the module docstring."""
    if current.kind is AnchorKind.ABSENT:
        # A set, not a move.
        return True
    if destination_is_personal:
        # Never drag an already-anchored account onto a personal tenant.
        return False
    return current.kind in (AnchorKind.LIVE, AnchorKind.RETIRED_PERSONAL)


async def move_account_anchor(
    users: Any,
    user: Any,
    *,
    to_enterprise_id: str,
    destination_is_personal: bool,
    own_live_personal: bool = False,
) -> bool:
    """Set or move ``user``'s anchor to ``to_enterprise_id``. True when written.

    ``own_live_personal`` says the caller has established — from the untenanted
    subject binding, keyed on this subject — that the account's *current* live
    anchor is its own personal tenant. That is the only way a LIVE anchor may
    move, and it is #1320's personal→company switch. Without it a live anchor is
    a company affiliation and stays put.

    Returns False without writing when the rule refuses, so a caller can turn
    that into its own refusal. Raises nothing on the refusal path.
    """
    current_id = getattr(user, "enterprise_id", None)
    if current_id == to_enterprise_id:
        return True

    current = await read_anchor(current_id)
    if not move_is_permitted(current, destination_is_personal=destination_is_personal):
        logger.warning(
            "account_anchor_move_refused",
            user_id=getattr(user, "user_id", None),
            from_kind=current.kind.value,
            destination_is_personal=destination_is_personal,
        )
        return False
    if current.kind is AnchorKind.LIVE and not own_live_personal:
        logger.warning(
            "account_anchor_move_refused",
            user_id=getattr(user, "user_id", None),
            from_kind=current.kind.value,
            reason="live_company_anchor",
        )
        return False

    user.enterprise_id = to_enterprise_id
    try:
        await users.update(user)
    except Exception:
        # Refuse rather than proceed on an in-memory change: every later step
        # would act on an anchor the database does not hold.
        logger.exception(
            "account_anchor_move_failed", user_id=getattr(user, "user_id", None)
        )
        user.enterprise_id = current_id
        return False
    logger.info(
        "account_anchor_moved",
        user_id=getattr(user, "user_id", None),
        from_kind=current.kind.value,
        to_enterprise_id=to_enterprise_id,
    )
    return True


async def accounts_anchored_to(enterprise_id: str) -> list[str]:
    """The ids of every account anchored to ``enterprise_id``.

    Addressed by the **enterprise**, not by an IdP subject, so a retirement can
    find the accounts it has to release and revoke from the organization id
    alone — after the subject binding that named them has been deleted. That is
    what makes those two steps resumable.
    """
    from sqlalchemy import select

    from faultmaven.infrastructure.persistence.models import UserModel

    async with get_db_session() as session:
        rows = await session.execute(
            select(UserModel.user_id).where(UserModel.enterprise_id == enterprise_id)
        )
        return [row[0] for row in rows.all()]


async def clear_anchors_anchored_to(enterprise_id: str) -> list[str]:
    """Release every account anchored to ``enterprise_id``. Returns the ids.

    The write ``--next-login fresh-tenant`` makes, and the reason
    ``users.enterprise_id`` had to become nullable: NULL is the only way to say
    "anchored to nothing" in a typed column, and it is the single state that
    lets an org-less login provision again.
    """
    from sqlalchemy import update

    from faultmaven.infrastructure.persistence.models import UserModel

    affected = await accounts_anchored_to(enterprise_id)
    if not affected:
        return []
    async with get_db_session() as session:
        await session.execute(
            update(UserModel)
            .where(UserModel.enterprise_id == enterprise_id)
            .values(enterprise_id=None)
        )
    logger.info(
        "account_anchors_cleared", enterprise_id=enterprise_id, accounts=len(affected)
    )
    return affected
