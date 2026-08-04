"""``main.py``'s HTTPException handler renders every ``detail`` shape sanely.

**Two** dict shapes reach the handler, and until these tests nothing pinned
either:

* **nested** — ``ErrorResponse.model_dump()``, i.e.
  ``{"schema_version": ..., "error": {"code": ..., "message": ...}}``. The case
  router raises it at 15 sites as ``detail=error_response.model_dump()``, so the
  shape is constructed *at runtime* and a grep for a literal
  ``{"error": {"message": ...}}`` finds nothing. It is nonetheless the common
  case on every case create/get/update/delete failure.
* **flat** — ``{"error": "<code>", "message": "<text>"}``, written literally at
  16 sites.

Both must yield the human message: the copilot and dashboard render ``detail``
verbatim as user-facing text, so returning the dict's ``str()`` puts a Python
dict repr in front of a user.

The handler's original nested branch got the *right* shape for the *wrong*
reason::

    if isinstance(detail, dict) and "error" in detail and "message" in detail["error"]:

``detail["error"]`` is a ``str`` in the flat shape, so ``"message" in ...`` is a
substring test over the error *code* rather than a key lookup. It happened to
be ``False`` for every code in the tree, which is why the flat shape fell
through correctly — but a code such as ``"message_send_failed"`` would take the
branch and raise ``TypeError: string indices must be integers`` *inside the
exception handler*. The branch now type-checks before subscripting.

``_main_reference_handler`` below is the handler as it stood on ``c453f60a``,
copied verbatim. Every shape it renders without crashing must render
byte-identically now: this is a differential oracle, so a future
"simplification" cannot silently change what a user sees.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from faultmaven.main import http_exception_handler
from faultmaven.models.api import ErrorDetail, ErrorResponse


async def _main_reference_handler(request, exc: HTTPException):
    """The handler as of ``c453f60a`` (pre-PR ``main``), copied verbatim.

    Kept as an oracle, not as production code. It raises ``TypeError`` on one
    input — see the error-code test below.
    """
    detail = exc.detail

    if isinstance(detail, dict) and "error" in detail and "message" in detail["error"]:
        error_message = detail["error"]["message"]
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": error_message},
            headers=getattr(exc, "headers", None),
        )
    elif isinstance(detail, dict):
        message = detail.get("message") or detail.get("detail") or str(detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": message},
            headers=getattr(exc, "headers", None),
        )
    else:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(detail)},
            headers=getattr(exc, "headers", None),
        )


async def _render(detail, status_code: int = 500):
    """Run the live handler over one ``detail`` and return (status, parsed body)."""
    response = await http_exception_handler(
        None, HTTPException(status_code=status_code, detail=detail)
    )
    return response.status_code, json.loads(response.body)


async def _assert_matches_main(detail, status_code: int = 500):
    """The live handler and pre-PR ``main`` produce byte-identical bodies."""
    exc = HTTPException(status_code=status_code, detail=detail)
    live = await http_exception_handler(None, exc)
    reference = await _main_reference_handler(None, exc)

    assert live.body == reference.body, (
        f"behaviour drifted from main for detail={detail!r}: "
        f"{live.body!r} != {reference.body!r}"
    )
    assert live.status_code == reference.status_code
    return json.loads(live.body)


def _error_response_detail(message: str, code: str = "FORBIDDEN") -> dict:
    """The nested shape as the case router actually builds it.

    Built from the real model rather than hand-written: a hand-written dict is
    what let the regression through in the first place, because the literal in
    the test was the flat shape nobody was raising.
    """
    return ErrorResponse(error=ErrorDetail(code=code, message=message)).model_dump()


# ===========================================================================
# The two live dict shapes
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nested_error_response_detail_yields_the_human_message():
    """``ErrorResponse.model_dump()`` — the case router's 15 sites.

    The failure this pins is not cosmetic: without the nested branch the body
    is ``{"detail": "{'schema_version': '3.1.0', 'error': {...}}"}`` and both
    frontends render that dict repr straight into the UI.
    """
    detail = _error_response_detail("Not authorized to delete this case")
    # Guard the fixture: if ErrorResponse ever flattens, this test must fail
    # loudly rather than quietly stop exercising the nested branch.
    assert isinstance(detail["error"], dict)
    assert detail["error"]["message"] == "Not authorized to delete this case"

    body = await _assert_matches_main(detail, status_code=403)

    assert body == {"detail": "Not authorized to delete this case"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flat_structured_detail_extracts_the_message():
    """``{"error": "<code>", "message": "<text>"}`` — the 16 literal sites."""
    body = await _assert_matches_main(
        {"error": "internal_error", "message": "Failed to list users"}
    )

    assert body == {"detail": "Failed to list users"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_string_detail_is_returned_verbatim():
    body = await _assert_matches_main("Logout failed")

    assert body == {"detail": "Logout failed"}


# ===========================================================================
# The shape that used to break the handler
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_error_code_containing_message_no_longer_breaks_the_handler():
    """A flat error *code* containing the substring "message".

    The only input where the live handler is *allowed* to differ from main:
    main raises ``TypeError`` here, indexing a ``str`` with a ``str`` inside the
    exception handler. The nested branch now type-checks first, so this takes
    the flat path and renders like any other site.
    """
    detail = {"error": "message_send_failed", "message": "Notification failed"}

    with pytest.raises(TypeError):
        await _main_reference_handler(
            None, HTTPException(status_code=502, detail=detail)
        )

    status, body = await _render(detail, status_code=502)

    assert status == 502
    assert body == {"detail": "Notification failed"}


# ===========================================================================
# Fall-throughs
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dict_without_message_falls_back_to_detail_then_repr():
    """The remaining fall-throughs, so the fallback chain stays covered."""
    body = await _assert_matches_main({"detail": "Only a detail key"})
    assert body == {"detail": "Only a detail key"}

    body = await _assert_matches_main({"code": "no_message_key"})
    assert body == {"detail": str({"code": "no_message_key"})}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nested_error_without_message_falls_back():
    """A dict ``error`` value lacking ``message`` must not be subscripted blindly."""
    body = await _assert_matches_main({"error": {"code": "X"}, "message": "fallback"})
    assert body == {"detail": "fallback"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_headers_are_preserved():
    """Headers ride along on the dict path as well as the string path."""
    exc = HTTPException(status_code=401, detail="Unauthorized")
    exc.headers = {"WWW-Authenticate": "Bearer"}
    response = await http_exception_handler(None, exc)

    assert response.headers["WWW-Authenticate"] == "Bearer"
