"""``GET /auth/oauth/authorize`` applies OAuth policy before consent (#1053).

The endpoint used to validate only ``response_type``, echoing ``client_id``,
``redirect_uri``, ``scope`` and ``state`` back to the dashboard unchecked; the
allowlists ran later, in ``create_authorization_code``. So a crafted URL produced
an ordinary 200 consent screen for a request the subsequent POST would refuse,
and the dashboard — which navigates to ``redirect_uri`` on Cancel — was handed an
attacker-chosen target to leave for.

⚠️ These tests wire a **real** ``OAuthServiceImpl``, not the ``AsyncMock`` the
neighbouring OAuth route tests use. That is the whole point: the route now
delegates the check to the service, and against an ``AsyncMock`` the delegated
call returns a coroutine that raises nothing, so every assertion below would pass
just as happily with the validation deleted. A mocked collaborator would make
this file prove nothing.

The rejections are asserted at the *consent* leg (``OAUTH_REQUIRE_CONSENT=true``,
the production default), because that is the branch that was unguarded — the
auto-approve branch reached ``create_authorization_code`` and was always covered.
"""

import os
from datetime import datetime

# Set environment variables FIRST - before ANY imports
os.environ["SKIP_SERVICE_CHECKS"] = "true"
os.environ["OAUTH_ENABLED"] = "true"

# Force the app below to be built with OAuth enabled. Clear the settings
# *singleton*; never drop faultmaven.config.settings from sys.modules (that
# leaves a second module object, each with its own cached instance).
from faultmaven.config.settings import reset_settings
from tests.integration._app_rebuild import rebuild_app

reset_settings()

import pytest
from fastapi import Request, status
from httpx import ASGITransport, AsyncClient

app = rebuild_app()

from faultmaven.config.settings import AuthSettings
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.oauth_service import OAuthServiceImpl
from faultmaven.modules.auth.infrastructure.repositories.oauth_code_repository import (
    InMemoryOAuthCodeRepository,
)

ALLOWED_CLIENT = "faultmaven-copilot"
# Matches the production default pattern: chrome-extension://<32 lowercase>/callback.html
ALLOWED_REDIRECT = "chrome-extension://" + ("a" * 32) + "/callback.html"


@pytest.fixture(autouse=True)
def rebuilt_settings_do_not_outlive_the_test():
    """Drop the singleton these tests rebuild under mutated OAuth env vars."""
    yield
    reset_settings()


@pytest.fixture(autouse=True)
def consent_required(monkeypatch):
    """Pin the branch under test: the consent leg, which is the production default."""
    monkeypatch.setenv("OAUTH_REQUIRE_CONSENT", "true")
    reset_settings()


@pytest.fixture
def auth_settings():
    """Real ``AuthSettings`` carrying the shipped default allowlists.

    Bound to the real type, not a ``Mock``: a Mock auto-creates whatever
    attribute the service reads, so a policy field that stopped being consulted
    would still look enforced here.
    """
    return AuthSettings(
        oauth_allowed_clients=[ALLOWED_CLIENT],
        oauth_redirect_uri_patterns=[
            r"^chrome-extension://[a-z]{32}/callback\.html$",
            r"^moz-extension://[a-f0-9-]{36}/callback\.html$",
        ],
    )


@pytest.fixture
def oauth_service(auth_settings):
    """The real service — the object whose policy this file is checking."""
    return OAuthServiceImpl(
        code_repository=InMemoryOAuthCodeRepository(),
        user_repository=None,
        token_generator=None,
        settings=auth_settings,
    )


@pytest.fixture
def user():
    return DevUser(
        user_id="test_user_123",
        username="testuser",
        email="test@example.com",
        display_name="Test User",
        created_at=datetime.utcnow(),
    )


@pytest.fixture
async def client(oauth_service, user):
    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.modules.auth.api.oauth import get_oauth_service
    from faultmaven.modules.auth.api.rate_limiting import reset_rate_limiter

    reset_rate_limiter()

    async def _oauth_service(request: Request):
        return oauth_service

    async def _require_authentication(request: Request = None):
        return user

    app.dependency_overrides[get_oauth_service] = _oauth_service
    app.dependency_overrides[require_authentication] = _require_authentication

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _params(**overrides) -> dict:
    """A request that is valid in every respect, unless a test spoils one field."""
    params = {
        "response_type": "code",
        "client_id": ALLOWED_CLIENT,
        "redirect_uri": ALLOWED_REDIRECT,
        "state": "random_state_123",
        "code_challenge": "test_challenge",
        "code_challenge_method": "S256",
        "scope": "openid profile email",
    }
    params.update(overrides)
    return params


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.asyncio
async def test_valid_request_still_reaches_the_consent_screen(client):
    """The control: everything below must fail for its own reason, not a broken baseline.

    Without this, a route that rejected *every* request would satisfy each
    rejection test in this file.
    """
    response = await client.get("/api/v1/auth/oauth/authorize", params=_params())

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["client_id"] == ALLOWED_CLIENT
    assert body["redirect_uri"] == ALLOWED_REDIRECT
    # The consent screen, not a minted code.
    assert "code" not in body


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.asyncio
async def test_unknown_client_is_refused_before_a_consent_screen(client):
    """An unrecognised client_id must not reach the dashboard as a display name.

    ``client_name`` falls back to ``client_id``, so a 200 here would let a
    crafted URL choose the heading on the one screen whose job is telling the
    user which application is asking.
    """
    response = await client.get(
        "/api/v1/auth/oauth/authorize",
        params=_params(client_id="attacker-chosen-app"),
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    # No consent payload came back — not merely "the status was 4xx".
    body = response.json()
    assert "client_name" not in body
    assert "redirect_uri" not in body


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_redirect",
    [
        "https://attacker.example/callback",  # off-allowlist https host
        "http://attacker.example/callback",  # insecure scheme
        "javascript:alert(1)",  # same-origin script execution on the dashboard
        "chrome-extension://" + ("a" * 32) + "/callback.html.evil",  # near-miss suffix
        "chrome-extension://TOOSHORT/callback.html",  # near-miss extension id
    ],
)
async def test_disallowed_redirect_uri_is_refused_before_a_consent_screen(
    client, bad_redirect
):
    """A redirect target the allowlist rejects must never reach the browser.

    The dashboard navigates to whatever ``redirect_uri`` the consent response
    carries when the user presses Cancel, so a 200 here is an open redirect —
    and for the ``javascript:`` value, script execution on the dashboard's own
    origin. The two near-misses are included because a validation loosened to
    ``startswith``/substring matching would still pass the obvious cases.
    """
    response = await client.get(
        "/api/v1/auth/oauth/authorize", params=_params(redirect_uri=bad_redirect)
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "redirect_uri" not in response.json()


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.asyncio
async def test_unsupported_code_challenge_method_is_refused_before_a_consent_screen(
    client,
):
    """The third check the mint path makes, and the one #1053 did not name.

    ``create_authorization_code`` refuses anything but S256. Leaving it out of
    the consent leg would rebuild the same defect one member short — a consent
    screen for a request that cannot mint a code.
    """
    response = await client.get(
        "/api/v1/auth/oauth/authorize", params=_params(code_challenge_method="plain")
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "client_name" not in response.json()


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.asyncio
async def test_unsupported_response_type_is_still_refused(client):
    """The one check that already existed, kept covered through the refactor."""
    response = await client.get(
        "/api/v1/auth/oauth/authorize", params=_params(response_type="token")
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.asyncio
async def test_the_mint_path_keeps_enforcing_the_same_policy(oauth_service):
    """The route check is additive, not a relocation.

    ``validate_authorization_request`` was extracted *out of*
    ``create_authorization_code``; if the extraction left the mint path calling
    nothing, this file's route tests would still pass while the security-relevant
    enforcement point had quietly become the only unguarded one.
    """
    from faultmaven.models.exceptions import InvalidRequestError
    from faultmaven.modules.auth.contracts import OAuthAuthorizationDTO

    with pytest.raises(InvalidRequestError):
        await oauth_service.create_authorization_code(
            "test_user_123",
            OAuthAuthorizationDTO(
                client_id="attacker-chosen-app",
                redirect_uri=ALLOWED_REDIRECT,
                state="s",
                code_challenge="c",
            ),
        )

    with pytest.raises(InvalidRequestError):
        await oauth_service.create_authorization_code(
            "test_user_123",
            OAuthAuthorizationDTO(
                client_id=ALLOWED_CLIENT,
                redirect_uri="https://attacker.example/callback",
                state="s",
                code_challenge="c",
            ),
        )
