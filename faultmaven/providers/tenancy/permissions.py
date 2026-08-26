"""Org-scoped permission resolution, behind the tenancy seam (#1163, Phase 0).

Resolves *what a caller may do inside an organization* — the org axis of
ADR-012 D9. The deployment axis (``platform_admin``, from ``users.dev_roles``
via the ``roles`` claim) is a different question with a different reader
(``require_platform_admin``) and is deliberately not answered here.

**Nothing calls this yet.** Phase 0 of
``docs/architecture/security/org-permission-enforcement.md`` builds the
machinery and enforces nothing: no token generator mints a ``permissions``
claim, so putting ``require_permission`` on a route today denies every caller,
platform admin included. ``tests/unit/modules/auth/
test_permission_enforcement_is_unwired.py`` pins that surface as empty so
Phase 0 cannot become Phase 2 by accident.

**Resolved per request, never minted into a token.** That is Decision 1 of the
design, and the reason is the shape of the bugs this codebase keeps finding
rather than latency: membership (#874) and role (#1042) were both verified at
login only, and both had to be closed by pairing the write with a revocation
watermark. A minted ``permissions`` claim would be a third login-time answer
needing the same pairing, on the one claim that decides authorization directly.

**The mode comes from the built ``TenantProvider``**, not from a second read of
``TENANT_PROVIDER``: two independent selectors are two things that can
disagree, and the disagreement would be an authorization one.
"""

from abc import ABC, abstractmethod
from typing import FrozenSet, Optional

from faultmaven.models.interfaces_user import IOrganizationRepository
from faultmaven.models.rbac import ROLE_PERMISSIONS, Permission, Role
from faultmaven.models.rbac_seed import ROLE_BY_ID
from faultmaven.providers.tenancy.base import TenantProvider

#: What the standalone account holds on the org axis.
#:
#: Derived from the authority model rather than restated as a literal — a
#: restated copy is how ``PLATFORM_ADMIN_ROLE_SET`` and
#: ``fm-demote-platform-admin`` came apart (#1040 item 3). The single standalone
#: account is legitimately its organization's admin, so this is admin's set; its
#: *operator* authority is the other axis and is not expressed here.
STANDALONE_PERMISSIONS: FrozenSet[Permission] = ROLE_PERMISSIONS[Role.ADMIN]


class PermissionResolver(ABC):
    """Resolves the permissions a user holds in an organization."""

    @abstractmethod
    async def resolve(
        self, user_id: Optional[str], organization_id: Optional[str]
    ) -> FrozenSet[Permission]:
        """Return the permissions ``user_id`` holds in ``organization_id``.

        Returns an empty set — never ``None`` — when the answer is "none".
        Callers test membership (``perm in result``), and an ``if result:``
        over a ``None`` would read "no permissions" and "not resolved" the same
        way, which is the fail-open shape this project has hit before.
        """

    async def has(
        self,
        user_id: Optional[str],
        organization_id: Optional[str],
        permission: Permission,
    ) -> bool:
        """Whether one permission is held. Fails closed on an unresolvable caller."""
        return permission in await self.resolve(user_id, organization_id)


class SingleTenantPermissionResolver(PermissionResolver):
    """Standalone: the fixed set for the single account.

    Reads no table. ``organization_members`` exists in the schema but standalone
    deployment does not populate it (migration 029's rows are inert reference
    data there, #706), so resolving from it would deny the only account the
    deployment has.
    """

    async def resolve(
        self, user_id: Optional[str], organization_id: Optional[str]
    ) -> FrozenSet[Permission]:
        return STANDALONE_PERMISSIONS


class MultiTenantPermissionResolver(PermissionResolver):
    """Cloud: the caller's ``organization_members`` role, expanded live."""

    def __init__(self, organization_repository: IOrganizationRepository):
        self._organizations = organization_repository

    async def resolve(
        self, user_id: Optional[str], organization_id: Optional[str]
    ) -> FrozenSet[Permission]:
        # A missing id is "we do not know who is asking", which is not a reason
        # to grant anything. Checked explicitly because the repository would
        # answer a ``None`` lookup with "no such member" — the same answer for a
        # different reason, and one query later.
        if not user_id or not organization_id:
            return frozenset()

        role_id = await self._organizations.get_member_role(
            organization_id=organization_id, user_id=user_id
        )
        if role_id is None:
            return frozenset()

        # A role_id outside the seeded system set (a future custom role, or a
        # row written by something that does not use SYSTEM_ROLE_IDS) grants
        # nothing here rather than falling back to a default tier.
        role = ROLE_BY_ID.get(role_id)
        if role is None:
            return frozenset()

        return ROLE_PERMISSIONS.get(role, frozenset())


async def create_permission_resolver(
    tenant_provider: TenantProvider,
    organization_repository: IOrganizationRepository,
) -> PermissionResolver:
    """Build the resolver matching the deployment's tenancy mode.

    The mode is read off ``tenant_provider`` — the object the factory in
    :mod:`faultmaven.providers.tenancy.factory` already built and already failed
    closed on — so there is exactly one place that decides which tenancy this
    deployment is.
    """
    if await tenant_provider.is_multi_tenant():
        return MultiTenantPermissionResolver(organization_repository)
    return SingleTenantPermissionResolver()
