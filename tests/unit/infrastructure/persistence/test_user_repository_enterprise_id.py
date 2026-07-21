"""User repository maps ``enterprise_id`` between ORM row and domain object.

Without this, a User resolved by email/username carries no enterprise, so a
caller cannot enforce "this user is in my enterprise" before adding them to an
organization — a cross-enterprise-add hole. These tests pin the round-trip.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from faultmaven.infrastructure.persistence.user_repository import (
    PostgreSQLUserRepository,
    User,
)
from faultmaven.providers.tenancy.single_tenant import DEFAULT_ENTERPRISE_ID

pytestmark = pytest.mark.unit


def _orm_stub(**overrides):
    """A stand-in for a UserModel row with the attributes _model_to_domain reads."""
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
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


def _repo():
    # _model_to_domain / _domain_to_dict never touch self.db.
    return PostgreSQLUserRepository(db_session=None)


def test_model_to_domain_maps_enterprise_id():
    user = _repo()._model_to_domain(_orm_stub(enterprise_id="ent-42"))
    assert user.enterprise_id == "ent-42"


def test_model_to_domain_enterprise_id_none_when_absent():
    stub = _orm_stub()
    del stub.enterprise_id  # older row / partial model
    user = _repo()._model_to_domain(stub)
    assert user.enterprise_id is None


def test_domain_to_dict_uses_user_enterprise_id():
    user = User(
        user_id="u-1",
        username="alice",
        email="alice@example.com",
        display_name="Alice",
        enterprise_id="ent-42",
        created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    assert _repo()._domain_to_dict(user)["enterprise_id"] == "ent-42"


def test_domain_to_dict_defaults_enterprise_id_when_unset():
    user = User(
        user_id="u-1",
        username="alice",
        email="alice@example.com",
        display_name="Alice",
        created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    assert user.enterprise_id is None  # model default
    # Column is NOT NULL → the write path fills the standalone default.
    assert _repo()._domain_to_dict(user)["enterprise_id"] == DEFAULT_ENTERPRISE_ID
