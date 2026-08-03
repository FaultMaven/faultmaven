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
the **class**, not on one site. ``test_no_500_site_interpolates_the_exception``
is the load-bearing one — it fails if *any* site in the router reintroduces the
pattern, including sites added later.
"""

import ast
import pathlib

import pytest

import faultmaven.modules.case.api.routes as routes_module
from faultmaven.exceptions import ServiceException

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
    """Class guard: no ``HTTPException(500)`` in the router interpolates ``e``.

    Pins the whole class rather than the one endpoint above, so a new handler
    copied from an old one cannot quietly reintroduce the leak. 4xx sites are
    deliberately out of scope — ``detail=str(e)`` on a ``ValidationException``
    arm is a domain message meant for the caller.
    """
    source = pathlib.Path(routes_module.__file__).read_text()
    tree = ast.parse(source)

    def is_500(call: ast.Call) -> bool:
        for kw in call.keywords:
            if kw.arg == "status_code":
                text = ast.unparse(kw.value)
                if "500" in text or "INTERNAL_SERVER_ERROR" in text:
                    return True
        return any(
            "500" in ast.unparse(a) or "INTERNAL_SERVER_ERROR" in ast.unparse(a)
            for a in call.args
        )

    def interpolates_exception(call: ast.Call) -> bool:
        for kw in call.keywords:
            if kw.arg == "detail":
                text = ast.unparse(kw.value)
                if "str(e)" in text or "{e}" in text:
                    return True
        return False

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        func = node.exc.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "HTTPException":
            continue
        if is_500(node.exc) and interpolates_exception(node.exc):
            offenders.append(node.lineno)

    assert offenders == [], (
        "HTTPException(500) sites interpolating the exception (leaks internal "
        f"text verbatim; use a static detail and log server-side): lines {offenders}"
    )
