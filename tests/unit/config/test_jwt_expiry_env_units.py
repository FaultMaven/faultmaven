"""JWT expiry env vars name their unit and refuse implausible values (#832).

The two fields do not share a unit — access is minutes, refresh is DAYS — so an
operator who read the old parallel names as both-minutes set `10080` on the days
field and got ~27 years of refresh validity, silently removing the
short-credential assumption the whole revocation design rests on.

Only `settings.auth` carries the documented `JWT_*_EXPIRY_*` aliases. The
`settings.security` half — the one the cloud RS256 generator and `AuthService`
mint from — declares the same field names with no alias, so it binds by FIELD
NAME (`JWT_ACCESS_TOKEN_EXPIRE_MINUTES` / `JWT_REFRESH_TOKEN_EXPIRE_DAYS`, EXPIRE
rather than EXPIRY; #888 tracks that operator-facing gap). Both halves carry the
same bounds, which is what makes the revocation entry ceiling sound: it is only
an upper bound on token lifetime if NEITHER half can be configured past it.
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
    # The security half's field-name binding
    "JWT_ACCESS_TOKEN_EXPIRE_MINUTES",
    "JWT_REFRESH_TOKEN_EXPIRE_DAYS",
)


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

    @pytest.mark.parametrize("value", ["1", "15", "60", "1440"])
    def test_access_minutes_in_range(self, value):
        settings = _auth_settings_with(JWT_ACCESS_TOKEN_EXPIRY_MINUTES=value)
        assert settings.jwt_access_token_expire_minutes == int(value)


class TestOnlyTheAuthHalfCarriesTheAliases:
    """The two halves declare the same field names; one is operator-facing."""

    def test_security_half_ignores_the_documented_aliases(self):
        """The `JWT_*_EXPIRY_*` names reach the auth half only (#888)."""
        security = _security_settings_with(
            JWT_ACCESS_TOKEN_EXPIRY_MINUTES="45",
            JWT_REFRESH_TOKEN_EXPIRY_DAYS="30",
        )

        assert security.jwt_access_token_expire_minutes == 15
        assert security.jwt_refresh_token_expire_days == 7

    def test_security_half_binds_by_field_name(self):
        """With no alias, the field NAME is the binding — EXPIRE, not EXPIRY.

        This is the half the cloud RS256 generator and AuthService mint from, so
        this form is the only way to move cloud token lifetimes today.
        """
        security = _security_settings_with(
            JWT_ACCESS_TOKEN_EXPIRE_MINUTES="45",
            JWT_REFRESH_TOKEN_EXPIRE_DAYS="30",
        )

        assert security.jwt_access_token_expire_minutes == 45
        assert security.jwt_refresh_token_expire_days == 30

    def test_the_field_name_form_does_not_reach_the_auth_half(self):
        auth = _auth_settings_with(
            JWT_ACCESS_TOKEN_EXPIRE_MINUTES="45",
            JWT_REFRESH_TOKEN_EXPIRE_DAYS="30",
        )

        assert auth.jwt_access_token_expire_minutes == 15
        assert auth.jwt_refresh_token_expire_days == 7


class TestBothHalvesAreBounded:
    """The entry ceiling is only an upper bound if NEITHER half can exceed it.

    The security half was unbounded while binding by field name, so a cloud
    deployment could mint a 365-day refresh token against a 90-day revocation
    entry — revoked for 90 days, live again for the next 275.
    """

    @pytest.mark.parametrize("value", ["0", "-1", "91", "365", "10080"])
    def test_security_refresh_days_out_of_range(self, value):
        with pytest.raises(ValidationError):
            _security_settings_with(JWT_REFRESH_TOKEN_EXPIRE_DAYS=value)

    @pytest.mark.parametrize("value", ["1", "7", "30", "90"])
    def test_security_refresh_days_in_range(self, value):
        settings = _security_settings_with(JWT_REFRESH_TOKEN_EXPIRE_DAYS=value)
        assert settings.jwt_refresh_token_expire_days == int(value)

    @pytest.mark.parametrize("value", ["0", "-1", "1441", "10080"])
    def test_security_access_minutes_out_of_range(self, value):
        with pytest.raises(ValidationError):
            _security_settings_with(JWT_ACCESS_TOKEN_EXPIRE_MINUTES=value)

    # 999 is the reviewer's reproduction value: in range, but it only reaches
    # the field at all through the field-name binding.
    @pytest.mark.parametrize("value", ["1", "15", "60", "999", "1440"])
    def test_security_access_minutes_in_range(self, value):
        settings = _security_settings_with(JWT_ACCESS_TOKEN_EXPIRE_MINUTES=value)
        assert settings.jwt_access_token_expire_minutes == int(value)

    @pytest.mark.parametrize(
        "half,field",
        [
            (AuthSettings, "jwt_access_token_expire_minutes"),
            (AuthSettings, "jwt_refresh_token_expire_days"),
            (SecuritySettings, "jwt_access_token_expire_minutes"),
            (SecuritySettings, "jwt_refresh_token_expire_days"),
        ],
    )
    def test_every_expiry_field_declares_the_shared_bounds(self, half, field):
        """Both halves, both fields — no unbounded expiry anywhere."""
        expected = {
            "jwt_access_token_expire_minutes": MAX_ACCESS_TOKEN_EXPIRY_MINUTES,
            "jwt_refresh_token_expire_days": MAX_REFRESH_TOKEN_EXPIRY_DAYS,
        }[field]

        assert _bound(half, field, "ge") == 1
        assert _bound(half, field, "le") == expected


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
