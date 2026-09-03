"""What a caller and an operator see when the tenant turn cap refuses.

The mechanism's own rules are pinned in
``tests/unit/infrastructure/protection/test_tenant_turn_cap.py``. This module is
about the seam between that mechanism and HTTP: the status a client gets, the
sentence a person reads, the headers a client acts on, the line an operator
greps for, and the flag that keeps a refusal off the quota that protects LLM
compute.

Driven through a real ``TestClient`` rather than by awaiting the dependency, so
the ``HTTPException`` is actually rendered — headers included. A dependency that
raised the right exception into a handler that dropped its headers would pass an
await-and-inspect test and ship a 429 with no ``Retry-After``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.params import Depends as DependsMarker
from fastapi.testclient import TestClient

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    Reservation,
    TenantTurnCapExceeded,
    TenantTurnCapUnavailable,
)
from faultmaven.modules.case.api import turn_cap
from faultmaven.modules.case.api.turn_cap import (
    RATE_LIMIT_REFUND_ATTR,
    TURN_CAP_ERROR_CODE,
    TURN_CAP_UNAVAILABLE_ERROR_CODE,
    enforce_tenant_turn_cap,
)

pytestmark = pytest.mark.unit

ORG = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def app(monkeypatch):
    """A one-route app carrying the real dependency.

    ``require_authentication`` is overridden rather than stubbed at the module
    level, so the dependency's own ``Depends(require_authentication)`` edge is
    the one being satisfied — a module-level stub would leave that edge, whose
    ordering the guard depends on, unexercised.
    """
    monkeypatch.setattr(turn_cap, "get_current_tenant_id", lambda: ORG)

    application = FastAPI()

    @application.post("/turns", dependencies=[Depends(enforce_tenant_turn_cap)])
    async def _turn_route(request: Request):
        # Reports the marker rather than a fixed body, so the "an admitted turn
        # is NOT marked" case can be asserted from the response.
        return {"marked": getattr(request.state, RATE_LIMIT_REFUND_ATTR, False)}

    application.dependency_overrides[require_authentication] = lambda: object()
    return application


def _at_the_cap(
    limit=30, used=30, reset_in=timedelta(hours=6), source="default_personal"
):
    async def _raise(organization_id, **_):
        raise TenantTurnCapExceeded(
            organization_id=organization_id,
            limit=limit,
            used=used,
            reset_at=datetime.now(timezone.utc) + reset_in,
            source=source,
        )

    return _raise


def test_a_tenant_at_its_cap_is_refused_with_a_message_that_says_when_it_resets(
    app, monkeypatch
):
    """Invariant 1, at the wire."""
    monkeypatch.setattr(turn_cap, "reserve_turn", _at_the_cap(limit=30, used=30))

    with TestClient(app) as client:
        response = client.post("/turns")

    assert response.status_code == 429
    detail = response.json()["detail"]
    assert "30" in detail
    assert "UTC" in detail
    assert "resets" in detail


def test_the_refusal_carries_a_wait_and_an_error_code_a_client_can_branch_on(
    app, monkeypatch
):
    """A rate-limit 429 and a cap 429 want opposite reactions from a client."""
    monkeypatch.setattr(
        turn_cap, "reserve_turn", _at_the_cap(reset_in=timedelta(minutes=30))
    )

    with TestClient(app) as client:
        response = client.post("/turns")

    assert response.headers["x-error-code"] == TURN_CAP_ERROR_CODE
    assert 1750 <= int(response.headers["Retry-After"]) <= 1802


def test_a_refusal_is_marked_so_the_rate_limiter_can_release_it(app, monkeypatch):
    """Invariant 6's mechanism half.

    The middleware reads this attribute on the way out. Asserted on the request
    scope rather than by observing the middleware, so the two halves are pinned
    independently — a rename on either side fails one of them.
    """
    seen = {}

    async def _raise(organization_id, **_):
        raise TenantTurnCapExceeded(
            organization_id=organization_id,
            limit=1,
            used=1,
            reset_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    monkeypatch.setattr(turn_cap, "reserve_turn", _raise)

    from starlette.middleware.base import BaseHTTPMiddleware

    class _Observer(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            seen["marked"] = getattr(request.state, RATE_LIMIT_REFUND_ATTR, False)
            return response

    app.add_middleware(_Observer)
    with TestClient(app) as client:
        assert client.post("/turns").status_code == 429

    assert seen["marked"] is True, (
        "the refusal did not mark the request, so the rate limiter will charge "
        "a turn that ran no model against the quota that protects LLM compute"
    )


def test_an_admitted_turn_is_not_marked_for_release(app, monkeypatch):
    """The mirror of the above: a served turn DID spend what the bucket meters."""

    async def _admit(organization_id, **_):
        return Reservation(
            organization_id=organization_id, used=1, limit=30, source="default_personal"
        )

    monkeypatch.setattr(turn_cap, "reserve_turn", _admit)

    with TestClient(app) as client:
        response = client.post("/turns")

    assert response.status_code == 200
    assert response.json() == {"marked": False}


def test_the_refusal_is_logged_with_the_organization_and_the_count(
    app, monkeypatch, caplog
):
    """Invariant 6's observability half.

    Both fields, because the operator's next action — raise this tenant's cap,
    or leave it — needs to know which tenant and how far over.
    """
    monkeypatch.setattr(
        turn_cap, "reserve_turn", _at_the_cap(limit=30, used=30, source="override")
    )

    with caplog.at_level(logging.INFO, logger=turn_cap.logger.name):
        with TestClient(app) as client:
            assert client.post("/turns").status_code == 429

    lines = [record.getMessage() for record in caplog.records]
    matching = [line for line in lines if ORG in line]
    assert matching, f"no log line names the organization: {lines}"
    assert any("30/30" in line for line in matching), matching
    assert any("override" in line for line in matching), matching


def test_a_cap_that_cannot_be_applied_refuses_with_503_not_429(app, monkeypatch):
    """Fail closed, but do not claim the caller spent an allowance they did not."""

    async def _unavailable(organization_id, **_):
        raise TenantTurnCapUnavailable("ledger unreachable")

    monkeypatch.setattr(turn_cap, "reserve_turn", _unavailable)

    with TestClient(app) as client:
        response = client.post("/turns")

    assert response.status_code == 503
    assert response.headers["x-error-code"] == TURN_CAP_UNAVAILABLE_ERROR_CODE
    # And it must not tell the user their day is spent.
    assert "today" not in response.json()["detail"].lower()


def test_the_unavailable_refusal_does_not_leak_the_underlying_error(app, monkeypatch):
    """The caller gets a next step; the cause goes to the log."""

    async def _unavailable(organization_id, **_):
        raise TenantTurnCapUnavailable(
            "connection to fmprod-db-7.internal:5432 refused"
        )

    monkeypatch.setattr(turn_cap, "reserve_turn", _unavailable)

    with TestClient(app) as client:
        detail = client.post("/turns").json()["detail"]

    assert "fmprod-db-7" not in detail
    assert "5432" not in detail


def test_a_request_with_no_usable_tenant_is_refused_rather_than_uncapped(
    app, monkeypatch
):
    """Invariant 5's front edge: this is not the place that decides "no tenant, no cap"."""
    monkeypatch.setattr(turn_cap, "get_current_tenant_id", lambda: None)

    called = []

    async def _reserve(organization_id, **_):  # pragma: no cover - must not run
        called.append(organization_id)
        return Reservation(organization_id, 1, None, "x")

    monkeypatch.setattr(turn_cap, "reserve_turn", _reserve)

    with TestClient(app) as client:
        response = client.post("/turns")

    assert response.status_code == 403
    assert not called


async def test_the_organization_is_the_one_the_rls_binder_bound(monkeypatch):
    """The seam between the request front door and this guard.

    ``bind_request_org_context`` (the app-level dependency that scopes every
    request for RLS) sets the tenant contextvar; this guard reads it. Nothing
    else relates the two, so the seam is asserted directly rather than through
    ``get_current_tenant_id`` being patched — a guard reading the actor's claim
    instead would pass every other case in this module and then write a ledger
    row the RLS ``WITH CHECK`` refuses, because single-tenant deployments force
    the Standalone org regardless of the claim.
    """
    from faultmaven.config.constants import STANDALONE_ORG_ID
    from faultmaven.config.tenant_context import set_current_org_id

    bound = "org-bound-by-the-front-door"
    reserved = []

    async def _reserve(organization_id, **_):
        reserved.append(organization_id)
        return Reservation(organization_id, 1, None, "company_uncapped")

    monkeypatch.setattr(turn_cap, "reserve_turn", _reserve)

    # Driven in this task rather than through ``TestClient``, which runs the app
    # in its own loop and therefore its own context: the claim under test is
    # which READ the guard performs, not how a loop copies contextvars. The
    # binder is a FastAPI dependency rather than a BaseHTTPMiddleware precisely
    # so that it and the endpoint share one context.
    set_current_org_id(bound)
    try:
        await enforce_tenant_turn_cap(
            request=Request(
                {
                    "type": "http",
                    "headers": [],
                    "method": "POST",
                    "path": "/turns",
                    "query_string": b"",
                }
            ),
            _authenticated=object(),
        )
    finally:
        set_current_org_id(STANDALONE_ORG_ID)

    assert reserved == [bound]


def test_authentication_is_inside_the_guards_dependency_tree(app):
    """Ordering, pinned where a reader will look for it.

    FastAPI inserts a route's ``dependencies=[...]`` at the FRONT of the
    dependant list, so this guard runs before the handler's own
    ``Depends(require_authentication)``. Declaring the dependency here is what
    puts authentication first; without it an unauthenticated POST would be
    answered by the tenant check, and in single-tenant mode it would charge the
    ledger for a request about to be rejected.
    """
    import inspect

    signature = inspect.signature(enforce_tenant_turn_cap)
    dependencies = [
        parameter.default.dependency
        for parameter in signature.parameters.values()
        if isinstance(parameter.default, DependsMarker)
    ]
    assert require_authentication in dependencies, (
        "enforce_tenant_turn_cap no longer depends on require_authentication, so "
        "it runs ahead of authentication on the turn route"
    )
