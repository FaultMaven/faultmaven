"""The one place an account's enterprise anchor is read and moved (#1045 D8 R2).

``users.enterprise_id`` says which enterprise owns an account — exactly one, NOT
NULL (ADR-017 D3). Two callers change it — the SSO login and the operator
command — and before this module they each had their own mover with their own
rule, which is how an unscoped login came to be able to drag a company-anchored
account back onto a personal tenant. There is one rule now, it lives here, and
both callers ask it rather than restating it.

The rule, in one sentence: **an anchor may be set when it is absent, and may
only ever move toward a company enterprise.**

* **absent → anything** is a *set*, not a move: nothing is being taken away.
  Under ADR-017 D3 ``users.enterprise_id`` is NOT NULL, so no persisted account
  is in this state — it is the degenerate case of an in-memory account that has
  not been anchored yet, kept because the rule has to answer for it.
* **a retired personal enterprise → a company enterprise** is the move a
  retired subject makes when a company invites them.
* **the subject's own live personal enterprise → a company enterprise** is the
  switch #1320 shipped, kept unchanged — the caller establishes "its own" from
  the untenanted subject binding and passes it in, and the rule weighs it.
* **a personal enterprise retired with ``fresh_tenant`` → this subject's own new
  personal enterprise** is the one move toward a personal tenant there is, and
  it exists because ``users.enterprise_id`` is NOT NULL (ADR-017 D3): the
  account cannot be parked at "anchored to nothing" while it waits to start
  over, so it stays on the retired enterprise and moves off it on the sign-in
  the operator authorised. Both halves are required — a recorded
  ``fresh_tenant`` retirement AND a destination the caller established, from
  this subject's own binding, to be personal.
* **anything else → a personal enterprise, when the anchor is already set**, is
  refused. That direction has no legitimate caller: it is what the reverse-move
  defect did, turning a half-finished re-anchor into a silent demotion back into
  a tenant the account had left.
* **a live company enterprise → anywhere else** is refused, as it always was.
  Moving an account between companies is an operator migration, never an
  implicit consequence of an IdP claim.

Liveness and "is this a retired personal enterprise?" are read from **typed
columns** — ``enterprises.deleted_at`` and
``sso_personal_enterprises.retirement_state`` — never from a settings blob.
Neither table is RLS-enrolled (the enterprise IS the tenant; the subject row is
read on the unauthenticated callback before one is bound), so these reads answer
with no tenant bound, which is exactly where the login needs them.

The retirement state sits on the SUBJECT row rather than on the enterprise, and
that placement is load-bearing: the question this module answers is "was *this
account's own* personal tenant retired?", which is a fact about the subject. An
enterprise-level marker could not distinguish it from "the company that owned
this account was removed", and those two have opposite consequences for the next
sign-in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

import structlog
from sqlalchemy import select

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.models import (
    EnterpriseModel,
    SSOPersonalEnterpriseModel,
)
from faultmaven.modules.auth.contracts import RETIREMENT_POLICY_FRESH_TENANT

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

        Two states do, and both are **positive statements** rather than
        absences, which is what keeps the permissive answer out of reach of an
        unreadable or unexpected value:

        * **no anchor at all** — unreachable for a persisted account since
          ``users.enterprise_id`` became NOT NULL (ADR-017 D3), and kept for the
          in-memory account that has not been anchored yet;
        * **this subject's own personal tenant, retired with the
          ``fresh_tenant`` policy** — the operator's recorded decision that this
          subject may start over. This is what replaced "clear the anchor":
          NOT NULL leaves no absence to mean it, so the release became the
          typed value an operator actually chose.

        Everything else refuses — a live anchor, a dangling one, a retired
        company, and a personal retirement whose policy is ``refuse`` or was
        never recorded. A retirement that failed before it stamped its policy
        therefore refuses, which is the direction that cannot strand an
        employee in a personal tenant.
        """
        if self.kind is AnchorKind.ABSENT:
            return True
        return (
            self.kind is AnchorKind.RETIRED_PERSONAL
            and self.retirement_policy == RETIREMENT_POLICY_FRESH_TENANT
        )


async def read_anchor(enterprise_id: Optional[str]) -> AnchorState:
    """Resolve an account's anchor from the typed columns."""
    if not enterprise_id:
        return AnchorState(AnchorKind.ABSENT, None, None)
    async with get_db_session() as session:
        enterprise = await session.get(EnterpriseModel, enterprise_id)
        if enterprise is None:
            return AnchorState(AnchorKind.DANGLING, enterprise_id, None)
        # The retirement policy is read from the SUBJECT row that owns this
        # enterprise, not from the enterprise itself. A company enterprise has
        # no such row, so ``policy`` is None for it — which is exactly the
        # distinction ``DELETED`` vs ``RETIRED_PERSONAL`` turns on.
        policy = (
            await session.execute(
                select(SSOPersonalEnterpriseModel.retirement_state).where(
                    SSOPersonalEnterpriseModel.enterprise_id == enterprise_id
                )
            )
        ).scalar_one_or_none()
        if enterprise.deleted_at is None:
            return AnchorState(AnchorKind.LIVE, enterprise_id, policy)
        kind = AnchorKind.RETIRED_PERSONAL if policy else AnchorKind.DELETED
        return AnchorState(kind, enterprise_id, policy)


def move_is_permitted(
    current: AnchorState,
    *,
    destination_is_personal: bool,
    own_live_personal: bool = False,
) -> bool:
    """The rule — **the whole rule** — as one expression with no I/O.

    ``own_live_personal`` belongs here rather than in the mover. It was a second
    guard beside this one, and that made the claim "the rule is one expression"
    false in a way a mutation exposed: forcing this function to True left the
    reverse move still refused, by the other guard, so a test that said the
    direction rule was what refused it was passing for the wrong reason.

    ``current.retirement_policy`` is read here for the same reason: the one
    permitted move toward a personal tenant is authorised by a value on the
    subject row, and a caller that re-derived that authorisation would be a
    second copy of the rule.

    It says the caller has established — from the untenanted subject binding,
    keyed on this subject — that the account's current LIVE anchor is its own
    personal tenant. That is the only way a live anchor may move, and it is
    #1320's personal→company switch. Without it a live anchor is a company
    affiliation and stays put.
    """
    if current.kind is AnchorKind.ABSENT:
        # A set, not a move.
        return True
    if destination_is_personal:
        # Exactly one anchored account may move onto a personal tenant: one
        # whose own personal tenant an operator retired with ``fresh_tenant``,
        # moving onto the replacement that retirement authorised. Every other
        # anchored account is refused — that is the reverse move, which turned a
        # half-finished re-anchor into a silent demotion.
        return (
            current.kind is AnchorKind.RETIRED_PERSONAL
            and current.retirement_policy == RETIREMENT_POLICY_FRESH_TENANT
        )
    if current.kind is AnchorKind.RETIRED_PERSONAL:
        return True
    if current.kind is AnchorKind.LIVE:
        return own_live_personal
    # DELETED (a removed company) and DANGLING (a broken row) are not licences
    # to move: neither is evidence about where this account belongs.
    return False


async def move_account_anchor(
    users: Any,
    user: Any,
    *,
    to_enterprise_id: str,
    destination_is_personal: bool,
    own_live_personal: bool = False,
) -> bool:
    """Set or move ``user``'s anchor to ``to_enterprise_id``. True when written.

    ``own_live_personal`` is passed straight through to
    :func:`move_is_permitted`, which owns the whole rule. This function does the
    I/O — read the current anchor, write the new one — and decides nothing.

    Returns False without writing when the rule refuses, so a caller can turn
    that into its own refusal. Raises nothing on the refusal path.
    """
    current_id = getattr(user, "enterprise_id", None)
    if current_id == to_enterprise_id:
        return True

    current = await read_anchor(current_id)
    if not move_is_permitted(
        current,
        destination_is_personal=destination_is_personal,
        own_live_personal=own_live_personal,
    ):
        logger.warning(
            "account_anchor_move_refused",
            user_id=getattr(user, "user_id", None),
            from_kind=current.kind.value,
            destination_is_personal=destination_is_personal,
            own_live_personal=own_live_personal,
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
    find the accounts whose tokens it has to revoke from the enterprise id
    alone, however the tenant was addressed. That is what makes the step
    resumable after an interrupted run.
    """
    from sqlalchemy import select

    from faultmaven.infrastructure.persistence.models import UserModel

    async with get_db_session() as session:
        rows = await session.execute(
            select(UserModel.user_id).where(UserModel.enterprise_id == enterprise_id)
        )
        return [row[0] for row in rows.all()]
