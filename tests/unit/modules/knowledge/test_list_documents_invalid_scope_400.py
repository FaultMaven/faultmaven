"""An invalid ``scope`` on GET /knowledge/documents must be a 400, not a 500.

Root cause guarded here: ``list_documents`` validates ``scope`` inside its
``try`` and raises its own ``HTTPException(400, "Invalid scope: ...")``, but the
only handler on that ``try`` was a bare ``except Exception`` which re-raised as
``HTTPException(500, "Failed to list documents")``. The handler's own refusal
was therefore reported as a server fault, and because the 500 branch
substitutes a static detail the caller lost the message naming the allowed
values — a caller who mistyped a query parameter was told the server broke.

Reachability is unconditional: the branch is a pure parameter check reached
before any service call, so any request carrying ``scope`` outside
{global, team, personal} took it.

Requests are driven through the real router over ``httpx.ASGITransport`` on a
single event loop rather than ``fastapi.testclient.TestClient``, matching the
convention used by the other API-layer regression tests in this suite.
"""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from faultmaven.api.v1.auth_dependencies import get_current_user_optional
from faultmaven.modules.knowledge.api.routes import (
    get_knowledge_service,
)
from faultmaven.modules.knowledge.api.routes import (
    router as knowledge_router,
)

LISTED = {"documents": [{"document_id": "doc-1"}], "total_count": 1}


def _build_app():
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api/v1")

    async def _knowledge_service():
        async def list_documents(**kwargs):
            return LISTED

        return SimpleNamespace(list_documents=list_documents)

    async def _current_user_optional():
        # Anonymous: _resolve_team_ids short-circuits to [] without needing a
        # team_service on app.state, keeping this test to the one behaviour.
        return None

    app.dependency_overrides[get_knowledge_service] = _knowledge_service
    app.dependency_overrides[get_current_user_optional] = _current_user_optional
    return app


async def _list_documents(params):
    transport = httpx.ASGITransport(app=_build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/api/v1/knowledge/documents", params=params)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_scope_surfaces_as_400_not_500():
    """The handler's own 400 must reach the client verbatim."""
    response = await _list_documents({"scope": "bogus"})

    # Exact status, not merely "not 500": a request that never reaches the
    # handler fails here rather than passing vacuously.
    assert response.status_code == 400, response.text

    detail = response.json()["detail"]
    assert "Invalid scope: bogus" in detail
    # The 500 branch replaced the detail with a static string.
    assert detail != "Failed to list documents"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["global", "team", "personal"])
async def test_valid_scopes_still_reach_the_handler(scope):
    """Vacuity control: the same app/path reaches the handler and succeeds.

    Sweeping all three accepted values also pins that the guard rejects only
    what is outside the allowed set, rather than an instance of it.
    """
    response = await _list_documents({"scope": scope})

    assert response.status_code == 200, response.text
    assert response.json() == LISTED
