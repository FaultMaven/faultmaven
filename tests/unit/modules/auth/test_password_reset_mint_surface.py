"""Password reset mints through the deployment's generator (#959).

Two properties, both of which the pre-#959 code could not hold:

**It works under either algorithm, with the key state production actually has.**
``UserService`` used to sign reset tokens with ``auth_service._private_key`` and
``security.jwt_algorithm``. ``AuthService._load_keys`` always produces an RSA
pair — configured, or dev-generated — so under ``JWT_ALGORITHM=HS256`` that is
an RSA PEM handed to an HMAC signer, which PyJWT rejects with
``InvalidKeyError``. The flow's own tests passed only because a fixture assigned
an HMAC secret to ``_private_key``, a state no deployment can reach. Here both
services are built the way a deployment builds them: the auth service carries
its dev RSA pair *and* the generator carries the HMAC secret, simultaneously.

**Three request outcomes, one observable.** A real account, an unknown address
and a deactivated account must be indistinguishable to whoever receives the
token — who can base64-decode the payload they were handed, so the comparison
that matters is real-vs-decoy on every claim, not decoy-vs-decoy. Only the
redemption differs, and only in that the two decoys never redeem.

No HTTP route reaches ``request_password_reset`` today: there is no endpoint
and no caller outside tests. These properties are pinned because the flow is
maintained and tested, not because a form is live.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import jwt as pyjwt
import pytest

from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
)
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthenticationError,
    AuthService,
)
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
    RS256JWTTokenGenerator,
)
from faultmaven.modules.auth.domain.services.user_service import (
    RESET_REFUSED_CODE,
    UserService,
)
from faultmaven.utils.password import verify_password
from tests.utils import InMemoryRevocationStore

pytestmark = pytest.mark.asyncio

SECRET = "unit-test-secret-key-please-ignore"
ISSUER = "faultmaven-api"
AUDIENCE = "faultmaven-app"
EMAIL = "reset-mint@local.faultmaven"
EMAIL_UNKNOWN = "nobody-here@local.faultmaven"
OLD_PASSWORD = "Str0ng-P4ssw0rd!"
NEW_PASSWORD = "An0ther-P4ssw0rd!"


def _settings(jwt_algorithm: str):
    """Settings shaped like a real deployment's, for both services.

    ``jwt_algorithm`` is carried deliberately: it is the setting the old signing
    path read, and the one that made HS256 deployments fail. Nothing on the new
    path consults it — each generator knows its own algorithm — so a test that
    passes here with ``HS256`` set is asserting exactly that.
    """
    return SimpleNamespace(
        auth=SimpleNamespace(
            auth_mode="local" if jwt_algorithm == "HS256" else "oauth",
            jwt_access_token_expire_minutes=60,
            jwt_refresh_token_expire_days=7,
        ),
        security=SimpleNamespace(
            jwt_algorithm=jwt_algorithm,
            jwt_issuer=ISSUER,
            jwt_audience=AUDIENCE,
            token_revocation_prefix="revoked:token:",
            jwt_private_key=None,
            jwt_public_key=None,
            jwt_private_key_path=None,
            jwt_public_key_path=None,
            jwt_secret_key=SimpleNamespace(get_secret_value=lambda: SECRET),
        ),
    )


def _fake_redis():
    import fakeredis.aioredis as fakeredis_aio

    return fakeredis_aio.FakeRedis(decode_responses=True)


def _rsa_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

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


def _generator(algorithm: str, store):
    if algorithm == "HS256":
        return HS256JWTTokenGenerator(
            secret_key=SECRET,
            revocation_store=store,
            access_token_expire_minutes=60,
            refresh_token_expire_days=7,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
    private_pem, public_pem = _rsa_keypair()
    return RS256JWTTokenGenerator(
        private_key=private_pem,
        public_key=public_pem,
        revocation_store=store,
        access_token_expire_minutes=60,
        refresh_token_expire_days=7,
        issuer=ISSUER,
        audience=AUDIENCE,
    )


async def _build(algorithm: str):
    """A UserService wired the way the Composition Root wires it."""
    store = InMemoryRevocationStore()
    settings = _settings(algorithm)

    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=settings,
    ):
        # No key fabrication: this is the RSA pair every install ends up with.
        auth_service = AuthService(revocation_store=store)
    assert "BEGIN" in auth_service._private_key, (
        "AuthService must hold a real RSA PEM here — that co-existence with an "
        "HMAC-signing generator is the state the old code could not survive."
    )

    with patch(
        "faultmaven.modules.auth.domain.services.user_service.get_settings",
        return_value=settings,
    ):
        user_service = UserService(
            user_repo=InMemoryUserRepository(),
            auth_service=auth_service,
            token_generator=_generator(algorithm, store),
            redis_client=_fake_redis(),
        )

    user = await user_service.register_user(
        email=EMAIL, password=OLD_PASSWORD, full_name="Reset Mint"
    )
    return user_service, user


@pytest.mark.parametrize("algorithm", ["HS256", "RS256"])
class TestResetRoundTripUnderBothAlgorithms:
    """Request → redeem, with the keys a deployment really has."""

    async def test_reset_completes(self, algorithm):
        user_service, user = await _build(algorithm)

        reset_token = await user_service.request_password_reset(email=EMAIL)
        updated = await user_service.reset_password(reset_token, NEW_PASSWORD)

        assert verify_password(NEW_PASSWORD, updated.hashed_password)
        assert not verify_password(OLD_PASSWORD, updated.hashed_password)

    async def test_the_token_is_signed_with_that_algorithm(self, algorithm):
        """The header names the generator's algorithm, not the setting's."""
        user_service, _user = await _build(algorithm)

        reset_token = await user_service.request_password_reset(email=EMAIL)

        assert pyjwt.get_unverified_header(reset_token)["alg"] == algorithm

    async def test_the_single_use_key_is_tracked(self, algorithm):
        """The jti in Redis is the jti in the token the caller was handed."""
        user_service, user = await _build(algorithm)

        reset_token = await user_service.request_password_reset(email=EMAIL)

        jti = pyjwt.decode(reset_token, options={"verify_signature": False})["jti"]
        assert await user_service.redis_client.get(f"password_reset:{jti}") == (
            user.user_id
        )


class TestDeactivatedAccountsAreNotEnumerable:
    """A deactivated account is refused at the mint, and says nothing by it."""

    async def _deactivate(self, user_service, user):
        stored = await user_service.user_repo.get(user.user_id)
        stored.is_active = False
        await user_service.user_repo.save(stored)

    async def test_a_decoy_is_not_distinguishable_from_a_real_token(self):
        """The comparison that matters: what the holder can decode.

        A payload is base64, so every claim is readable by whoever receives the
        token. A decoy that carried a marker address — the pre-#959
        ``dummy@dummy.local`` — announced itself to one decode, which is the
        whole anti-enumeration measure undone.
        """
        real_service, real_user = await _build("HS256")
        real_token = await real_service.request_password_reset(email=EMAIL)

        # Same address, same submitted spelling; the account is deactivated in
        # one deployment and absent from the other.
        deactivated_service, deactivated_user = await _build("HS256")
        await self._deactivate(deactivated_service, deactivated_user)
        deactivated_token = await deactivated_service.request_password_reset(
            email=EMAIL
        )

        unknown_service, _ = await _build("HS256")
        unknown_token = await unknown_service.request_password_reset(
            email=EMAIL_UNKNOWN
        )

        real = await real_service.token_generator.verify_password_reset_token(
            real_token
        )
        deactivated = (
            await (
                deactivated_service.token_generator.verify_password_reset_token(
                    deactivated_token
                )
            )
        )
        unknown = await unknown_service.token_generator.verify_password_reset_token(
            unknown_token
        )

        # Same header and same claim NAMES across all three.
        headers = {
            pyjwt.get_unverified_header(t)["alg"]
            for t in (real_token, deactivated_token, unknown_token)
        }
        assert headers == {"HS256"}
        assert sorted(real) == sorted(deactivated) == sorted(unknown)

        # Same claim VALUES for everything the holder could compare, including
        # the address they submitted.
        for claim in ("type", "iss", "aud", "email"):
            assert real[claim] == deactivated[claim], claim
        assert real["email"] == EMAIL.lower()
        assert unknown["email"] == EMAIL_UNKNOWN.lower()

        # sub and jti are the only differences, and neither discloses anything:
        # a real user_id is itself a uuid4, so all four are uuid4 strings.
        assert deactivated["sub"] != deactivated_user.user_id
        for claim_value in (
            real["sub"],
            real["jti"],
            deactivated["sub"],
            deactivated["jti"],
            unknown["sub"],
            unknown["jti"],
        ):
            uuid.UUID(claim_value)
        assert real["sub"] == real_user.user_id
        assert len({real["jti"], deactivated["jti"], unknown["jti"]}) == 3

        # Exactly which claims differ, asserted rather than asserted-about: any
        # claim that starts differing shows up here. iat/exp are excluded
        # because these three tokens are minted seconds apart by this test —
        # they carry request time, which the holder knows anyway; their
        # LIFETIME is asserted equal above.
        time_claims = {"iat", "exp"}
        assert {
            claim
            for claim in real
            if claim not in time_claims and real[claim] != deactivated[claim]
        } == {"sub", "jti"}

        # Lifetimes agree, so exp/iat cannot separate them either.
        assert real["exp"] - real["iat"] == deactivated["exp"] - deactivated["iat"]

    async def test_the_submitted_spelling_does_not_leak_through_case(self):
        """Lookup is case-insensitive; the claim must be too.

        Otherwise a real token carries the STORED spelling while a decoy
        carries the SUBMITTED one, and the difference answers "does this
        account exist" for any address registered in mixed case.
        """
        user_service, _user = await _build("HS256")
        generator = user_service.token_generator

        real_token = await user_service.request_password_reset(email=EMAIL.upper())
        decoy = await generator.generate_dummy_reset_token(EMAIL.upper())

        real_claims = await generator.verify_password_reset_token(real_token)
        decoy_claims = await generator.verify_password_reset_token(decoy)
        assert real_claims["email"] == decoy_claims["email"] == EMAIL.lower()

    async def test_the_decoys_still_agree_with_each_other(self):
        """Deactivated and unknown differ only in the address each was asked."""
        user_service, user = await _build("HS256")
        await self._deactivate(user_service, user)

        deactivated_token = await user_service.request_password_reset(email=EMAIL)
        unknown_token = await user_service.request_password_reset(email=EMAIL_UNKNOWN)

        generator = user_service.token_generator
        deactivated = await generator.verify_password_reset_token(deactivated_token)
        unknown = await generator.verify_password_reset_token(unknown_token)

        assert pyjwt.get_unverified_header(deactivated_token) == (
            pyjwt.get_unverified_header(unknown_token)
        )
        assert sorted(deactivated) == sorted(unknown)
        assert deactivated["exp"] - deactivated["iat"] == (
            unknown["exp"] - unknown["iat"]
        )
        # Each echoes the address it was asked about, and nothing else.
        assert deactivated["email"] == EMAIL.lower()
        assert unknown["email"] == EMAIL_UNKNOWN.lower()
        assert deactivated["sub"] != user.user_id

    async def test_the_deactivated_accounts_token_does_not_redeem(self):
        user_service, user = await _build("HS256")
        await self._deactivate(user_service, user)

        reset_token = await user_service.request_password_reset(email=EMAIL)

        with pytest.raises(AuthenticationError) as refusal:
            await user_service.reset_password(reset_token, NEW_PASSWORD)
        assert refusal.value.error_code == RESET_REFUSED_CODE

        stored = await user_service.user_repo.get(user.user_id)
        assert verify_password(OLD_PASSWORD, stored.hashed_password)

    async def test_no_single_use_key_is_written_for_a_refused_mint(self):
        """Nothing to redeem means nothing to track — and nothing to leak.

        A Redis key naming the deactivated user would be an oracle for anyone
        who can read the store, and would also let the decoy be told apart from
        the unknown-email one by an operator staring at the same store.
        """
        user_service, user = await _build("HS256")
        await self._deactivate(user_service, user)

        reset_token = await user_service.request_password_reset(email=EMAIL)

        jti = pyjwt.decode(reset_token, options={"verify_signature": False})["jti"]
        assert await user_service.redis_client.get(f"password_reset:{jti}") is None


class TestTheGeneratorRefusesWhatIsNotAResetToken:
    """Type is checked where the signature is, so callers catch one thing."""

    @pytest.mark.parametrize("algorithm", ["HS256", "RS256"])
    async def test_an_access_token_is_not_a_reset_token(self, algorithm):
        user_service, user = await _build(algorithm)
        generator = user_service.token_generator

        access_token = await generator.generate_access_token(
            SimpleNamespace(
                user_id=user.user_id,
                username=user.username,
                email=user.email,
                roles=["member"],
                is_active=True,
                organization_id="org-1",
            )
        )

        with pytest.raises(pyjwt.InvalidTokenError):
            await generator.verify_password_reset_token(access_token)

    async def test_a_token_from_another_deployment_is_refused(self):
        """A different secret is a different deployment."""
        user_service, _user = await _build("HS256")

        forged = pyjwt.encode(
            {
                "sub": "someone",
                "type": "password_reset",
                "iss": ISSUER,
                "aud": AUDIENCE,
                "exp": 9999999999,
                "jti": "forged",
            },
            "a-different-secret-entirely-not-this-deployments",
            algorithm="HS256",
        )

        with pytest.raises(pyjwt.InvalidTokenError):
            await user_service.token_generator.verify_password_reset_token(forged)
