"""Team service — team-membership resolution for KB scoping.

This is the read-side resolution seam the KB retrieval paths consume to build
the team arm of a principal's read scope (``build_kb_scope_filter``): agent
retrieval (``AgentOrchestrationService``) and the KB document inventory route
both call ``list_all_user_team_ids`` on the wired ``team_service``.

Scope (ADR-010 / ADR-013): membership *resolution* lives in the core (this
service + ``ITeamRepository``), config-selected like the tenant provider. Team
*management* (create team, invite/assign members) is the composed Cloud admin
module — it drives the same repository from faultmaven-cloud. The service is
therefore wired only in multi-tenant (Cloud) deployments; standalone leaves it
unwired (``team_service=None``) so team collaboration stays inert. Even when
wired, it degrades safely: a user with no memberships resolves to ``[]`` and KB
scope collapses to ``personal ∪ global``.
"""

from typing import List

from faultmaven.models.interfaces_user import ITeamRepository


class TeamService:
    """Resolve a user's team memberships for KB read-scope construction."""

    def __init__(self, team_repository: ITeamRepository):
        self._team_repository = team_repository

    async def list_all_user_team_ids(self, user_id: str) -> List[str]:
        """Return every team id ``user_id`` belongs to (empty when none).

        Isolation is enforced in the repository, which joins ``team_members``
        through the RLS-tenanted ``teams`` table so cross-organization
        membership fails closed under the ``faultmaven_app`` role.
        """
        return await self._team_repository.list_all_user_team_ids(user_id)
