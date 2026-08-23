"""Auth-module RBAC: the operator role, over the shared org role vocabulary.

There used to be two full copies of the role vocabulary — this module and
:mod:`faultmaven.models.rbac` — each defining its own ``Role``, ``Permission``,
``ROLE_PERMISSIONS`` and helpers. ``test_platform_admin_role_separation.py``
parametrised over both to keep them honest, which is a tripwire for drift, not a
cure for it: two enums that must agree will eventually not, and the failure mode
is a role that grants different permissions depending on which import the caller
reached for.

So the org-scoped vocabulary now has **one** definition, in
:mod:`faultmaven.models.rbac` — the copy migration 029 seeds from and that
``rbac_seed.SYSTEM_ROLE_IDS`` maps to stable ids — and this module re-exports it
alongside what is genuinely auth-module knowledge: the deployment-scoped operator
role, which deliberately is not part of that vocabulary (#1040 item 4).

Import either module and get the same objects. This one stays the auth module's
door because ``contracts.py`` re-exports the operator constants from here, and
because the operator/org split is the thing this module exists to state.
"""

from faultmaven.models.rbac import (
    ROLE_PERMISSIONS,
    Permission,
    Role,
    get_permissions_for_role,
    get_permissions_for_roles,
    has_all_permissions,
    has_any_permission,
    has_permission,
    has_role,
)

# The cross-tenant OPERATOR role (ADR-012 D9).
#
# Deliberately NOT a member of ``Role``: the two are orthogonal axes and
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

# The base marker every account carries from registration and JIT SSO
# provisioning. It grants nothing (``get_permissions_for_roles(["user"])`` is
# empty — it is not a member of ``Role``); it exists so an account's role list is
# never empty. Named here because the demotion path has to be able to say
# "remove what the promotion granted, but not this".
BASE_USER_ROLE = "user"

# The roles an operator account holds. An operator needs authority inside its
# own organization too — `platform_admin` grants none, by construction — so
# every path that provisions one grants the org role alongside it. Defined here
# rather than at a provisioning site so the bootstrap seed, `create_user.py`,
# and `fm-promote-platform-admin` cannot answer "does platform_admin imply
# admin?" differently and produce operators with unequal in-org authority.
PLATFORM_ADMIN_ROLE_SET = [BASE_USER_ROLE, Role.ADMIN.value, PLATFORM_ADMIN_ROLE]

# What a promotion actually *adds* on top of an ordinary account, and therefore
# what a demotion has to take away for the two to be inverses (#1040 item 3).
#
# Derived from the set above rather than restated, because the asymmetry this
# fixes came from restating it: `fm-demote-platform-admin` removed only
# `platform_admin` while promotion granted the org `admin` too, so
# promote-then-demote left an account holding org authority it never had. That
# was latent while org roles enforced nothing and becomes real the moment they
# do.
#
# `BASE_USER_ROLE` is excluded because a demotion must leave a usable account,
# not an account with an empty role list.
OPERATOR_GRANTED_ROLES = [r for r in PLATFORM_ADMIN_ROLE_SET if r != BASE_USER_ROLE]

__all__ = [
    "ROLE_PERMISSIONS",
    "BASE_USER_ROLE",
    "OPERATOR_GRANTED_ROLES",
    "PLATFORM_ADMIN_ROLE",
    "PLATFORM_ADMIN_ROLE_SET",
    "Permission",
    "Role",
    "get_permissions_for_role",
    "get_permissions_for_roles",
    "has_all_permissions",
    "has_any_permission",
    "has_permission",
    "has_role",
]
