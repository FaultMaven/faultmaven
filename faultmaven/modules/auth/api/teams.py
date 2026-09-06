"""Team read API — list the caller's teams (ADR-013 §D4).

The frontend needs team *names* (not just ids) to render case share badges and to
populate the share-to-team picker; ``GET /teams`` serves both. Team *management*
(create team, invite/assign members) is the composed Cloud admin module — this
endpoint is read-only.

Cloud-only: ``team_service`` is unwired in standalone (single implicit team), so
the endpoint returns an empty list there. Membership resolution is scoped by the
enterprise RLS binding (``TeamService.list_user_teams`` joins ``team_members``
through the RLS-tenanted ``teams`` table), so a caller only ever sees teams in
their own enterprise — which is also the only place a team may have members
(ADR-017 D4).
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.modules.auth.contracts import UserDTO

router = APIRouter(prefix="/teams", tags=["teams"])
logger = logging.getLogger(__name__)


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


@router.get("", response_model=List[TeamResponse], summary="List My Teams")
@trace("api_list_my_teams")
async def list_my_teams(
    request: Request,
    current_user: UserDTO = Depends(require_authentication),
) -> List[TeamResponse]:
    """List the teams the authenticated user belongs to.

    Read-only; the dashboard uses it to resolve team ids to names (case share
    badges) and to populate the share-to-team picker. Returns an empty list in
    standalone, where team sharing is unwired (``team_service is None``).
    """
    team_service = getattr(request.app.state, "team_service", None)
    if not team_service:
        return []
    teams = await team_service.list_user_teams(current_user.user_id)
    return [
        TeamResponse(
            team_id=t.team_id,
            name=t.name,
            description=t.description,
            enterprise_id=t.enterprise_id,
        )
        for t in teams
    ]
