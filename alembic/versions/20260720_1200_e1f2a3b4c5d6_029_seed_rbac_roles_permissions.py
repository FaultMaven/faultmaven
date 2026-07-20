"""029_seed_rbac_roles_permissions

Seed the system RBAC roles, permissions, and their grants.

The clean baseline (001) *creates* the ``roles`` / ``permissions`` /
``role_permissions`` tables but never populated them — so despite the schema
existing, there was no role to assign and ``organization_members.role_id``
(NOT NULL, ``ON DELETE RESTRICT``) had no valid target. No org member could be
added and no permission check could pass. This migration fills that gap.

The three organization-scoped system roles (``admin`` / ``member`` / ``viewer``)
and the fourteen permissions are a frozen snapshot of the authority model in
``faultmaven/models/rbac.py`` (``Role`` × ``Permission`` × ``ROLE_PERMISSIONS``).
Role IDs match ``faultmaven/models/rbac_seed.py:SYSTEM_ROLE_IDS`` (fixed,
deterministic UUIDs) so runtime callers — notably the Cloud org-management API —
can map a role name to its ``role_id`` without a query. A migration-integration
test asserts the seeded rows equal both that constant and ``ROLE_PERMISSIONS``,
so the snapshot here can never silently diverge from the live model.

Permissions are stored as ``(resource, action)`` pairs (the enum's ``a:b`` value
split on ``:``), matching how ``user_has_permission`` looks them up.

Applies uniformly to every deployment. In standalone the rows are inert
reference data (local auth derives roles from ``users.dev_roles``, #706); they
light up only in multi-tenant cloud where ``organization_members`` is used.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-20 12:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "d0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# --- Frozen seed snapshot (see module docstring) -----------------------------

# role name -> (role_id, description). IDs mirror rbac_seed.SYSTEM_ROLE_IDS.
_ROLES = {
    "admin": (
        "50551907-a02c-5bf7-9aa4-4a98f3c4eb64",
        "Full access to organization resources.",
    ),
    "member": (
        "5cb4c3f5-227c-5d73-95a5-d9d2e619ca72",
        "Standard investigator access.",
    ),
    "viewer": (
        "834b74a5-33a7-5248-9fd0-b040c12aef7b",
        "Read-only access.",
    ),
}

# permission "resource:action" -> permission_id.
_PERMISSIONS = {
    "cases:read": "f3adedf0-09df-5151-9490-83da7cff6c34",
    "cases:write": "01fe79a0-a217-5998-9ab8-e7adde82ffde",
    "cases:delete": "91ed1dc0-2eb1-519f-b1e2-a1e93815cb59",
    "cases:assign": "818985d2-a051-5e55-86bb-1ba378835787",
    "cases:close": "5de3208e-7f06-5037-b8d9-8f5f90a0fbdf",
    "sessions:read": "f7c7be05-bf80-5504-832d-210827a0e450",
    "sessions:create": "cd3ba768-fd85-5c10-83da-830194251e29",
    "sessions:execute": "7123672b-6cb6-529d-87b1-6c74101c44a3",
    "sessions:manage": "7a9a6bde-3a6a-5086-b5c1-7703d524ce59",
    "evidence:read": "d06d9c54-a00e-58e3-ad38-19dbd32975ee",
    "evidence:upload": "0bd8e72e-a9ef-5ee4-9a67-32b8602c9d94",
    "evidence:delete": "a41fd5f5-f56b-5fb5-82f0-ff9c8f8ef2bf",
    "org:manage_users": "d3a93745-5f12-5622-a596-96e518669e90",
    "org:manage_settings": "31938541-ec6c-526f-a4ee-06e8d1b59252",
}

# role name -> permissions it grants (snapshot of ROLE_PERMISSIONS).
_GRANTS = {
    "admin": list(_PERMISSIONS.keys()),  # all permissions
    "member": [
        "cases:read",
        "cases:write",
        "cases:assign",
        "sessions:read",
        "sessions:create",
        "sessions:execute",
        "sessions:manage",
        "evidence:read",
        "evidence:upload",
    ],
    "viewer": [
        "cases:read",
        "sessions:read",
        "evidence:read",
    ],
}


def _all_role_ids() -> list[str]:
    return [rid for rid, _ in _ROLES.values()]


def upgrade() -> None:
    roles_tbl = sa.table(
        "roles",
        sa.column("role_id", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
        sa.column("scope", sa.String),
        sa.column("is_system_role", sa.Boolean),
    )
    permissions_tbl = sa.table(
        "permissions",
        sa.column("permission_id", sa.String),
        sa.column("resource", sa.String),
        sa.column("action", sa.String),
        sa.column("description", sa.Text),
    )
    role_permissions_tbl = sa.table(
        "role_permissions",
        sa.column("role_id", sa.String),
        sa.column("permission_id", sa.String),
    )

    op.bulk_insert(
        roles_tbl,
        [
            {
                "role_id": role_id,
                "name": name,
                "description": description,
                "scope": "organization",
                "is_system_role": True,
            }
            for name, (role_id, description) in _ROLES.items()
        ],
    )

    op.bulk_insert(
        permissions_tbl,
        [
            {
                "permission_id": permission_id,
                "resource": value.split(":", 1)[0],
                "action": value.split(":", 1)[1],
                "description": None,
            }
            for value, permission_id in _PERMISSIONS.items()
        ],
    )

    op.bulk_insert(
        role_permissions_tbl,
        [
            {
                "role_id": _ROLES[role_name][0],
                "permission_id": _PERMISSIONS[perm_value],
            }
            for role_name, perm_values in _GRANTS.items()
            for perm_value in perm_values
        ],
    )


def downgrade() -> None:
    role_ids = _all_role_ids()
    permission_ids = list(_PERMISSIONS.values())

    role_id_list = ", ".join(f"'{rid}'" for rid in role_ids)
    permission_id_list = ", ".join(f"'{pid}'" for pid in permission_ids)

    # role_permissions first (composite FKs to both roles and permissions), then
    # the parents. Ordered so the RESTRICT FK from organization_members.role_id
    # is never violated — there are no member rows in a pre-multi-tenant deploy.
    op.execute(f"DELETE FROM role_permissions WHERE role_id IN ({role_id_list})")
    op.execute(f"DELETE FROM permissions WHERE permission_id IN ({permission_id_list})")
    op.execute(f"DELETE FROM roles WHERE role_id IN ({role_id_list})")
