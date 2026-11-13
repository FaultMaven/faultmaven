"""OAuth codes and refresh tokens are bound to the client they were issued to (#2750).

Driven through ``POST /auth/oauth/token`` with the real OAuth service and the
real token generators, on both signing algorithms. A code stores its client and
the exchange refuses any other before spending it; a refresh token names its
client, the grant refuses it under any other without revoking it, and a token
naming none is bound at its first OAuth refresh, but only to a known client
(owner ruling).
``POST /auth/refresh`` takes no client but carries the binding forward.

A refresh token is redeemed once on both paths, also under concurrency (#2764):
of two concurrent refreshes exactly one receives a pair, so a token never forks
into two chains or two clients.
"""

import asyncio
import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient

from tests.integration._app_rebuild import rebuild_app_under
from tests.utils import InMemoryRevocationStore

OAUTH_ENVIRONMENT = {
    "SKIP_SERVICE_CHECKS": "true",
    "OAUTH_ENABLED": "true",
    "OAUTH_REQUIRE_CONSENT": "false",
}

app = rebuild_app_under(OAUTH_ENVIRONMENT)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.usefixtures("oauth_environment"),
]

COPILOT = "faultmaven-copilot"
OTHER = "another-client"
SERVICE = "faultmaven-slack-agent"
REDIRECT = "https://abcdefghijklmnopabcdefghijklmnop.chromiumapp.org/"
TOKEN = "/api/v1/auth/oauth/token"
CODE_REFUSED = "[CLIENT_MISMATCH] Authorization code is not valid for this client"
REFRESH_REFUSED = "[CLIENT_MISMATCH] Refresh token is not valid for this client"


def _generator(algorithm, revocation_store):
    from faultmaven.modules.auth.domain.services.jwt_token_generator import (
        HS256JWTTokenGenerator,
        RS256JWTTokenGenerator,
    )

    common = dict(
        revocation_store=revocation_store,
        access_token_expire_minutes=15,
        refresh_token_expire_days=7,
        issuer="faultmaven",
        audience="faultmaven-api",
    )
    if algorithm == "HS256":
        secret = "integration-test-secret-padded-to-32-bytes-2750"
        return HS256JWTTokenGenerator(secret_key=secret, **common), secret
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    generator = RS256JWTTokenGenerator(
        private_key=private_pem, public_key=public_pem, **common
    )
    return generator, private_pem


class _Users:
    """Accounts served to the OAuth service, /auth/refresh and the CLI's mint."""

    def __init__(self, user):
        self.users = {user.user_id: user}

    async def get(self, user_id):
        return self.users.get(user_id)

    get_user = get

    async def get_user_by_username(self, username):
        return next((u for u in self.users.values() if u.username == username), None)

    async def create_user(self, username, *_, account_kind, service_channel, **kw):
        from faultmaven.modules.auth.domain.models.auth import DevUser

        user = DevUser(
            user_id=f"user-{username}",
            username=username,
            email=f"{username}@faultmaven.example",
            display_name=kw.get("display_name") or username,
            created_at=datetime.now(timezone.utc),
            account_kind=account_kind,
            service_channel=service_channel,
            enterprise_id=kw["enterprise_id"],
        )
        return await self.update_user(user)

    async def update_user(self, user):
        self.users[user.user_id] = user
        return user


@pytest.fixture(params=["HS256", "RS256"])
async def env(request, monkeypatch):
    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.config.settings import get_settings
    from faultmaven.modules.auth.api import auth as auth_routes
    from faultmaven.modules.auth.api.oauth import get_oauth_service
    from faultmaven.modules.auth.api.rate_limiting import reset_rate_limiter
    from faultmaven.modules.auth.domain.models.auth import DevUser
    from faultmaven.modules.auth.domain.services.oauth_service import (
        OAuthServiceImpl,
    )
    from faultmaven.modules.auth.infrastructure.repositories.oauth_code_repository import (
        InMemoryOAuthCodeRepository,
    )

    reset_rate_limiter()
    revocations = InMemoryRevocationStore()
    generator, signing_key = _generator(request.param, revocations)
    user = DevUser(
        user_id="user-2750",
        username="binder",
        email="binder@example.com",
        display_name="Binder",
        created_at=datetime.now(timezone.utc),
    )
    users = _Users(user)
    service = OAuthServiceImpl(
        code_repository=InMemoryOAuthCodeRepository(),
        user_repository=users,
        token_generator=generator,
        settings=get_settings().auth,
    )

    async def _users(_request):
        return users

    async def _revocations(_request):
        return revocations

    # /auth/refresh resolves its generator by auth mode; both answer this one.
    monkeypatch.setattr(auth_routes, "get_user_store", _users)
    monkeypatch.setattr(auth_routes, "get_token_revocation_store", _revocations)
    monkeypatch.setattr(auth_routes, "_build_local_jwt_generator", lambda _: generator)
    monkeypatch.setattr(app.state, "jwt_token_generator", generator, raising=False)
    app.dependency_overrides[require_authentication] = lambda: user
    app.dependency_overrides[get_oauth_service] = lambda: service
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield SimpleNamespace(
                client=client,
                generator=generator,
                service=service,
                users=users,
                user=user,
                algorithm=request.param,
                signing_key=signing_key,
                revocations=revocations,
            )
    finally:
        app.dependency_overrides.clear()


def _claims(token):
    return jwt.decode(token, options={"verify_signature": False})


def _client_claim(token):
    return _claims(token).get("client_id")


async def _unbound_refresh_token(env):
    return await env.generator.generate_refresh_token(
        env.user, state_read_at=datetime.now(timezone.utc)
    )


async def _code(client, via_consent=False):
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    params = {
        "client_id": COPILOT,
        "redirect_uri": REDIRECT,
        "state": "state-2750",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if via_consent:
        response = await client.post(
            "/api/v1/auth/oauth/authorize",
            json={**params, "approved": True, "scope": "openid"},
        )
    else:
        response = await client.get(
            "/api/v1/auth/oauth/authorize", params={**params, "response_type": "code"}
        )
    assert response.status_code == 200, response.text
    return response.json()["code"], verifier


async def _exchange(client, code, verifier, client_id):
    return await client.post(
        TOKEN,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
        },
    )


async def _refresh(client, refresh_token, client_id):
    return await client.post(
        TOKEN,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )


def _assert_refused(response, description):
    assert response.status_code == 400, response.text
    assert response.json() == {
        "error": "invalid_grant",
        "error_description": description,
    }


async def _bound_refresh_token(client):
    code, verifier = await _code(client)
    response = await _exchange(client, code, verifier, COPILOT)
    assert response.status_code == 200, response.text
    return response.json()["refresh_token"]


@pytest.mark.parametrize("via_consent", [False, True], ids=["auto", "consent"])
async def test_a_code_is_redeemed_only_by_its_client(env, via_consent):
    code, verifier = await _code(env.client, via_consent)

    _assert_refused(await _exchange(env.client, code, verifier, OTHER), CODE_REFUSED)

    # Refused before the claim: the rightful client still redeems it.
    response = await _exchange(env.client, code, verifier, COPILOT)
    assert response.status_code == 200, response.text
    assert _client_claim(response.json()["refresh_token"]) == COPILOT
    assert "client_id" not in _claims(response.json()["access_token"])


async def test_a_refresh_token_is_refused_under_another_client(env):
    token = await _bound_refresh_token(env.client)

    _assert_refused(await _refresh(env.client, token, OTHER), REFRESH_REFUSED)
    assert await env.generator.validate_refresh_token(token) is not None

    response = await _refresh(env.client, token, COPILOT)
    assert response.status_code == 200, response.text
    assert _client_claim(response.json()["refresh_token"]) == COPILOT


@pytest.mark.parametrize("known, other", [(COPILOT, SERVICE), (SERVICE, COPILOT)])
async def test_a_token_naming_no_client_is_bound_to_a_known_client(env, known, other):
    token = await _unbound_refresh_token(env)
    assert _client_claim(token) is None

    response = await _refresh(env.client, token, known)
    assert response.status_code == 200, response.text
    rotated = response.json()["refresh_token"]
    assert _client_claim(rotated) == known

    _assert_refused(await _refresh(env.client, rotated, other), REFRESH_REFUSED)


async def test_a_token_naming_no_client_is_refused_under_an_unknown_client(env):
    token = await _unbound_refresh_token(env)

    _assert_refused(await _refresh(env.client, token, OTHER), REFRESH_REFUSED)
    assert await env.generator.validate_refresh_token(token) is not None


async def test_auth_refresh_carries_the_binding_to_the_oauth_grant(env):
    token = await _bound_refresh_token(env.client)

    response = await env.client.post(
        "/api/v1/auth/refresh", json={"refresh_token": token}
    )
    assert response.status_code == 200, response.text
    rotated = response.json()["refresh_token"]
    assert _client_claim(rotated) == COPILOT

    _assert_refused(await _refresh(env.client, rotated, OTHER), REFRESH_REFUSED)
    assert (await _refresh(env.client, rotated, COPILOT)).status_code == 200


async def test_a_claim_that_is_not_a_string_is_a_mismatch(env):
    token = await _bound_refresh_token(env.client)
    forged = jwt.encode(
        {**_claims(token), "client_id": [COPILOT]},
        env.signing_key,
        algorithm=env.algorithm,
    )
    assert await env.generator.validate_refresh_token(forged) is not None

    _assert_refused(await _refresh(env.client, forged, COPILOT), REFRESH_REFUSED)


async def test_an_overlong_client_is_never_bound(env, monkeypatch):
    # Even a configured one: the cap is checked on its own.
    allowed = [COPILOT, "c" * 256]
    monkeypatch.setattr(env.service.settings, "oauth_allowed_clients", allowed)
    token = await _unbound_refresh_token(env)

    _assert_refused(await _refresh(env.client, token, "c" * 256), REFRESH_REFUSED)
    assert await env.generator.validate_refresh_token(token) is not None


async def test_a_cli_minted_credential_refreshes_under_the_service_client(
    env, monkeypatch, capsys
):
    """The done-when: the CLI's own mint, with its default ``--client-id``."""
    from faultmaven.cli import provision_service_account as cli
    from faultmaven.config.settings import AuthMode

    class _Container:
        jwt_token_generator = env.generator

        async def initialize(self):
            pass

        def get_user_store(self):
            return env.users

    oauth_mode = SimpleNamespace(auth=SimpleNamespace(auth_mode=AuthMode.OAUTH))
    monkeypatch.setattr(cli, "container", _Container())
    monkeypatch.setattr(cli, "get_settings", lambda: oauth_mode)

    assert await cli.provision("slack-agent", "service", "slack", token_only=True)
    out = capsys.readouterr().out.split()
    credential = next(line for line in out if line.startswith("eyJ"))

    _assert_refused(await _refresh(env.client, credential, COPILOT), REFRESH_REFUSED)
    assert (await _refresh(env.client, credential, SERVICE)).status_code == 200


# ---------------------------------------------------------------------------
# One redemption per refresh token, also under concurrency (#2764)
# ---------------------------------------------------------------------------

REVOKED = "[INVALID_REFRESH_TOKEN] Invalid or expired refresh token"
REFRESH = "/api/v1/auth/refresh"


def _hold_mints_until_two_arrive(monkeypatch, generator):
    """Hold each refresh at its mint until two have passed validation.

    Patched on the class: ``/auth/refresh`` builds its generator per request.
    Bounded, so a refresh that never reaches the mint fails the test.
    """
    cls = type(generator)
    mint = cls.generate_token_pair
    arrived = []
    both = asyncio.Event()

    async def held(self, *args, **kwargs):
        arrived.append(1)
        if len(arrived) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=10)
        return await mint(self, *args, **kwargs)

    monkeypatch.setattr(cls, "generate_token_pair", held)


def _won_and_lost(responses, lost_status):
    statuses = sorted(r.status_code for r in responses)
    assert statuses == sorted([200, lost_status]), [r.text for r in responses]
    won = next(r for r in responses if r.status_code == 200)
    lost = next(r for r in responses if r.status_code != 200)
    return won.json(), lost.json()


async def test_concurrent_oauth_refreshes_deliver_one_pair(env, monkeypatch):
    token = await _bound_refresh_token(env.client)
    _hold_mints_until_two_arrive(monkeypatch, env.generator)

    won, lost = _won_and_lost(
        await asyncio.gather(*(_refresh(env.client, token, COPILOT) for _ in "ab")),
        400,
    )

    assert await env.generator.validate_refresh_token(won["refresh_token"])
    assert await env.generator.validate_refresh_token(token) is None
    # The loser's answer is a revoked token's, byte for byte.
    assert lost == {"error": "invalid_grant", "error_description": REVOKED}
    assert (await _refresh(env.client, token, COPILOT)).json() == lost


async def test_a_token_naming_no_client_binds_to_one_client_under_a_race(
    env, monkeypatch
):
    """#2750's invariant: one client-less token is bound to at most one client."""
    token = await _unbound_refresh_token(env)
    _hold_mints_until_two_arrive(monkeypatch, env.generator)

    responses = await asyncio.gather(
        _refresh(env.client, token, COPILOT), _refresh(env.client, token, SERVICE)
    )
    won, lost = _won_and_lost(responses, 400)

    winner = COPILOT if responses[0].status_code == 200 else SERVICE
    assert _client_claim(won["refresh_token"]) == winner
    assert lost == {"error": "invalid_grant", "error_description": REVOKED}
    assert await env.generator.validate_refresh_token(token) is None


async def test_concurrent_auth_refreshes_deliver_one_pair(env, monkeypatch):
    token = await _bound_refresh_token(env.client)
    _hold_mints_until_two_arrive(monkeypatch, env.generator)

    won, lost = _won_and_lost(
        await asyncio.gather(
            *(env.client.post(REFRESH, json={"refresh_token": token}) for _ in "ab")
        ),
        401,
    )

    assert await env.generator.validate_refresh_token(won["refresh_token"])
    assert await env.generator.validate_refresh_token(token) is None
    # The 401 a revoked token gets, byte for byte.
    revoked = await env.client.post(REFRESH, json={"refresh_token": token})
    assert revoked.status_code == 401
    assert lost == revoked.json()
    assert "invalid, expired, or revoked" in str(lost)


async def _redeem(env, path, token):
    if path == "oauth":
        return await _refresh(env.client, token, COPILOT)
    return await env.client.post(REFRESH, json={"refresh_token": token})


@pytest.mark.parametrize("path", ["oauth", "auth"])
async def test_a_failed_mint_leaves_the_token_redeemable(env, monkeypatch, path):
    token = await _bound_refresh_token(env.client)
    cls = type(env.generator)
    mint = cls.generate_token_pair
    failures = []

    async def fails_once(self, *args, **kwargs):
        if not failures:
            failures.append(1)
            raise RuntimeError("mint failed")
        return await mint(self, *args, **kwargs)

    monkeypatch.setattr(cls, "generate_token_pair", fails_once)

    assert (await _redeem(env, path, token)).status_code == 500
    assert (await _redeem(env, path, token)).status_code == 200


@pytest.mark.parametrize("path", ["oauth", "auth"])
async def test_a_refresh_token_with_no_jti_is_refused(env, path):
    from faultmaven.modules.auth.domain.services.jwt_token_generator import (
        RefreshClaim,
    )

    claims = _claims(await _bound_refresh_token(env.client))
    del claims["jti"]
    jti_less = jwt.encode(claims, env.signing_key, algorithm=env.algorithm)

    assert await env.generator.validate_refresh_token(jti_less) is None
    assert await env.generator.claim_refresh_token(claims) is RefreshClaim.NO_JTI
    response = await _redeem(env, path, jti_less)
    assert response.status_code == (400 if path == "oauth" else 401), response.text


@pytest.mark.parametrize("path", ["oauth", "auth"])
async def test_a_claim_the_store_cannot_write_delivers_nothing(env, monkeypatch, path):
    token = await _bound_refresh_token(env.client)

    async def unwritable(jti, ttl):
        raise ConnectionError("store unavailable")

    monkeypatch.setattr(env.revocations, "claim_token", unwritable)

    response = await _redeem(env, path, token)
    assert response.status_code == 500, response.text
    assert "access_token" not in response.text


@pytest.mark.parametrize("path", ["oauth", "auth"])
async def test_a_token_that_expires_during_the_mint_still_gets_its_pair(
    env, monkeypatch, path
):
    """It validated, so it is claimable: the claim never checks expiry (#2764).

    The clock is moved past the token's 7-day ``exp`` once the mint returns,
    for this module and PyJWT alike, so a claim that decoded the token again
    would refuse it.
    """
    import jwt.api_jwt

    from faultmaven.modules.auth.domain.services import jwt_token_generator

    token = await _bound_refresh_token(env.client)
    cls = type(env.generator)
    mint = cls.generate_token_pair

    class _PastExp(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=8)

    async def mint_then_expire(self, *args, **kwargs):
        pair = await mint(self, *args, **kwargs)
        monkeypatch.setattr(jwt_token_generator, "datetime", _PastExp)
        monkeypatch.setattr(jwt.api_jwt, "datetime", _PastExp)
        return pair

    monkeypatch.setattr(cls, "generate_token_pair", mint_then_expire)

    response = await _redeem(env, path, token)
    assert response.status_code == 200, response.text
    assert await env.revocations.is_revoked(_claims(token)["jti"])
