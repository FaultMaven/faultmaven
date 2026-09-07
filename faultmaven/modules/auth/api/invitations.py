"""Invitations — the invitee's half of the consent (ADR-017 D4).

Three routes, all addressed by invitation id and all answerable by exactly one
account: the invitee. They live apart from ``modules/auth/api/teams`` for that
reason — everything there is addressed by *team* id and gated on membership or
on the team-admin role, and an invitee is neither.

**A pending invitation grants nothing.** It is an offer, and membership is
created by the accept and by nothing else. That is the consent D4 asks for, and
it is what keeps a stranger from pulling a colleague into a team's view without
a word.

**Anything not addressed to me is 404.** Another account's invitation, an
invitation in another enterprise, and an id that names nothing are one answer.
A 403 would confirm the id exists, which would make invitation ids probeable —
and the id is the whole of what an accept needs.

**Expiry is lazy.** Nothing sweeps the table: an offer past its deadline keeps
``status='pending'`` in the database until a read or an accept notices, and the
one that notices is the one that stamps it. Every verb here settles it through
the same reader, so accepting, declining and listing agree about an elapsed
offer whatever order they happen in — 410, not "no longer open".
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, Path, status

from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.modules.auth.api.teams import (
    InvitationResponse,
    TeamContext,
    TeamResponse,
    _team_response,
    invitation_response,
    require_invitation_context,
)

router = APIRouter(prefix="/invitations", tags=["teams"])
logger = logging.getLogger(__name__)


@router.get(
    "",
    response_model=List[InvitationResponse],
    summary="List Invitations Addressed To Me",
)
@trace("api_list_my_invitations")
async def list_my_invitations(
    context: TeamContext = Depends(require_invitation_context),
) -> List[InvitationResponse]:
    """The live offers addressed to the caller.

    Addressed two ways, because an offer may predate the account: by
    ``invited_user_id`` once it has resolved, and by the caller's own address
    while it has not — which is how somebody invited before they signed up sees
    the invitation waiting for them on their first visit.

    Pending only. An offer past its deadline is stamped ``expired`` on the way
    through and left out, so the list is what a person can actually act on.

    The team names are resolved in **one** query for the whole page. The invitee
    is not a member yet, so ``GET /teams`` cannot tell them what they are being
    invited to; without the names the list is a column of opaque ids, and
    fetching them one row at a time made the cost of opening a mailbox linear in
    how many offers were in it.
    """
    invitations = await context.service.list_my_invitations(
        enterprise_id=context.enterprise_id,
        user_id=context.user.user_id,
        email=context.user.email,
    )
    names = await context.service.name_teams(
        enterprise_id=context.enterprise_id,
        team_ids=[invitation.team_id for invitation in invitations],
    )
    return [
        invitation_response(invitation, team_name=names.get(invitation.team_id))
        for invitation in invitations
    ]


@router.post(
    "/{invitation_id}/accept",
    response_model=TeamResponse,
    summary="Accept An Invitation",
)
@trace("api_accept_invitation")
async def accept_invitation(
    invitation_id: str = Path(..., description="Invitation ID"),
    context: TeamContext = Depends(require_invitation_context),
) -> TeamResponse:
    """Consent: join the team this invitation names.

    The only way a membership is created on this surface. An admin cannot add a
    member; they can only offer.

    410 when the offer has run out — distinct from the 404 an offer that was
    never yours gets, because the caller was entitled to that invitation and is
    entitled to know it lapsed rather than to be told it never existed.
    """
    team = await context.service.accept_invitation(
        enterprise_id=context.enterprise_id,
        invitation_id=invitation_id,
        user_id=context.user.user_id,
        email=context.user.email,
    )
    return _team_response(team)


@router.delete(
    "/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Decline An Invitation",
)
@trace("api_decline_invitation")
async def decline_invitation(
    invitation_id: str = Path(..., description="Invitation ID"),
    context: TeamContext = Depends(require_invitation_context),
) -> None:
    """Refuse an offer.

    Recorded rather than deleted: the team admin's list is the record of who was
    offered a place and what they said, and a row that vanished would read as an
    offer never made. An offer that had already run out answers 410 and is
    recorded as ``expired``, not as a decline nobody made.
    """
    await context.service.decline_invitation(
        enterprise_id=context.enterprise_id,
        invitation_id=invitation_id,
        user_id=context.user.user_id,
        email=context.user.email,
    )
