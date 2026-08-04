"""An unavailable user store on GET /auth/users must be a 503, not a 500.

Same defect class as the rest of this PR, but reached through a *helper*
rather than by a literal ``raise`` in the handler body — which is why the
lexical AST sweep did not see it.

``list_users`` calls ``get_user_store(request)`` inside its ``try``. That
dependency raises ``HTTPException(503, "User management service unavailable.
Please check server startup logs.")`` when ``app.state.user_store`` is absent.
``list_users``'s only handler was a bare ``except Exception`` re-raising as a
500 whose body is built from ``str(e)``:

    500 {"error": "internal_error",
         "message": "503: User management service unavailable. ..."}

So the operator lost the 503 that names a startup/wiring problem, and the
stringified inner status leaked into the message field. The other three
``get_user_store`` call sites in the same module already re-raise
``HTTPException``; this one was the outlier.

Dependency state is driven the way the real thing is: the admin gate is
overridden (the route is platform-admin only), while the user store is left
absent on ``app.state`` so the genuine ``get_user_store`` helper produces the
503 rather than a stubbed stand-in.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from faultmaven.api.v1.auth_dependencies import require_platform_admin
from faultmaven.modules.auth.api.auth import router as auth_router
from tests.utils import asgi_request

UNAVAILABLE_DETAIL = (
    "User management service unavailable. Please check server startup logs."
)


class _User:
    """Minimal row shape consumed by the handler's projection."""

    def __init__(self):
        self.user_id = "u-1"
        self.username = "alice"
        self.email = "alice@example.com"
        self.display_name = "Alice"
        self.roles = ["user"]
        self.is_active = True
        self.created_at = "2026-01-01T00:00:00+00:00"


def _build_app(user_store):
    """``user_store=None`` leaves app.state bare — the production 503 trigger."""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.state.user_store = user_store

    async def _admin():
        return SimpleNamespace(user_id="admin-1", roles=["platform_admin"])

    app.dependency_overrides[require_platform_admin] = _admin
    return app


async def _list_users(app):
    return await asgi_request(app, "GET", "/api/v1/auth/users")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_absent_user_store_surfaces_as_503_not_500():
    """The helper's 503 must reach the client, not be rewritten as a 500."""
    response = await _list_users(_build_app(user_store=None))

    # Exact status, not merely "not 500": a request that never reaches the
    # handler fails here rather than passing vacuously.
    assert response.status_code == 503, response.text

    # Exact equality: the 500 branch built its body from str(e), which would
    # embed the inner status and the internal_error envelope.
    assert response.json()["detail"] == UNAVAILABLE_DETAIL


@pytest.mark.unit
@pytest.mark.asyncio
async def test_available_user_store_still_reaches_the_handler():
    """Vacuity control: the same app/path reaches the handler and succeeds.

    Proves the 503 above comes from the store-availability check rather than
    from routing, the admin gate, or dependency resolution.
    """

    async def list_users(limit):
        return [_User()]

    async def count_users():
        return 1

    store = SimpleNamespace(list_users=list_users, count_users=count_users)
    response = await _list_users(_build_app(user_store=store))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert [u["username"] for u in body["users"]] == ["alice"]
