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

from faultmaven.config.settings import MAX_TOKEN_LIFETIME_DAYS
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

HS256_SECRET = "unit-test-secret-key-min-32-bytes!"
# The production defaults on purpose: an access lifetime that coincides with
# another token type's would let a per-type ceiling pass these tests by accident.
ACCESS_MINUTES = 15
REFRESH_DAYS = 7
DEFAULT_HINT = "access_token"

#: The absolute ceiling the code applies — the schema bound on token lifetime,
#: read from the same constant rather than restated, so this suite fails if the
#: bound and the ceiling ever drift apart.
MAX_ENTRY_TTL = MAX_TOKEN_LIFETIME_DAYS * 86400


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
        access_token_expire_minutes=ACCESS_MINUTES,
        refresh_token_expire_days=REFRESH_DAYS,
        issuer="faultmaven",
        audience="faultmaven-api",
    )


def _hs256_generator(store):
    return HS256JWTTokenGenerator(
        secret_key=HS256_SECRET,
        revocation_store=store,
        access_token_expire_minutes=ACCESS_MINUTES,
        refresh_token_expire_days=REFRESH_DAYS,
        issuer="faultmaven",
        audience="faultmaven-api",
    )


def _auth_settings_stub():
    """``settings.auth`` as the container passes it — the single expiry source."""
    return SimpleNamespace(
        jwt_access_token_expire_minutes=ACCESS_MINUTES,
        jwt_refresh_token_expire_days=REFRESH_DAYS,
    )


def _oauth_service(generator):
    """The service as the route gets it; revocation needs only the generator."""
    return OAuthServiceImpl(
        code_repository=None,
        user_repository=None,
        token_generator=generator,
        settings=_auth_settings_stub(),
    )


def _user():
    return SimpleNamespace(
        user_id="user-830",
        # Real user types all declare is_active; a stand-in that omits it
        # is not modelling a user. The mint gate refuses on absence.
        is_active=True,
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
            claims, "attacker-secret-not-ours-32-bytes!", algorithm="HS256"
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


class TestEntryLifetimeIsBounded:
    """Storage is bounded by an absolute ceiling, never by a supplied claim."""

    @pytest.mark.parametrize("hint", ALL_HINTS)
    @pytest.mark.parametrize("token_type", ["access", "refresh", "password_reset"])
    async def test_far_future_exp_is_capped(self, hint, token_type):
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
        assert 0 < ttl <= MAX_ENTRY_TTL

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


class TestEntryNeverExpiresBeforeTheTokenItRevokes:
    """The safety property: a lapsed entry resurrects a revoked token.

    Three ways to produce one, all of which a ceiling taken from the *currently
    configured* lifetime of *some type* would allow:

    - a token type with its own lifetime (`password_reset`),
    - a token minted before an operator lowered the setting,
    - a hint that routes a refresh token onto the access path — `token_type_hint`
      is optional in RFC 7009 and the handler routes an absent hint to access.
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

    @pytest.mark.parametrize("hint", ALL_HINTS)
    async def test_password_reset_token_entry_covers_its_full_hour(self, hint):
        """A type with neither the access nor the refresh lifetime.

        Reset tokens are signed with the auth service's key — the same key this
        generator verifies with — so they pass the signature gate and land on
        whichever path the hint chose. An access-lifetime ceiling would expire
        their entry 45 minutes before the token, and reset_password consults
        that same entry (#829): the link would work again.
        """
        redis = _fake_redis()
        generator = _rs256_generator(_store(redis))
        now = datetime.now(timezone.utc)
        reset_token = pyjwt.encode(
            {
                "sub": "user-830",
                "jti": "reset-jti",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(hours=1)).timestamp()),
                "type": "password_reset",
            },
            DEPLOYMENT_PRIVATE_KEY,
            algorithm="RS256",
        )

        await _call_revoke(_oauth_service(generator), reset_token, hint)

        ttl = await redis.ttl("revoked:token:jti:reset-jti")
        assert 3540 <= ttl <= 3600

    async def test_token_minted_before_the_expiry_was_lowered_stays_revoked(self):
        """An operator lowering the setting must not resurrect live tokens.

        30-day token, then the deployment is reconfigured to 7 days. A ceiling
        read from the *current* setting would drop the entry on day 7 and the
        outstanding token would work again for its remaining 23.
        """
        redis = _fake_redis()
        store = _store(redis)
        minting_generator = RS256JWTTokenGenerator(
            private_key=DEPLOYMENT_PRIVATE_KEY,
            public_key=DEPLOYMENT_PUBLIC_KEY,
            revocation_store=store,
            access_token_expire_minutes=ACCESS_MINUTES,
            refresh_token_expire_days=30,
            issuer="faultmaven",
            audience="faultmaven-api",
        )
        refresh = await minting_generator.generate_refresh_token(_user())
        jti = pyjwt.decode(refresh, options={"verify_signature": False})["jti"]

        # Same deployment, expiry now lowered to the 7-day default.
        await _call_revoke(_oauth_service(_rs256_generator(store)), refresh)

        ttl = await redis.ttl(f"revoked:token:jti:{jti}")
        assert ttl >= 30 * 86400 - 60
