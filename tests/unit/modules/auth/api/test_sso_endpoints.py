"""Endpoint tests for the SSO hosted-login router (ADR-015, PR 2b).

Runs the real router + real ``SSOLoginService`` (fake collaborators) on a
minimal FastAPI app, exercising the browser-visible contract: 302 redirects
with ``Cache-Control: no-store``, the browser-binding state cookie
(set at /login, required + cleared at /callback), uniform 401 on any bad
exchange, 503 when the composition root didn't provide the service, and per-IP
rate limits on all three endpoints. The ephemeral store here is a dict-backed
single-use fake — TestClient runs each request on its own event loop, which
FakeRedis can't span; real-store (GETDEL/TTL) semantics are covered by the
service-level tests. The client uses an https base URL so the Secure state
cookie round-trips in the cookie jar like it does in the real (cloud, https)
deployment.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.modules.auth.api.rate_limiting import reset_rate_limiter
from faultmaven.modules.auth.api.sso import router as sso_router
from faultmaven.modules.auth.contracts import ISSOIdentityProvider, SSOIdentity
from faultmaven.modules.auth.domain.services.sso_login_service import SSOLoginService

DASHBOARD_URL = "https://app.faultmaven.test"


class InMemoryEphemeralStore:
    """Single-use dict-backed stand-in for SSOEphemeralStore (no event loop)."""

    def __init__(self):
        self._state: dict[str, dict] = {}
        self._login: dict[str, dict] = {}

    async def put_state(self, state, payload, ttl_seconds):
        self._state[state] = payload

    async def consume_state(self, state):
        return self._state.pop(state, None)

    async def put_login(self, code, payload, ttl_seconds):
        self._login[code] = payload

    async def consume_login(self, code):
        return self._login.pop(code, None)


class FakeProvider(ISSOIdentityProvider):
    @property
    def provider_name(self) -> str:
        return "workos"

    def build_authorization_url(self, *, state: str) -> str:
        return f"https://authkit.test/authorize?state={state}"

    def exchange_code(self, code: str) -> SSOIdentity:
        return SSOIdentity(
            provider="workos",
            provider_user_id="user_wos_123",
            email="alex@example.com",
            email_verified=True,
        )


def make_user():
    return SimpleNamespace(
        user_id="u-1",
        username="alex",
        email="alex@example.com",
        display_name="Alex Example",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_active=True,
        is_dev_user=False,
        roles=["user"],
    )


class FakeUserRepository:
    def __init__(self, user):
        self.user = user

    async def get_by_sso(self, provider, provider_id):
        return self.user

    async def get(self, user_id):
        return self.user if self.user and self.user.user_id == user_id else None

    async def update(self, user):
        # Returning-subject profile sync (ADR-015 D4) — a no-op here.
        return user


class FakeTokenGenerator:
    async def generate_access_token(self, user):
        return "access-token"

    async def generate_refresh_token(self, user):
        return "refresh-token"


class FakeSessionService:
    async def create_session(self, user_id, client_id=None, metadata=None):
        return SimpleNamespace(session_id="sess-1"), False


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    reset_rate_limiter()
    yield
    reset_rate_limiter()


@pytest.fixture
def store():
    return InMemoryEphemeralStore()


@pytest.fixture
def client(store):
    service = SSOLoginService(
        identity_provider=FakeProvider(),
        ephemeral_store=store,
        user_repository=FakeUserRepository(make_user()),
        token_generator=FakeTokenGenerator(),
        session_service=FakeSessionService(),
        dashboard_url=DASHBOARD_URL,
        access_token_expires_in=3600,
    )
    app = FastAPI()
    app.include_router(sso_router, prefix="/api/v1")
    app.state.sso_login_service = service
    # https base URL: the Secure state cookie must round-trip in the jar.
    return TestClient(app, base_url="https://testserver")


def start_login(client, **params):
    response = client.get(
        "/api/v1/auth/sso/login", params=params, follow_redirects=False
    )
    assert response.status_code == 302
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    return response, state


def login_and_get_completion_code(client) -> str:
    _, state = start_login(client)
    callback = client.get(
        "/api/v1/auth/sso/callback",
        params={"code": "authkit-code", "state": state},
        follow_redirects=False,
    )
    return parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]


# =============================================================================
# /login
# =============================================================================


def test_login_redirects_to_idp_with_no_store(client):
    response = client.get("/api/v1/auth/sso/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith(
        "https://authkit.test/authorize?state="
    )
    assert response.headers["cache-control"] == "no-store"


def test_login_sets_browser_binding_state_cookie(client):
    response, state = start_login(client)
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"fm_sso_state={state};")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/api/v1/auth/sso" in cookie


# =============================================================================
# /callback
# =============================================================================


def test_callback_redirects_to_dashboard_with_completion_code(client):
    _, state = start_login(client, return_to="/cases")

    response = client.get(
        "/api/v1/auth/sso/callback",
        params={"code": "authkit-code", "state": state},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    location = response.headers["location"]
    assert location.startswith(f"{DASHBOARD_URL}/auth/sso/callback?")
    params = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
    assert params["return_to"] == "/cases"
    assert "code" in params
    # The single-use state cookie is cleared once the flow completes.
    assert 'fm_sso_state=""' in response.headers["set-cookie"]


def test_callback_without_state_cookie_is_rejected(client):
    # Login-CSRF: replaying a valid callback URL in a browser that did not
    # start the flow (no cookie jar entry) must fail, not mint a login.
    _, state = start_login(client)
    bare_client = TestClient(client.app, base_url="https://testserver")

    response = bare_client.get(
        "/api/v1/auth/sso/callback",
        params={"code": "authkit-code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "error=sso_state_invalid" in response.headers["location"]


def test_callback_with_bad_state_redirects_with_error(client):
    response = client.get(
        "/api/v1/auth/sso/callback",
        params={"code": "x", "state": "bogus"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "error=sso_state_invalid" in response.headers["location"]


# =============================================================================
# /exchange
# =============================================================================


def test_exchange_returns_auth_token_response(client):
    code = login_and_get_completion_code(client)
    response = client.post("/api/v1/auth/sso/exchange", json={"code": code})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["access_token"] == "access-token"
    assert body["refresh_token"] == "refresh-token"
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    assert body["session_id"] == "sess-1"
    assert body["user"]["user_id"] == "u-1"
    assert body["user"]["roles"] == ["user"]


def test_exchange_is_single_use(client):
    code = login_and_get_completion_code(client)
    assert (
        client.post("/api/v1/auth/sso/exchange", json={"code": code}).status_code == 200
    )
    replay = client.post("/api/v1/auth/sso/exchange", json={"code": code})
    assert replay.status_code == 401


def test_exchange_unknown_code_returns_uniform_401(client):
    response = client.post(
        "/api/v1/auth/sso/exchange", json={"code": "never-issued-code-123"}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "invalid_grant"
    assert response.headers["www-authenticate"] == "Bearer"


def test_exchange_rejects_malformed_code(client):
    # Below min_length: rejected by validation before touching the store.
    response = client.post("/api/v1/auth/sso/exchange", json={"code": "short"})
    assert response.status_code == 422


# =============================================================================
# service unavailable / rate limiting
# =============================================================================


def test_endpoints_503_when_service_not_composed():
    app = FastAPI()
    app.include_router(sso_router, prefix="/api/v1")
    client = TestClient(app)
    assert (
        client.get("/api/v1/auth/sso/login", follow_redirects=False).status_code == 503
    )


def test_exchange_rate_limited_per_ip(client):
    for _ in range(5):
        response = client.post(
            "/api/v1/auth/sso/exchange", json={"code": "never-issued-code-123"}
        )
        assert response.status_code == 401
    limited = client.post(
        "/api/v1/auth/sso/exchange", json={"code": "never-issued-code-123"}
    )
    assert limited.status_code == 429


def test_login_rate_limited_per_ip(client):
    for _ in range(10):
        assert (
            client.get("/api/v1/auth/sso/login", follow_redirects=False).status_code
            == 302
        )
    assert (
        client.get("/api/v1/auth/sso/login", follow_redirects=False).status_code == 429
    )


def test_callback_rate_limited_per_ip(client):
    for _ in range(10):
        response = client.get(
            "/api/v1/auth/sso/callback",
            params={"state": "bogus"},
            follow_redirects=False,
        )
        assert response.status_code == 302
    limited = client.get(
        "/api/v1/auth/sso/callback", params={"state": "bogus"}, follow_redirects=False
    )
    assert limited.status_code == 429
