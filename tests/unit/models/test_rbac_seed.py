"""SYSTEM_ROLE_IDS is internally consistent and covers every Role.

The Cloud org-management API resolves a Role to its seeded ``role_id`` through
this constant, so a missing or duplicated entry would break member management.
(The migration-integration suite separately verifies these IDs match the DB.)
"""

import uuid

import pytest

from faultmaven.models.rbac import Role
from faultmaven.models.rbac_seed import ROLE_BY_ID, SYSTEM_ROLE_IDS

pytestmark = pytest.mark.unit


def test_covers_every_role():
    assert set(SYSTEM_ROLE_IDS) == set(Role)


def test_ids_are_unique_valid_uuids():
    ids = list(SYSTEM_ROLE_IDS.values())
    assert len(set(ids)) == len(ids)
    for value in ids:
        uuid.UUID(value)  # raises if not a valid UUID


def test_role_by_id_is_the_exact_inverse():
    assert ROLE_BY_ID == {rid: role for role, rid in SYSTEM_ROLE_IDS.items()}
    for role, rid in SYSTEM_ROLE_IDS.items():
        assert ROLE_BY_ID[rid] is role


def test_lookup_works_with_plain_role_name():
    # Role is a str enum, so the string value keys the same entry.
    assert SYSTEM_ROLE_IDS["admin"] == SYSTEM_ROLE_IDS[Role.ADMIN]
