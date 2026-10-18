"""Revision 020 on a real PostgreSQL (#1040).

``020_org_permissions_are_management_only`` prunes the RBAC seed to the
management axis: it deletes every grant of a ``cases:*`` or ``evidence:*``
permission, then the eight permission rows, and rewrites the three system
roles' descriptions to say what each manages. Its downgrade restores the
baseline's descriptions, eight rows and fifteen system-role grants.

On PostgreSQL the foreign keys are enforced, so ``role_permissions``' ``ON
DELETE CASCADE`` runs whatever the revision does first; this test pins the
rows each direction leaves on the dialect Cloud runs. The SQLite half, which
also pins the order of the two ``DELETE`` statements (there the cascade runs
only under ``PRAGMA foreign_keys``) and renders the PostgreSQL script, is
``TestOrgPermissionsAreManagementOnlyRevision`` in
``tests/integration/test_alembic_migrations.py``.
"""

from __future__ import annotations

import os
from collections import Counter

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.security.conftest import alembic_on, database_of_its_own

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

#: ``020_org_permissions_are_management_only``.
_REVISION = "596f06c52909"  # pragma: allowlist secret
#: ``019_verified_by_is_the_display_name``, the revision 020 parents onto.
_PARENT_REVISION = "82bc95a1c02f"  # pragma: allowlist secret

_ADMIN = "50551907-a02c-5bf7-9aa4-4a98f3c4eb64"
_MEMBER = "5cb4c3f5-227c-5d73-95a5-d9d2e619ca72"
_VIEWER = "834b74a5-33a7-5248-9fd0-b040c12aef7b"

#: The baseline's data permission rows: permission_id -> (resource, action).
_DATA_PERMISSIONS = {
    "f3adedf0-09df-5151-9490-83da7cff6c34": ("cases", "read"),
    "01fe79a0-a217-5998-9ab8-e7adde82ffde": ("cases", "write"),
    "91ed1dc0-2eb1-519f-b1e2-a1e93815cb59": ("cases", "delete"),
    "818985d2-a051-5e55-86bb-1ba378835787": ("cases", "assign"),
    "5de3208e-7f06-5037-b8d9-8f5f90a0fbdf": ("cases", "close"),
    "d06d9c54-a00e-58e3-ad38-19dbd32975ee": ("evidence", "read"),
    "0bd8e72e-a9ef-5ee4-9a67-32b8602c9d94": ("evidence", "upload"),
    "a41fd5f5-f56b-5fb5-82f0-ff9c8f8ef2bf": ("evidence", "delete"),
}
_ORG_MANAGE_USERS = "d3a93745-5f12-5622-a596-96e518669e90"
_ORG_MANAGE_SETTINGS = "31938541-ec6c-526f-a4ee-06e8d1b59252"

_BASELINE_DESCRIPTIONS = [
    ("admin", "Full access to organization resources."),
    ("member", "Standard investigator access."),
    ("viewer", "Read-only access."),
]
_MANAGEMENT_DESCRIPTIONS = [
    ("admin", "Manages the organization's roster and settings."),
    ("member", "Belongs to the organization; manages nothing."),
    ("viewer", "Belongs to the organization; manages nothing."),
]


async def _query(url: str, sql: str) -> list:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return [tuple(row) for row in (await conn.execute(text(sql))).fetchall()]
    finally:
        await engine.dispose()


async def _tables(url: str) -> tuple[list, list, list]:
    """``permissions``, ``role_permissions`` and ``roles``, every column."""
    return (
        await _query(url, "SELECT * FROM permissions ORDER BY permission_id"),
        await _query(
            url, "SELECT * FROM role_permissions ORDER BY role_id, permission_id"
        ),
        await _query(url, "SELECT * FROM roles ORDER BY role_id"),
    )


async def _data_rows(url: str) -> tuple[list, list]:
    """The ``cases`` / ``evidence`` permission rows and every grant of one."""
    return (
        await _query(
            url,
            "SELECT permission_id, resource, action, description FROM permissions "
            "WHERE resource IN ('cases', 'evidence') ORDER BY permission_id",
        ),
        await _query(
            url,
            "SELECT rp.role_id, rp.permission_id FROM role_permissions rp "
            "JOIN permissions p ON p.permission_id = rp.permission_id "
            "WHERE p.resource IN ('cases', 'evidence') "
            "ORDER BY rp.role_id, rp.permission_id",
        ),
    )


async def _descriptions(url: str) -> list:
    return await _query(url, "SELECT name, description FROM roles ORDER BY name")


async def test_020_prunes_to_the_management_axis_and_steps_back():
    superuser_url = os.environ["DATABASE_URL"]
    async with database_of_its_own(superuser_url, "fm_020_org_perms") as url:
        result = alembic_on(url, f"upgrade {_PARENT_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        parent = await _tables(url)

        # Positive control: the parent carries the baseline's eight data rows,
        # their fifteen grants, and the baseline's descriptions.
        data_permissions, data_grants = await _data_rows(url)
        assert {
            permission_id: (resource, action, description)
            for permission_id, resource, action, description in data_permissions
        } == {
            permission_id: (resource, action, None)
            for permission_id, (resource, action) in _DATA_PERMISSIONS.items()
        }
        assert Counter(role_id for role_id, _ in data_grants) == {
            _ADMIN: 8,
            _MEMBER: 5,
            _VIEWER: 2,
        }
        assert await _descriptions(url) == _BASELINE_DESCRIPTIONS
        kept_permissions = await _query(
            url,
            "SELECT * FROM permissions WHERE resource NOT IN ('cases', 'evidence') "
            "ORDER BY permission_id",
        )
        assert [row[0] for row in kept_permissions] == sorted(
            [_ORG_MANAGE_USERS, _ORG_MANAGE_SETTINGS]
        )

        result = alembic_on(url, f"upgrade {_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await _data_rows(url) == ([], [])
        assert await _query(
            url,
            "SELECT count(*) FROM role_permissions WHERE permission_id NOT IN "
            "(SELECT permission_id FROM permissions)",
        ) == [(0,)]
        permissions, grants, _ = await _tables(url)
        assert permissions == kept_permissions
        assert grants == sorted(
            [(_ADMIN, _ORG_MANAGE_USERS), (_ADMIN, _ORG_MANAGE_SETTINGS)]
        )
        assert await _descriptions(url) == _MANAGEMENT_DESCRIPTIONS

        result = alembic_on(url, f"downgrade {_PARENT_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await _tables(url) == parent
        assert await _descriptions(url) == _BASELINE_DESCRIPTIONS
