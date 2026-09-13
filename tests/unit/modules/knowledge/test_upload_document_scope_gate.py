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


def _client(service, *, is_admin: bool, team_service=None):
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api/v1")
    # The route reads membership from the same `app.state.team_service` signal
    # the teams routes read; `None` is standalone (teams unavailable).
    app.state.team_service = team_service

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
    _post(
        _client(service, is_admin=False, team_service=_team_service(member=True)),
        scope="team",
        team_id="team-9",
    )

    kwargs = service.upload_document.await_args.kwargs
    assert kwargs["scope"] == "team"
    assert kwargs["team_id"] == "team-9"


def test_publishing_into_a_team_you_do_not_belong_to_is_refused(service):
    """#854 — a `team_id` is content injected into that team's knowledge scope.

    The caller names it, so it must name a team the caller belongs to. The two
    sibling authoring paths enforce this through
    `ConversionService._ensure_team_publish_allowed`; without the same check
    here, ANY authenticated user could publish a runbook into ANY team in their
    enterprise, which is then retrieved into that team's investigations.
    Verified before the fix: this returned 201 with the foreign team_id
    forwarded to the service.
    """
    response = _post(
        _client(service, is_admin=False, team_service=_team_service(member=False)),
        scope="team",
        team_id="victim-team",
    )

    assert response.status_code == 403
    service.upload_document.assert_not_awaited()


def test_team_scope_is_refused_when_teams_are_unavailable(service):
    """Fail closed: no team service wired means no team publishing."""
    response = _post(
        _client(service, is_admin=False, team_service=None),
        scope="team",
        team_id="team-9",
    )

    assert response.status_code == 403
    service.upload_document.assert_not_awaited()


@pytest.mark.parametrize(
    "spelling", ["Global", "GLOBAL", " global", "bogus", "undefined"]
)
def test_a_scope_outside_the_closed_set_is_refused(service, spelling):
    """The gate is an exact `== "global"` match, so a near-miss must not pass it.

    It is not merely that the operator check is skipped. `KnowledgeService`
    routes an unrecognised scope to its `else` branch, which writes the file
    into `data/knowledge/global/` — so before this was a closed set, a
    non-operator sending `scope="Global"` planted a file in the global runbook
    tree and then 500'd, leaving an orphan `.md` that a later
    `POST /knowledge/scan` would mint a global draft from.
    """
    response = _post(_client(service, is_admin=False), scope=spelling)

    assert response.status_code == 422
    service.upload_document.assert_not_awaited()


def test_an_empty_scope_falls_back_to_personal(service):
    """An empty form value takes the declared default, and that default is safe.

    Worth pinning separately from the rejections above: it is accepted rather
    than refused, so the property that matters is where it LANDS — never the
    platform tier.
    """
    response = _post(_client(service, is_admin=False), scope="")

    assert response.status_code == 201, response.text
    assert service.upload_document.await_args.kwargs["scope"] == "personal"


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


def _team_service(*, member: bool):
    """A team service whose membership answer is fixed.

    `is_team_member` resolves the roster through this object; what matters to
    the route is only the yes/no, so the double answers that directly.
    """
    return SimpleNamespace(
        list_all_user_team_ids=AsyncMock(
            return_value=["team-9"] if member else ["some-other-team"]
        )
    )


# ---------------------------------------------------------------------------
# The tenant the row is stamped with
# ---------------------------------------------------------------------------


def test_upload_stamps_the_session_enterprise_not_a_sentinel():
    """#1143, made reachable by #1377.

    `KnowledgeService.upload_document` stamped
    `SingleTenantProvider.DEFAULT_ENTERPRISE_ID` on `uploaded_files`,
    `conversion_jobs`, `conversion_drafts`, `knowledge_items` and the
    `resource_shares` row. That was harmless while the route refused every
    upload under multi with the platform-tier gate — the path was unreachable
    there. Opening personal and team scope makes it reachable, and
    `writable_enterprise_id`'s docstring names what follows: under multi the
    sentinel "is not the caller's tenant", so PostgreSQL rejects the INSERT with
    `new row violates row-level security policy` — and on SQLite, which has no
    RLS, the row lands in a FOREIGN TENANT silently.

    Asserted at the source of the value rather than through the route, because
    the route tests mock the service and so cannot see the stamp at all.
    """
    import inspect

    from faultmaven.config.tenant_context import (
        get_current_enterprise_id,
        set_current_enterprise_id,
    )
    from faultmaven.modules.knowledge.domain.services import knowledge_service

    source = inspect.getsource(knowledge_service.KnowledgeService.upload_document)
    assert (
        "writable_enterprise_id" in source
    ), "upload_document no longer resolves the enterprise from the session"
    assert (
        "DEFAULT_ENTERPRISE_ID" not in source
    ), "upload_document stamps a hardcoded single-tenant sentinel again"

    # And the helper it now uses really does follow the bound session, which is
    # the property the assertion above is only a proxy for.
    from faultmaven.config.tenant_context import writable_enterprise_id

    token_before = get_current_enterprise_id()
    try:
        set_current_enterprise_id("ent-tenant-b")
        assert writable_enterprise_id(None) == "ent-tenant-b"
    finally:
        set_current_enterprise_id(token_before)
