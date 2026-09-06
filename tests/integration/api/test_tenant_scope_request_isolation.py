"""Integration tests for per-request tenant-context isolation (ADR-010 P2e, ADR-017).

The unit tests for ``bind_request_enterprise_context`` call the dependency
directly, so they cannot exercise the two properties the RLS design leans on at
the ASGI level:

* **Binder-before-endpoint ordering** — the global dependency must resolve in
  the request handler's own task *before* the endpoint body runs, so any
  transaction the endpoint opens sees the request's enterprise in the contextvar
  (the engine ``begin`` listener reads it at transaction start).
* **Cross-request isolation** — an enterprise bound in one request's task must
  never leak into another request. Servers give each request a fresh task, and a
  task gets a *copy* of its parent context, so ``ContextVar.set`` inside a
  request stays inside it. These tests drive real requests through the ASGI
  stack (each request in its own task, as uvicorn does) to pin that behavior.

The probe app is wired exactly like ``main.py``: the binder registered as a
FastAPI **global dependency** (``dependencies=[...]``), not a Starlette
middleware. A separate test asserts the real app carries that registration.

The **billing organization** rides the same binding and is asserted beside the
enterprise on every arm, because ADR-017 D2's whole point is that the two are
different kinds of thing: one is the isolation binding and one is attribution.
A leak of either across requests is a defect, but only the first is a wall.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from faultmaven.api.middleware.auth import get_auth_service
from faultmaven.api.middleware.tenant_scope import bind_request_enterprise_context
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import (
    get_current_billing_organization_id,
    get_current_enterprise_id,
    set_current_billing_organization_id,
    set_current_enterprise_id,
)
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE

ENTERPRISE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ENTERPRISE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
#: A's billing organization. Only A has one, so the arms below also show that
#: "no organization" is an ordinary answer rather than a failure (ADR-017 D5).
ORGANIZATION_A = "cccccccc-cccc-cccc-cccc-cccccccccccc"

#: The single override point for "which tenant provider is in force".
#: ``bind_request_enterprise_context`` selects its arm through this module
#: attribute and ``config.tenant_context.usable_tenant_id`` resolves the same
#: one, so patching it here governs both — patching the middleware's own name
#: would move only the arm selection and leave the sentinel rule reading the real
#: configuration.
_PROVIDER_TARGET = "faultmaven.providers.tenancy.factory.requested_tenant_provider"


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    """Keep contextvar state from leaking across tests."""
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)


def _claims(token: str) -> dict:
    """Map a bearer token to the verified claim set the binder reads.

    ``token-unknown`` verifies but carries no enterprise claim — the fail-closed
    case ADR-017 forbids rescuing from ``users.enterprise_id``.
    """
    claims: dict = {"sub": f"user-{token}"}
    if token == "token-a":
        claims["enterprise_id"] = ENTERPRISE_A
        claims["organization_id"] = ORGANIZATION_A
    elif token == "token-b":
        claims["enterprise_id"] = ENTERPRISE_B
    return claims


def _probe_app() -> FastAPI:
    """A FastAPI app wired exactly like main.py: binder as a global dependency."""
    app = FastAPI(dependencies=[Depends(bind_request_enterprise_context)])

    @app.get("/tenant")
    async def read_tenant():
        return {
            "enterprise": get_current_enterprise_id(),
            "organization": get_current_billing_organization_id(),
        }

    @app.get("/tenant-slow")
    async def read_tenant_slow():
        # Yield long enough for another request's binder to run in between the
        # binder for this request and this read. If request tasks shared a
        # context, the other request's enterprise would show up here.
        await asyncio.sleep(0.05)
        return {
            "enterprise": get_current_enterprise_id(),
            "organization": get_current_billing_organization_id(),
        }

    # The auth service is only consulted in multi-tenant mode; token -> claims.
    auth_service = AsyncMock()

    async def _verify(token, token_type="access"):
        return _claims(token)

    auth_service.verify_token_with_revocation_check.side_effect = _verify
    app.dependency_overrides[get_auth_service] = lambda: auth_service
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_binder_runs_before_endpoint_in_request_task(monkeypatch):
    """The bound enterprise is visible in the endpoint body (same task,
    dependency resolved first) — so any transaction the endpoint opens is scoped
    to it. The billing organization arrives on the same binding."""
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_MULTI)
    async with _client(_probe_app()) as client:
        # Each request issued from the test runs as its own task in the
        # transport, mirroring the server's task-per-request model.
        response = await asyncio.create_task(
            client.get("/tenant", headers={"Authorization": "Bearer token-a"})
        )

    assert response.status_code == 200
    assert response.json() == {
        "enterprise": ENTERPRISE_A,
        "organization": ORGANIZATION_A,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enterprise_bound_in_one_request_does_not_leak_into_the_next(monkeypatch):
    """Sequential requests: an authenticated request binds its enterprise; a
    following unauthenticated request must see the pristine default, not the
    previous request's enterprise."""
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_MULTI)
    async with _client(_probe_app()) as client:
        first = await asyncio.create_task(
            client.get("/tenant", headers={"Authorization": "Bearer token-a"})
        )
        second = await asyncio.create_task(client.get("/tenant"))

    assert first.json()["enterprise"] == ENTERPRISE_A
    # No token -> binder binds the empty non-tenant (no enterprise-owned rows, no
    # platform-tier write license, #770); ENTERPRISE_A must not have leaked, and
    # neither may A's billing organization.
    assert second.json() == {"enterprise": "", "organization": None}
    # And nothing leaked out into the test's own context either.
    assert get_current_enterprise_id() == STANDALONE_ENTERPRISE_ID
    assert get_current_billing_organization_id() is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_requests_each_see_their_own_enterprise(monkeypatch):
    """Two in-flight requests for different enterprises: each endpoint body reads
    the enterprise its own binder set, even while the other request holds a
    different one in its task's context."""
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_MULTI)
    async with _client(_probe_app()) as client:
        resp_a, resp_b = await asyncio.gather(
            client.get("/tenant-slow", headers={"Authorization": "Bearer token-a"}),
            client.get("/tenant-slow", headers={"Authorization": "Bearer token-b"}),
        )

    assert resp_a.json() == {"enterprise": ENTERPRISE_A, "organization": ORGANIZATION_A}
    # B is in no organization, which is the ordinary steady state under D5 — and
    # A's must not have bled across the concurrent binding.
    assert resp_b.json() == {"enterprise": ENTERPRISE_B, "organization": None}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multi_tenant_verified_user_without_enterprise_is_403_through_the_stack(
    monkeypatch,
):
    """The fail-closed path holds end-to-end: a verified user with no enterprise
    claim is rejected before the endpoint runs — never rescued by reading
    ``users.enterprise_id`` (ADR-017, "No data migration")."""
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_MULTI)
    async with _client(_probe_app()) as client:
        response = await client.get(
            "/tenant", headers={"Authorization": "Bearer token-unknown"}
        )

    assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_single_tenant_forces_standalone_through_the_stack(monkeypatch):
    """Single-tenant ignores any presented token and pins the Standalone
    enterprise — and bills nobody, because a standalone deployment has no
    organization row at all (ADR-017 D8)."""
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_SINGLE)
    async with _client(_probe_app()) as client:
        response = await client.get(
            "/tenant", headers={"Authorization": "Bearer token-a"}
        )

    assert response.json() == {
        "enterprise": STANDALONE_ENTERPRISE_ID,
        "organization": None,
    }


@pytest.mark.integration
def test_real_app_registers_binder_as_global_dependency():
    """main.py must carry the binder as an app-level global dependency — the
    probe-app tests above are only faithful if the real app is wired the same."""
    from faultmaven.main import app

    registered = [d.dependency for d in (app.router.dependencies or [])]
    assert bind_request_enterprise_context in registered
