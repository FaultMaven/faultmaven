"""JWT expiry has ONE source, and it governs every mint path (#888).

Token lifetimes are configured by exactly one pair of environment variables —
``JWT_ACCESS_TOKEN_EXPIRY_MINUTES`` and ``JWT_REFRESH_TOKEN_EXPIRY_DAYS``,
living on ``settings.auth`` — and they are effective in every auth mode.

Both halves used to declare the same two field names: the auth half bound them
by ``validation_alias`` (the EXPIRY spelling), the security half by field name
(the EXPIRE spelling). Whichever half a minting path happened to be built from
decided which env spelling reached it, so each documented spelling worked in
exactly one mode and was silently inert in the other.

Asserting on settings objects is what let that hide — a value can land on a
settings half that no minting path reads. These guards therefore assert on
**minted tokens**: build the generator through its production construction
site, mint, verify the signature, and measure ``exp - iat``.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import jwt
import pytest
from pydantic import ValidationError

from faultmaven.config.settings import FaultMavenSettings, SecuritySettings

pytestmark = pytest.mark.unit

# Every env name that has ever addressed these two fields. Cleared before each
# construction so an ambient .env cannot decide the outcome of a test about
# which names bind.
JWT_EXPIRY_ENV_NAMES = (
    "JWT_ACCESS_TOKEN_EXPIRY",
    "JWT_REFRESH_TOKEN_EXPIRY",
    "JWT_ACCESS_TOKEN_EXPIRY_MINUTES",
    "JWT_REFRESH_TOKEN_EXPIRY_DAYS",
    "JWT_ACCESS_TOKEN_EXPIRE_MINUTES",
    "JWT_REFRESH_TOKEN_EXPIRE_DAYS",
)

ISSUER = "faultmaven-api"
AUDIENCE = "faultmaven-app"
SECRET = "unit-test-hs256-secret-please-ignore"

# (access minutes, refresh days) — both non-default, so a run that quietly
# mints the 15/7 schema defaults cannot pass by coincidence.
LIFETIME_PAIRS = [(23, 11), (37, 3)]


@pytest.fixture(autouse=True)
def _clear_expiry_env(monkeypatch):
    """No ambient expiry configuration reaches any construction in this module."""
    for name in JWT_EXPIRY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[str, str]:
    """A throwaway RSA keypair, generated here so no key material is committed."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return private_pem, public_pem


def _revocation_store():
    store = Mock()
    store.is_token_revoked = AsyncMock(return_value=False)
    store.add_revoked_token = AsyncMock()
    store.revoke_user_tokens_before = AsyncMock()
    return store


def _user():
    return SimpleNamespace(
        user_id="user-888",
        username="expiry-user",
        email="expiry@local.faultmaven",
        roles=["user"],
        organization_id="org-888",
    )


def _configured_settings(monkeypatch, minutes: int, days: int) -> FaultMavenSettings:
    """Real settings built from the environment, as production builds them."""
    monkeypatch.setenv("JWT_ACCESS_TOKEN_EXPIRY_MINUTES", str(minutes))
    monkeypatch.setenv("JWT_REFRESH_TOKEN_EXPIRY_DAYS", str(days))
    monkeypatch.setenv("JWT_ISSUER", ISSUER)
    monkeypatch.setenv("JWT_AUDIENCE", AUDIENCE)
    return FaultMavenSettings(_env_file=None)


def _lifetime(payload: dict) -> timedelta:
    return timedelta(seconds=payload["exp"] - payload["iat"])


class TestCloudMintHonoursTheKnob:
    """RS256/cloud tokens carry the configured lifetime.

    The generator is built through ``create_jwt_token_generator`` — the
    container factory that wires it in production — because the defect lived in
    that wiring, not in the generator. A hand-built generator would prove
    nothing about which settings half the cloud path reaches.
    """

    @pytest.mark.parametrize("minutes,days", LIFETIME_PAIRS)
    @pytest.mark.asyncio
    async def test_access_token_lifetime(self, monkeypatch, rsa_keypair, minutes, days):
        private_pem, public_pem = rsa_keypair
        monkeypatch.setenv("JWT_PRIVATE_KEY", private_pem)
        monkeypatch.setenv("JWT_PUBLIC_KEY", public_pem)
        settings = _configured_settings(monkeypatch, minutes, days)

        from faultmaven.container.providers.services import create_jwt_token_generator

        generator = create_jwt_token_generator(settings, _revocation_store())
        token = await generator.generate_access_token(_user())

        payload = jwt.decode(
            token,
            public_pem,
            algorithms=["RS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
        )
        assert _lifetime(payload) == timedelta(minutes=minutes)

    @pytest.mark.parametrize("minutes,days", LIFETIME_PAIRS)
    @pytest.mark.asyncio
    async def test_refresh_token_lifetime(
        self, monkeypatch, rsa_keypair, minutes, days
    ):
        private_pem, public_pem = rsa_keypair
        monkeypatch.setenv("JWT_PRIVATE_KEY", private_pem)
        monkeypatch.setenv("JWT_PUBLIC_KEY", public_pem)
        settings = _configured_settings(monkeypatch, minutes, days)

        from faultmaven.container.providers.services import create_jwt_token_generator

        generator = create_jwt_token_generator(settings, _revocation_store())
        token = await generator.generate_refresh_token(_user())

        payload = jwt.decode(
            token,
            public_pem,
            algorithms=["RS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
        )
        assert _lifetime(payload) == timedelta(days=days)


class TestLocalMintHonoursTheSameKnob:
    """HS256/local tokens carry the same configured lifetime.

    The local path already read ``settings.auth``, so this holds before and
    after the fix; it is here so the property is asserted for both modes rather
    than assumed for one.
    """

    @pytest.mark.parametrize("minutes,days", LIFETIME_PAIRS)
    @pytest.mark.asyncio
    async def test_access_and_refresh_lifetimes(self, monkeypatch, minutes, days):
        monkeypatch.setenv("JWT_SECRET_KEY", SECRET)
        settings = _configured_settings(monkeypatch, minutes, days)

        from faultmaven.modules.auth.api import auth as auth_api

        monkeypatch.setattr(auth_api, "get_settings", lambda: settings)
        generator = auth_api._build_local_jwt_generator(_revocation_store())

        access = jwt.decode(
            await generator.generate_access_token(_user()),
            SECRET,
            algorithms=["HS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
        )
        # The HS256 refresh payload hardcodes its iss/aud rather than taking the
        # generator's — a separate inconsistency, outside #888's scope — so only
        # the signature is verified here. The lifetime is what this asserts.
        refresh = jwt.decode(
            await generator.generate_refresh_token(_user()),
            SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )

        assert _lifetime(access) == timedelta(minutes=minutes)
        assert _lifetime(refresh) == timedelta(days=days)


class TestRetiredSpellingIsRejected:
    """The EXPIRE spelling fails the boot instead of binding silently.

    It used to reach the security half by field-name binding and was the
    documented knob for cloud. Now that the field is gone, an environment still
    setting it would be silently inert — the exact failure mode this design
    removes — so settings construction refuses it and names the replacement.
    """

    @pytest.mark.parametrize(
        "retired,replacement",
        [
            ("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "JWT_ACCESS_TOKEN_EXPIRY_MINUTES"),
            ("JWT_REFRESH_TOKEN_EXPIRE_DAYS", "JWT_REFRESH_TOKEN_EXPIRY_DAYS"),
        ],
    )
    def test_unified_settings_construction_fails(
        self, monkeypatch, retired, replacement
    ):
        monkeypatch.setenv(retired, "30")

        with pytest.raises(ValidationError) as exc_info:
            FaultMavenSettings(_env_file=None)

        message = str(exc_info.value)
        assert retired in message
        assert replacement in message

    @pytest.mark.parametrize(
        "retired,replacement",
        [
            ("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "JWT_ACCESS_TOKEN_EXPIRY_MINUTES"),
            ("JWT_REFRESH_TOKEN_EXPIRE_DAYS", "JWT_REFRESH_TOKEN_EXPIRY_DAYS"),
        ],
    )
    def test_security_half_construction_fails(self, monkeypatch, retired, replacement):
        """The gate lives on the half that used to carry the field."""
        monkeypatch.setenv(retired, "30")

        with pytest.raises(ValidationError) as exc_info:
            SecuritySettings()

        message = str(exc_info.value)
        assert retired in message
        assert replacement in message

    def test_the_current_spelling_still_boots(self, monkeypatch):
        """The gate is scoped to the retired names, not to expiry configuration."""
        monkeypatch.setenv("JWT_ACCESS_TOKEN_EXPIRY_MINUTES", "23")
        monkeypatch.setenv("JWT_REFRESH_TOKEN_EXPIRY_DAYS", "11")

        settings = FaultMavenSettings(_env_file=None)

        assert settings.auth.jwt_access_token_expire_minutes == 23
        assert settings.auth.jwt_refresh_token_expire_days == 11


class TestRevocationWatermarkTracksTheSingleSource:
    """The #769 watermark TTL is derived from the one configurable lifetime.

    With a single source, "the watermark outlives every mintable token" is
    structural: the value the generators mint with is the value this reads.
    """

    @pytest.mark.parametrize("minutes,days", LIFETIME_PAIRS)
    def test_watermark_ttl_equals_configured_refresh_lifetime(
        self, monkeypatch, minutes, days
    ):
        monkeypatch.setenv("JWT_SECRET_KEY", SECRET)
        settings = _configured_settings(monkeypatch, minutes, days)

        from faultmaven.modules.auth.domain.services import auth_service as auth_module

        monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
        service = auth_module.AuthService(revocation_store=_revocation_store())

        assert service._longest_token_lifetime_seconds() == days * 86400

    def test_watermark_covers_a_maximal_access_lifetime(self, monkeypatch):
        """Access expiry is covered too: nothing ties it to the refresh knob.

        At its schema maximum (1 day) it exceeds the shortest permitted refresh
        lifetime, so the bound must be the max of the two, not the refresh one.
        """
        monkeypatch.setenv("JWT_SECRET_KEY", SECRET)
        settings = _configured_settings(monkeypatch, 1440, 1)

        from faultmaven.modules.auth.domain.services import auth_service as auth_module

        monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
        service = auth_module.AuthService(revocation_store=_revocation_store())

        assert service._longest_token_lifetime_seconds() == 1440 * 60
