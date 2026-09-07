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
one that notices is the one that stamps it. So this list never shows an offer
that has run out, and accepting one answers 410 rather than joining a team on
the strength of a fortnight-old invitation.
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, Path, Request, status

from faultmaven.api.v1.auth_dependencies import (
    require_actor_enterprise,
    require_authentication,
)
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.modules.auth.api.teams import (
    REASON_NO_INVITATIONS,
    InvitationResponse,
    TeamResponse,
    invitation_response,
    require_team_service,
)
from faultmaven.modules.auth.contracts import UserDTO

router = APIRouter(prefix="/invitations", tags=["teams"])
logger = logging.getLogger(__name__)


@router.get(
    "",
    response_model=List[InvitationResponse],
    summary="List Invitations Addressed To Me",
)
@trace("api_list_my_invitations")
async def list_my_invitations(
    request: Request,
    current_user: UserDTO = Depends(require_authentication),
) -> List[InvitationResponse]:
    """The live offers addressed to the caller.

    Addressed two ways, because an offer may predate the account: by
    ``invited_user_id`` once it has resolved, and by the caller's own address
    while it has not — which is how somebody invited before they signed up sees
    the invitation waiting for them on their first visit.

    Pending only. An offer past its deadline is stamped ``expired`` on the way
    through and left out, so the list is what a person can actually act on.
    """
    team_service = require_team_service(request, REASON_NO_INVITATIONS)
    enterprise_id = require_actor_enterprise(current_user)
    invitations = await team_service.list_my_invitations(
        enterprise_id=enterprise_id,
        user_id=current_user.user_id,
        email=current_user.email,
    )

    # The team's name, resolved per offer. The invitee is not a member yet, so
    # ``GET /teams`` cannot tell them what they are being invited to; without
    # this the list is a column of opaque ids. The lookup is enterprise-scoped
    # by RLS and by the service's own predicate, so it can only ever name a team
    # in the caller's own enterprise.
    rendered = []
    for invitation in invitations:
        team_name = await team_service.get_team_name(invitation.team_id)
        rendered.append(invitation_response(invitation, team_name=team_name))
    return rendered


@router.post(
    "/{invitation_id}/accept",
    response_model=TeamResponse,
    summary="Accept An Invitation",
)
@trace("api_accept_invitation")
async def accept_invitation(
    request: Request,
    invitation_id: str = Path(..., description="Invitation ID"),
    current_user: UserDTO = Depends(require_authentication),
) -> TeamResponse:
    """Consent: join the team this invitation names.

    The only way a membership is created on this surface. An admin cannot add a
    member; they can only offer.

    410 when the offer has run out — distinct from the 404 an offer that was
    never yours gets, because the caller was entitled to that invitation and is
    entitled to know it lapsed rather than to be told it never existed.
    """
    team_service = require_team_service(request, REASON_NO_INVITATIONS)
    enterprise_id = require_actor_enterprise(current_user)
    team = await team_service.accept_invitation(
        enterprise_id=enterprise_id,
        invitation_id=invitation_id,
        user=current_user,
    )
    return TeamResponse(
        team_id=team.team_id,
        name=team.name,
        description=team.description,
        enterprise_id=team.enterprise_id,
    )


@router.delete(
    "/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Decline An Invitation",
)
@trace("api_decline_invitation")
async def decline_invitation(
    request: Request,
    invitation_id: str = Path(..., description="Invitation ID"),
    current_user: UserDTO = Depends(require_authentication),
) -> None:
    """Refuse an offer.

    Recorded rather than deleted: the team admin's list is the record of who was
    offered a place and what they said, and a row that vanished would read as an
    offer never made.
    """
    team_service = require_team_service(request, REASON_NO_INVITATIONS)
    enterprise_id = require_actor_enterprise(current_user)
    await team_service.decline_invitation(
        enterprise_id=enterprise_id,
        invitation_id=invitation_id,
        user=current_user,
    )
