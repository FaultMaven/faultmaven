"""Integration tests for per-request tenant-context isolation (ADR-010 P2e).

The unit tests for ``bind_request_org_context`` call the dependency directly, so
they cannot exercise the two properties the RLS design leans on at the ASGI
level:

* **Binder-before-endpoint ordering** — the global dependency must resolve in
  the request handler's own task *before* the endpoint body runs, so any
  transaction the endpoint opens sees the request's org in the contextvar (the
  engine ``begin`` listener reads it at transaction start).
* **Cross-request isolation** — an org bound in one request's task must never
  leak into another request. Servers give each request a fresh task, and a task
  gets a *copy* of its parent context, so ``ContextVar.set`` inside a request
  stays inside it. These tests drive real requests through the ASGI stack (each
  request in its own task, as uvicorn does) to pin that behavior.

The probe app is wired exactly like ``main.py``: the binder registered as a
FastAPI **global dependency** (``dependencies=[...]``), not a Starlette
middleware. A separate test asserts the real app carries that registration.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from faultmaven.api.middleware import tenant_scope
from faultmaven.api.middleware.auth import get_auth_service
from faultmaven.api.middleware.tenant_scope import bind_request_org_context
from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.tenant_context import get_current_org_id, set_current_org_id
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE

ORG_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ORG_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _reset_org_context():
    """Keep contextvar state from leaking across tests."""
    set_current_org_id(STANDALONE_ORG_ID)
    yield
    set_current_org_id(STANDALONE_ORG_ID)


def _user(token: str) -> AuthenticatedUser:
    """Map a bearer token to a verified user in that token's org."""
    org = {"token-a": ORG_A, "token-b": ORG_B}.get(token, "")
    return AuthenticatedUser.from_jwt_claims(
        {"sub": f"user-{token}", "organization_id": org}
    )


def _probe_app() -> FastAPI:
    """A FastAPI app wired exactly like main.py: binder as a global dependency."""
    app = FastAPI(dependencies=[Depends(bind_request_org_context)])

    @app.get("/org")
    async def read_org():
        return {"org": get_current_org_id()}

    @app.get("/org-slow")
    async def read_org_slow():
        # Yield long enough for another request's binder to run in between the
        # binder for this request and this read. If request tasks shared a
        # context, the other request's org would show up here.
        await asyncio.sleep(0.05)
        return {"org": get_current_org_id()}

    # The auth service is only consulted in multi-tenant mode; token -> org.
    auth_service = AsyncMock()
    auth_service.extract_user_from_token_with_revocation_check.side_effect = _user
    app.dependency_overrides[get_auth_service] = lambda: auth_service
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_binder_runs_before_endpoint_in_request_task(monkeypatch):
    """The bound org is visible in the endpoint body (same task, dependency
    resolved first) — so any transaction the endpoint opens is scoped to it."""
    monkeypatch.setattr(
        tenant_scope, "requested_tenant_provider", lambda: BUILTIN_MULTI
    )
    async with _client(_probe_app()) as client:
        # Each request issued from the test runs as its own task in the
        # transport, mirroring the server's task-per-request model.
        response = await asyncio.create_task(
            client.get("/org", headers={"Authorization": "Bearer token-a"})
        )

    assert response.status_code == 200
    assert response.json() == {"org": ORG_A}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_org_bound_in_one_request_does_not_leak_into_the_next(monkeypatch):
    """Sequential requests: an authenticated request binds its org; a following
    unauthenticated request must see the pristine default, not the previous
    request's org."""
    monkeypatch.setattr(
        tenant_scope, "requested_tenant_provider", lambda: BUILTIN_MULTI
    )
    async with _client(_probe_app()) as client:
        first = await asyncio.create_task(
            client.get("/org", headers={"Authorization": "Bearer token-a"})
        )
        second = await asyncio.create_task(client.get("/org"))

    assert first.json() == {"org": ORG_A}
    # No token -> binder binds the empty non-org (no org-owned rows, no
    # platform-tier write license, #770); ORG_A must not have leaked.
    assert second.json() == {"org": ""}
    # And nothing leaked out into the test's own context either.
    assert get_current_org_id() == STANDALONE_ORG_ID


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_requests_each_see_their_own_org(monkeypatch):
    """Two in-flight requests for different orgs: each endpoint body reads the
    org its own binder set, even while the other request holds a different org
    in its task's context."""
    monkeypatch.setattr(
        tenant_scope, "requested_tenant_provider", lambda: BUILTIN_MULTI
    )
    async with _client(_probe_app()) as client:
        resp_a, resp_b = await asyncio.gather(
            client.get("/org-slow", headers={"Authorization": "Bearer token-a"}),
            client.get("/org-slow", headers={"Authorization": "Bearer token-b"}),
        )

    assert resp_a.json() == {"org": ORG_A}
    assert resp_b.json() == {"org": ORG_B}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multi_tenant_verified_user_without_org_is_403_through_the_stack(
    monkeypatch,
):
    """The fail-closed path holds end-to-end: a verified user with no org claim
    is rejected before the endpoint runs."""
    monkeypatch.setattr(
        tenant_scope, "requested_tenant_provider", lambda: BUILTIN_MULTI
    )
    async with _client(_probe_app()) as client:
        response = await client.get(
            "/org", headers={"Authorization": "Bearer token-unknown"}
        )

    assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_single_tenant_forces_standalone_through_the_stack(monkeypatch):
    """Single-tenant ignores any presented token and pins the Standalone org."""
    monkeypatch.setattr(
        tenant_scope, "requested_tenant_provider", lambda: BUILTIN_SINGLE
    )
    async with _client(_probe_app()) as client:
        response = await client.get("/org", headers={"Authorization": "Bearer token-a"})

    assert response.json() == {"org": STANDALONE_ORG_ID}


@pytest.mark.integration
def test_real_app_registers_binder_as_global_dependency():
    """main.py must carry the binder as an app-level global dependency — the
    probe-app tests above are only faithful if the real app is wired the same."""
    from faultmaven.main import app

    registered = [d.dependency for d in (app.router.dependencies or [])]
    assert bind_request_org_context in registered
