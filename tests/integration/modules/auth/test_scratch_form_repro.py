import os
from unittest.mock import AsyncMock

os.environ["SKIP_SERVICE_CHECKS"] = "true"
os.environ["OAUTH_ENABLED"] = "true"

import faultmaven
from faultmaven.config.settings import reset_settings
from tests.integration._app_rebuild import rebuild_app

reset_settings()

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

app = rebuild_app()
from faultmaven.modules.auth.contracts import OAuthTokenDTO


@pytest.fixture
async def client():
    from faultmaven.modules.auth.api.rate_limiting import reset_rate_limiter

    reset_rate_limiter()
    from faultmaven.modules.auth.api.oauth import get_oauth_service

    svc = AsyncMock()
    svc.refresh_access_token.return_value = OAuthTokenDTO(
        access_token="a",
        refresh_token="r",
        token_type="Bearer",
        expires_in=900,
        refresh_expires_in=604800,
        user_id="u",
        username="n",
    )
    svc.revoke_token.return_value = None
    svc.revoke_refresh_token.return_value = None

    async def override(request: Request):
        return svc

    app.dependency_overrides[get_oauth_service] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_probe(client):
    print("\nLOADED FROM:", faultmaven.__file__)
    r = await client.post(
        "/api/v1/auth/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "x",
            "client_id": "faultmaven-copilot",
        },
    )
    print("TOKEN form ->", r.status_code, r.text[:400])
    r2 = await client.post(
        "/api/v1/auth/oauth/revoke",
        data={"token": "x", "client_id": "faultmaven-copilot"},
    )
    print("REVOKE form ->", r2.status_code, r2.text[:400])
    r3 = await client.post(
        "/api/v1/auth/oauth/token",
        json={
            "grant_type": "refresh_token",
            "refresh_token": "x",
            "client_id": "faultmaven-copilot",
        },
    )
    print("TOKEN json ->", r3.status_code, r3.text[:200])
    r4 = await client.post(
        "/api/v1/auth/oauth/token",
        json={"grant_type": "bogus", "client_id": "c"},
    )
    print("TOKEN bad grant ->", r4.status_code, r4.text[:300])
    r5 = await client.post(
        "/api/v1/auth/oauth/token",
        content=b"{}",
        headers={"content-type": "text/plain"},
    )
    print("TOKEN text/plain ->", r5.status_code, r5.text[:200])
