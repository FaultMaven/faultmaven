"""The revocation store only ever records tokens this deployment signed (#830).

``POST /api/v1/auth/oauth/revoke`` is unauthenticated by design (RFC 7009), and
before this gate the handler decoded the submitted token with
``verify_signature: False`` — so any caller could write a key of their choosing,
with a TTL of their choosing (a crafted ``exp``), into Redis.

These tests drive the real endpoint function through the real
``OAuthServiceImpl``, the real token generators and the PRODUCTION revocation
store over FakeRedis, and assert on the store's contents. They pin the property,
not one crafted token: every way of presenting a token this deployment did not
sign must leave the store untouched while the endpoint still answers success.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from faultmaven.modules.auth.api.oauth import RevokeRequest, revoke
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
    RS256JWTTokenGenerator,
)
from faultmaven.modules.auth.domain.services.oauth_service import OAuthServiceImpl
from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
    RedisTokenRevocationStore,
)

pytestmark = pytest.mark.asyncio

HS256_SECRET = "unit-test-secret-key-please-ignore"
ACCESS_MINUTES = 60
REFRESH_DAYS = 7
DEFAULT_HINT = "access_token"


def _keypair():
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
    return private_pem, public_pem


# Generated once: RSA keygen is the slowest thing in this module and the keys
# carry no per-test state.
DEPLOYMENT_PRIVATE_KEY, DEPLOYMENT_PUBLIC_KEY = _keypair()
ATTACKER_PRIVATE_KEY, _ATTACKER_PUBLIC_KEY = _keypair()


def _settings_stub():
    return SimpleNamespace(
        jwt_access_token_expire_minutes=ACCESS_MINUTES,
        jwt_refresh_token_expire_days=REFRESH_DAYS,
    )


def _fake_redis():
    import fakeredis.aioredis as fakeredis_aio

    return fakeredis_aio.FakeRedis(decode_responses=True)


def _store(redis):
    return RedisTokenRevocationStore(redis, key_prefix="revoked:token:")


def _rs256_generator(store):
    return RS256JWTTokenGenerator(
        private_key=DEPLOYMENT_PRIVATE_KEY,
        public_key=DEPLOYMENT_PUBLIC_KEY,
        revocation_store=store,
        settings=_settings_stub(),
        issuer="faultmaven",
        audience="faultmaven-api",
    )


def _hs256_generator(store):
    return HS256JWTTokenGenerator(
        secret_key=HS256_SECRET,
        revocation_store=store,
        settings=_settings_stub(),
        issuer="faultmaven",
        audience="faultmaven-api",
    )


def _oauth_service(generator):
    """The service as the route gets it; revocation needs only the generator."""
    return OAuthServiceImpl(
        code_repository=None,
        user_repository=None,
        token_generator=generator,
        settings=_settings_stub(),
    )


def _user():
    return SimpleNamespace(
        user_id="user-830",
        username="revoker",
        email="revoker@local.faultmaven",
        roles=["user"],
        organization_id="org-830",
    )


async def _call_revoke(oauth_service, token: str, hint=DEFAULT_HINT):
    """Invoke the endpoint exactly as FastAPI would.

    ``hint=None`` is the RFC 7009 case of a client that sends no
    ``token_type_hint`` at all — which the handler routes as an access token.
    """
    return await revoke(
        revoke_request=RevokeRequest(
            token=token, token_type_hint=hint, client_id="faultmaven-copilot"
        ),
        oauth_service=oauth_service,
    )


async def _keys(redis):
    return [k async for k in redis.scan_iter("*")]


def _forged_tokens():
    """Every shape of "a token this deployment did not sign"."""
    now = datetime.now(timezone.utc)
    far_future = int((now + timedelta(days=3650)).timestamp())
    claims = {
        "sub": "user-830",
        "jti": "attacker-chosen-jti",
        "iat": int(now.timestamp()),
        "exp": far_future,
        "type": "access",
    }
    return {
        # alg=none: unsigned, the cheapest forgery
        "unsigned": pyjwt.encode(claims, key="", algorithm="none"),
        # Signed, but with a key this deployment does not hold
        "wrong_hs256_key": pyjwt.encode(
            claims, "attacker-secret-not-ours", algorithm="HS256"
        ),
        "wrong_rs256_key": pyjwt.encode(
            claims, ATTACKER_PRIVATE_KEY, algorithm="RS256"
        ),
        # Not a JWT at all
        "garbage": "not-a-jwt",
        # Valid signature shape but tampered payload
        "tampered": pyjwt.encode(claims, DEPLOYMENT_PRIVATE_KEY, algorithm="RS256")[:-4]
        + "AAAA",
    }


class TestForgedTokensNeverReachTheStore:
    """The property: no store write without a signature-valid token."""

    @pytest.mark.parametrize("shape", sorted(_forged_tokens()))
    @pytest.mark.parametrize("hint", ["access_token", "refresh_token"])
    async def test_forged_token_writes_nothing_and_still_succeeds(self, shape, hint):
        redis = _fake_redis()
        oauth_service = _oauth_service(_rs256_generator(_store(redis)))

        result = await _call_revoke(oauth_service, _forged_tokens()[shape], hint)

        # RFC 7009: revoking an invalid token is a success...
        assert result == {}
        # ...but nothing about it is worth storing.
        assert await _keys(redis) == []

    async def test_forged_token_cannot_choose_a_key_name(self):
        """The attacker-chosen jti must not appear anywhere in the keyspace."""
        redis = _fake_redis()
        oauth_service = _oauth_service(_rs256_generator(_store(redis)))

        await _call_revoke(oauth_service, _forged_tokens()["unsigned"])

        assert await redis.get("revoked:token:jti:attacker-chosen-jti") is None
        assert await _keys(redis) == []


class TestGenuineTokensAreStillRevoked:
    """The gate must not cost the endpoint its actual job."""

    async def test_signed_access_token_is_revoked(self):
        redis = _fake_redis()
        store = _store(redis)
        generator = _rs256_generator(store)
        access = await generator.generate_access_token(_user())

        await _call_revoke(_oauth_service(generator), access, "access_token")

        jti = pyjwt.decode(access, options={"verify_signature": False})["jti"]
        assert await store.is_revoked(jti) is True
        assert await generator.validate_access_token(access) is None

    async def test_signed_refresh_token_is_revoked(self):
        redis = _fake_redis()
        store = _store(redis)
        generator = _rs256_generator(store)
        refresh = await generator.generate_refresh_token(_user())

        await _call_revoke(_oauth_service(generator), refresh, "refresh_token")

        assert await generator.validate_refresh_token(refresh) is None

    async def test_hint_does_not_have_to_match_the_token(self):
        """``token_type_hint`` is a hint; a token we signed stays revocable."""
        redis = _fake_redis()
        store = _store(redis)
        generator = _rs256_generator(store)
        refresh = await generator.generate_refresh_token(_user())

        await _call_revoke(_oauth_service(generator), refresh, "access_token")

        assert await generator.validate_refresh_token(refresh) is None

    async def test_local_logout_path_still_revokes(self):
        """The authenticated HS256 path shares the helper and must not break."""
        redis = _fake_redis()
        store = _store(redis)
        generator = _hs256_generator(store)
        refresh = await generator.generate_refresh_token(_user())

        await generator.revoke_refresh_token(refresh)

        assert await generator.validate_refresh_token(refresh) is None

    async def test_local_generator_rejects_a_foreign_token(self):
        """Same gate on the HS256 half — not just the cloud generator."""
        redis = _fake_redis()
        generator = _hs256_generator(_store(redis))

        await generator.revoke_access_token(_forged_tokens()["unsigned"])

        assert await _keys(redis) == []


class TestExpiredTokens:
    """An expired token has nothing left to revoke."""

    async def test_signed_but_expired_token_writes_nothing(self):
        redis = _fake_redis()
        oauth_service = _oauth_service(_rs256_generator(_store(redis)))
        expired = pyjwt.encode(
            {
                "sub": "user-830",
                "jti": "expired-jti",
                "iat": int(
                    (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
                ),
                "exp": int(
                    (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()
                ),
                "type": "access",
            },
            DEPLOYMENT_PRIVATE_KEY,
            algorithm="RS256",
        )

        result = await _call_revoke(oauth_service, expired)

        assert result == {}
        assert await _keys(redis) == []


ALL_HINTS = ["access_token", "refresh_token", None]


class TestEntryLifetimeIsBoundedByConfiguration:
    """TTL comes from configuration, never from a caller-supplied claim."""

    @pytest.mark.parametrize("hint", ALL_HINTS)
    @pytest.mark.parametrize(
        "token_type,ttl_cap",
        [
            ("access", ACCESS_MINUTES * 60),
            ("refresh", REFRESH_DAYS * 86400),
        ],
    )
    async def test_far_future_exp_is_capped(self, hint, token_type, ttl_cap):
        """Even a legitimately signed token cannot buy a multi-year entry."""
        redis = _fake_redis()
        generator = _rs256_generator(_store(redis))
        now = datetime.now(timezone.utc)
        long_lived = pyjwt.encode(
            {
                "sub": "user-830",
                "jti": "long-lived-jti",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(days=3650)).timestamp()),
                "type": token_type,
            },
            DEPLOYMENT_PRIVATE_KEY,
            algorithm="RS256",
        )

        await _call_revoke(_oauth_service(generator), long_lived, hint)

        ttl = await redis.ttl("revoked:token:jti:long-lived-jti")
        assert 0 < ttl <= ttl_cap

    async def test_normal_token_ttl_still_tracks_its_own_expiry(self):
        """The cap is a ceiling, not a replacement: a short token stays short."""
        redis = _fake_redis()
        generator = _rs256_generator(_store(redis))
        now = datetime.now(timezone.utc)
        short = pyjwt.encode(
            {
                "sub": "user-830",
                "jti": "short-jti",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=5)).timestamp()),
                "type": "access",
            },
            DEPLOYMENT_PRIVATE_KEY,
            algorithm="RS256",
        )

        await _call_revoke(_oauth_service(generator), short, "access_token")

        ttl = await redis.ttl("revoked:token:jti:short-jti")
        assert 290 <= ttl <= 300


class TestCeilingFollowsTheTokenNotTheHint:
    """`token_type_hint` is optional and may be wrong (RFC 7009 §2.1).

    The handler routes an absent hint to the access path, so a genuine refresh
    token reaches `revoke_access_token` as a matter of course. Taking the
    ceiling from the routed method would truncate its entry to the access
    lifetime — the entry would expire in minutes while the token stayed valid
    for days, after the endpoint answered 200.
    """

    @pytest.mark.parametrize("hint", ALL_HINTS)
    async def test_refresh_token_keeps_its_full_entry_under_any_hint(self, hint):
        redis = _fake_redis()
        generator = _rs256_generator(_store(redis))
        refresh = await generator.generate_refresh_token(_user())
        jti = pyjwt.decode(refresh, options={"verify_signature": False})["jti"]

        await _call_revoke(_oauth_service(generator), refresh, hint)

        ttl = await redis.ttl(f"revoked:token:jti:{jti}")
        # Covers the token's remaining life, not the 1h access lifetime.
        assert ttl > ACCESS_MINUTES * 60
        assert REFRESH_DAYS * 86400 - 60 <= ttl <= REFRESH_DAYS * 86400

    @pytest.mark.parametrize("hint", ALL_HINTS)
    async def test_access_token_is_not_extended_by_a_refresh_hint(self, hint):
        """The same rule in the other direction: no free extension either."""
        redis = _fake_redis()
        generator = _rs256_generator(_store(redis))
        access = await generator.generate_access_token(_user())
        jti = pyjwt.decode(access, options={"verify_signature": False})["jti"]

        await _call_revoke(_oauth_service(generator), access, hint)

        ttl = await redis.ttl(f"revoked:token:jti:{jti}")
        assert 0 < ttl <= ACCESS_MINUTES * 60

    async def test_local_logout_refresh_entry_covers_the_token(self):
        """The HS256 path routes by method name only — same ceiling rule."""
        redis = _fake_redis()
        generator = _hs256_generator(_store(redis))
        refresh = await generator.generate_refresh_token(_user())
        jti = pyjwt.decode(refresh, options={"verify_signature": False})["jti"]

        # Deliberately the ACCESS method: nothing but the token's own claims
        # may decide the ceiling.
        await generator.revoke_access_token(refresh)

        ttl = await redis.ttl(f"revoked:token:jti:{jti}")
        assert ttl > ACCESS_MINUTES * 60
