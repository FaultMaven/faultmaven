"""Teams — the sharing unit, formed by consent (ADR-013 §D4, ADR-017 D4).

Everything under ``/teams``: listing the caller's teams, creating one, reading a
roster, offering someone a place, and leaving. The invitee's own half of the
consent — listing, accepting and declining the offers addressed to *them* — is
``modules/auth/api/invitations``, because those routes are addressed by
invitation id and answerable by exactly one account, which is a different
audience from everything here.

**Any account may create a team and is its team admin** (D4). Creating one
grants nothing over anybody else: a team of one sees what its one member already
saw. Members join only by accepting an invitation, which is the consent that
keeps a stranger from pulling a colleague into a team's view without a word.

**Enterprise-scoped, in the read shape.** A team in another enterprise answers
404 to everyone — never 403, because a 403 confirms that the id names something
(ADR-017 D2). The predicate is applied three times over on purpose: PostgreSQL
RLS scopes ``teams`` and (by one hop) ``team_members`` on the bound enterprise,
``TeamService`` compares the anchors explicitly so the rule also holds where
there is no RLS, and ``ITeamRepository.add_member`` refuses a member anchored
elsewhere.

**Cloud-only, and refused rather than hidden in standalone.** ``team_service``
is unwired under ``TENANT_PROVIDER=single`` (ADR-017 D8: one enterprise, one
default team, one user — there is nobody to invite), and these routes answer 403
with a reason slug there. The routes are still *mounted*, and the published
contract therefore describes one surface for every deployment: it is the same
signal ``GET /teams``, the KB inventory route and ``GET /meta/capabilities``
already read (``app.state.team_service is None``), and it keeps
``docs/reference/api/openapi.json`` a function of the code rather than of a
deployment's ``TENANT_PROVIDER``.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Path, Request, status
from pydantic import BaseModel, EmailStr, Field, field_validator

from faultmaven.api.v1.auth_dependencies import (
    require_actor_enterprise,
    require_authentication,
)
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.auth.exceptions import TeamOperationRefused

router = APIRouter(prefix="/teams", tags=["teams"])
logger = logging.getLogger(__name__)

#: What a standalone deployment is told when it reaches a team-management or an
#: invitation route. Two slugs rather than one because they answer two different
#: questions and a client may want to hide two different pieces of UI.
REASON_NO_TEAMS = "single_tenant_has_no_teams"
REASON_NO_INVITATIONS = "single_tenant_has_no_invitations"


class TeamResponse(BaseModel):
    """A team the caller belongs to."""

    team_id: str
    name: str
    description: Optional[str] = None
    #: The enterprise the team belongs to. A team is parented by the enterprise
    #: and may span organizations (ADR-017 D4), so there is no organization
    #: field here — not even an optional one, because a tolerated old field is
    #: what keeps a frontend reading it.
    enterprise_id: str


class TeamCreateRequest(BaseModel):
    """What it takes to create a team: a name, and optionally a description.

    ``max_length`` matches ``teams.name``'s ``VARCHAR(200)`` exactly. A wider
    request field does not accept more — it defers the refusal to PostgreSQL,
    which answers ``StringDataRightTruncation`` and a 500 where a 422 naming the
    field belongs. (``description`` is ``TEXT``; the cap here is a request-size
    bound, not a column one.)
    """

    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("name", mode="after")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        """Mirror of the DB ``teams_name_not_empty`` CHECK.

        ``min_length=1`` accepts a single space; the database's
        ``LENGTH(TRIM(name)) > 0`` does not. Same rule, two layers — neither
        bypassable independently.
        """
        if not value.strip():
            raise ValueError("name must not be whitespace-only")
        return value


class TeamMemberResponse(BaseModel):
    """One row of a team's roster."""

    user_id: str
    team_id: str
    #: ``admin`` for whoever created the team (and anyone later promoted),
    #: ``member`` for everyone who joined by accepting an invitation. Nullable
    #: because the column is: a membership row seeded by the standalone
    #: bootstrap carries no role.
    team_role: Optional[str] = None
    joined_at: datetime


class InvitationCreateRequest(BaseModel):
    """The address being offered a place on the team."""

    email: EmailStr


class InvitationResponse(BaseModel):
    """An offer to join a team, and what became of it.

    ``invited_user_id`` is ``None`` while the address has no account in this
    enterprise. That is a legitimate steady state, not a pending write: an
    address with no account can be invited, and the offer resolves if and when
    that address signs up **into this enterprise** (ADR-017 D4). One that signs
    up elsewhere never resolves, and the offer expires where it was issued.
    """

    invitation_id: str
    team_id: str
    #: The team's name, so the invitee's list is readable without a second call.
    #: Nullable for the same reason the team lookup can come back empty — a team
    #: soft-deleted after the offer was made.
    team_name: Optional[str] = None
    enterprise_id: str
    email: str
    invited_user_id: Optional[str] = None
    invited_by: Optional[str] = None
    #: ``pending`` | ``accepted`` | ``revoked`` | ``expired``.
    status: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    accepted_at: Optional[datetime] = None
    #: Who ended the offer, and when. Published because the design claim —
    #: "the row tells a decline from a withdrawal apart" — is only true if a
    #: client can see it: ``status`` is ``revoked`` either way, and comparing
    #: ``revoked_by`` against ``invited_by`` is the whole of the difference.
    #: Both are ``None`` until the offer is ended.
    revoked_by: Optional[str] = None
    revoked_at: Optional[datetime] = None


def _team_response(team) -> TeamResponse:
    return TeamResponse(
        team_id=team.team_id,
        name=team.name,
        description=team.description,
        enterprise_id=team.enterprise_id,
    )


def invitation_response(invitation, team_name: Optional[str] = None):
    """Render an invitation for the wire. Shared with the invitee-side router."""
    return InvitationResponse(
        invitation_id=invitation.invitation_id,
        team_id=invitation.team_id,
        team_name=team_name,
        enterprise_id=invitation.enterprise_id,
        email=invitation.email,
        invited_user_id=invitation.invited_user_id,
        invited_by=invitation.invited_by,
        status=invitation.status.value,
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
        accepted_at=invitation.accepted_at,
        revoked_by=invitation.revoked_by,
        revoked_at=invitation.revoked_at,
    )


@dataclass(frozen=True)
class TeamContext:
    """Everything a consent route needs before it can do anything.

    The wired service, the enterprise the request is bound to, and who is
    asking. Resolved once, as a dependency, rather than as a two-line preamble
    repeated in nine handlers: a preamble is something a tenth route can
    forget, and forgetting it here means either a 500 in standalone (the
    service is ``None``) or an unscoped query (no enterprise predicate). As a
    dependency it is structural — a handler that does not declare it has no
    service to call.
    """

    service: Any
    enterprise_id: str
    user: UserDTO


def require_team_service(request: Request, reason: str = REASON_NO_TEAMS):
    """The wired ``TeamService``, or a 403 naming why there is none.

    ``app.state.team_service`` is the deployment's own answer to "is team
    collaboration live here?" — it is ``None`` under the single-tenant provider
    and wired under the multi-tenant one — and it is the signal ``GET /teams``,
    the KB inventory route and ``GET /meta/capabilities`` already read. Using it
    here too means there is one fact, read in one way, rather than a second
    ``TENANT_PROVIDER`` test that could disagree with it.

    It is also what a **failed wiring** looks like: ``TeamService`` refuses to
    construct without its repositories, so a composition failure leaves this
    ``None`` and the surface says "not available here" rather than answering
    404 for every team in the deployment.

    Raises the domain refusal rather than an ``HTTPException`` for the reason
    every other refusal on this surface does: the reason slug has to reach the
    client, and ``api.exception_handlers.http_exception_handler`` flattens a
    dict ``detail`` to its human message.
    """
    team_service = getattr(request.app.state, "team_service", None)
    if team_service is None:
        raise TeamOperationRefused(
            reason=reason,
            message=(
                "This deployment has a single enterprise and one default "
                "team; teams are not formed by consent here."
            ),
            status_code=status.HTTP_403_FORBIDDEN,
        )
    return team_service


def team_context_dependency(reason: str):
    """Build the router-wide dependency, carrying the right refusal slug.

    Two slugs, because they answer two different questions and a client may
    want to hide two different pieces of UI: ``single_tenant_has_no_teams`` on
    ``/teams``, ``single_tenant_has_no_invitations`` on ``/invitations``.
    """

    async def resolve(
        request: Request,
        current_user: UserDTO = Depends(require_authentication),
    ) -> TeamContext:
        service = require_team_service(request, reason)
        return TeamContext(
            service=service,
            enterprise_id=require_actor_enterprise(current_user),
            user=current_user,
        )

    return resolve


#: The dependency every route under ``/teams`` except ``GET /teams`` declares.
require_team_context = team_context_dependency(REASON_NO_TEAMS)

#: The same, for the invitation routes — on both routers, since the team admin's
#: half of the consent lives under ``/teams`` and the invitee's under
#: ``/invitations``, and both are the same feature to a client hiding UI.
require_invitation_context = team_context_dependency(REASON_NO_INVITATIONS)


@router.get("", response_model=List[TeamResponse], summary="List My Teams")
@trace("api_list_my_teams")
async def list_my_teams(
    request: Request,
    current_user: UserDTO = Depends(require_authentication),
) -> List[TeamResponse]:
    """List the teams the authenticated user belongs to.

    Read-only; the dashboard uses it to resolve team ids to names (case share
    badges) and to populate the share-to-team picker. Returns an empty list in
    standalone, where team sharing is unwired (``team_service is None``) — an
    empty list, not the 403 the management routes answer, because "which teams
    am I in?" has a true and useful answer there and it is "none".
    """
    team_service = getattr(request.app.state, "team_service", None)
    if not team_service:
        return []
    teams = await team_service.list_user_teams(current_user.user_id)
    return [_team_response(team) for team in teams]


@router.post(
    "",
    response_model=TeamResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create A Team",
)
@trace("api_create_team")
async def create_team(
    body: TeamCreateRequest,
    context: TeamContext = Depends(require_team_context),
) -> TeamResponse:
    """Create a team in the caller's enterprise, with the caller as its admin.

    Any authenticated account may do this (ADR-017 D4) — there is no role to
    hold and nothing to be granted. The team is parented by the enterprise the
    request is bound to and references no organization, so it may later span
    cost centres.

    409 when a live team in the enterprise already has that name. A retired team
    does not hold its name: the uniqueness rule is partial on
    ``deleted_at IS NULL``.
    """
    team = await context.service.create_team(
        enterprise_id=context.enterprise_id,
        creator_user_id=context.user.user_id,
        name=body.name,
        description=body.description,
    )
    return _team_response(team)


@router.get(
    "/{team_id}/members",
    response_model=List[TeamMemberResponse],
    summary="List Team Members",
)
@trace("api_list_team_members")
async def list_team_members(
    team_id: str = Path(..., description="Team ID"),
    context: TeamContext = Depends(require_team_context),
) -> List[TeamMemberResponse]:
    """The roster, readable by any member of the team.

    A team the caller is not in is 404, whether it is in their enterprise or
    not: who is on a team is exactly what a team shares, so it is readable by
    the people who agreed to share it and by nobody else.
    """
    members = await context.service.list_members(
        enterprise_id=context.enterprise_id,
        team_id=team_id,
        user_id=context.user.user_id,
    )
    return [
        TeamMemberResponse(
            user_id=member.user_id,
            team_id=member.team_id,
            team_role=member.team_role,
            joined_at=member.joined_at,
        )
        for member in members
    ]


@router.delete(
    "/{team_id}/members/me",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Leave A Team",
)
@trace("api_leave_team")
async def leave_team(
    team_id: str = Path(..., description="Team ID"),
    context: TeamContext = Depends(require_team_context),
) -> None:
    """Leave a team. The last member out takes the team with them.

    ``/members/me`` rather than ``/members/{user_id}``: consent forms a team and
    only the member's own withdrawal unforms their part of it. There is no
    "remove somebody else" on this surface at all.

    Refused (409) when the leaver is the team's only admin and other members
    remain — those members would be left sharing into a team nobody can
    administer. The sole member of a team strands nobody, so their leaving
    retires it, and its pending invitations are revoked with it.
    """
    await context.service.leave_team(
        enterprise_id=context.enterprise_id,
        team_id=team_id,
        user_id=context.user.user_id,
    )


@router.post(
    "/{team_id}/invitations",
    response_model=InvitationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Invite An Address To A Team",
)
@trace("api_create_team_invitation")
async def create_team_invitation(
    body: InvitationCreateRequest,
    team_id: str = Path(..., description="Team ID"),
    context: TeamContext = Depends(require_invitation_context),
) -> InvitationResponse:
    """Offer an address a place on the team. Team admin only.

    The rule is by **domain**, so nothing here enumerates accounts (ADR-017 D3):
    an address is refused for being outside the enterprise's domain before any
    account is looked up, and an address whose account is anchored to another
    enterprise is refused with exactly the same status and body as one that has
    no account at all.

    Idempotent: inviting an address that already has a live offer on this team
    returns that offer rather than minting a second one — including when a
    concurrent invite won the race.
    """
    invitation = await context.service.invite(
        enterprise_id=context.enterprise_id,
        team_id=team_id,
        actor_user_id=context.user.user_id,
        email=str(body.email),
    )
    return invitation_response(invitation)


@router.get(
    "/{team_id}/invitations",
    response_model=List[InvitationResponse],
    summary="List A Team's Invitations",
)
@trace("api_list_team_invitations")
async def list_team_invitations(
    team_id: str = Path(..., description="Team ID"),
    context: TeamContext = Depends(require_invitation_context),
) -> List[InvitationResponse]:
    """Every offer this team has issued, and what became of it. Admin only.

    Not filtered by status: the record of who was offered a place, and whether
    they accepted, declined, were withdrawn or ran out of time, is the thing an
    admin needs. Offers past their deadline are reported — and stamped —
    ``expired`` here, which is what keeps lazy expiry indistinguishable from a
    swept table at every surface a person sees.
    """
    invitations = await context.service.list_team_invitations(
        enterprise_id=context.enterprise_id,
        team_id=team_id,
        user_id=context.user.user_id,
    )
    return [invitation_response(invitation) for invitation in invitations]


@router.delete(
    "/{team_id}/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke A Team Invitation",
)
@trace("api_revoke_team_invitation")
async def revoke_team_invitation(
    team_id: str = Path(..., description="Team ID"),
    invitation_id: str = Path(..., description="Invitation ID"),
    context: TeamContext = Depends(require_invitation_context),
) -> None:
    """Withdraw an offer. Team admin only.

    410 for an offer that has already run out: ``revoked_by`` is the record of
    who ended it, and writing a withdrawal nobody performed would put a decision
    in the record that no person made.
    """
    await context.service.revoke_invitation(
        enterprise_id=context.enterprise_id,
        team_id=team_id,
        invitation_id=invitation_id,
        actor_user_id=context.user.user_id,
    )
