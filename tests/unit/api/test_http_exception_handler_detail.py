"""``main.py``'s HTTPException handler renders every ``detail`` shape sanely.

The handler used to open with a branch that could never run::

    if isinstance(detail, dict) and "error" in detail and "message" in detail["error"]:
        error_message = detail["error"]["message"]

``detail["error"]`` is a **flat string** — an error *code* — at every
dict-detail raise site in the tree, so ``"message" in detail["error"]`` is a
substring test over that code, not a key lookup. No code contains
``"message"``, so the branch was dead; the dict fall-through below it did all
the work. Worse, a code that *did* contain the substring (say
``"message_send_failed"``) would have taken the branch and raised
``TypeError: string indices must be integers`` *inside the exception handler*.

The branch is gone. These tests pin the three shapes that reach the handler —
nothing pinned it before, which is how a dead branch survived.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from faultmaven.main import http_exception_handler


async def _render(detail, status_code: int = 500):
    """Run the handler over one ``detail`` and return (status, parsed body)."""
    response = await http_exception_handler(
        None, HTTPException(status_code=status_code, detail=detail)
    )
    return response.status_code, json.loads(response.body)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flat_structured_detail_extracts_the_message():
    """The shape every dict-detail site actually raises."""
    status, body = await _render(
        {"error": "internal_error", "message": "Failed to list users"}
    )

    assert status == 500
    assert body == {"detail": "Failed to list users"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_string_detail_is_returned_verbatim():
    status, body = await _render("Logout failed", status_code=500)

    assert status == 500
    assert body == {"detail": "Logout failed"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_error_code_containing_message_no_longer_breaks_the_handler():
    """The shape that would have taken the dead branch and raised TypeError.

    With branch 1 present this call died inside the exception handler —
    ``"message" in "message_send_failed"`` is ``True``, then
    ``detail["error"]["message"]`` indexes a string with a string. Now it takes
    the one remaining dict path and renders the message like any other site.
    """
    status, body = await _render(
        {"error": "message_send_failed", "message": "Notification delivery failed"},
        status_code=502,
    )

    assert status == 502
    assert body == {"detail": "Notification delivery failed"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dict_without_message_falls_back_to_detail_then_repr():
    """The remaining fall-throughs, so the fallback chain stays covered."""
    _, body = await _render({"detail": "Only a detail key"})
    assert body == {"detail": "Only a detail key"}

    _, body = await _render({"code": "no_message_key"})
    assert body == {"detail": str({"code": "no_message_key"})}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_headers_are_preserved():
    """Headers ride along on the dict path as well as the string path."""
    exc = HTTPException(status_code=401, detail="Unauthorized")
    exc.headers = {"WWW-Authenticate": "Bearer"}
    response = await http_exception_handler(None, exc)

    assert response.headers["WWW-Authenticate"] == "Bearer"
