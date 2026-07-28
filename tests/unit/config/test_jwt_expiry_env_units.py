"""JWT expiry env vars name their unit and refuse implausible values (#832).

The two fields do not share a unit — access is minutes, refresh is DAYS — so an
operator who read the old parallel names as both-minutes set `10080` on the days
field and got ~27 years of refresh validity, silently removing the
short-credential assumption the whole revocation design rests on.

Only `settings.auth` carries the env aliases; `settings.security` declares the
same field names with no alias and always sits at its defaults (the split that
`AuthService._longest_token_lifetime_seconds` reads across).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from faultmaven.config.settings import AuthSettings, SecuritySettings

# Every name that has ever addressed these two fields. Cleared before each
# construction so an ambient .env cannot decide the outcome of a test about
# which names bind.
JWT_EXPIRY_ENV_NAMES = (
    "JWT_ACCESS_TOKEN_EXPIRY",
    "JWT_REFRESH_TOKEN_EXPIRY",
    "JWT_ACCESS_TOKEN_EXPIRY_MINUTES",
    "JWT_REFRESH_TOKEN_EXPIRY_DAYS",
)


def _clean_env(**overrides):
    env = {k: v for k, v in os.environ.items() if k not in JWT_EXPIRY_ENV_NAMES}
    env.update(overrides)
    return patch.dict(os.environ, env, clear=True)


def _auth_settings_with(**env):
    with _clean_env(**env):
        return AuthSettings()


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

    @pytest.mark.parametrize("value", ["1", "15", "60", "1440"])
    def test_access_minutes_in_range(self, value):
        settings = _auth_settings_with(JWT_ACCESS_TOKEN_EXPIRY_MINUTES=value)
        assert settings.jwt_access_token_expire_minutes == int(value)


class TestOnlyTheAuthHalfCarriesTheAliases:
    """The two halves declare the same field names; one is operator-facing."""

    def test_security_half_ignores_the_env_vars(self):
        with _clean_env(
            JWT_ACCESS_TOKEN_EXPIRY_MINUTES="45",
            JWT_REFRESH_TOKEN_EXPIRY_DAYS="30",
        ):
            security = SecuritySettings()

        assert security.jwt_access_token_expire_minutes == 15
        assert security.jwt_refresh_token_expire_days == 7
