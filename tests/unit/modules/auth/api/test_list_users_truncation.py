"""``GET /auth/users`` says so when its listing is capped.

The handler fetches ``list_users(limit=1000)`` but reports ``total`` from
``count_users()``, and takes no pagination parameters — so past 1000 users the
response paired a short list with a larger total and offered the caller no way
to ask for the rest. Read as "these are the users", it is wrong.

The fix is a ``truncated`` flag rather than pagination: the endpoint has no
consumer today (the dashboard uses ``/api/v1/admin/users``), so this states the
shortfall honestly without designing an API for a caller that does not exist.
"""

from types import SimpleNamespace

import httpx
import pytest

from faultmaven.api.v1.auth_dependencies import require_platform_admin
from faultmaven.modules.auth.api.auth import router as auth_router


class _User:
    """Minimal row shape consumed by the handler's projection."""

    def __init__(self, n: int):
        self.user_id = f"u-{n}"
        self.username = f"user{n}"
        self.email = f"user{n}@example.com"
        self.display_name = f"User {n}"
        self.roles = ["user"]
        self.is_active = True
        self.created_at = "2026-01-01T00:00:00+00:00"


def _build_app(returned: int, total: int):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")

    async def list_users(limit):
        return [_User(i) for i in range(returned)]

    async def count_users():
        return total

    app.state.user_store = SimpleNamespace(
        list_users=list_users, count_users=count_users
    )

    async def _admin():
        return SimpleNamespace(user_id="admin-1", roles=["platform_admin"])

    app.dependency_overrides[require_platform_admin] = _admin
    return app


async def _list_users(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/api/v1/auth/users")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_capped_listing_is_reported_as_truncated():
    """Fewer rows than the count means the caller is not seeing everything."""
    response = await _list_users(_build_app(returned=3, total=1500))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1500
    assert len(body["users"]) == 3
    assert body["truncated"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_complete_listing_is_not_reported_as_truncated():
    """Control: the flag is not simply always ``True``."""
    response = await _list_users(_build_app(returned=3, total=3))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    assert body["truncated"] is False
