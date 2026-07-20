"""System RBAC role identities — the stable IDs of the seeded system roles.

The ``roles`` / ``permissions`` / ``role_permissions`` tables are populated by
migration ``029_seed_rbac_roles_permissions`` (revision ``e1f2a3b4c5d6``) from
the authority model in :mod:`faultmaven.models.rbac` (``ROLE_PERMISSIONS``). The
system roles are seeded with **fixed, deterministic UUIDs** so that any layer
which must reference a role by identity — most importantly the Cloud-composed
organization-management API, which resolves a :class:`~faultmaven.models.rbac.Role`
to the ``role_id`` that ``organization_members.role_id`` (NOT NULL) requires —
can do so without a database round-trip or a hard-coded query.

``SYSTEM_ROLE_IDS`` is the single runtime source of truth for that mapping. It is
verified against the actual seeded rows by the migration's integration test, so
the constant and the database can never silently drift.

These IDs are frozen: changing them (or adding a new system role) requires a new
migration, not an edit here — the value of a stable identity is that it stays
stable.
"""

from faultmaven.models.rbac import Role

# name → role_id. Keyed by the Role enum (a str enum, so ``SYSTEM_ROLE_IDS["admin"]``
# and ``SYSTEM_ROLE_IDS[Role.ADMIN]`` are equivalent).
SYSTEM_ROLE_IDS: dict[Role, str] = {
    Role.ADMIN: "50551907-a02c-5bf7-9aa4-4a98f3c4eb64",
    Role.MEMBER: "5cb4c3f5-227c-5d73-95a5-d9d2e619ca72",
    Role.VIEWER: "834b74a5-33a7-5248-9fd0-b040c12aef7b",
}

# Reverse mapping: role_id → Role. Lets a caller render a stored ``role_id`` back
# to its role name without a query (e.g. an org-member listing).
ROLE_BY_ID: dict[str, Role] = {rid: role for role, rid in SYSTEM_ROLE_IDS.items()}
