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

Test scaffolding lives in ``conftest.py`` alongside the sibling module.
"""

from types import SimpleNamespace

import pytest

CASE_ID = "case-does-not-exist-0001"
PATH = f"/api/v1/cases/{CASE_ID}/uploaded-files"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_case_surfaces_as_404_not_500(build_app, call_api):
    """The handler's own 404 must reach the client verbatim."""
    response = await call_api(build_app(case=None), "GET", PATH)

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
async def test_known_case_still_reaches_the_handler_and_lists_files(
    build_app, call_api
):
    """Vacuity control: the same app/path reaches the handler and succeeds.

    Proves the 404 above is produced by the handler rather than by routing,
    dependency resolution, or a redirect short-circuiting the request.
    """
    app = build_app(case=SimpleNamespace(uploaded_files=[]))

    response = await call_api(app, "GET", PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_count"] == 0
    assert body["files"] == []
    assert response.headers["X-Total-Count"] == "0"
