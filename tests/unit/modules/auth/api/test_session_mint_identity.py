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
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)

BEARER_USER = "user-authenticated-01"
NAMED_BY_CALLER = "user-someone-else-99"


#: What the stand-in reports as the second half of its return. Named so a test
#: can assert the route read the SERVICE's answer rather than the value its own
#: fallback invents — which is False, and therefore indistinguishable from a
#: real one if the fake reports False too.
RESUMED = True


async def _create_session_fake(user_id, client_id=None, metadata=None):
    """Stands in for ``AuthSessionService.create_session``.

    Signature-faithful, and both halves of that are load-bearing:

    * **The parameter ORDER is the real one** — ``(user_id, client_id,
      metadata)``. Spelled ``(user_id, metadata, client_id)`` it binds
      correctly only for as long as the route keeps passing the last two by
      keyword; the day someone passes them positionally, ``metadata`` lands in
      ``client_id`` and every test here stays green.
    * **It returns the real SHAPE**, ``(SessionContext, resumed)``. The handler
      unpacks a tuple and falls back to ``(result, False)`` for anything else,
      so a bare object sent every assertion in this module through the FALLBACK
      arm — a branch production never takes, on a route whose ``session_resumed``
      is read from the value that fallback invents.

    ``test_the_stand_in_matches_the_real_service_signature`` keeps the first of
    those true as the real service changes; ``session_resumed`` in the vacuity
    control keeps the second true.
    """
    session = SimpleNamespace(
        session_id="sess-minted-0001",
        user_id=user_id,
        created_at=datetime.now(timezone.utc),
    )
    return session, RESUMED


def _build_app(*, current_user):
    """Mount the real session router; capture what the service is asked for.

    ``app.state.minted`` collects the ``user_id`` of every ``create_session``
    call — the artifact these tests read, rather than a value reconstructed
    from the same inputs the route saw.
    """
    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")
    app.state.minted = []

    async def _session_service():
        async def create_session_impl(user_id, client_id=None, metadata=None):
            app.state.minted.append(user_id)
            return await _create_session_fake(
                user_id, client_id=client_id, metadata=metadata
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
    # And that the route read the SERVICE's answer rather than the one its
    # `else: was_resumed = False` fallback invents — which is what a stand-in
    # returning a bare object silently exercised instead.
    assert response.json()["session_resumed"] is RESUMED


@pytest.mark.unit
def test_the_stand_in_matches_the_real_service_signature():
    """The fake takes what the real ``create_session`` takes, in that order.

    Kept as an assertion rather than as a comment because the failure it
    guards against is invisible at the call site: the route passes
    ``client_id`` and ``metadata`` by keyword, so a stand-in with them
    transposed binds correctly today and mis-binds the moment anybody passes
    them positionally — with these tests still green, still asserting about a
    ``user_id`` that happens to be first either way.

    ``self`` is dropped: the fake is a plain function and the real one is a
    method, and that difference is not drift.
    """
    real = inspect.signature(AuthSessionService.create_session)
    fake = inspect.signature(_create_session_fake)

    assert list(fake.parameters) == [name for name in real.parameters if name != "self"]


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


# =============================================================================
# The listing, which lost the same parameter one contract later (#1447 §1)
# =============================================================================
#
# These two mirror the pair above, onto ``GET /api/v1/sessions``. The mint's
# entry exists because FastAPI drops an undeclared query parameter silently, so
# a route that still DECLARES ``user_id`` while ignoring its value passes every
# request-level test and republishes the parameter to every client regenerating
# its types. That argument does not belong to the mint; it belongs to the
# parameter, and the listing carried the identical one — as a FILTER, on a route
# that took no auth dependency at all.


@pytest.mark.unit
@pytest.mark.security
def test_the_listing_handler_declares_no_user_id_parameter():
    """Gone from the signature, not merely unread.

    Re-adding it unread would keep the request-level tests green: they assert
    the listing answers with the caller's own sessions, which stays true while
    the parameter is ignored — right up until somebody wires it back up.
    """
    from faultmaven.modules.auth.api.session import list_sessions

    assert "user_id" not in inspect.signature(list_sessions).parameters


@pytest.mark.unit
@pytest.mark.security
def test_the_published_listing_declares_no_user_id_query_parameter():
    """And it is not published, which is what a client actually reads.

    Not ``parameters == []`` as on the mint: the listing legitimately publishes
    ``session_type``, ``limit`` and ``offset``. What must never come back is a
    parameter naming WHOSE sessions are listed, so the assertion names that and
    carries the survivors with it — a listing that published nothing would
    otherwise pass this while having lost its pagination.
    """
    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")

    operation = app.openapi()["paths"]["/api/v1/sessions"]["get"]
    published = {
        parameter["name"]
        for parameter in operation.get("parameters", [])
        if parameter.get("in") == "query"
    }

    assert "user_id" not in published
    assert published == {"session_type", "limit", "offset"}
