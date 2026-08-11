"""The authorize endpoints hand the request's tenant to the OAuth service (#872).

The service-level guarantee (``tests/unit/modules/auth/test_oauth_pkce_org_claim.py``)
proves that a code carrying an organization mints tokens claiming it. That is
worth nothing if the route never puts an organization on the code — and the route
is where it comes from, because ``GET|POST /auth/oauth/authorize`` is the last
authenticated hop before an unauthenticated exchange.

So this covers one link of that chain, and only one: whatever
``user.organization_id`` holds must reach ``create_authorization_code``. It
overrides ``require_authentication`` with a hand-built user, so it does **not**
establish the other link — that ``user.organization_id`` is the RLS-bound tenant
rather than a raw JWT claim. That half is pinned by
``tests/unit/api/v1/test_auth_dependencies_org_scope.py::test_org_sourced_from_contextvar_not_raw_claim``,
which fails if ``get_current_user_optional`` is changed to read the claim
directly. Neither test is sufficient alone; stating which one covers what is the
point, because a reader who believes this file proves both would not notice the
other going away.
"""

import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock

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
from faultmaven.modules.auth.domain.models.auth import DevUser

TENANT = "org_acme_7f3c"
REDIRECT = "chrome-extension://test123/callback"


@pytest.fixture(autouse=True)
def rebuilt_settings_do_not_outlive_the_test():
    """Drop the singleton these tests rebuild under mutated OAuth env vars."""
    yield
    reset_settings()


@pytest.fixture
def oauth_service():
    service = AsyncMock()
    service.create_authorization_code.return_value = "test_authorization_code_123"
    return service


@pytest.fixture
def tenant_user():
    """A user bound to a real tenant, as a multi-tenant request would carry.

    ``DevUser.__post_init__`` stamps the Standalone sentinel when no org is
    given, so the tenant is set explicitly — otherwise the assertions below would
    be satisfied by the sentinel and could not tell a forwarded tenant from a
    defaulted one.
    """
    return DevUser(
        user_id="test_user_123",
        username="testuser",
        email="test@example.com",
        display_name="Test User",
        created_at=datetime.utcnow(),
        organization_id=TENANT,
    )


@pytest.fixture
async def client(oauth_service, tenant_user):
    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.modules.auth.api.oauth import get_oauth_service
    from faultmaven.modules.auth.api.rate_limiting import reset_rate_limiter

    reset_rate_limiter()

    async def _oauth_service(request: Request):
        return oauth_service

    async def _require_authentication(request: Request = None):
        return tenant_user

    app.dependency_overrides[get_oauth_service] = _oauth_service
    app.dependency_overrides[require_authentication] = _require_authentication

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _captured_org(oauth_service) -> str:
    """The organization the route passed to the service."""
    oauth_service.create_authorization_code.assert_called_once()
    return oauth_service.create_authorization_code.call_args.kwargs["organization_id"]


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.asyncio
async def test_get_authorize_auto_approve_captures_the_bound_org(
    client, oauth_service, monkeypatch
):
    """The auto-approve arm (dev/test) forwards the request's tenant."""
    monkeypatch.setenv("OAUTH_REQUIRE_CONSENT", "false")
    reset_settings()

    response = await client.get(
        "/api/v1/auth/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "faultmaven-copilot",
            "redirect_uri": REDIRECT,
            "state": "random_state_123",
            "code_challenge": "test_challenge",
            "code_challenge_method": "S256",
            "scope": "openid profile email",
        },
    )

    assert response.status_code == status.HTTP_200_OK
    assert _captured_org(oauth_service) == TENANT


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.asyncio
async def test_post_authorize_approval_captures_the_bound_org(client, oauth_service):
    """The consent arm — the one production runs — forwards it too.

    Both arms are covered because they are separate call sites: a fix applied to
    only one leaves whichever arm the deployment actually uses still broken.
    """
    response = await client.post(
        "/api/v1/auth/oauth/authorize",
        json={
            "approved": True,
            "code_challenge": "test_challenge",
            "code_challenge_method": "S256",
            "client_id": "faultmaven-copilot",
            "redirect_uri": REDIRECT,
            "scope": "openid profile email",
            "state": "random_state_123",
        },
    )

    assert response.status_code == status.HTTP_200_OK
    assert _captured_org(oauth_service) == TENANT
