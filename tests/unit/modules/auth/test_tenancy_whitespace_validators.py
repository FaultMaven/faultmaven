"""Cross-layer whitespace-rejection validators — tenancy domain.

Cat #10/#11 audit follow-up, Item 1. Pins each Pydantic validator
against the matching DB CHECK constraint on the auth-tenancy tables.
Pydantic ``min_length=1`` accepts a single space; the DB
``LENGTH(TRIM(col)) > 0`` CHECK rejects it. Without the validator,
the two layers disagree and a domain object Pydantic accepts
IntegrityError's at the persist boundary.

Mirrored pairs covered here:
    Organization.name <-> organizations_name_not_empty
    Team.name         <-> teams_name_not_empty
    Enterprise.name   <-> enterprises_name_not_empty
    Role.scope        <-> roles_scope_check  (enum, not whitespace)

The slug fields (organizations.slug, enterprises.slug) use
``LENGTH(slug) > 0`` (no TRIM) on the DB side, so only ``min_length=1``
is mirrored — whitespace-only slugs pass both layers by design.

Both legacy (``faultmaven.models.interfaces_user``) and new
(``faultmaven.modules.auth.domain.models.organization``) Pydantic
families are covered — they shadow each other and must stay in sync.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from faultmaven.models.interfaces_user import (
    Enterprise,
    EnterprisePlanTier,
)
from faultmaven.models.interfaces_user import Organization as LegacyOrganization
from faultmaven.models.interfaces_user import Role as LegacyRole
from faultmaven.models.interfaces_user import RoleScope as LegacyRoleScope
from faultmaven.models.interfaces_user import Team as LegacyTeam
from faultmaven.modules.auth.domain.models.organization import (
    Organization,
    Role,
    RoleScope,
    Team,
)


def _now() -> datetime:
    return datetime.now(UTC)


# Both Pydantic families must stay in sync — see module docstring.
ORG_MODELS = pytest.mark.parametrize(
    "Model", [Organization, LegacyOrganization], ids=["new", "legacy"]
)
TEAM_MODELS = pytest.mark.parametrize(
    "Model", [Team, LegacyTeam], ids=["new", "legacy"]
)
ROLE_MODELS = pytest.mark.parametrize(
    ("role_cls", "scope_enum"),
    [(Role, RoleScope), (LegacyRole, LegacyRoleScope)],
    ids=["new", "legacy"],
)
WHITESPACE_VALUES = pytest.mark.parametrize(
    "bad_value", ["", " ", "  ", "\t", "\n", "  \t\n"]
)


# =============================================================================
# Organization.name (mirror of organizations_name_not_empty)
# =============================================================================


def _org_kwargs(**overrides):
    base = dict(
        organization_id="org-1",
        name="Acme Corp",
        slug="acme",
        created_at=_now(),
        updated_at=_now(),
    )
    base.update(overrides)
    return base


@ORG_MODELS
@WHITESPACE_VALUES
def test_organization_name_rejects_empty_or_whitespace(Model, bad_value: str):
    with pytest.raises(ValidationError):
        Model(**_org_kwargs(name=bad_value))


@ORG_MODELS
def test_organization_name_accepts_normal_value(Model):
    org = Model(**_org_kwargs(name="Real Org"))
    assert org.name == "Real Org"


# =============================================================================
# Organization.slug (mirror of organizations_slug_not_empty — LENGTH(slug) > 0)
# =============================================================================


@ORG_MODELS
def test_organization_slug_rejects_empty_string(Model):
    with pytest.raises(ValidationError):
        Model(**_org_kwargs(slug=""))


@ORG_MODELS
def test_organization_slug_allows_whitespace_to_match_db_check(Model):
    """DB ``organizations_slug_not_empty`` is ``LENGTH(slug) > 0`` (no
    TRIM), so a whitespace-only slug passes both DB and Pydantic by
    design — pinned here so a future tightening of one side surfaces
    the other."""
    org = Model(**_org_kwargs(slug=" "))
    assert org.slug == " "


# =============================================================================
# Team.name (mirror of teams_name_not_empty)
# =============================================================================


def _team_kwargs(**overrides):
    base = dict(
        team_id="team-1",
        organization_id="org-1",
        name="Platform",
        created_at=_now(),
        updated_at=_now(),
    )
    base.update(overrides)
    return base


@TEAM_MODELS
@WHITESPACE_VALUES
def test_team_name_rejects_empty_or_whitespace(Model, bad_value: str):
    with pytest.raises(ValidationError):
        Model(**_team_kwargs(name=bad_value))


@TEAM_MODELS
def test_team_name_accepts_normal_value(Model):
    team = Model(**_team_kwargs(name="SRE"))
    assert team.name == "SRE"


# =============================================================================
# Enterprise.name (mirror of enterprises_name_not_empty — legacy file only)
# =============================================================================
# Enterprise is only defined as a Pydantic model in interfaces_user.py;
# the auth module file has no Enterprise class today. If/when one is
# added, extend this section to cover both.


def _enterprise_kwargs(**overrides):
    base = dict(
        enterprise_id="ent-1",
        name="Acme Corp",
        slug="acme",
        plan_tier=EnterprisePlanTier.FREE,
        created_at=_now(),
        updated_at=_now(),
    )
    base.update(overrides)
    return base


@WHITESPACE_VALUES
def test_enterprise_name_rejects_empty_or_whitespace(bad_value: str):
    with pytest.raises(ValidationError):
        Enterprise(**_enterprise_kwargs(name=bad_value))


def test_enterprise_name_accepts_normal_value():
    ent = Enterprise(**_enterprise_kwargs(name="Real Corp"))
    assert ent.name == "Real Corp"


def test_enterprise_slug_rejects_empty_string():
    with pytest.raises(ValidationError):
        Enterprise(**_enterprise_kwargs(slug=""))


# =============================================================================
# Role.scope (mirror of roles_scope_check — typed as RoleScope enum)
# =============================================================================


def _role_kwargs(scope, **overrides):
    base = dict(
        role_id="role-1",
        name="admin",
        scope=scope,
        created_at=_now(),
    )
    base.update(overrides)
    return base


@ROLE_MODELS
@pytest.mark.parametrize("scope", ["system", "enterprise", "organization", "team"])
def test_role_scope_accepts_all_check_constraint_values(
    role_cls, scope_enum, scope: str
):
    role = role_cls(**_role_kwargs(scope=scope))
    assert role.scope == scope_enum(scope)


@ROLE_MODELS
@pytest.mark.parametrize("bad_scope", ["", " ", "global", "ORG", "user", "system "])
def test_role_scope_rejects_values_outside_check_constraint(
    role_cls, scope_enum, bad_scope: str
):
    with pytest.raises(ValidationError):
        role_cls(**_role_kwargs(scope=bad_scope))
