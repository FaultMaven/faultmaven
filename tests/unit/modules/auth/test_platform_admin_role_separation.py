"""The operator role must stay disjoint from the org-scoped role vocabulary.

ADR-012 D9 splits one ambiguous ``admin`` role string into two axes:
``platform_admin`` (deployment operator, cross-tenant) and ``Role.ADMIN``
(organization-scoped, tenant-bounded). The separation is enforced by absence —
``platform_admin`` is simply not a member of the ``Role`` enum — which is
exactly the kind of invariant that a well-meaning future edit erases without
anything failing. These tests make that edit fail loudly.
"""

import pytest

from faultmaven.models import rbac as models_rbac
from faultmaven.modules.auth.contracts import (
    PLATFORM_ADMIN_ROLE,
    PLATFORM_ADMIN_ROLE_SET,
)
from faultmaven.modules.auth.domain.models import rbac as auth_rbac

# Both live copies of the role vocabulary. `models.rbac` is the one
# `UserService.assign_role` validates against and that migration 029 seeds;
# `modules.auth...rbac` is the one the auth service uses. A role added to
# either must satisfy the same invariant.
ROLE_ENUMS = [models_rbac.Role, auth_rbac.Role]


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize("role_enum", ROLE_ENUMS)
def test_platform_admin_is_not_an_org_role(role_enum):
    """`platform_admin` must not be assignable as an organization role.

    `UserService.assign_role` accepts any value in this enum, so membership
    here would let the user-management API mint cross-tenant operators.
    """
    assert PLATFORM_ADMIN_ROLE not in [r.value for r in role_enum]


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize("role_enum", ROLE_ENUMS)
def test_org_admin_is_not_the_operator_role(role_enum):
    """The org admin role must keep its own distinct string."""
    assert role_enum.ADMIN.value == "admin"
    assert role_enum.ADMIN.value != PLATFORM_ADMIN_ROLE


@pytest.mark.unit
@pytest.mark.security
def test_platform_admin_grants_no_org_permissions_by_itself():
    """Holding only the operator role confers no organization permissions.

    Cross-tenant reach and in-org authority are separate grants; an operator's
    org permissions come from the `admin` role it is provisioned alongside.
    """
    assert auth_rbac.get_permissions_for_roles([PLATFORM_ADMIN_ROLE]) == set()
    assert models_rbac.get_permissions_for_roles([PLATFORM_ADMIN_ROLE]) == set()


@pytest.mark.unit
def test_operator_role_set_carries_both_axes():
    """The provisioned operator holds the operator role AND the org admin role.

    Every provisioning path (bootstrap seed, `create_user.py`,
    `fm-promote-platform-admin`) consumes this one list, so they cannot
    produce operators with unequal in-org authority.
    """
    assert PLATFORM_ADMIN_ROLE in PLATFORM_ADMIN_ROLE_SET
    assert "admin" in PLATFORM_ADMIN_ROLE_SET
    assert "user" in PLATFORM_ADMIN_ROLE_SET
    # And that set does grant the full org permission set, via `admin`.
    assert models_rbac.get_permissions_for_roles(PLATFORM_ADMIN_ROLE_SET) == set(
        models_rbac.ROLE_PERMISSIONS[models_rbac.Role.ADMIN]
    )
