"""Raw exception text never reaches an auth-module response body.

The auth routers had 19 sites that handed the caller the text of an internal
exception caught by a broad ``except``:

* 18 shaped like ``raise HTTPException(500, detail=f"...: {str(e)}")`` (five in
  ``auth.py``, thirteen in ``session.py``), two of which used the dict form
  ``detail={"error": "internal_error", "message": str(e)}``; and
* ``GET /auth/health``, which returned **200** with ``"error": str(e)`` in the
  body — on a route that takes no credentials at all.

Nothing downstream scrubs any of it. ``main.py``'s ``HTTPException`` handler
returns a string ``detail`` verbatim and, for a dict ``detail``, extracts
``detail["message"]`` verbatim — so both forms reach the client as
``{"detail": "<raw driver text>"}``. The scrubbing 500 handler beside it fires
only for *unhandled* exceptions, which an explicitly raised ``HTTPException``
is not.

This is the auth-module analogue of #866 (knowledge) and #966 (case), and
follows those modules' ``test_error_text_not_echoed.py``: the assertions are on
the **class**, not on the sites that happened to exist when it was written.
``test_no_500_site_interpolates_the_exception`` and
``test_no_except_handler_returns_the_exception`` are the load-bearing ones —
they fail if *any* site under ``modules/auth/api/`` reintroduces the pattern,
including sites added later.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException

import faultmaven.modules.auth.api as auth_api_package
from faultmaven.api.v1.auth_dependencies import require_platform_admin
from faultmaven.exceptions import ServiceException
from faultmaven.modules.auth.api.auth import router as auth_router
from tests.error_text_ast import (
    http_exception_leak_sites,
    returned_body_leak_sites,
)

AUTH_API_DIR = pathlib.Path(auth_api_package.__file__).parent

# A ServiceException carrying the sort of internals that wrapping leaks: the
# sentinel plus driver/topology text a caller must never be handed.
_SECRET = "secret-internal-xyzzy"
_INTERNAL_ERROR = (
    f"{_SECRET}: (psycopg2.OperationalError) FATAL: relation "
    'sqlalchemy table "users" does not exist at db.internal:5432'
)


def _assert_no_leak(body_text: str) -> None:
    """Nothing recognizably from the internal exception survives into the body."""
    assert _SECRET not in body_text
    assert "psycopg2" not in body_text
    assert "sqlalchemy table" not in body_text
    assert "db.internal:5432" not in body_text


# ===========================================================================
# Class guards
# ===========================================================================


def _auth_api_sources() -> list[pathlib.Path]:
    """Every module in the auth API surface, so a new router is covered too."""
    paths = sorted(p for p in AUTH_API_DIR.glob("*.py") if p.name != "__init__.py")
    # Guard the guard: an empty sweep would pass vacuously.
    assert len(paths) >= 5, f"auth API surface unexpectedly small: {paths}"
    return paths


@pytest.mark.unit
def test_no_500_site_interpolates_the_exception():
    """No 5xx under ``modules/auth/api/`` carries ``e`` into the response.

    Pins the whole class rather than the endpoints probed below, so a new
    handler copied from an old one cannot quietly reintroduce the leak. Both
    detail forms are covered: the f-string and the
    ``{"error": ..., "message": ...}`` dict, whose ``message`` value main.py
    extracts and returns verbatim. The analysis in ``tests/error_text_ast``
    follows local aliases and matches ``ast.Name`` nodes, so ``repr(e)``,
    ``f"{e!s}"``, ``e.args[0]``, a positional ``detail`` and a payload built one
    statement earlier are all caught too.

    4xx sites are deliberately out of scope: ``detail=str(e)`` on an
    ``InvalidGrantError`` or ``ValidationException`` arm is a domain message
    meant for the caller, not internal text escaping a broad except.
    """
    offenders = [
        site for path in _auth_api_sources() for site in http_exception_leak_sites(path)
    ]

    assert offenders == [], (
        "5xx HTTPException sites carrying the caught exception into the "
        "response (leaks internal text verbatim; use a static detail and log "
        f"server-side): {offenders}"
    )


@pytest.mark.unit
def test_no_except_handler_returns_the_exception():
    """No ``return`` inside an ``except`` embeds the exception in a body.

    The ``HTTPException`` guard above cannot see ``GET /auth/health``: it
    degraded by *returning* ``{"status": "unhealthy", "error": str(e)}`` with a
    200, on an unauthenticated route. Same leak, different wire shape.
    """
    offenders = [
        site for path in _auth_api_sources() for site in returned_body_leak_sites(path)
    ]

    assert offenders == [], (
        "return statements inside except handlers embedding the caught "
        "exception in the response body (leaks internal text on a possibly "
        f"unauthenticated route; use a static message): {offenders}"
    )


# ===========================================================================
# End-to-end leak probes
# ===========================================================================


def _build_app(*, user_store=None):
    """The real auth router behind main.py's own ``HTTPException`` handler.

    The handler matters: FastAPI's default would serialize a dict ``detail``
    as-is, so a probe without it would not exercise the path that actually
    renders ``detail["message"]`` to the client.
    """
    from faultmaven.main import http_exception_handler

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.state.user_store = user_store

    async def _admin():
        return SimpleNamespace(user_id="admin-1", roles=["platform_admin"])

    app.dependency_overrides[require_platform_admin] = _admin
    return app


async def _get(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_users_500_does_not_echo_the_exception():
    """A representative dict-detail 500 arm, driven end to end.

    ``list_users`` stands in for the class: its ``except Exception`` arm is
    reached by any failure in the handler body, here a store that raises
    ``ServiceException``.
    """

    async def list_users(limit):
        raise ServiceException(_INTERNAL_ERROR)

    async def count_users():
        return 0

    store = SimpleNamespace(list_users=list_users, count_users=count_users)
    response = await _get(_build_app(user_store=store), "/api/v1/auth/users")

    assert response.status_code == 500, response.text
    _assert_no_leak(response.text)
    # The generic replacement still names the operation that failed.
    assert response.json()["detail"] == "Failed to list users"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_auth_health_200_does_not_echo_the_exception():
    """The unauthenticated degraded-200 arm keeps its shape without the text."""
    with patch(
        "faultmaven.modules.auth.api.auth.check_auth_services_health",
        side_effect=ServiceException(_INTERNAL_ERROR),
    ):
        response = await _get(_build_app(), "/api/v1/auth/health")

    assert response.status_code == 200, response.text
    _assert_no_leak(response.text)

    body = response.json()
    # Shape preserved: callers key on `status` and on `error` being present.
    assert body["status"] == "unhealthy"
    assert body["error"] == "Auth health check failed"
    assert "timestamp" in body
