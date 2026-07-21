"""Unit tests for the unified token revocation store (#767).

Before #767, revocation writes were fragmented across three stores
(``revoked:token:*`` Redis keys, ``oauth:revoked:*`` Redis keys, and
``RedisTokenManager`` metadata flags) while the request-path check read only
the first — so OAuth-revoked and logout-"revoked" tokens kept working, and
local-mode refresh was broken outright (``RedisTokenManager`` does not
implement ``ITokenRevocationStore``, so every HS256 validate call raised and
fail-closed to None).

These tests pin the unified contract with the PRODUCTION store class
(``RedisTokenRevocationStore`` over FakeRedis), not a test double:

1. The local-mode generator wired exactly as the routes wire it can mint,
   validate, and revoke (the pre-#767 wiring failed validation outright).
2. A token revoked through a generator is rejected by the request-path check
   in AuthService when both share the store instance — the core #767 claim.
3. The store honours the configured single key prefix.
4. Failure posture: validation fails CLOSED on store errors; revocation
   writes propagate store errors; logout surfaces write failures.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.modules.auth.domain.services.auth_service import (
    AuthService,
    TokenRevocationError,
)
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
)
from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
    RedisTokenRevocationStore,
)

pytestmark = pytest.mark.asyncio

SECRET = "unit-test-secret-key-please-ignore"


def _fake_redis():
    import fakeredis.aioredis as fakeredis_aio

    return fakeredis_aio.FakeRedis(decode_responses=True)


def _store(redis):
    return RedisTokenRevocationStore(redis, key_prefix="revoked:token:")


def _generator(store):
    settings_stub = SimpleNamespace(
        jwt_access_token_expire_minutes=60,
        jwt_refresh_token_expire_days=7,
    )
    return HS256JWTTokenGenerator(
        secret_key=SECRET,
        revocation_store=store,
        settings=settings_stub,
        issuer="faultmaven",
        audience="faultmaven-api",
    )


def _user():
    return SimpleNamespace(
        user_id="user-767",
        username="revoker",
        email="revoker@local.faultmaven",
        roles=["user"],
        organization_id="org-767",
    )


def _auth_service_settings():
    """Settings stub matching the HS256 generator's fixed iss/aud."""
    settings = SimpleNamespace(
        auth=SimpleNamespace(auth_mode="local"),
        security=SimpleNamespace(
            jwt_algorithm="HS256",
            jwt_access_token_expire_minutes=60,
            jwt_refresh_token_expire_days=7,
            jwt_issuer="faultmaven",
            jwt_audience="faultmaven-api",
            token_revocation_prefix="revoked:token:",
            jwt_private_key=None,
            jwt_public_key=None,
            jwt_private_key_path=None,
            jwt_public_key_path=None,
            jwt_secret_key=SimpleNamespace(get_secret_value=lambda: SECRET),
        ),
    )
    return settings


class TestLocalGeneratorWithProductionStore:
    """Regression: the HS256 generator must work with the PRODUCTION store.

    Pre-#767, local routes passed RedisTokenManager (which lacks
    ``is_revoked``/``add_revoked_token``) as the revocation store, so every
    ``validate_*`` call raised AttributeError internally and returned None —
    local /auth/refresh always 401'd and rotation revoked nothing.
    """

    async def test_mint_validate_revoke_roundtrip(self):
        store = _store(_fake_redis())
        generator = _generator(store)
        user = _user()

        access = await generator.generate_access_token(user)
        refresh = await generator.generate_refresh_token(user)

        assert await generator.validate_access_token(access) is not None
        assert await generator.validate_refresh_token(refresh) is not None

        await generator.revoke_refresh_token(refresh)
        assert await generator.validate_refresh_token(refresh) is None
        # The access token is untouched
        assert await generator.validate_access_token(access) is not None

    async def test_store_writes_under_single_prefix(self):
        redis = _fake_redis()
        store = _store(redis)
        generator = _generator(store)

        access = await generator.generate_access_token(_user())
        await generator.revoke_access_token(access)

        keys = [k async for k in redis.scan_iter("*")]
        assert len(keys) == 1
        assert keys[0].startswith("revoked:token:")


class TestGeneratorRevocationVisibleToRequestPath:
    """The core #767 fix: one store instance, writers and reader agree."""

    async def test_generator_revoked_token_rejected_by_auth_service(self):
        store = _store(_fake_redis())
        generator = _generator(store)

        with patch(
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=_auth_service_settings(),
        ):
            auth_service = AuthService(revocation_store=store)

            access = await generator.generate_access_token(_user())

            # Sanity: passes the request-path check before revocation
            claims = await auth_service.verify_token_with_revocation_check(
                access, token_type="access"
            )
            assert claims["sub"] == "user-767"

            # Revoke through the generator (the /oauth/revoke + logout path)
            await generator.revoke_access_token(access)

            # The request-path check must now reject it
            with pytest.raises(TokenRevocationError):
                await auth_service.verify_token_with_revocation_check(
                    access, token_type="access"
                )


class TestFailurePosture:
    """Documented split: validate fails CLOSED, writes propagate errors."""

    async def test_validate_fails_closed_on_store_error(self):
        store = _store(_fake_redis())
        generator = _generator(store)
        access = await generator.generate_access_token(_user())
        refresh = await generator.generate_refresh_token(_user())

        async def boom(jti):
            raise ConnectionError("store down")

        store.is_revoked = boom

        assert await generator.validate_access_token(access) is None
        assert await generator.validate_refresh_token(refresh) is None

    async def test_revoke_propagates_store_write_error(self):
        store = _store(_fake_redis())
        generator = _generator(store)
        refresh = await generator.generate_refresh_token(_user())

        async def boom(jti, ttl):
            raise ConnectionError("store down")

        store.add_revoked_token = boom

        with pytest.raises(ConnectionError):
            await generator.revoke_refresh_token(refresh)

    async def test_revoke_tolerates_undecodable_token(self):
        store = _store(_fake_redis())
        generator = _generator(store)
        # RFC 7009: revoking an invalid token is success, not an error
        await generator.revoke_access_token("not-a-jwt")


class TestLogoutRevokesViaSharedStore:
    """Logout writes the access token's jti into the shared store."""

    async def test_logout_revokes_current_token(self):
        from faultmaven.modules.auth.api import auth as auth_routes

        store = _store(_fake_redis())
        generator = _generator(store)
        user = _user()
        access = await generator.generate_access_token(user)

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(token_revocation_store=store))
        )

        result = await auth_routes.logout(request, current_user=user, token=access)

        assert result.revoked_tokens == 1
        # The generator (same store) now refuses the token
        assert await generator.validate_access_token(access) is None

    async def test_logout_surfaces_store_write_failure(self):
        from fastapi import HTTPException

        from faultmaven.modules.auth.api import auth as auth_routes

        store = _store(_fake_redis())
        generator = _generator(store)
        user = _user()
        access = await generator.generate_access_token(user)

        async def boom(jti, ttl):
            raise ConnectionError("store down")

        store.add_revoked_token = boom
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(token_revocation_store=store))
        )

        with pytest.raises(HTTPException) as exc_info:
            await auth_routes.logout(request, current_user=user, token=access)
        assert exc_info.value.status_code == 500


class TestDIFactoryUsesConfiguredPrefix:
    """create_token_revocation_store must honour the settings prefix."""

    async def test_factory_prefix(self):
        from faultmaven.container.providers.services import (
            create_token_revocation_store,
        )

        redis = _fake_redis()
        settings = SimpleNamespace(
            security=SimpleNamespace(token_revocation_prefix="revoked:token:")
        )
        store = create_token_revocation_store(settings, cache_client=redis)

        await store.add_revoked_token("jti-1", 60)
        keys = [k async for k in redis.scan_iter("*")]
        assert keys == ["revoked:token:jti-1"]
        assert await store.is_revoked("jti-1") is True
        assert await store.is_revoked("jti-2") is False
