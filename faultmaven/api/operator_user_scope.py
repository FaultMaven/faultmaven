"""Tenant confinement for operator user administration (#1318, ADR-012 D7/D9).

The predicate the operator user-administration routes resolve their target
through: a ``platform_admin`` whose request is bound to enterprise A administers
A's accounts and no others. It lives outside any one route module for the reason
``operator_grants`` and ``operator_audit`` do — ``/api/v1/admin/users*`` and the
two ``/api/v1/auth/users*`` operator routes must resolve identically, and a route
added later has to inherit the decision rather than re-derive it.

**The enterprise, not the organization** (ADR-017 D1/D2). The organization is a
billing target and grants nothing about data, so confining an operator to one
would be confining them by who pays rather than by who may be seen. Membership in
the enterprise is ``users.enterprise_id`` — the account's single isolation
anchor — so the predicate needs no roster table and no join.

Why a predicate rather than a grant
-----------------------------------
The case surface next door reaches another tenant through a break-glass grant
(``operator_grants.authorize_content_read``). That mechanism is **case-scoped by
construction** and does not fit a user: ``operator_access_grants.target_case_id``
is ``NOT NULL``, ``find_live_grant`` keys on it, and
``bind_grant_enterprise_scope`` rebinds RLS to the enterprise the grant names.
Reaching a *user* through it would mean either minting a grant that names an
unrelated case — putting a false justification into an append-only audit row —
or making the case id optional, which is a second grant model wearing the first
one's schema. So cross-enterprise user administration is **refused** here rather
than granted. The audited break-glass path for this surface (ADR-012 D9's
option A) is a later change, tracked on #1318; this module is deliberately not
half of it, and writes no audit row.

Where the predicate applies
---------------------------
Under ``TENANT_PROVIDER=multi`` the target's ``users.enterprise_id`` must equal
the operator's own. Under ``single`` the deployment *is* one enterprise, so the
predicate is satisfied by construction and no read is needed. Keying on tenancy
rather than on ``is_cloud`` is what ``admin_cases`` does, for the same reason:
``multi`` cannot boot outside cloud today, and if that changed, confining is the
direction to be wrong in.

**There is no database backstop under this one.** ``users`` is deliberately
outside RLS — every enterprise's accounts share one table, and the login path has
to reach a row before any tenant is bound — so unlike the ``organization_members``
read this replaces, the application predicate here is the whole of the
confinement. It must not be relaxed on the belief that a policy is underneath it.

404, not 403
------------
Out-of-tenant and absent share one status and one body on every id-addressed
route, so no caller can tell them apart — the rule
``docs/architecture/security/rbac.md`` states under "Tenant-Scoped Resolution",
and the reason :func:`user_not_found` exists rather than a per-route message. A
403 reserved for "you may not see this" would confirm that the id names a real
account in another tenant, which is the existence oracle the 404 avoids. The one
403 is :func:`OperatorUserScope.tenant`'s, raised by
``require_actor_enterprise`` when the *caller* carries no usable enterprise:
that refusal does not depend on the requested id, so it is not an oracle.
"""

import logging
from typing import Optional

from fastapi import HTTPException, status
from starlette.requests import Request

from faultmaven.api.v1.auth_dependencies import require_actor_enterprise
from faultmaven.providers.tenancy.factory import (
    BUILTIN_MULTI,
    requested_tenant_provider,
)

logger = logging.getLogger(__name__)

#: Refused when the account store the predicate needs is absent. A 503, not a
#: pass: without it there is no way to establish that the target is in the
#: operator's tenant, so the routes must be unreachable rather than unconfined.
NO_ACCOUNT_STORE_MSG = (
    "Account store not available; operator user administration was refused "
    "rather than served without its tenant predicate."
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
    whether a specific target is inside the operator's enterprise
    (:meth:`admits`) or which accounts are (:meth:`member_ids`).

    Deliberately **not** a dependency that refuses on its own. The account store
    is consulted only where the predicate bites, so a single-tenant deployment
    (and every route test of one) never has to provide one, and the fail-closed
    503 below cannot be triggered by a deployment the predicate does not apply
    to.
    """

    def __init__(self, users):
        self._users = users

    @property
    def confined(self) -> bool:
        """Whether more than one tenant exists to confine the operator to."""
        return requested_tenant_provider() == BUILTIN_MULTI

    def tenant(self, operator) -> Optional[str]:
        """The enterprise this operator administers within, or ``None``.

        ``None`` means "the whole deployment", which is only ever the
        single-tenant answer. Raises 403 (``require_actor_enterprise``) when
        confinement applies and the caller carries no usable enterprise —
        holding the cross-tenant role does not exempt an operator from acting
        inside the enterprise their request is bound to.
        """
        if not self.confined:
            return None
        return require_actor_enterprise(operator)

    def _repository(self):
        if self._users is None:
            logger.error("operator_user_scope_account_store_missing")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=NO_ACCOUNT_STORE_MSG,
            )
        return self._users

    async def admits(self, operator, user_id: Optional[str]) -> bool:
        """Whether this operator may administer ``user_id``.

        ``False`` is the answer for an out-of-enterprise account AND for an id
        that names nobody: the read cannot distinguish them, and the routes must
        not either. Callers turn it into :func:`user_not_found`.
        """
        enterprise_id = self.tenant(operator)
        if enterprise_id is None:
            return True
        if not user_id:
            # "We do not know who the target is" is not a reason to admit one.
            # Checked here rather than left to the query, which would answer
            # "no such account" for the same reason one query later.
            return False
        target = await self._repository().get(user_id)
        if target is None:
            return False
        # Equality against the operator's own enterprise, not truthiness of the
        # target's: an account whose anchor is missing must be refused, not
        # admitted for want of a value to compare.
        return getattr(target, "enterprise_id", None) == enterprise_id

    async def member_ids(self, operator) -> Optional[frozenset[str]]:
        """The account ids this operator may administer, or ``None`` for all.

        ``None`` is the single-tenant answer — the deployment is the tenant —
        and is what the listing routes pass through as "do not filter". It is
        never the answer under ``multi``: an operator whose enterprise holds no
        accounts resolves to the empty set, which filters everything out, so the
        two cannot be confused.
        """
        enterprise_id = self.tenant(operator)
        if enterprise_id is None:
            return None
        return await self._repository().list_enterprise_member_ids(enterprise_id)


async def get_operator_user_scope(request: Request) -> OperatorUserScope:
    """Resolve this request's operator user scope (Composition Root).

    The account store is reached through ``app.state.user_store``, whose
    repository is the one thing that can answer "which enterprise is this
    account anchored to?". ``None`` when the store is unwired, which
    :meth:`OperatorUserScope._repository` turns into a 503 at the point the
    predicate is actually needed.
    """
    store = getattr(request.app.state, "user_store", None)
    return OperatorUserScope(users=getattr(store, "user_repository", None))
