"""Role-Based Access Control (RBAC) Models (TASK-017)

Purpose: Define roles, permissions, and their mappings for authorization.

This module provides:
- Role enum for organization-level roles (admin, member, viewer)
- Permission enum for granular access control
- Role-permission mapping for easy lookup
- Helper functions for permission checking

Design Reference: TASK-017 JWT Authentication & Authorization Middleware
"""

from enum import Enum


class Role(str, Enum):
    """Organization-level roles.

    Roles define the level of access a user has within an organization.
    Each role maps to a set of permissions.

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
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
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


def get_permissions_for_role(role: Role) -> frozenset[Permission]:
    """Get all permissions granted by a role.

    Args:
        role: Role to get permissions for

    Returns:
        Set of permissions granted by the role
    """
    return ROLE_PERMISSIONS.get(role, frozenset())


def get_permissions_for_roles(roles: list[str]) -> set[Permission]:
    """Get combined permissions for multiple roles.

    This aggregates permissions from all specified roles.
    Useful when a user has multiple roles.

    Args:
        roles: List of role names (strings)

    Returns:
        Combined set of permissions from all roles
    """
    permissions: set[Permission] = set()
    for role_name in roles:
        try:
            role = Role(role_name)
            permissions.update(ROLE_PERMISSIONS.get(role, frozenset()))
        except ValueError:
            # Unknown role, skip
            continue
    return permissions


def has_permission(user_permissions: list[str], required_permission: str) -> bool:
    """Check if user has a specific permission.

    Args:
        user_permissions: List of permission strings the user has
        required_permission: Permission string to check for

    Returns:
        True if user has the required permission
    """
    return required_permission in user_permissions


def has_any_permission(
    user_permissions: list[str], required_permissions: list[str]
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
    user_permissions: list[str], required_permissions: list[str]
) -> bool:
    """Check if user has all of the specified permissions.

    Args:
        user_permissions: List of permission strings the user has
        required_permissions: List of permissions to check for

    Returns:
        True if user has all of the required permissions
    """
    return all(perm in user_permissions for perm in required_permissions)


def has_role(user_roles: list[str], required_role: str) -> bool:
    """Check if user has a specific role.

    Args:
        user_roles: List of role names the user has
        required_role: Role name to check for

    Returns:
        True if user has the required role
    """
    return required_role in user_roles


def is_admin(user_roles: list[str]) -> bool:
    """Check if user is an admin.

    Args:
        user_roles: List of role names the user has

    Returns:
        True if user has admin role
    """
    return Role.ADMIN.value in user_roles
