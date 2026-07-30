"""JWT expiry env vars name their unit and refuse implausible values (#832).

The two fields do not share a unit — access is minutes, refresh is DAYS — so an
operator who read the old parallel names as both-minutes set `10080` on the days
field and got ~27 years of refresh validity, silently removing the
short-credential assumption the whole revocation design rests on.

The pair is declared exactly once, on `settings.auth`, and governs every auth
mode (#888). `SecuritySettings` declaring the same two field names is what let
one documented spelling reach cloud and the other reach local, each inert in the
other mode; that duplicate is gone, and the bounds on the surviving declaration
are therefore the only mintable range there is — which is what makes the
revocation entry ceiling sound.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from faultmaven.config.settings import (
    MAX_ACCESS_TOKEN_EXPIRY_MINUTES,
    MAX_REFRESH_TOKEN_EXPIRY_DAYS,
    MAX_TOKEN_LIFETIME_DAYS,
    AuthSettings,
    SecuritySettings,
)


def _bound(model, field: str, kind: str):
    """Read a declared ge/le off a pydantic field, so tests assert the SCHEMA."""
    for constraint in model.model_fields[field].metadata:
        value = getattr(constraint, kind, None)
        if value is not None:
            return value
    raise AssertionError(f"{model.__name__}.{field} declares no {kind} bound")


# Every name that has ever addressed these two fields. Cleared before each
# construction so an ambient .env cannot decide the outcome of a test about
# which names bind.
JWT_EXPIRY_ENV_NAMES = (
    "JWT_ACCESS_TOKEN_EXPIRY",
    "JWT_REFRESH_TOKEN_EXPIRY",
    "JWT_ACCESS_TOKEN_EXPIRY_MINUTES",
    "JWT_REFRESH_TOKEN_EXPIRY_DAYS",
    # Retired: these once bound a duplicate declaration on the security half.
    "JWT_ACCESS_TOKEN_EXPIRE_MINUTES",
    "JWT_REFRESH_TOKEN_EXPIRE_DAYS",
)

EXPIRY_FIELDS = ("jwt_access_token_expire_minutes", "jwt_refresh_token_expire_days")


def _clean_env(**overrides):
    env = {k: v for k, v in os.environ.items() if k not in JWT_EXPIRY_ENV_NAMES}
    env.update(overrides)
    return patch.dict(os.environ, env, clear=True)


def _auth_settings_with(**env):
    with _clean_env(**env):
        return AuthSettings()


def _security_settings_with(**env):
    with _clean_env(**env):
        return SecuritySettings()


class TestUnitSuffixedNamesBind:
    """The current names, and only the current names, reach the fields."""

    def test_refresh_days_binds(self):
        settings = _auth_settings_with(JWT_REFRESH_TOKEN_EXPIRY_DAYS="9")
        assert settings.jwt_refresh_token_expire_days == 9

    def test_access_minutes_binds(self):
        settings = _auth_settings_with(JWT_ACCESS_TOKEN_EXPIRY_MINUTES="45")
        assert settings.jwt_access_token_expire_minutes == 45

    def test_defaults(self):
        settings = _auth_settings_with()
        assert settings.jwt_access_token_expire_minutes == 15
        assert settings.jwt_refresh_token_expire_days == 7


class TestOldUnsuffixedNamesDoNotBind:
    """No alias shim: the ambiguous names are gone, not quietly honoured.

    A deployment still carrying them must fail visibly (defaults, then a bad
    login lifetime) rather than keep a value whose unit was misread.
    """

    def test_old_refresh_name_is_inert(self):
        settings = _auth_settings_with(JWT_REFRESH_TOKEN_EXPIRY="9")
        assert settings.jwt_refresh_token_expire_days == 7

    def test_old_access_name_is_inert(self):
        settings = _auth_settings_with(JWT_ACCESS_TOKEN_EXPIRY="45")
        assert settings.jwt_access_token_expire_minutes == 15


class TestBoundsRejectImplausibleValues:
    """A value that would defeat the revocation design fails at boot."""

    @pytest.mark.parametrize("value", ["0", "-1", "91", "10080", "3650"])
    def test_refresh_days_out_of_range(self, value):
        with pytest.raises(ValidationError):
            _auth_settings_with(JWT_REFRESH_TOKEN_EXPIRY_DAYS=value)

    @pytest.mark.parametrize("value", ["1", "7", "30", "90"])
    def test_refresh_days_in_range(self, value):
        settings = _auth_settings_with(JWT_REFRESH_TOKEN_EXPIRY_DAYS=value)
        assert settings.jwt_refresh_token_expire_days == int(value)

    @pytest.mark.parametrize("value", ["0", "-1", "1441", "10080"])
    def test_access_minutes_out_of_range(self, value):
        with pytest.raises(ValidationError):
            _auth_settings_with(JWT_ACCESS_TOKEN_EXPIRY_MINUTES=value)

    # 999 is the reviewer's #888 reproduction value: in range, unremarkable, and
    # under the old split it moved cloud lifetimes only through the retired name.
    @pytest.mark.parametrize("value", ["1", "15", "60", "999", "1440"])
    def test_access_minutes_in_range(self, value):
        settings = _auth_settings_with(JWT_ACCESS_TOKEN_EXPIRY_MINUTES=value)
        assert settings.jwt_access_token_expire_minutes == int(value)


class TestExpiryIsDeclaredExactlyOnce:
    """One declaration is the fix for #888, so the schema pins it.

    A second field of the same name on another half is not a duplicate value —
    it is a second knob, reachable by a different env name, that some minting
    path will eventually read instead.
    """

    @pytest.mark.parametrize("field", EXPIRY_FIELDS)
    def test_the_auth_half_declares_it(self, field):
        assert field in AuthSettings.model_fields

    @pytest.mark.parametrize("field", EXPIRY_FIELDS)
    def test_the_security_half_does_not(self, field):
        assert field not in SecuritySettings.model_fields

    @pytest.mark.parametrize("field", EXPIRY_FIELDS)
    def test_the_security_half_holds_no_expiry_value(self, field):
        """Not merely undeclared — unreadable, so a stale read cannot resolve."""
        security = _security_settings_with()
        assert not hasattr(security, field)


class TestTheSingleSourceIsBounded:
    """The entry ceiling is only an upper bound if the one source can't exceed it.

    The retired security half was unbounded at first while binding by field
    name, so a cloud deployment could mint a 365-day refresh token against a
    90-day revocation entry — revoked for 90 days, live again for the next 275.
    With one declaration there is one place to bound, and this is it.
    """

    @pytest.mark.parametrize("field", EXPIRY_FIELDS)
    def test_every_expiry_field_declares_the_shared_bounds(self, field):
        expected = {
            "jwt_access_token_expire_minutes": MAX_ACCESS_TOKEN_EXPIRY_MINUTES,
            "jwt_refresh_token_expire_days": MAX_REFRESH_TOKEN_EXPIRY_DAYS,
        }[field]

        assert _bound(AuthSettings, field, "ge") == 1
        assert _bound(AuthSettings, field, "le") == expected


class TestTheCeilingIsActuallyTheMaximum:
    """`MAX_TOKEN_LIFETIME_DAYS` is only sound while it dominates every bound.

    It is the refresh bound today, which exceeds the access bound (1 day) and the
    password-reset lifetime (1 hour). Raising either past 90 days without moving
    the ceiling would silently reintroduce entries that expire before their
    tokens, so this fails loudly instead.
    """

    def test_dominates_the_access_token_bound(self):
        assert MAX_ACCESS_TOKEN_EXPIRY_MINUTES * 60 <= MAX_TOKEN_LIFETIME_DAYS * 86400

    def test_dominates_the_refresh_token_bound(self):
        assert MAX_REFRESH_TOKEN_EXPIRY_DAYS <= MAX_TOKEN_LIFETIME_DAYS

    def test_dominates_the_password_reset_lifetime(self):
        from faultmaven.modules.auth.domain.services.user_service import (
            PASSWORD_RESET_TOKEN_EXPIRY_HOURS,
        )

        assert (
            PASSWORD_RESET_TOKEN_EXPIRY_HOURS * 3600 <= MAX_TOKEN_LIFETIME_DAYS * 86400
        )

    def test_dominates_the_local_session_token_bound(self):
        assert (
            _bound(AuthSettings, "local_token_expiry_hours", "le") * 3600
            <= MAX_TOKEN_LIFETIME_DAYS * 86400
        )
