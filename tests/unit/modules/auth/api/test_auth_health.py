"""``GET /auth/health`` actually reports the auth services' state.

The endpoint called ``check_auth_services_health()`` with no arguments while the
helper had required ``request: Request`` since 39807ebe (2026-01-11), when the
Service Locator removal moved store resolution onto ``request.app.state``. Every
call raised ``TypeError``, the broad ``except`` caught it, and the route
answered **200** with ``{"status": "unhealthy", "error": "Auth health check
failed"}`` — for roughly seven months, on every deployment, whatever the real
state of the stores.

Nothing caught it because the only coverage
(``test_auth_health_200_does_not_echo_the_exception``) patches
``check_auth_services_health`` with a ``Mock``, which accepts any signature. It
asserts the except arm's shape, so it passes whether or not the call is
well-formed — it is a leak probe, not a health probe.

These tests therefore drive the **real** helper through the route. The
load-bearing assertion is the positive one: a correctly wired app must report
``healthy``, which is unreachable if the call raises for any reason.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from faultmaven.modules.auth.api.auth import router as auth_router


def _build_app(*, user_store=None, token_revocation_store=None):
    """The real auth router with the two stores the health check reads."""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.state.user_store = user_store
    app.state.token_revocation_store = token_revocation_store
    return app


async def _get(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_reports_healthy_when_both_stores_are_wired():
    """The positive case — the one the seven-month regression made unreachable.

    Asserting "the route returns 200" would be vacuous: it returned 200 while
    broken, which is exactly why the failure was invisible. The evidence that
    the call is well-formed is that the *good* status is reached and that both
    stores are named in the body.
    """
    app = _build_app(
        user_store=SimpleNamespace(),
        token_revocation_store=SimpleNamespace(),
    )

    response = await _get(app, "/api/v1/auth/health")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "healthy", body
    # The except arm's distinguishing key. Its absence is what separates a real
    # health report from the swallowed-exception shape that replaced it.
    assert "error" not in body, body

    services = body["services"]
    assert services["user_store"]["status"] == "available"
    assert services["token_revocation_store"]["status"] == "available"
    # Resolved off request.app.state, so the reported type proves the endpoint
    # inspected *this* app rather than returning a constant.
    assert services["user_store"]["type"] == "SimpleNamespace"
    assert services["token_revocation_store"]["type"] == "SimpleNamespace"
    assert "timestamp" in body


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_store", "revocation_store", "unavailable"),
    [
        (None, SimpleNamespace(), "user_store"),
        (SimpleNamespace(), None, "token_revocation_store"),
        (None, None, "user_store"),
    ],
)
async def test_health_reports_degraded_when_a_store_is_missing(
    user_store, revocation_store, unavailable
):
    """A missing store is ``degraded`` — distinct from the ``unhealthy`` except arm.

    Both halves matter: a probe that could only ever say ``degraded`` would be
    as useless as one that could only ever say ``unhealthy``, so the healthy
    case above and these share one code path with different inputs.
    """
    app = _build_app(user_store=user_store, token_revocation_store=revocation_store)

    response = await _get(app, "/api/v1/auth/health")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "degraded", body
    assert "error" not in body, body
    assert body["services"][unavailable]["status"] == "unavailable"
    assert body["services"][unavailable]["type"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_passes_the_request_through_to_the_helper():
    """Pin the defect directly: the helper is called with the live ``Request``.

    The end-to-end tests above already fail if the call is malformed, but they
    fail through the except arm, which reports ``unhealthy`` for *any* reason.
    This one names the cause, so a future signature change is diagnosed rather
    than merely detected.
    """
    seen = {}

    async def _spy(request):
        seen["request"] = request
        return {"authentication": {"status": "healthy", "services": {}}}

    import faultmaven.modules.auth.api.auth as auth_module

    original = auth_module.check_auth_services_health
    auth_module.check_auth_services_health = _spy
    try:
        response = await _get(_build_app(), "/api/v1/auth/health")
    finally:
        auth_module.check_auth_services_health = original

    assert response.status_code == 200, response.text
    request = seen.get("request")
    assert request is not None, "helper was never called"
    # A real Starlette Request bound to this app — the attribute the helper
    # reads. A positional/keyword mix-up would surface here as the wrong object.
    assert request.app.state.user_store is None
    assert hasattr(request, "app"), type(request)
