"""Role-Based Access Control (RBAC) Models (TASK-017)

Purpose: Define roles, permissions, and their mappings for authorization.

This module provides:
- Role enum for organization-level roles (admin, member, viewer)
- PLATFORM_ADMIN_ROLE, the cross-tenant operator role (distinct from Role.ADMIN)
- Permission enum for granular access control
- Role-permission mapping for easy lookup
- Helper functions for permission checking

Design Reference: TASK-017 JWT Authentication & Authorization Middleware
"""

from enum import Enum
from typing import FrozenSet, List, Set

# The cross-tenant OPERATOR role (ADR-012 D9).
#
# Deliberately NOT a member of ``Role`` below: the two are orthogonal axes and
# conflating them is the bug D9 exists to fix.
#
#   Role.ADMIN ("admin")      — organization-scoped. Full access to ONE org's
#                               resources; tenant-bounded, never crosses tenants.
#   PLATFORM_ADMIN_ROLE       — deployment-scoped. The operator who runs the
#                               deployment: cross-tenant case listing, user
#                               administration, LLM configuration, global KB.
#
# Keeping it out of ``Role`` also keeps it out of ``ROLE_PERMISSIONS``, so
# holding it grants no org permissions by itself — an operator's in-org
# authority still comes from Role.ADMIN. A standalone deployment's single
# account legitimately holds both.
PLATFORM_ADMIN_ROLE = "platform_admin"

# The roles an operator account holds. An operator needs authority inside its
# own organization too — `platform_admin` grants none, by construction — so
# every path that provisions one grants the org role alongside it. Defined here
# rather than at a provisioning site so the bootstrap seed, `create_user.py`,
# and `fm-promote-platform-admin` cannot answer "does platform_admin imply
# admin?" differently and produce operators with unequal in-org authority.
PLATFORM_ADMIN_ROLE_SET = ["user", "admin", PLATFORM_ADMIN_ROLE]


class Role(str, Enum):
    """Organization-level roles.

    Roles define the level of access a user has within an organization.
    Each role maps to a set of permissions.

    These are tenant-bounded. For the cross-tenant operator role, see
    :data:`PLATFORM_ADMIN_ROLE` — it is intentionally not a member here.

    Attributes:
        ADMIN: Full access to organization resources
        MEMBER: Standard investigator access
        VIEWER: Read-only access
    """

    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class Permission(str, Enum):
    """Granular permissions for fine-grained access control.

    Permissions follow the format: resource:action
    where resource is the entity type and action is the operation.

    Resource Categories:
    - cases: Case management operations
    - sessions: Investigation session operations
    - evidence: Evidence artifact operations
    - org: Organization management operations
    """

    # Cases
    CASES_READ = "cases:read"
    CASES_WRITE = "cases:write"
    CASES_DELETE = "cases:delete"
    CASES_ASSIGN = "cases:assign"
    CASES_CLOSE = "cases:close"

    # Sessions
    SESSIONS_READ = "sessions:read"
    SESSIONS_CREATE = "sessions:create"
    SESSIONS_EXECUTE = "sessions:execute"
    SESSIONS_MANAGE = "sessions:manage"  # pause, resume, complete

    # Evidence
    EVIDENCE_READ = "evidence:read"
    EVIDENCE_UPLOAD = "evidence:upload"
    EVIDENCE_DELETE = "evidence:delete"

    # Organization
    ORG_MANAGE_USERS = "org:manage_users"
    ORG_MANAGE_SETTINGS = "org:manage_settings"


# Role-Permission mapping
# Each role grants a set of permissions
ROLE_PERMISSIONS: dict[Role, FrozenSet[Permission]] = {
    Role.ADMIN: frozenset(
        [
            # All permissions
            Permission.CASES_READ,
            Permission.CASES_WRITE,
            Permission.CASES_DELETE,
            Permission.CASES_ASSIGN,
            Permission.CASES_CLOSE,
            Permission.SESSIONS_READ,
            Permission.SESSIONS_CREATE,
            Permission.SESSIONS_EXECUTE,
            Permission.SESSIONS_MANAGE,
            Permission.EVIDENCE_READ,
            Permission.EVIDENCE_UPLOAD,
            Permission.EVIDENCE_DELETE,
            Permission.ORG_MANAGE_USERS,
            Permission.ORG_MANAGE_SETTINGS,
        ]
    ),
    Role.MEMBER: frozenset(
        [
            # Standard investigator permissions
            Permission.CASES_READ,
            Permission.CASES_WRITE,
            Permission.CASES_ASSIGN,
            Permission.SESSIONS_READ,
            Permission.SESSIONS_CREATE,
            Permission.SESSIONS_EXECUTE,
            Permission.SESSIONS_MANAGE,
            Permission.EVIDENCE_READ,
            Permission.EVIDENCE_UPLOAD,
        ]
    ),
    Role.VIEWER: frozenset(
        [
            # Read-only permissions
            Permission.CASES_READ,
            Permission.SESSIONS_READ,
            Permission.EVIDENCE_READ,
        ]
    ),
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


def has_permission(user_permissions: List[str], required_permission: str) -> bool:
    """Check if user has a specific permission.

    Args:
        user_permissions: List of permission strings the user has
        required_permission: Permission string to check for

    Returns:
        True if user has the required permission
    """
    return required_permission in user_permissions


def has_any_permission(
    user_permissions: List[str], required_permissions: List[str]
) -> bool:
    """Check if user has any of the specified permissions.

    Args:
        user_permissions: List of permission strings the user has
        required_permissions: List of permissions to check for

    Returns:
        True if user has at least one of the required permissions
    """
    return any(perm in user_permissions for perm in required_permissions)


def has_all_permissions(
    user_permissions: List[str], required_permissions: List[str]
) -> bool:
    """Check if user has all of the specified permissions.

    Args:
        user_permissions: List of permission strings the user has
        required_permissions: List of permissions to check for

    Returns:
        True if user has all of the required permissions
    """
    return all(perm in user_permissions for perm in required_permissions)


def has_role(user_roles: List[str], required_role: str) -> bool:
    """Check if user has a specific role.

    Args:
        user_roles: List of role names the user has
        required_role: Role name to check for

    Returns:
        True if user has the required role
    """
    return required_role in user_roles
