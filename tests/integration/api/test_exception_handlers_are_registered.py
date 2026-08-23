"""The handlers are wired to the app that ships, not just to a fixture.

`tests/unit/api/test_request_validation_handler.py` proves the validation
handler behaves — a 422 whose `input` is bytes, an UploadFile, NaN or a lone
surrogate is rendered rather than crashing the handler into a 500 (#1048). It
proves it against a `FastAPI()` the fixture builds, which is the right subject
for behaviour and says nothing about production.

The gap that leaves is exact: deleting `app.add_exception_handler(
RequestValidationError, request_validation_exception_handler)` from `main.py`
leaves all 51 of those tests passing, while every real request falls back to
FastAPI's default handler. Verified by doing it. A fix nothing asserts is
installed is a fix that can be uninstalled silently.

These tests read the registration off the real app object. They are integration
tests because importing `faultmaven.main` builds it.
"""

import pytest
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError

from faultmaven.api.exception_handlers import (
    get_exception_handlers,
    http_exception_handler,
    request_validation_exception_handler,
)


@pytest.fixture(scope="module")
def app():
    from faultmaven.main import app as production_app

    return production_app


@pytest.mark.integration
def test_the_validation_handler_is_the_one_that_ships(app):
    """#1048's fix is only a fix if the app actually uses it."""
    registered = app.exception_handlers.get(RequestValidationError)

    assert registered is request_validation_exception_handler, (
        "faultmaven.main does not register the project's "
        "RequestValidationError handler, so validation errors fall back to "
        "FastAPI's default and #1048's totality guarantee does not apply to "
        "any real request"
    )


@pytest.mark.integration
def test_every_domain_handler_is_registered(app):
    """The same exposure for the rest of `get_exception_handlers()`.

    Registration is a loop in main.py; a handler added to the mapping and never
    reaching the app would be covered by unit tests and dead in production.
    """
    missing = [
        exc_type.__name__
        for exc_type, handler in get_exception_handlers().items()
        if app.exception_handlers.get(exc_type) is not handler
    ]

    assert not missing, (
        f"declared in get_exception_handlers() but not registered on the app: "
        f"{missing}"
    )


@pytest.mark.integration
def test_the_http_exception_handler_is_the_one_that_ships(app):
    """The case where losing the registration fails *silently*.

    FastAPI does `exception_handlers.setdefault(HTTPException, ...)` at
    construction, so dropping ours does not leave HTTPException unhandled — it
    falls back to FastAPI's default, which renders a dict `detail` into the
    body raw and reintroduces the shape #1048 is about. Nothing would 500,
    nothing would error; responses would just quietly change shape.

    Scope, so the guarantee is not read as broader than it is: this is keyed on
    `fastapi.exceptions.HTTPException`. `starlette.exceptions.HTTPException`
    remains mapped to FastAPI's own handler, so a router-raised 404 or 405 —
    anything raising Starlette's class rather than FastAPI's — does not pass
    through the coercion this module applies. Harmless today (those details are
    plain phrase strings, and nothing under `faultmaven/` imports Starlette's
    class), but it is a real edge of the guarantee rather than an oversight in
    this assertion.
    """
    registered = app.exception_handlers.get(HTTPException)

    assert registered is http_exception_handler, (
        "faultmaven.main does not register the project's HTTPException "
        "handler, so FastAPI's default renders dict details raw and the "
        "coercion this handler applies is not in effect"
    )
