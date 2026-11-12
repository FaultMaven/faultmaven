"""OAuth codes and refresh tokens are bound to the client they were issued to (#2750).

Driven through ``POST /auth/oauth/token`` with the real OAuth service and the
real token generators, on both signing algorithms. A code stores its client and
the exchange refuses any other before spending it; a refresh token names its
client, the grant refuses it under any other without revoking it, and a token
naming none is bound to the client of its first OAuth refresh (owner ruling).
``POST /auth/refresh`` takes no client but carries the binding forward.
"""

import base64
import hashlib
import secrets
from datetime import datetime, timezone
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
    """One account, served to the OAuth service (``get``) and /auth/refresh."""

    def __init__(self, user):
        self.user = user

    async def get(self, user_id):
        return self.user if user_id == self.user.user_id else None

    get_user = get


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
                user=user,
                algorithm=request.param,
                signing_key=signing_key,
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


async def test_a_token_naming_no_client_is_bound_on_first_use(env):
    token = await _unbound_refresh_token(env)
    assert _client_claim(token) is None

    response = await _refresh(env.client, token, OTHER)
    assert response.status_code == 200, response.text
    rotated = response.json()["refresh_token"]
    assert _client_claim(rotated) == OTHER

    _assert_refused(await _refresh(env.client, rotated, COPILOT), REFRESH_REFUSED)


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


async def test_an_overlong_client_is_never_bound(env):
    token = await _unbound_refresh_token(env)

    _assert_refused(await _refresh(env.client, token, "c" * 256), REFRESH_REFUSED)
    assert await env.generator.validate_refresh_token(token) is not None
