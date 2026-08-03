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

The requests are driven through the real router over
``httpx.ASGITransport`` on a single event loop. ``fastapi.testclient.TestClient``
is deliberately not used: it creates a fresh event loop per request, which
breaks async fakeredis-backed infrastructure elsewhere in this suite.
"""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from faultmaven.api.v1.auth_dependencies import get_current_user_optional
from faultmaven.modules.case.api.routes import (
    _di_get_case_service_dependency,
    _di_get_session_service_dependency,
)
from faultmaven.modules.case.api.routes import (
    router as case_router,
)

SESSION_ID = "sess-expired-0001"


def _build_app(session, case_id="case-123"):
    """Mount the real case router with only the two services this route uses.

    ``session`` is what the session service resolves ``SESSION_ID`` to:
    ``None`` models an invalid/expired session (the 401 path), an object with a
    ``user_id`` models a live one (the 200 path used as the vacuity control).
    """
    app = FastAPI()
    app.include_router(case_router, prefix="/api/v1")

    async def _session_service():
        async def get_session(session_id, validate=True):
            return session

        return SimpleNamespace(get_session=get_session)

    async def _case_service():
        # Signature mirrors CaseService.get_or_create_case_for_session exactly.
        # A fake that accepts **kwargs would pass on arguments the real method
        # rejects, letting a handler pass here and fail in production.
        async def get_or_create_case_for_session(
            session_id, user_id=None, force_new=False, title=None
        ):
            return case_id

        return SimpleNamespace(
            get_or_create_case_for_session=get_or_create_case_for_session
        )

    async def _current_user_optional():
        return None

    app.dependency_overrides[_di_get_session_service_dependency] = _session_service
    app.dependency_overrides[_di_get_case_service_dependency] = _case_service
    app.dependency_overrides[get_current_user_optional] = _current_user_optional
    return app


async def _post_case_for_session(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(f"/api/v1/cases/sessions/{SESSION_ID}/case")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expired_session_surfaces_as_401_not_500():
    """The handler's own 401 must reach the client verbatim."""
    response = await _post_case_for_session(_build_app(session=None))

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
async def test_live_session_still_reaches_the_handler_and_returns_a_case():
    """Vacuity control: the same app/path reaches the handler and succeeds.

    Proves the 401 above is produced by the handler rather than by routing,
    dependency resolution, or a redirect short-circuiting the request.
    """
    live_session = SimpleNamespace(user_id="user-1")
    response = await _post_case_for_session(_build_app(session=live_session))

    assert response.status_code == 200, response.text
    assert response.json()["case_id"] == "case-123"
