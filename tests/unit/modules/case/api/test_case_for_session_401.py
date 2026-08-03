"""An invalid/expired session on POST /cases/sessions/{id}/case must be a 401.

Root cause guarded here: ``create_case_for_session`` raises its own
``HTTPException(401, "Invalid or expired session")`` when the session service
cannot resolve the session, but its ``except`` chain was only
``except ValidationException`` followed by a bare ``except Exception``. The
bare handler caught the handler's *own* ``HTTPException`` and re-wrapped it as
``500 "Failed to manage session case: 401: Invalid or expired session"``.

Consequence: the client never saw a 401 and therefore never re-authenticated —
an expired session presented as a server fault. The sibling handler
``resume_case_in_session`` directly below already re-raises ``HTTPException``;
this route now matches it.

Test scaffolding (app builder, request helper, signature-faithful service
fakes) lives in ``conftest.py`` alongside the rationale for driving requests
over ASGITransport rather than TestClient.
"""

from types import SimpleNamespace

import pytest

SESSION_ID = "sess-expired-0001"
PATH = f"/api/v1/cases/sessions/{SESSION_ID}/case"
CASE_ID = "case-123"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expired_session_surfaces_as_401_not_500(build_app, call_api):
    """The handler's own 401 must reach the client verbatim."""
    response = await call_api(build_app(session=None), "POST", PATH)

    # Exact status, not merely "not 500": a request that 404s (router not
    # mounted where we think) or 307s (trailing-slash redirect) would fail here
    # rather than passing vacuously.
    assert response.status_code == 401, response.text

    detail = response.json()["detail"]
    assert detail == "Invalid or expired session"
    # The 500-wrap stringified the HTTPException, embedding its status as text.
    assert "401:" not in detail
    assert "Failed to manage session case" not in detail


@pytest.mark.unit
@pytest.mark.asyncio
async def test_live_session_still_reaches_the_handler_and_returns_a_case(
    build_app, call_api
):
    """Vacuity control: the same app/path reaches the handler and succeeds.

    Proves the 401 above is produced by the handler rather than by routing,
    dependency resolution, or a redirect short-circuiting the request.
    """
    live_session = SimpleNamespace(user_id="user-1")
    app = build_app(session=live_session, case_id=CASE_ID)

    response = await call_api(app, "POST", PATH)

    assert response.status_code == 200, response.text
    assert response.json()["case_id"] == CASE_ID
