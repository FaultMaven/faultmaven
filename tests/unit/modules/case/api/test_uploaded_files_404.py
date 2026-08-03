"""An unknown case on GET /cases/{case_id}/uploaded-files must be a 404.

Same defect class as ``test_case_for_session_401.py``, second instance in the
same file: ``list_uploaded_files`` raises its own
``HTTPException(404, "Case not found")`` when the case service cannot resolve
the case, but its ``except`` chain was ``NotFoundError`` /
``PermissionDeniedException`` / bare ``Exception`` with no
``except HTTPException: raise``. The bare handler caught the handler's *own*
``HTTPException`` and re-wrapped it as ``500 {"detail": "404: Case not found"}``.

The path is live: ``CaseService.get_case`` returns ``None`` both for a case
that does not exist and for one the caller may not access, so any unknown or
inaccessible case id reached this branch and was reported as a server fault.

Conventions match the sibling test module: requests are driven through the real
router over ``httpx.ASGITransport`` on a single event loop rather than
``fastapi.testclient.TestClient``, which creates a fresh event loop per request.
"""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_case_service
from faultmaven.modules.case.api.routes import router as case_router

CASE_ID = "case-does-not-exist-0001"


def _build_app(case):
    """Mount the real case router with only what this route resolves.

    ``case`` is what the case service resolves ``CASE_ID`` to: ``None`` models
    an unknown or inaccessible case (the 404 path), an object exposing
    ``uploaded_files`` models a live one (the 200 path used as the control).
    """
    app = FastAPI()
    app.include_router(case_router, prefix="/api/v1")

    async def _case_service():
        async def get_case(case_id, user_id=None):
            return case

        return SimpleNamespace(get_case=get_case)

    async def _current_user():
        return SimpleNamespace(user_id="user-1")

    app.dependency_overrides[get_case_service] = _case_service
    app.dependency_overrides[require_authentication] = _current_user
    return app


async def _get_uploaded_files(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(f"/api/v1/cases/{CASE_ID}/uploaded-files")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_case_surfaces_as_404_not_500():
    """The handler's own 404 must reach the client verbatim."""
    response = await _get_uploaded_files(_build_app(case=None))

    # Exact status, not merely "not 500": a request that never reaches the
    # handler (router not mounted where we think, or a redirect) fails here
    # rather than passing vacuously.
    assert response.status_code == 404, response.text

    detail = response.json()["detail"]
    assert detail == "Case not found"
    # The 500-wrap stringified the HTTPException, embedding its status as text.
    assert "404:" not in detail


@pytest.mark.unit
@pytest.mark.asyncio
async def test_known_case_still_reaches_the_handler_and_lists_files():
    """Vacuity control: the same app/path reaches the handler and succeeds.

    Proves the 404 above is produced by the handler rather than by routing,
    dependency resolution, or a redirect short-circuiting the request.
    """
    response = await _get_uploaded_files(
        _build_app(case=SimpleNamespace(uploaded_files=[]))
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_count"] == 0
    assert body["files"] == []
    assert response.headers["X-Total-Count"] == "0"
