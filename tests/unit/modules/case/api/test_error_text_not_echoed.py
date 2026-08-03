"""Raw exception text never reaches a case-module 500 response body.

The case router had 21 sites shaped like::

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"...: {str(e)}")

which hand the caller the text of an internal exception. That text is not
harmless: ``ServiceException`` wrapping embeds SQLAlchemy statement and table
names, and a Redis failure embeds ``host:port``.

Nothing downstream scrubs it. ``main.py``'s ``HTTPException`` handler returns a
string ``detail`` **verbatim**, and the scrubbing 500 handler beside it only
runs for *unhandled* exceptions — so an explicitly raised
``HTTPException(500, detail=...)`` bypasses the scrubber entirely and is
serialized straight to the client.

This is the case-module analogue of the knowledge module's #866 fix, and these
tests follow that module's ``test_error_text_not_echoed.py``: they assert on
the **class**, not on one site. The two class guards are the load-bearing ones
— they fail if *any* site in the router reintroduces the pattern, including
sites added later.

Four sites survived the original sweep because they do not name the exception
in the ``detail`` expression at all: this router builds an ``ErrorResponse``
one statement earlier and passes ``error_response.model_dump()``, and two of
them *return* a body instead of raising. Those are fixed and the guards now
follow local aliases and cover returned bodies — see ``tests/error_text_ast``.

Scope is ``routes.py``, as before. ``modules/case/api/replay.py`` has its own
sites and is knowingly not covered here; it is queued with the remaining
unswept modules.
"""

import pathlib

import pytest

import faultmaven.modules.case.api.routes as routes_module
from faultmaven.exceptions import ServiceException
from tests.error_text_ast import (
    http_exception_leak_sites,
    returned_body_leak_sites,
)

# A ServiceException carrying the sort of internals that wrapping leaks.
_SECRET = "secret-internal-detail"
_INTERNAL_ERROR = (
    f"{_SECRET}: (psycopg2.OperationalError) FATAL: relation "
    'sqlalchemy table "users" does not exist at db.internal:5432'
)

SESSION_ID = "sess-leak-0001"
PATH = f"/api/v1/cases/sessions/{SESSION_ID}/case"


def _assert_no_leak(body_text: str) -> None:
    """Nothing recognizably from the internal exception survives into the body."""
    assert _SECRET not in body_text
    assert "psycopg2" not in body_text
    assert "sqlalchemy table" not in body_text
    assert "db.internal:5432" not in body_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_500_body_does_not_echo_the_exception(build_app, call_api):
    """A representative bare-except 500 arm, driven end to end.

    ``create_case_for_session`` is the stand-in: its ``except Exception`` arm
    is reached by any failure inside the handler body, here a service that
    raises ``ServiceException``.
    """
    from types import SimpleNamespace

    async def get_or_create_case_for_session(
        session_id, user_id=None, force_new=False, title=None
    ):
        raise ServiceException(_INTERNAL_ERROR)

    failing_service = SimpleNamespace(
        get_or_create_case_for_session=get_or_create_case_for_session
    )
    app = build_app(
        session=SimpleNamespace(user_id="user-1"), case_service=failing_service
    )

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 500, response.text
    _assert_no_leak(response.text)
    # The generic replacement still tells the caller which operation failed.
    assert response.json()["detail"] == "Failed to manage session case"


@pytest.mark.unit
def test_no_500_site_interpolates_the_exception():
    """Class guard: no 5xx in the router carries ``e`` into the response.

    Pins the whole class rather than the one endpoint above, so a new handler
    copied from an old one cannot quietly reintroduce the leak. 4xx sites are
    deliberately out of scope — ``detail=str(e)`` on a ``ValidationException``
    arm is a domain message meant for the caller.

    The original version of this guard inspected the ``detail`` expression for
    the substrings ``str(e)``/``{e}``, and so reported this file clean while
    three sites leaked. This router does not put the text in ``detail``: it
    builds the payload one statement earlier and passes an alias::

        error_response = ErrorResponse(error=ErrorDetail(message=str(e)))
        raise HTTPException(500, detail=error_response.model_dump())

    ``tests/error_text_ast`` follows local aliases and matches ``ast.Name``
    nodes rather than unparsed substrings, which is what closes that gap (and
    ``repr(e)``, ``f"{e!s}"`` and ``e.args[0]`` with it).
    """
    offenders = http_exception_leak_sites(pathlib.Path(routes_module.__file__))

    assert offenders == [], (
        "5xx HTTPException sites carrying the caught exception into the "
        "response (leaks internal text verbatim; use a static message and log "
        f"server-side): {offenders}"
    )


@pytest.mark.unit
def test_no_except_handler_returns_the_exception():
    """No ``return`` inside an ``except`` carries ``e`` into a body either.

    The guard above cannot see these: ``list_cases`` degraded by *returning* a
    ``JSONResponse(503, content=error_response.model_dump())``, and
    ``GET /cases/health`` by returning ``{"error": str(e)}`` with a 200. Same
    leak, different wire shape.
    """
    offenders = returned_body_leak_sites(pathlib.Path(routes_module.__file__))

    assert offenders == [], (
        "return statements inside except handlers carrying the caught "
        "exception into the response body (use a static message and log "
        f"server-side): {offenders}"
    )
