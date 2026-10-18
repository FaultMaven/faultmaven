"""020_org_permissions_are_management_only

Prunes the organization's permissions to the management axis (#1040, owner
ruling 2026-09-24): the ``cases:*`` and ``evidence:*`` permissions and every
grant of one are deleted, leaving ``org:manage_users`` and
``org:manage_settings``, granted to ``admin`` alone.

Organization roles are the organization's management vocabulary and gate no
data (ADR-017 D5: roles "no longer gate data visibility, which they never
should have"). The baseline nevertheless seeded eight data permissions and
granted them by role — all eight to ``admin``; ``cases:read``,
``cases:write``, ``cases:assign``, ``evidence:read`` and ``evidence:upload`` to
``member``; ``cases:read`` and ``evidence:read`` to ``viewer``. Nothing ever
checked one: the only reader of ``role_permissions`` is organization
management, which asks only for the two ``org:*`` permissions. The map they
were seeded from (``faultmaven.models.rbac.ROLE_PERMISSIONS``) is pruned in the
same change, so the seed and the map agree again.

This is an additive revision, not an amendment of ``001_enterprise_baseline``:
additive revisions already sit on top of the baseline, so an amendment would
miss every database stamped at one of them.

The upgrade
-----------

Deletes every grant of a ``cases`` or ``evidence`` permission from
``role_permissions`` (a non-system role's included), then the eight
``permissions`` rows, matched on ``resource``. The grants go first and
explicitly: SQLite runs the ``ON DELETE CASCADE`` on ``role_permissions`` only
when ``PRAGMA foreign_keys`` is on. Neither table is enrolled in row-level
security (``_TENANT_TABLES`` in the baseline does not list them, and no later
revision enrols them), so no ``row_security`` setting is needed on PostgreSQL.
The same two ``DELETE`` statements run on both dialects.

``downgrade()``
---------------

Re-seeds exactly the baseline's eight ``cases:*`` / ``evidence:*`` permission
rows and its fifteen grants of them to the three system roles, with the
baseline's ids, copied rather than imported so a later edit there cannot move
them. A grant to a non-system role that the upgrade deleted is not restored:
nothing recorded it.

Revision ID: 596f06c52909
Revises: 82bc95a1c02f
Create Date: 2026-10-18 12:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "596f06c52909"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = (
    "82bc95a1c02f"  # pragma: allowlist secret
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The baseline's ``cases:*`` and ``evidence:*`` permission rows:
#: value -> permission_id.
_DATA_PERMISSIONS = {
    "cases:read": "f3adedf0-09df-5151-9490-83da7cff6c34",
    "cases:write": "01fe79a0-a217-5998-9ab8-e7adde82ffde",
    "cases:delete": "91ed1dc0-2eb1-519f-b1e2-a1e93815cb59",
    "cases:assign": "818985d2-a051-5e55-86bb-1ba378835787",
    "cases:close": "5de3208e-7f06-5037-b8d9-8f5f90a0fbdf",
    "evidence:read": "d06d9c54-a00e-58e3-ad38-19dbd32975ee",
    "evidence:upload": "0bd8e72e-a9ef-5ee4-9a67-32b8602c9d94",
    "evidence:delete": "a41fd5f5-f56b-5fb5-82f0-ff9c8f8ef2bf",
}
#: The baseline's grants of them: system role_id -> permission values.
_DATA_GRANTS = {
    # admin
    "50551907-a02c-5bf7-9aa4-4a98f3c4eb64": list(_DATA_PERMISSIONS),
    # member
    "5cb4c3f5-227c-5d73-95a5-d9d2e619ca72": [
        "cases:read",
        "cases:write",
        "cases:assign",
        "evidence:read",
        "evidence:upload",
    ],
    # viewer
    "834b74a5-33a7-5248-9fd0-b040c12aef7b": ["cases:read", "evidence:read"],
}


def upgrade() -> None:
    """Retire the data permissions' grants, then the permissions."""
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT permission_id FROM permissions "
        "WHERE resource IN ('cases', 'evidence'))"
    )
    op.execute("DELETE FROM permissions WHERE resource IN ('cases', 'evidence')")


def downgrade() -> None:
    """Re-seed the baseline's data permissions and their system-role grants."""
    permissions = sa.table(
        "permissions",
        sa.column("permission_id", sa.String),
        sa.column("resource", sa.String),
        sa.column("action", sa.String),
        sa.column("description", sa.Text),
    )
    role_permissions = sa.table(
        "role_permissions",
        sa.column("role_id", sa.String),
        sa.column("permission_id", sa.String),
    )
    op.bulk_insert(
        permissions,
        [
            {
                "permission_id": permission_id,
                "resource": value.split(":", 1)[0],
                "action": value.split(":", 1)[1],
                "description": None,
            }
            for value, permission_id in _DATA_PERMISSIONS.items()
        ],
    )
    op.bulk_insert(
        role_permissions,
        [
            {"role_id": role_id, "permission_id": _DATA_PERMISSIONS[value]}
            for role_id, values in _DATA_GRANTS.items()
            for value in values
        ],
    )
