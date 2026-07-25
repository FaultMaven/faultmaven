"""Unit tests for per-user token revocation (#769).

Before #769, bulk per-user revocation revoked nothing: the admin endpoint
called ``RedisTokenManager.revoke_user_tokens``, which flipped ``is_revoked``
flags on opaque-token metadata that no request path reads and that JWTs are
never written into — so the endpoint answered 200 "Revoked all 0 tokens for
user" while every outstanding JWT kept authenticating until expiry.
``AuthService.revoke_user_tokens`` (called by the deactivate/delete, password
and role-change flows) was a documented no-op returning 0.

The fix is a per-user revocation watermark in the one deployment-wide store
(#767): revocation records *when* it happened, and every validate path
rejects tokens whose ``iat`` is at or before that instant.

These tests use the PRODUCTION store class (``RedisTokenRevocationStore`` over
FakeRedis), not a test double, and assert on the ENDPOINT RESPONSE as well as
the store — the original bug was precisely that a caller reported success for
a revocation that never happened.

Covered:
1. The admin endpoint actually invalidates outstanding tokens, and reports the
   watermark rather than a fabricated count.
2. The watermark covers tokens from every mint path (HS256 and RS256
   generators, and AuthService's own synchronous mint) — the completeness
   property a per-user JTI index could not guarantee.
3. Tokens minted AFTER the revocation still work (re-login is not broken).
4. Failure posture: the write propagates, so the endpoint 500s rather than
   confirming a revocation that did not land.
5. The two revocation arms (per-jti and per-user) are independent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from faultmaven.exceptions import ServiceError
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
USER_ID = "user-769"


def _fake_redis():
    import fakeredis.aioredis as fakeredis_aio

    return fakeredis_aio.FakeRedis(decode_responses=True)


def _store(redis=None):
    return RedisTokenRevocationStore(
        redis if redis is not None else _fake_redis(),
        key_prefix="revoked:token:",
    )


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


def _user(user_id: str = USER_ID):
    return SimpleNamespace(
        user_id=user_id,
        username="revoked-user",
        email="revoked@local.faultmaven",
        roles=["user"],
        organization_id="org-769",
    )


def _auth_service_settings():
    """Settings stub matching the HS256 generator's fixed iss/aud."""
    return SimpleNamespace(
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


def _auth_service(store):
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=_auth_service_settings(),
    ):
        return AuthService(revocation_store=store)


def _admin_request(auth_service):
    """Fake request wired the way production wires the admin endpoint."""
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(auth_service=auth_service))
    )


class TestAdminEndpointActuallyRevokes:
    """The observable the original bug got wrong: the endpoint's own response.

    A store-only assertion would have passed against the broken
    implementation too, because the broken path wrote to a real (but unread)
    store. These assert the response AND that the token stops authenticating.
    """

    async def test_endpoint_invalidates_outstanding_token(self):
        from faultmaven.modules.auth.api import auth as auth_routes

        store = _store()
        generator = _generator(store)
        auth_service = _auth_service(store)
        user = _user()

        access = await generator.generate_access_token(user)
        # Sanity: the token authenticates before revocation.
        assert await generator.validate_access_token(access) is not None

        response = await auth_routes.revoke_user_tokens(
            USER_ID, _admin_request(auth_service), _=user
        )

        # The token no longer authenticates on any path.
        assert await generator.validate_access_token(access) is None
        with pytest.raises(TokenRevocationError):
            await auth_service.verify_token_with_revocation_check(
                access, token_type="access"
            )

        # ...and the response describes what actually happened.
        assert response.message == "All tokens revoked for user"
        watermark = datetime.fromisoformat(response.revoked_before)
        assert watermark.tzinfo is not None

    async def test_response_carries_no_fabricated_token_count(self):
        """Regression: the old response claimed "Revoked all N tokens"."""
        from faultmaven.modules.auth.api import auth as auth_routes

        store = _store()
        auth_service = _auth_service(store)

        response = await auth_routes.revoke_user_tokens(
            USER_ID, _admin_request(auth_service), _=_user()
        )

        assert not hasattr(response, "revoked_tokens")
        assert "0 tokens" not in response.message

    async def test_endpoint_500s_when_the_write_fails(self):
        """A failed revocation must never read as a successful one."""
        from faultmaven.modules.auth.api import auth as auth_routes

        store = _store()
        auth_service = _auth_service(store)

        async def boom(user_id, revoked_at, ttl):
            raise ConnectionError("store down")

        store.revoke_user_tokens_before = boom

        with pytest.raises(HTTPException) as exc_info:
            await auth_routes.revoke_user_tokens(
                USER_ID, _admin_request(auth_service), _=_user()
            )
        assert exc_info.value.status_code == 500


class TestWatermarkCoversEveryMintPath:
    """Completeness: the property that motivated a watermark over a JTI index.

    FaultMaven mints tokens from three implementations, one of them
    synchronous. A watermark covers all of them without mint-time bookkeeping;
    an index would silently under-revoke whenever a mint path forgot to
    register while still reporting a complete revocation.
    """

    async def test_hs256_generator_tokens_are_revoked(self):
        store = _store()
        generator = _generator(store)
        auth_service = _auth_service(store)

        access = await generator.generate_access_token(_user())
        refresh = await generator.generate_refresh_token(_user())

        await auth_service.revoke_user_tokens(USER_ID)

        assert await generator.validate_access_token(access) is None
        assert await generator.validate_refresh_token(refresh) is None

    async def test_rs256_generator_tokens_are_revoked(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        from faultmaven.modules.auth.domain.services.jwt_token_generator import (
            RS256JWTTokenGenerator,
        )

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        public_pem = (
            key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )

        store = _store()
        generator = RS256JWTTokenGenerator(
            private_key=private_pem,
            public_key=public_pem,
            revocation_store=store,
            settings=SimpleNamespace(
                jwt_access_token_expire_minutes=60,
                jwt_refresh_token_expire_days=7,
            ),
        )
        auth_service = _auth_service(store)

        access = await generator.generate_access_token(_user())
        refresh = await generator.generate_refresh_token(_user())

        await auth_service.revoke_user_tokens(USER_ID)

        assert await generator.validate_access_token(access) is None
        assert await generator.validate_refresh_token(refresh) is None

    async def test_auth_service_own_synchronous_mint_is_revoked(self):
        """AuthService.generate_access_token is sync and mints its own jti.

        It could not write a per-user index without becoming async, so this
        is the mint path an index-based fix would most likely have missed.
        """
        store = _store()
        auth_service = _auth_service(store)

        with patch(
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=_auth_service_settings(),
        ):
            token = auth_service.generate_access_token(
                user_id=USER_ID,
                organization_id="org-769",
                email="revoked@local.faultmaven",
                roles=["user"],
            )

            await auth_service.revoke_user_tokens(USER_ID)

            with pytest.raises(TokenRevocationError):
                await auth_service.verify_token_with_revocation_check(
                    token, token_type="access"
                )

    async def test_other_users_are_unaffected(self):
        store = _store()
        generator = _generator(store)
        auth_service = _auth_service(store)

        victim_token = await generator.generate_access_token(_user())
        bystander_token = await generator.generate_access_token(_user("user-other"))

        await auth_service.revoke_user_tokens(USER_ID)

        assert await generator.validate_access_token(victim_token) is None
        assert await generator.validate_access_token(bystander_token) is not None


class TestReLoginStillWorks:
    """The watermark must not lock a user out permanently.

    Time is controlled by placing the watermark in the past rather than by
    mocking the decode path, so these exercise the real generator, the real
    production store, and the real validation rule. ``iat`` has whole-second
    granularity, and the rule is ``iat <= watermark`` (fail-secure), so a
    token minted in the same second as the revocation is rejected on
    purpose — the user retries and succeeds a second later.
    """

    async def test_token_minted_after_the_watermark_is_accepted(self):
        store = _store()
        generator = _generator(store)

        # A revocation that happened a minute ago.
        past = int(datetime.now(timezone.utc).timestamp()) - 60
        await store.revoke_user_tokens_before(USER_ID, past, ttl=3600)

        # The user logs back in; the fresh token is strictly newer.
        fresh = await generator.generate_access_token(_user())

        assert await generator.validate_access_token(fresh) is not None

    async def test_token_minted_before_the_watermark_is_still_rejected(self):
        """The contrast case, on the same clock: older tokens stay dead."""
        store = _store()
        generator = _generator(store)

        stale = await generator.generate_access_token(_user())
        future = int(datetime.now(timezone.utc).timestamp()) + 60
        await store.revoke_user_tokens_before(USER_ID, future, ttl=3600)

        assert await generator.validate_access_token(stale) is None


class TestRevocationArmsAreIndependent:
    """Per-jti and per-user revocation must not interfere."""

    async def test_per_jti_revocation_does_not_set_a_user_watermark(self):
        store = _store()
        generator = _generator(store)

        first = await generator.generate_access_token(_user())
        second = await generator.generate_access_token(_user())

        await generator.revoke_access_token(first)

        assert await generator.validate_access_token(first) is None
        # The user's other token must survive a single-token revocation.
        assert await generator.validate_access_token(second) is not None

    async def test_watermark_survives_independently_of_jti_entries(self):
        store = _store()
        auth_service = _auth_service(store)

        await auth_service.revoke_user_tokens(USER_ID)

        assert await store.is_user_revoked(
            USER_ID, int(datetime.now(timezone.utc).timestamp())
        )
        assert await store.is_revoked("some-unrelated-jti") is False


class TestStoreContract:
    """Direct unit coverage of the production store's watermark methods."""

    async def test_unknown_user_has_no_watermark(self):
        store = _store()
        assert await store.is_user_revoked("never-revoked", 1_700_000_000) is False

    async def test_iat_at_the_watermark_is_revoked(self):
        """Boundary: `iat == watermark` counts as revoked (fail-secure)."""
        store = _store()
        await store.revoke_user_tokens_before(USER_ID, 1_700_000_000, ttl=3600)

        assert await store.is_user_revoked(USER_ID, 1_700_000_000) is True
        assert await store.is_user_revoked(USER_ID, 1_699_999_999) is True
        assert await store.is_user_revoked(USER_ID, 1_700_000_001) is False

    async def test_watermark_uses_its_own_key_namespace(self):
        """A user watermark must not be readable as a revoked jti."""
        redis = _fake_redis()
        store = _store(redis)
        await store.revoke_user_tokens_before(USER_ID, 1_700_000_000, ttl=3600)

        assert await store.is_revoked(USER_ID) is False
        assert await redis.get(f"revoked:token:user:{USER_ID}") == "1700000000"

    async def test_later_revocation_overwrites_the_watermark(self):
        store = _store()
        await store.revoke_user_tokens_before(USER_ID, 1_700_000_000, ttl=3600)
        await store.revoke_user_tokens_before(USER_ID, 1_700_000_500, ttl=3600)

        assert await store.is_user_revoked(USER_ID, 1_700_000_400) is True

    async def test_watermark_ttl_outlives_the_longest_token(self):
        """A watermark that expired before the tokens it revokes would
        resurrect them, so the TTL must cover the refresh-token lifetime."""
        redis = _fake_redis()
        store = _store(redis)
        auth_service = _auth_service(store)

        await auth_service.revoke_user_tokens(USER_ID)

        ttl = await redis.ttl(f"revoked:token:user:{USER_ID}")
        assert ttl >= 7 * 86400 - 5


class TestServiceFailurePosture:
    """Writes propagate; a caller must never report an unrecorded revocation."""

    async def test_revoke_user_tokens_raises_on_store_failure(self):
        store = _store()
        auth_service = _auth_service(store)

        async def boom(user_id, revoked_at, ttl):
            raise ConnectionError("store down")

        store.revoke_user_tokens_before = boom

        with pytest.raises(ServiceError):
            await auth_service.revoke_user_tokens(USER_ID)

    async def test_revoke_user_tokens_raises_without_a_store(self):
        auth_service = _auth_service(None)

        with pytest.raises(ServiceError):
            await auth_service.revoke_user_tokens(USER_ID)

    async def test_request_path_fails_open_on_store_read_failure(self):
        """Documented posture (#767): the request path prefers availability."""
        store = _store()
        generator = _generator(store)
        auth_service = _auth_service(store)
        access = await generator.generate_access_token(_user())

        async def boom(user_id, issued_at):
            raise ConnectionError("store down")

        store.is_user_revoked = boom

        claims = await auth_service.verify_token_with_revocation_check(
            access, token_type="access"
        )
        assert claims["sub"] == USER_ID
