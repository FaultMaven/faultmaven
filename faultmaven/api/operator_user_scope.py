"""Tenant confinement for operator user administration (#1318, ADR-012 D7/D9).

The predicate the operator user-administration routes resolve their target
through: a ``platform_admin`` whose request is bound to organization A
administers A's users and no others. It lives outside any one route module for
the reason ``operator_grants`` and ``operator_audit`` do — ``/api/v1/admin/users*``
and the two ``/api/v1/auth/users*`` operator routes must resolve identically,
and a route added later has to inherit the decision rather than re-derive it.

Why a predicate rather than a grant
-----------------------------------
The case surface next door reaches another tenant through a break-glass grant
(``operator_grants.authorize_content_read``). That mechanism is **case-scoped by
construction** and does not fit a user: ``operator_access_grants.target_case_id``
is ``NOT NULL`` (migration 036), ``find_live_grant`` keys on it, and
``bind_grant_org_scope`` rebinds RLS to the organization the grant names.
Reaching a *user* through it would mean either minting a grant that names an
unrelated case — putting a false justification into an append-only audit row —
or making the case id optional, which is a second grant model wearing the first
one's schema. So cross-tenant user administration is **refused** here rather
than granted. The audited break-glass path for this surface (ADR-012 D9's
option A) is a later change, tracked on #1318; this module is deliberately not
half of it, and writes no audit row.

Where the predicate applies
---------------------------
Under ``TENANT_PROVIDER=multi`` the target must hold an ``organization_members``
row in the operator's own organization. Under ``single`` the deployment *is* one
organization — ``organization_members`` is not populated there at all (migration
029's rows are inert reference data, #706; see
``providers.tenancy.permissions.SingleTenantPermissionResolver``, which reads no
table for exactly this reason) — so the predicate is satisfied by construction
and no membership row is consulted. Keying on tenancy rather than on
``is_cloud`` is what ``admin_cases`` does, for the same reason: ``multi`` cannot
boot outside cloud today, and if that changed, confining is the direction to be
wrong in.

The membership read is doubly guarded under ``multi``: ``organization_members``
is RLS-tenanted (migration 018) and the session is bound to the operator's
organization, so a lookup naming any other organization returns nothing even if
the application predicate were removed.

404, not 403
------------
Out-of-tenant and absent share one status and one body on every id-addressed
route, so no caller can tell them apart — the rule
``docs/architecture/security/rbac.md`` states under "Tenant-Scoped Resolution",
and the reason :func:`user_not_found` exists rather than a per-route message. A
403 reserved for "you may not see this" would confirm that the id names a real
account in another tenant, which is the existence oracle the 404 avoids. The one
403 is :func:`OperatorUserScope.tenant`'s, raised by
``require_actor_organization`` when the *caller* carries no usable organization:
that refusal does not depend on the requested id, so it is not an oracle.
"""

import logging
from typing import Optional

from fastapi import HTTPException, status
from starlette.requests import Request

from faultmaven.api.v1.auth_dependencies import require_actor_organization
from faultmaven.models.interfaces_user import IOrganizationRepository
from faultmaven.providers.tenancy.factory import (
    BUILTIN_MULTI,
    requested_tenant_provider,
)

logger = logging.getLogger(__name__)

#: Refused when the membership store the predicate needs is absent. A 503, not a
#: pass: without it there is no way to establish that the target is in the
#: operator's tenant, so the routes must be unreachable rather than unconfined.
NO_MEMBERSHIP_STORE_MSG = (
    "Organization membership store not available; operator user administration "
    "was refused rather than served without its tenant predicate."
)


def user_not_found(user_id: str) -> HTTPException:
    """The single "this user is not here" answer of the operator user surface.

    One constructor rather than a literal per call site: the whole value of
    answering 404 for an out-of-tenant target is that it is **indistinguishable**
    from the answer for an absent one, and two hand-written messages drift. The
    text matches ``str(NotFoundError("User", user_id))``, which is what the
    routes that already translate that exception emit for a genuinely absent id.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"User not found: {user_id}"
    )


class OperatorUserScope:
    """One request's authority to administer user accounts.

    Resolved per request from the composition root, then asked — by the route —
    whether a specific target is inside the operator's tenant
    (:meth:`admits`) or which users are (:meth:`member_ids`).

    Deliberately **not** a dependency that refuses on its own. The membership
    store is consulted only where the predicate bites, so a single-tenant
    deployment (and every route test of one) never has to provide it, and the
    fail-closed 503 below cannot be triggered by a deployment the predicate does
    not apply to.
    """

    def __init__(self, organizations: Optional[IOrganizationRepository]):
        self._organizations = organizations

    @property
    def confined(self) -> bool:
        """Whether more than one tenant exists to confine the operator to."""
        return requested_tenant_provider() == BUILTIN_MULTI

    def tenant(self, operator) -> Optional[str]:
        """The organization this operator administers within, or ``None``.

        ``None`` means "the whole deployment", which is only ever the
        single-tenant answer. Raises 403 (``require_actor_organization``) when
        confinement applies and the caller carries no usable organization —
        holding the cross-tenant role does not exempt an operator from acting
        inside the organization their request is bound to.
        """
        if not self.confined:
            return None
        return require_actor_organization(operator)

    def _repository(self) -> IOrganizationRepository:
        if self._organizations is None:
            logger.error("operator_user_scope_membership_store_missing")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=NO_MEMBERSHIP_STORE_MSG,
            )
        return self._organizations

    async def admits(self, operator, user_id: Optional[str]) -> bool:
        """Whether this operator may administer ``user_id``.

        ``False`` is the answer for an out-of-tenant user AND for an id that
        names nobody: the membership read cannot distinguish them, and the
        routes must not either. Callers turn it into :func:`user_not_found`.
        """
        organization_id = self.tenant(operator)
        if organization_id is None:
            return True
        if not user_id:
            # "We do not know who the target is" is not a reason to admit one.
            # Checked here rather than left to the query, which would answer
            # "no such member" for the same reason one query later.
            return False
        role_id = await self._repository().get_member_role(
            organization_id=organization_id, user_id=user_id
        )
        # `is not None`, not truthiness: a member row with a falsy role_id is
        # still a member, and reading it as "not a member" would be a refusal
        # for the wrong reason — the inverse of the fail-open shape, but still
        # a predicate that does not mean what it says.
        return role_id is not None

    async def member_ids(self, operator) -> Optional[frozenset[str]]:
        """The user ids this operator may administer, or ``None`` for all of them.

        ``None`` is the single-tenant answer — the deployment is the tenant — and
        is what the listing routes pass through as "do not filter". It is never
        the answer under ``multi``: an operator with no members resolves to the
        empty set, which filters everything out, so the two cannot be confused.
        """
        organization_id = self.tenant(operator)
        if organization_id is None:
            return None
        members = await self._repository().list_organization_members(organization_id)
        return frozenset(member.user_id for member in members)


async def get_operator_user_scope(request: Request) -> OperatorUserScope:
    """Resolve this request's operator user scope (Composition Root)."""
    return OperatorUserScope(
        organizations=getattr(request.app.state, "organization_repository", None)
    )
