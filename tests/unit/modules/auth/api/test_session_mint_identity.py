"""`POST /api/v1/sessions` mints the server's identity, never the caller's.

The route used to accept a ``user_id`` **query parameter** and resolve it
first::

    if not user_id:            # a supplied value wins unconditionally
        if current_user: ...   # the bearer is never consulted, never compared

So a caller named whose identity got minted, and the authenticated user — if
there was one — was not even looked at. That is not a privilege check that
failed; there was no check, because a parameter whose only legal value is one
the server already knows has nothing to check *against*.

It matters beyond the session row because a session id is still worth something
on its own: ``api/middleware/logging.py`` attributes an unauthenticated request
to the ``user_id`` of whatever session a client-supplied ``X-Session-ID`` names,
and ``api/middleware/idempotency.py`` withholds this route from idempotency
replay for exactly that reason. Minting is a credential operation.

Contract 6.0.0 REMOVES the parameter rather than constraining it. These tests
pin both halves of that: the value a request supplies is not the identity
minted (whether or not it is authenticated), and the parameter is not published.

Scope note: nothing here asserts on an error body or status, so the app is
built from the session router alone. ``get_exception_handlers()`` returns only
the DOMAIN handlers — ``main.py`` registers ``http_exception_handler`` and
``request_validation_exception_handler`` separately — so an app built without
those sees raw FastAPI error rendering rather than production's, and an
assertion on one would be asserting about the test harness.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from faultmaven.api.v1.auth_dependencies import get_current_user_optional
from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.modules.auth.api.session import create_session
from faultmaven.modules.auth.api.session import router as session_router

BEARER_USER = "user-authenticated-01"
NAMED_BY_CALLER = "user-someone-else-99"


def _build_app(*, current_user):
    """Mount the real session router; capture what the service is asked for.

    ``app.state.minted`` collects the positional ``user_id`` of every
    ``create_session`` call — the artifact these tests read, rather than a value
    reconstructed from the same inputs the route saw.
    """
    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")
    app.state.minted = []

    async def _session_service():
        async def create_session_impl(user_id, metadata=None, client_id=None):
            app.state.minted.append(user_id)
            return SimpleNamespace(
                session_id="sess-minted-0001",
                user_id=user_id,
                created_at=datetime.now(timezone.utc),
            )

        return SimpleNamespace(create_session=create_session_impl)

    async def _current_user():
        return current_user

    app.dependency_overrides[get_session_service] = _session_service
    app.dependency_overrides[get_current_user_optional] = _current_user
    return app


async def _post(app, url):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(url, json={})


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.session
@pytest.mark.asyncio
async def test_a_named_user_id_does_not_bind_an_authenticated_mint():
    """The bearer is minted; the query string is not consulted.

    The pre-fix route bound ``NAMED_BY_CALLER`` here and never read the bearer
    at all, so this fails loudly against it rather than passing narrowly.
    """
    app = _build_app(current_user=SimpleNamespace(user_id=BEARER_USER))

    response = await _post(app, f"/api/v1/sessions?user_id={NAMED_BY_CALLER}")

    assert response.status_code == 201, response.text
    assert app.state.minted == [BEARER_USER]
    assert response.json()["user_id"] == BEARER_USER


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.session
@pytest.mark.asyncio
async def test_a_named_user_id_does_not_bind_an_unauthenticated_mint():
    """With no bearer the server generates an id; the caller still cannot pick.

    This is the self-hosted shape — port 8090 on 0.0.0.0, no proxy, no token —
    and the arm that made the parameter reachable without any credential at
    all.
    """
    app = _build_app(current_user=None)

    response = await _post(app, f"/api/v1/sessions?user_id={NAMED_BY_CALLER}")

    assert response.status_code == 201, response.text
    (minted,) = app.state.minted
    assert minted != NAMED_BY_CALLER
    # The anonymous arm's own shape, so this cannot pass on a stray empty value.
    assert minted.startswith("user_")
    assert response.json()["user_id"] == minted


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.session
@pytest.mark.asyncio
async def test_the_authenticated_mint_still_binds_the_bearer():
    """Vacuity control: the identity that SHOULD be minted still is.

    Without this, both tests above would keep passing if the route stopped
    minting anything recognisable at all.
    """
    app = _build_app(current_user=SimpleNamespace(user_id=BEARER_USER))

    response = await _post(app, "/api/v1/sessions")

    assert response.status_code == 201, response.text
    assert app.state.minted == [BEARER_USER]


@pytest.mark.unit
@pytest.mark.security
def test_the_handler_declares_no_user_id_parameter():
    """The parameter is gone from the signature, not merely ignored.

    Asserted on the handler rather than only over HTTP because FastAPI drops an
    undeclared query parameter silently: a route that still *declared* it while
    ignoring its value would pass the request-level tests above and publish the
    parameter to every client regenerating its types.
    """
    assert "user_id" not in inspect.signature(create_session).parameters


@pytest.mark.unit
@pytest.mark.security
def test_the_published_operation_declares_no_query_parameters():
    """And it is not published, which is what a client actually reads.

    Generated from the router here rather than read out of the committed
    artifact: the artifact is regenerated by ``scripts/generate_api_docs.py``
    and gated by ``api-contract-drift``, so reading it back would assert that
    the generator ran, not that the route changed.
    """
    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")

    operation = app.openapi()["paths"]["/api/v1/sessions"]["post"]

    assert operation.get("parameters", []) == []
