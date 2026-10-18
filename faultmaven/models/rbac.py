"""The organization's role vocabulary: who may manage an organization.

An organization BILLS (ADR-017 D2); it does not isolate and it does not share.
Its roles are therefore the organization's *management* vocabulary and nothing
else: they say who may manage the organization's roster and its settings, and
they gate no data. Case, evidence and knowledge visibility is the enterprise's
(RLS on ``enterprise_id``) and the team's (consent, ``resource_shares``), and
ADR-017 D5 is explicit that roles "no longer gate data visibility, which they
never should have".

So the mapping below carries exactly the management axis (#1040, owner ruling
2026-09-24): ``admin`` holds ``org:manage_users`` and ``org:manage_settings``,
``member`` and ``viewer`` hold nothing. The live reader is Cloud's organization
management, which asks ``IOrganizationRepository.user_has_permission`` for one
of those two per request; the ``permissions`` / ``role_permissions`` rows it
joins are seeded from this map (``001_enterprise_baseline``, pruned to it by
``020_org_permissions_are_management_only``).

The deployment-scoped operator role ``platform_admin`` is a different axis and
lives in :mod:`faultmaven.modules.auth.domain.models.rbac`.
"""

from enum import Enum
from typing import FrozenSet, List, Set


class Role(str, Enum):
    """Organization-level roles: the organization's management vocabulary.

    A role says what its holder may manage in ONE organization. It gates no
    data (ADR-017 D5): which cases, evidence and runbooks an account sees is
    decided by its enterprise and its teams, whatever role it holds.

    These are tenant-bounded, and this enum is load-bearing beyond permissions:
    ``UserService.assign_role`` validates against it, so a role added here
    becomes assignable through ``POST /admin/users/{id}/roles``. The
    cross-tenant operator role ``platform_admin``
    (``modules.auth.domain.models.rbac.PLATFORM_ADMIN_ROLE``) is therefore
    deliberately NOT a member — adding it here would let the user-management
    API mint deployment operators. Grant it via
    the ``fm-promote-platform-admin`` command instead.

    Attributes:
        ADMIN: Manages the organization's roster and settings
        MEMBER: Belongs to the organization; manages nothing
        VIEWER: Belongs to the organization; manages nothing
    """

    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class Permission(str, Enum):
    """What an organization role may manage, as ``resource:action``.

    Only the management axis: managing the organization's members and its
    settings. There is deliberately no case, evidence or session permission
    here — those would make an organization role a data gate, which ADR-017 D5
    forbids (#1040).
    """

    ORG_MANAGE_USERS = "org:manage_users"
    ORG_MANAGE_SETTINGS = "org:manage_settings"


#: Role -> the permissions it grants. Every role is listed: ``member`` and
#: ``viewer`` granting nothing is the statement, not an omission.
ROLE_PERMISSIONS: dict[Role, FrozenSet[Permission]] = {
    Role.ADMIN: frozenset(
        [
            Permission.ORG_MANAGE_USERS,
            Permission.ORG_MANAGE_SETTINGS,
        ]
    ),
    Role.MEMBER: frozenset(),
    Role.VIEWER: frozenset(),
}


def get_permissions_for_role(role: Role) -> FrozenSet[Permission]:
    """Get all permissions granted by a role.

    Args:
        role: Role to get permissions for

    Returns:
        Set of permissions granted by the role
    """
    return ROLE_PERMISSIONS.get(role, frozenset())


def get_permissions_for_roles(roles: List[str]) -> Set[Permission]:
    """Get combined permissions for multiple roles.

    This aggregates permissions from all specified roles.
    Useful when a user has multiple roles.

    Args:
        roles: List of role names (strings)

    Returns:
        Combined set of permissions from all roles
    """
    permissions: Set[Permission] = set()
    for role_name in roles:
        try:
            role = Role(role_name)
            permissions.update(ROLE_PERMISSIONS.get(role, frozenset()))
        except ValueError:
            # Unknown role, skip
            continue
    return permissions
