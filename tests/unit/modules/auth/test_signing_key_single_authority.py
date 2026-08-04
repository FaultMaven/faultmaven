"""One resolver decides what this deployment signs with (#959).

``AuthService._load_keys`` is the only code that turns configuration into an RSA
pair: it reads the direct-value and the file-path spellings, refuses a
half-configured pair, and — when nothing at all was declared — deliberately
generates a development pair. The token generators are built FROM that pair.

The quadrant that forces the issue is ``AUTH_MODE=oauth`` on a standalone
deployment with no keys declared, which the config layer permits with a warning.
A generator resolving keys from settings on its own finds nothing there and is
built keyless: the reset mint then dies inside PyJWT with a bare
``TypeError: Expecting a PEM-formatted key`` — a regression against the old code,
which signed with AuthService's dev pair and worked. Resolving a *second* dev
pair instead would be worse still: two independent pairs in one process, every
token minted by one rejected by the other, which is the 401 storm ``_load_keys``
exists to prevent.

So the property is: whatever key AuthService ended up with, that is the key the
generator signs with — asserted by verifying a generator-minted token through
``AuthService.verify_token``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.container.providers.services import (
    create_jwt_token_generator,
    create_signing_token_generator,
    create_user_service,
)
from faultmaven.modules.auth.domain.services.auth_service import AuthService
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    RS256JWTTokenGenerator,
    SigningKeyUnavailableError,
    build_rs256_token_generator,
)
from faultmaven.utils.password import verify_password
from tests.utils import InMemoryRevocationStore

pytestmark = pytest.mark.asyncio

EMAIL = "quadrant@local.faultmaven"
OLD_PASSWORD = "Str0ng-P4ssw0rd!"
NEW_PASSWORD = "An0ther-P4ssw0rd!"


def _unconfigured_oauth_settings():
    """OAuth mode, standalone, nothing declared — the warn-only quadrant.

    Every key field is absent, which is what sends ``_load_keys`` down its
    "nothing requested" branch and makes the development pair the deliberate
    selection rather than a fabrication over a configured half.
    """
    return SimpleNamespace(
        auth=SimpleNamespace(
            auth_mode="oauth",
            oauth_enabled=True,
            jwt_access_token_expire_minutes=60,
            jwt_refresh_token_expire_days=7,
        ),
        security=SimpleNamespace(
            jwt_algorithm="RS256",
            jwt_issuer="faultmaven-api",
            jwt_audience="faultmaven-app",
            token_revocation_prefix="revoked:token:",
            jwt_private_key=None,
            jwt_public_key=None,
            jwt_private_key_path=None,
            jwt_public_key_path=None,
            jwt_secret_key=None,
        ),
        database=SimpleNamespace(database_url=":memory:"),
    )


def _fake_redis():
    import fakeredis.aioredis as fakeredis_aio

    return fakeredis_aio.FakeRedis(decode_responses=True)


def _auth_service(settings, store):
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=settings,
    ):
        return AuthService(revocation_store=store)


def _wire(settings, store, auth_service):
    """THE wiring, called — not a local copy of it.

    ``create_signing_token_generator`` is what ``register_services`` invokes,
    and it takes the auth service rather than a key pair precisely so that no
    caller — including this test — can supply keys of its own and prove
    something about a wiring nobody runs.
    """
    return create_signing_token_generator(settings, store, auth_service)


class TestUnconfiguredOAuthStandalone:
    """The quadrant where settings alone yield no key at all."""

    async def test_the_generator_signs_with_the_auth_services_pair(self):
        settings = _unconfigured_oauth_settings()
        store = InMemoryRevocationStore()
        auth_service = _auth_service(settings, store)

        assert auth_service.signing_private_key, "dev keys are the deliberate branch"

        generator = _wire(settings, store, auth_service)
        assert isinstance(generator, RS256JWTTokenGenerator)
        assert generator.private_key == auth_service.signing_private_key
        assert generator.public_key == auth_service.verification_public_key

    async def test_a_generator_minted_token_verifies_on_the_request_path(self):
        """Two dev pairs in one process would 401 every request; one cannot."""
        settings = _unconfigured_oauth_settings()
        store = InMemoryRevocationStore()
        auth_service = _auth_service(settings, store)
        generator = _wire(settings, store, auth_service)

        token = await generator.generate_access_token(
            SimpleNamespace(
                user_id="user-quadrant",
                username="quadrant",
                email=EMAIL,
                roles=["member"],
                is_active=True,
                organization_id="org-1",
            )
        )

        with patch(
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=settings,
        ):
            claims = auth_service.verify_token(token)
        assert claims["sub"] == "user-quadrant"

    async def test_password_reset_round_trips(self):
        """What broke on main-plus-a-second-resolver: reset with dev keys."""
        settings = _unconfigured_oauth_settings()
        store = InMemoryRevocationStore()
        auth_service = _auth_service(settings, store)
        generator = _wire(settings, store, auth_service)

        with patch(
            "faultmaven.modules.auth.domain.services.user_service.get_settings",
            return_value=settings,
        ):
            user_service = create_user_service(
                auth_service, generator, _fake_redis(), settings
            )
        assert user_service is not None, "the wiring must not skip UserService here"

        await user_service.register_user(
            email=EMAIL, password=OLD_PASSWORD, full_name="Quadrant"
        )
        reset_token = await user_service.request_password_reset(email=EMAIL)
        updated = await user_service.reset_password(reset_token, NEW_PASSWORD)

        assert verify_password(NEW_PASSWORD, updated.hashed_password)

    async def test_the_oauth_factory_takes_the_same_pair(self):
        """The OAuth generator and the signing generator cannot diverge."""
        settings = _unconfigured_oauth_settings()
        store = InMemoryRevocationStore()
        auth_service = _auth_service(settings, store)

        oauth_generator = create_jwt_token_generator(settings, store, auth_service)

        assert oauth_generator.private_key == auth_service.signing_private_key
        assert (
            oauth_generator.public_key
            == _wire(settings, store, auth_service).public_key
        )


class TestAKeylessGeneratorIsRefused:
    """The contract the docstring claims, exercised rather than asserted."""

    async def test_missing_pair_raises_rather_than_building(self):
        settings = _unconfigured_oauth_settings()

        with pytest.raises(SigningKeyUnavailableError) as refusal:
            build_rs256_token_generator(
                settings,
                InMemoryRevocationStore(),
                private_key=None,
                public_key=None,
            )
        assert "private" in str(refusal.value) and "public" in str(refusal.value)

    async def test_half_a_pair_is_refused_too(self):
        settings = _unconfigured_oauth_settings()
        auth_service = _auth_service(settings, InMemoryRevocationStore())

        with pytest.raises(SigningKeyUnavailableError):
            build_rs256_token_generator(
                settings,
                InMemoryRevocationStore(),
                private_key=auth_service.signing_private_key,
                public_key=None,
            )
