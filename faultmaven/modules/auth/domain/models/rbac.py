"""Auth-module RBAC: the operator axis, beside the organization's vocabulary.

The organization's role vocabulary — ``Role``, ``Permission``,
``ROLE_PERMISSIONS`` and the helpers that expand them — has one definition and
one door, :mod:`faultmaven.models.rbac`, which faultmaven-cloud imports
directly and from which the RBAC seed is written (#1040 item 4). It is the
organization's management vocabulary only and gates no data (ADR-017 D5).

This module is not a second door to it. It holds what is genuinely auth-module
knowledge: the deployment-scoped operator role, which deliberately is not part
of that vocabulary, and the role-list conventions built on it (the base
marker, the operator grant set, what a demotion takes away, and how a roleless
account is reported). It reaches the organization's module under a private
name (``_org_rbac``) only to spell two of those conventions in the enum's own
values, so no name of that vocabulary is an attribute of this module.
``contracts.py`` re-exports the operator constants from here.
"""

from typing import List, Sequence

from faultmaven.models import rbac as _org_rbac

# The cross-tenant OPERATOR role (ADR-012 D9).
#
# Deliberately NOT a member of ``Role``: the two are orthogonal axes and
# conflating them is the bug D9 exists to fix.
#
#   Role.ADMIN ("admin")      — organization-scoped. Manages ONE organization's
#                               roster and settings and gates no data (ADR-017
#                               D5); tenant-bounded, never crosses tenants.
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
#: The org-scoped role a promotion grants alongside the operator role, as a
#: plain string. Named so the demotion path can say "keep the org admin" in the
#: same vocabulary the flag is named in, without reaching past this module for
#: the enum.
ORG_ADMIN_ROLE = _org_rbac.Role.ADMIN.value

PLATFORM_ADMIN_ROLE_SET = [BASE_USER_ROLE, ORG_ADMIN_ROLE, PLATFORM_ADMIN_ROLE]

# What a promotion actually *adds* on top of an ordinary account, and therefore
# what a demotion has to take away for the two to be inverses (#1040 item 3).
#
# Derived from the set above rather than restated, because the asymmetry this
# fixes came from restating it: `fm-demote-platform-admin` removed only
# `platform_admin` while promotion granted the org `admin` too, so
# promote-then-demote left an account holding the org-management role it never
# had.
#
# `BASE_USER_ROLE` is excluded because a demotion must leave a usable account,
# not an account with an empty role list.
OPERATOR_GRANTED_ROLES = [r for r in PLATFORM_ADMIN_ROLE_SET if r != BASE_USER_ROLE]

#: The role an account holding none is reported and filtered under.
ROLELESS_ACCOUNT_ROLE = _org_rbac.Role.MEMBER.value


def effective_roles(roles: Sequence[str]) -> List[str]:
    """The roles an account is reported and filtered under: its own, or
    ``[ROLELESS_ACCOUNT_ROLE]`` when it holds none.

    The one statement of that rule. The account list's role filter states it
    in SQL too (``user_repository``), naming the same constant.
    """
    return list(roles) if roles else [ROLELESS_ACCOUNT_ROLE]


__all__ = [
    "BASE_USER_ROLE",
    "OPERATOR_GRANTED_ROLES",
    "ORG_ADMIN_ROLE",
    "PLATFORM_ADMIN_ROLE",
    "PLATFORM_ADMIN_ROLE_SET",
    "ROLELESS_ACCOUNT_ROLE",
    "effective_roles",
]
