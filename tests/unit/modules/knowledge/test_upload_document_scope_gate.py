"""Uploading a runbook file is an input method; the gate belongs on the SCOPE.

#1377: `POST /knowledge/documents` carried `require_platform_admin` on the route
AND hard-coded `scope="global"`, so "operator-only" had become a property of
*uploading* rather than of the platform tier it was meant to guard. The effect
was that the same finished `.md` an operator could upload, no one else could put
anywhere — while Convert and Write Runbook already let any user author at their
own scope.

The three authoring paths now agree: any authenticated user authors, the author
chooses the scope, and `global` — the org-free platform tier that every tenant
reads — is what requires the operator.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.modules.knowledge.api.routes import get_knowledge_service
from faultmaven.modules.knowledge.api.routes import router as knowledge_router

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

RUNBOOK = b"---\nid: x\ndomain: database\nservice: redis\n---\n\n# A runbook\n"


@pytest.fixture
def service():
    return SimpleNamespace(
        upload_document=AsyncMock(
            return_value={"document_id": "kb-1", "status": "uploaded"}
        )
    )


def _client(service, *, is_admin: bool):
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api/v1")

    async def _service():
        return service

    async def _user():
        return SimpleNamespace(
            user_id="user-1",
            organization_id="org-1",
            enterprise_id="ent-1",
            is_platform_admin=lambda: is_admin,
        )

    app.dependency_overrides[get_knowledge_service] = _service
    app.dependency_overrides[require_authentication] = _user
    return TestClient(app)


def _post(client, **form):
    return client.post(
        "/api/v1/knowledge/documents",
        data={"title": "Redis OOM", "document_type": "runbook", **form},
        files={"file": ("redis-oom.md", RUNBOOK, "text/markdown")},
    )


def test_a_regular_user_can_upload_at_personal_scope(service):
    """The gap this closes: a finished runbook had nowhere to go."""
    response = _post(_client(service, is_admin=False), scope="personal")

    assert response.status_code == 201, response.text
    kwargs = service.upload_document.await_args.kwargs
    assert kwargs["scope"] == "personal"


def test_the_uploader_is_recorded_as_the_owner(service):
    """`owner_id` is not bookkeeping — it is personal-scope VISIBILITY.

    It decides the on-disk path (`user_<id>/`) and, through
    `build_kb_scope_filter`, whether the author can see their own item: the
    filter admits a non-global row by `{"owner_id": owner_id}`. The route never
    passed it while it only wrote global, so without this a personal upload
    would succeed and then be invisible to the person who made it.
    """
    _post(_client(service, is_admin=False), scope="personal")

    assert service.upload_document.await_args.kwargs["owner_id"] == "user-1"


def test_team_scope_carries_the_team(service):
    _post(_client(service, is_admin=False), scope="team", team_id="team-9")

    kwargs = service.upload_document.await_args.kwargs
    assert kwargs["scope"] == "team"
    assert kwargs["team_id"] == "team-9"


def test_team_scope_without_a_team_is_refused(service):
    """Same 400 Write Runbook answers — a team item with no team is unshareable."""
    response = _post(_client(service, is_admin=False), scope="team")

    assert response.status_code == 400
    service.upload_document.assert_not_awaited()


def test_a_regular_user_cannot_upload_at_global_scope(service):
    """The tier the gate actually guards: every tenant reads global."""
    response = _post(_client(service, is_admin=False), scope="global")

    assert response.status_code == 403
    service.upload_document.assert_not_awaited()


def test_an_operator_can_still_upload_at_global_scope(service):
    """The behaviour that already worked must keep working.

    This is the whole of what the route did before, and it is unchanged.
    """
    response = _post(_client(service, is_admin=True), scope="global")

    assert response.status_code == 201, response.text
    assert service.upload_document.await_args.kwargs["scope"] == "global"


def test_the_default_scope_is_not_global(service):
    """Omitting the scope must not publish platform-wide.

    The #1166 rule for every KB write path: the tier every tenant reads is
    never what you get by saying nothing.
    """
    response = _post(_client(service, is_admin=False))

    assert response.status_code == 201, response.text
    assert service.upload_document.await_args.kwargs["scope"] != "global"
