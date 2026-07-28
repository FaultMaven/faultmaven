"""``User.organization_id`` is runtime-only and must never be persisted (#869).

Organization affiliation lives in ``organization_members``; the ``users`` table
has no organization column. The field exists purely so mint-time tenancy can
ride the token chain — the SSO exchange attaches the organization resolved at
callback time, ``/auth/refresh`` re-attaches the validated refresh claim, and
``resolve_organization_claim`` reads it when building the token claim.

If it ever started persisting, a user's organization would become a stale copy
of a membership row that RLS, not the user row, is the source of truth for.
These tests pin both directions: nothing writes it, and nothing reads it back.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from faultmaven.infrastructure.persistence.models import UserModel
from faultmaven.infrastructure.persistence.user_repository import (
    PostgreSQLUserRepository,
    User,
)

pytestmark = pytest.mark.unit

ORG = "22222222-2222-2222-2222-222222222222"


def _repo():
    # _model_to_domain / _domain_to_dict never touch self.db.
    return PostgreSQLUserRepository(db_session=None)


def _user(**overrides) -> User:
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    base = dict(
        user_id="u-1",
        username="alice",
        email="alice@example.com",
        display_name="Alice",
        enterprise_id="ent-42",
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return User(**base)


def _orm_stub(**overrides):
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    base = dict(
        user_id="u-1",
        username="alice",
        email="alice@example.com",
        enterprise_id="ent-42",
        display_name="Alice",
        avatar_url=None,
        timezone="UTC",
        locale="en-US",
        hashed_password=None,
        is_active=True,
        is_email_verified=True,
        email_verified_at=now,
        sso_provider=None,
        sso_provider_id=None,
        created_at=now,
        updated_at=now,
        last_login_at=None,
        last_password_change_at=None,
        deleted_at=None,
        dev_roles=None,
        account_kind="individual",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_the_field_defaults_to_none():
    assert _user().organization_id is None


def test_domain_to_dict_never_writes_the_organization():
    """The write path is an explicit column list — the org must not be in it."""
    values = _repo()._domain_to_dict(_user(organization_id=ORG))

    assert "organization_id" not in values
    assert ORG not in str(values)


def test_the_users_table_has_no_organization_column():
    """The reason the field can never be persisted, stated against the schema."""
    assert "organization_id" not in UserModel.__table__.columns


def test_a_user_read_back_carries_no_organization():
    """Load direction: whatever was attached at mint time does not survive."""
    loaded = _repo()._model_to_domain(_orm_stub())

    assert loaded.organization_id is None


def test_attaching_the_organization_does_not_disturb_the_persisted_shape():
    """The dict written for an org-attached user is byte-identical to the one
    written for the same user without it."""
    without = _repo()._domain_to_dict(_user())
    with_org = _repo()._domain_to_dict(_user(organization_id=ORG))

    assert with_org == without
