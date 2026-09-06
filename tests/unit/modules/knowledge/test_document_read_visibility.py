"""#867: read visibility on the id-addressed KB document endpoints.

``GET /knowledge/documents/{id}`` and ``.../snippet`` had no auth dependency
and no scope predicate, so any unauthenticated caller could read the full
content of every document — including other users' personal runbooks. They now
require authentication and resolve the target through
``KnowledgeService.get_document_visible`` → ``get_visible_by_id``, the same
read-visibility rule the inventory listing uses (global ∪ own ∪
shared-to-my-teams).

Also pins the 403-vs-404 oracle fix on the write routes: a refusal over a
document the actor cannot even see must answer 404 with the identical detail
string an absent id gets, or the refusal itself confirms the document exists.

Repository coverage runs against BOTH implementations — the SQL predicate over
FK-on aiosqlite (the production constraint regime, mirroring
``test_documents_inventory.py``) and the in-memory fallback.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.knowledge.domain import global_authoring
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
    KnowledgeScope,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
    DatabaseKnowledgeItemRepository,
    InMemoryKnowledgeItemRepository,
)
from faultmaven.providers.tenancy.factory import BUILTIN_SINGLE

GLOBAL_ID = "kb_0123456789ab"


def _mk_item(
    item_id,
    *,
    scope=KnowledgeScope.GLOBAL,
    owner_id=None,
    org="org-1",
    is_published=True,
):
    return KnowledgeItem(
        item_id=item_id,
        # Global rows are the org-free platform tier (#770).
        enterprise_id=None if scope == KnowledgeScope.GLOBAL else org,
        title="Runbook",
        content="# body\nline two\nline three",
        item_type=KnowledgeItemType.RUNBOOK,
        scope=scope,
        owner_id=owner_id,
        is_published=is_published,
    )


# ===========================================================================
# Repository: get_visible_by_id — SQL implementation
# ===========================================================================


@pytest.fixture
async def db_factory():
    """FK-on aiosqlite session factory with two orgs and two users seeded."""
    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from faultmaven.infrastructure.persistence.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # replicates the #378 connect listener
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "INSERT INTO enterprises (enterprise_id, name, slug) "
                "VALUES ('ent-1', 'Default', 'default')"
            )
        )
        for org in ("org-1", "org-2"):
            await conn.execute(
                text(
                    "INSERT INTO organizations "
                    "(organization_id, enterprise_id, name, slug) "
                    f"VALUES ('{org}', 'ent-1', '{org}', '{org}')"
                )
            )
        for uid in ("user-1", "user-2"):
            await conn.execute(
                text(
                    "INSERT INTO users "
                    "(user_id, enterprise_id, username, email, display_name) "
                    f"VALUES ('{uid}', 'ent-1', '{uid}', '{uid}@e', '{uid}')"
                )
            )

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed(factory, item):
    async with factory() as session:
        await DatabaseKnowledgeItemRepository(session).create(item)


async def _visible(factory, item_id, org, user_id=None, team_ids=None):
    async with factory() as session:
        return await DatabaseKnowledgeItemRepository(session).get_visible_by_id(
            item_id,
            organization_id=org,
            user_id=user_id,
            team_ids=team_ids,
        )


@pytest.mark.unit
@pytest.mark.knowledge_base
@pytest.mark.asyncio
class TestGetVisibleByIdSQL:
    async def test_global_visible_to_everyone(self, db_factory):
        # The platform tier is org-free (#770): an org member, a caller from a
        # different org, and an actorless load all reach it.
        await _seed(db_factory, _mk_item(GLOBAL_ID))
        for org, user_id in (("org-1", "user-1"), ("org-2", "user-2"), ("org-1", None)):
            got = await _visible(db_factory, GLOBAL_ID, org, user_id=user_id)
            assert got is not None and got.item_id == GLOBAL_ID

    @pytest.mark.parametrize("is_published", [True, False])
    async def test_owner_reaches_own_personal_regardless_of_publication(
        self, db_factory, is_published
    ):
        # Publication is a listing-surface concern, never an access-control
        # one: an id-addressed read must reach the owner's own row either way.
        await _seed(
            db_factory,
            _mk_item(
                "p-1",
                scope=KnowledgeScope.PERSONAL,
                owner_id="user-1",
                is_published=is_published,
            ),
        )
        got = await _visible(db_factory, "p-1", "org-1", user_id="user-1")
        assert got is not None and got.item_id == "p-1"

    async def test_unpublished_global_is_unreachable_by_a_non_owner(self, db_factory):
        # DELETE of a built-in global runbook is implemented as *unpublish*
        # (KnowledgeService.delete_document), so an unpublished global row is a
        # deleted one. Global ids are handed to anonymous callers by the list
        # route, so leaving it id-readable would serve deleted content to any
        # authenticated caller.
        await _seed(db_factory, _mk_item(GLOBAL_ID, is_published=False))
        for org, user_id in (("org-1", "user-1"), ("org-2", "user-2"), ("org-1", None)):
            assert await _visible(db_factory, GLOBAL_ID, org, user_id=user_id) is None

    async def test_unpublished_team_share_is_unreachable_by_a_member(self, db_factory):
        # Same rule on the share arm: only the owner is exempt.
        from faultmaven.infrastructure.persistence.share_repository import (
            PostgreSQLShareRepository,
        )

        await _seed(
            db_factory,
            _mk_item(
                "t-3",
                scope=KnowledgeScope.TEAM,
                owner_id="user-1",
                is_published=False,
            ),
        )
        async with db_factory() as session:
            await PostgreSQLShareRepository(session).share(
                resource_type="knowledge_item",
                resource_id="t-3",
                scope_type="team",
                scope_id="team-A",
                organization_id="org-1",
                created_by="user-1",
            )

        assert (
            await _visible(
                db_factory, "t-3", "org-1", user_id="user-2", team_ids=["team-A"]
            )
            is None
        )
        # The author still reaches their own unpublished row.
        owner = await _visible(
            db_factory, "t-3", "org-1", user_id="user-1", team_ids=["team-A"]
        )
        assert owner is not None and owner.item_id == "t-3"

    async def test_non_owner_cannot_reach_personal(self, db_factory):
        await _seed(
            db_factory,
            _mk_item("p-1", scope=KnowledgeScope.PERSONAL, owner_id="user-1"),
        )
        assert await _visible(db_factory, "p-1", "org-1", user_id="user-2") is None
        assert await _visible(db_factory, "p-1", "org-1") is None

    async def test_team_share_grants_visibility(self, db_factory):
        from faultmaven.infrastructure.persistence.share_repository import (
            PostgreSQLShareRepository,
        )

        await _seed(
            db_factory,
            _mk_item("t-1", scope=KnowledgeScope.TEAM, owner_id="user-1"),
        )
        async with db_factory() as session:
            await PostgreSQLShareRepository(session).share(
                resource_type="knowledge_item",
                resource_id="t-1",
                scope_type="team",
                scope_id="team-A",
                organization_id="org-1",
                created_by="user-1",
            )

        member = await _visible(
            db_factory, "t-1", "org-1", user_id="user-2", team_ids=["team-A"]
        )
        nonmember = await _visible(
            db_factory, "t-1", "org-1", user_id="user-2", team_ids=["team-B"]
        )
        assert member is not None and member.item_id == "t-1"
        assert nonmember is None

    async def test_cross_org_team_share_is_not_visible(self, db_factory):
        # The org predicate guards the org-owned arms: holding the same team id
        # in a different org must not reach another org's row (defense in depth
        # on top of RLS).
        from faultmaven.infrastructure.persistence.share_repository import (
            PostgreSQLShareRepository,
        )

        await _seed(
            db_factory,
            _mk_item("t-2", scope=KnowledgeScope.TEAM, owner_id="user-1", org="org-2"),
        )
        async with db_factory() as session:
            await PostgreSQLShareRepository(session).share(
                resource_type="knowledge_item",
                resource_id="t-2",
                scope_type="team",
                scope_id="team-A",
                organization_id="org-2",
                created_by="user-1",
            )

        assert (
            await _visible(
                db_factory, "t-2", "org-1", user_id="user-2", team_ids=["team-A"]
            )
            is None
        )

    async def test_share_row_stamped_with_a_foreign_org_does_not_grant(
        self, db_factory
    ):
        # The share sub-select is the one arm that reached across orgs: it
        # matched on (resource_type, scope_type, scope_id) only, so a row
        # stamped with another org's id granted visibility. Defense in depth
        # on top of RLS — the docstring claims it, so it must hold.
        from faultmaven.infrastructure.persistence.share_repository import (
            PostgreSQLShareRepository,
        )

        await _seed(
            db_factory,
            _mk_item("t-4", scope=KnowledgeScope.TEAM, owner_id="user-1", org="org-1"),
        )
        async with db_factory() as session:
            await PostgreSQLShareRepository(session).share(
                resource_type="knowledge_item",
                resource_id="t-4",
                scope_type="team",
                scope_id="team-A",
                organization_id="org-2",  # not the resource's org
                created_by="user-1",
            )

        assert (
            await _visible(
                db_factory, "t-4", "org-1", user_id="user-2", team_ids=["team-A"]
            )
            is None
        )
        # Same clause, same rule on the listing surface.
        async with db_factory() as session:
            listed = await DatabaseKnowledgeItemRepository(session).list_for_inventory(
                organization_id="org-1", user_id="user-2", team_ids=["team-A"]
            )
        assert [i.item_id for i in listed] == []

    async def test_absent_id_returns_none(self, db_factory):
        assert await _visible(db_factory, "nope", "org-1", user_id="user-1") is None


# ===========================================================================
# Repository: get_visible_by_id — in-memory implementation (interface parity)
# ===========================================================================


@pytest.mark.unit
@pytest.mark.knowledge_base
@pytest.mark.asyncio
class TestGetVisibleByIdInMemory:
    async def _repo_with(self, items):
        repo = InMemoryKnowledgeItemRepository()
        for it in items:
            await repo.create(it)
        return repo

    async def test_global_visible_to_everyone(self):
        repo = await self._repo_with([_mk_item(GLOBAL_ID)])
        for org, user_id in (("org-1", "user-1"), ("org-2", "user-2"), ("org-1", None)):
            got = await repo.get_visible_by_id(
                GLOBAL_ID, organization_id=org, user_id=user_id
            )
            assert got is not None and got.item_id == GLOBAL_ID

    @pytest.mark.parametrize("is_published", [True, False])
    async def test_owner_reaches_own_personal_regardless_of_publication(
        self, is_published
    ):
        repo = await self._repo_with(
            [
                _mk_item(
                    "p-1",
                    scope=KnowledgeScope.PERSONAL,
                    owner_id="user-1",
                    is_published=is_published,
                )
            ]
        )
        got = await repo.get_visible_by_id(
            "p-1", organization_id="org-1", user_id="user-1"
        )
        assert got is not None

    async def test_unpublished_global_is_unreachable_by_a_non_owner(self):
        # Mirrors the SQL rule: unpublish is the delete semantics for built-in
        # global rows, so a non-owner must not reach an unpublished row.
        repo = await self._repo_with([_mk_item(GLOBAL_ID, is_published=False)])
        for org, user_id in (("org-1", "user-1"), ("org-2", "user-2"), ("org-1", None)):
            assert (
                await repo.get_visible_by_id(
                    GLOBAL_ID, organization_id=org, user_id=user_id
                )
                is None
            )

    async def test_non_owner_and_cross_org_and_absent_return_none(self):
        repo = await self._repo_with(
            [_mk_item("p-1", scope=KnowledgeScope.PERSONAL, owner_id="user-1")]
        )
        assert (
            await repo.get_visible_by_id(
                "p-1", organization_id="org-1", user_id="user-2"
            )
            is None
        )
        assert (
            await repo.get_visible_by_id(
                "p-1", organization_id="org-2", user_id="user-1"
            )
            is None
        )
        assert (
            await repo.get_visible_by_id(
                "nope", organization_id="org-1", user_id="user-1"
            )
            is None
        )
        # team_ids is accepted for interface parity but the fallback does not
        # model the share table — a team-shared item stays invisible here.
        assert (
            await repo.get_visible_by_id(
                "p-1",
                organization_id="org-1",
                user_id="user-2",
                team_ids=["team-A"],
            )
            is None
        )


# ===========================================================================
# Service: get_document_visible over the SQL repository
# ===========================================================================


def _service_over(factory):
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    svc = KnowledgeService.__new__(KnowledgeService)
    svc._db_session_factory = factory
    svc._vector_store = MagicMock()
    svc._tracer = MagicMock()
    svc._share_repo = None
    return svc


@pytest.mark.unit
@pytest.mark.knowledge_base
@pytest.mark.asyncio
class TestGetDocumentVisibleService:
    async def test_owner_gets_content_and_non_owner_gets_none(self, db_factory):
        svc = _service_over(db_factory)
        await _seed(
            db_factory,
            _mk_item("p-1", scope=KnowledgeScope.PERSONAL, owner_id="user-1"),
        )

        owner = SimpleNamespace(user_id="user-1", organization_id="org-1")
        other = SimpleNamespace(user_id="user-2", organization_id="org-1")

        doc = await svc.get_document_visible("p-1", user=owner, team_ids=[])
        assert doc is not None
        assert doc["document_id"] == "p-1"
        assert doc["content"] == "# body\nline two\nline three"
        assert await svc.get_document_visible("p-1", user=other, team_ids=[]) is None

    async def test_unscoped_get_document_still_reaches_the_row(self, db_factory):
        # The trusted load must stay unscoped: the write routes evaluate the
        # policy against it, and the single-tenant operator override has to
        # work on documents the operator cannot list.
        svc = _service_over(db_factory)
        await _seed(
            db_factory,
            _mk_item("p-1", scope=KnowledgeScope.PERSONAL, owner_id="user-1"),
        )
        assert await svc.get_document("p-1") is not None


# ===========================================================================
# Route wiring: GET /documents/{id} and /snippet
# ===========================================================================


def _user(*, user_id="u1", roles=("user",)) -> DevUser:
    return DevUser(
        user_id=user_id,
        username=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        created_at=datetime.now(timezone.utc),
        roles=list(roles),
    )


def _doc(*, scope="personal", owner_id="u1"):
    return {
        "document_id": "doc1",
        "title": "T",
        "content": "alpha\nbeta\ngamma",
        "document_type": "runbook",
        "tags": [],
        "scope": scope,
        "owner_id": owner_id,
        "source_url": None,
        "created_at": "",
        "updated_at": "",
        "metadata": {},
    }


def _app(knowledge_service, user, team_service=None):
    """Knowledge router mounted on a bare app.

    ``user=None`` leaves ``require_authentication`` unmocked and stubs the
    optional-auth dependency it wraps, so the real 401 path runs.
    ``team_service`` is placed on ``app.state`` exactly as the composition root
    does, so ``_resolve_team_ids`` runs for real when one is supplied.
    """
    from fastapi import FastAPI

    from faultmaven.api.exception_handlers import get_exception_handlers
    from faultmaven.api.v1.auth_dependencies import (
        get_current_user_optional,
        require_authentication,
    )
    from faultmaven.modules.knowledge.api.routes import get_knowledge_service, router

    app = FastAPI()
    app.include_router(router)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.dependency_overrides[get_knowledge_service] = lambda: knowledge_service
    if team_service is not None:
        app.state.team_service = team_service
    if user is None:
        app.dependency_overrides[get_current_user_optional] = lambda: None
    else:
        app.dependency_overrides[require_authentication] = lambda: user
    return app


def _client(knowledge_service, user, team_service=None):
    """Sync test client for the knowledge router."""
    from fastapi.testclient import TestClient

    return TestClient(
        _app(knowledge_service, user, team_service), raise_server_exceptions=False
    )


def _read_service(visible_doc):
    service = MagicMock()
    service.get_document_visible = AsyncMock(return_value=visible_doc)
    service.get_document = AsyncMock(return_value=visible_doc)
    return service


@pytest.mark.unit
@pytest.mark.knowledge_base
class TestDocumentReadRoutes:
    @pytest.mark.parametrize(
        "path",
        ["/knowledge/documents/doc1", "/knowledge/documents/doc1/snippet"],
    )
    def test_unauthenticated_is_rejected(self, path):
        service = _read_service(_doc())
        resp = _client(service, None).get(path)
        assert resp.status_code == 401
        service.get_document_visible.assert_not_awaited()

    @pytest.mark.parametrize(
        "path",
        ["/knowledge/documents/doc1", "/knowledge/documents/doc1/snippet"],
    )
    def test_invisible_document_is_404(self, path):
        # Someone else's personal runbook: the scoped read returns None, so the
        # route answers exactly as for an absent id.
        service = _read_service(None)
        resp = _client(service, _user()).get(path)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Document not found"

    @pytest.mark.parametrize("scope", ["personal", "team", "global"])
    def test_visible_document_is_returned_with_content(self, scope):
        service = _read_service(_doc(scope=scope))
        resp = _client(service, _user()).get("/knowledge/documents/doc1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["document_id"] == "doc1"
        assert body["content"] == "alpha\nbeta\ngamma"
        # The scoped read is the one that runs — never the unscoped load.
        service.get_document_visible.assert_awaited_once()
        service.get_document.assert_not_awaited()

    @pytest.mark.parametrize(
        "path",
        ["/knowledge/documents/doc1", "/knowledge/documents/doc1/snippet"],
    )
    def test_the_caller_and_their_team_memberships_reach_the_scoped_read(self, path):
        # The team arm is only as good as its wiring: pin the exact principal
        # and the exact team ids, so dropping or swapping either fails here
        # rather than silently widening (or emptying) the visibility rule.
        service = _read_service(_doc())
        team_service = MagicMock()
        team_service.list_all_user_team_ids = AsyncMock(
            return_value=["team-A", "team-B"]
        )
        user = _user(user_id="u7")

        resp = _client(service, user, team_service).get(path)

        assert resp.status_code == 200
        team_service.list_all_user_team_ids.assert_awaited_once_with("u7")
        service.get_document_visible.assert_awaited_once_with(
            "doc1", user=user, team_ids=["team-A", "team-B"]
        )

    def test_snippet_returns_lines_for_a_visible_document(self):
        service = _read_service(_doc())
        resp = _client(service, _user()).get(
            "/knowledge/documents/doc1/snippet?line_start=1&max_lines=2"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["snippet"] == "alpha\nbeta"
        assert body["total_lines"] == 3
        service.get_document_visible.assert_awaited_once()

    @pytest.mark.parametrize(
        "path",
        ["/knowledge/documents/doc1", "/knowledge/documents/doc1/snippet"],
    )
    def test_500_does_not_echo_exception_text(self, path):
        service = MagicMock()
        service.get_document_visible = AsyncMock(
            side_effect=RuntimeError("postgres://user:secret@host/db exploded")
        )
        resp = _client(service, _user()).get(path)
        assert resp.status_code == 500
        assert "secret" not in resp.text
        assert "exploded" not in resp.text


@pytest.mark.unit
@pytest.mark.knowledge_base
@pytest.mark.asyncio
class TestUnpublishedGlobalIsNotReadableEndToEnd:
    """Route → service → repository over real aiosqlite, no service double.

    ``DELETE /knowledge/documents/{id}`` on a built-in global runbook is an
    *unpublish*; the id-addressed reads must stop serving it.
    """

    async def _get(self, db_factory, path):
        from httpx import ASGITransport, AsyncClient

        app = _app(_service_over(db_factory), _user())
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as client:
            return await client.get(path)

    @pytest.mark.parametrize(
        "path",
        [
            f"/knowledge/documents/{GLOBAL_ID}",
            f"/knowledge/documents/{GLOBAL_ID}/snippet",
        ],
    )
    async def test_unpublished_global_answers_404(self, db_factory, path):
        await _seed(db_factory, _mk_item(GLOBAL_ID, is_published=False))
        resp = await self._get(db_factory, path)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Document not found"
        assert "line two" not in resp.text

    @pytest.mark.parametrize(
        "path",
        [
            f"/knowledge/documents/{GLOBAL_ID}",
            f"/knowledge/documents/{GLOBAL_ID}/snippet",
        ],
    )
    async def test_published_global_is_still_served(self, db_factory, path):
        # The gate is on publication, not on the global arm itself.
        await _seed(db_factory, _mk_item(GLOBAL_ID))
        resp = await self._get(db_factory, path)
        assert resp.status_code == 200


@pytest.mark.unit
@pytest.mark.knowledge_base
class TestAggregateReadRoutesRequireAuth:
    """``/knowledge/stats`` and ``/knowledge/analytics/search`` (#867).

    Same defect class as the id-addressed reads: no ``current_user``, while
    ``rbac.md`` documented them as "Any authenticated user". Aggregate counts
    only, and a cross-repo sweep found no consumer calling them at all — so
    they are closed rather than documented as public.
    """

    @pytest.mark.parametrize(
        "path", ["/knowledge/stats", "/knowledge/analytics/search"]
    )
    def test_unauthenticated_is_rejected(self, path):
        service = MagicMock()
        service.get_knowledge_stats = AsyncMock(return_value={"total_documents": 3})
        service.get_search_analytics = AsyncMock(return_value={"search_volume": 1})

        resp = _client(service, None).get(path)

        assert resp.status_code == 401
        service.get_knowledge_stats.assert_not_awaited()
        service.get_search_analytics.assert_not_awaited()

    @pytest.mark.parametrize(
        "path", ["/knowledge/stats", "/knowledge/analytics/search"]
    )
    def test_authenticated_caller_is_served(self, path):
        service = MagicMock()
        service.get_knowledge_stats = AsyncMock(return_value={"total_documents": 3})
        service.get_search_analytics = AsyncMock(return_value={"search_volume": 1})

        resp = _client(service, _user()).get(path)

        assert resp.status_code == 200


# A ``TestFallbackContainerService`` class used to sit here, pinning that the
# container's stand-in KnowledgeService answered the scoped read fail-CLOSED.
# The stand-in is gone (#899): it fabricated a document for any plausible id on
# the *unscoped* read — the very content invention that made a fail-closed
# scoped read necessary alongside it — and the container now returns None rather
# than substituting anything. The scoped-read rule itself (#867) is pinned above
# against the real service.


# ===========================================================================
# Write-route oracle: a refusal must not confirm the document exists
# ===========================================================================


def _write_service(*, trusted_doc, visible_doc):
    service = MagicMock()
    service.get_document = AsyncMock(return_value=trusted_doc)
    service.get_document_visible = AsyncMock(return_value=visible_doc)
    service.update_document_metadata = AsyncMock(return_value={"document_id": "doc1"})
    service.delete_document = AsyncMock(return_value={"success": True})
    return service


@pytest.mark.unit
@pytest.mark.knowledge_base
class TestWriteRouteExistenceOracle:
    def test_refused_and_invisible_target_is_404_like_an_absent_one(self, monkeypatch):
        monkeypatch.setattr(
            global_authoring, "requested_tenant_provider", lambda: BUILTIN_SINGLE
        )
        # Someone else's personal runbook, not shared with the caller.
        service = _write_service(
            trusted_doc=_doc(scope="personal", owner_id="someone_else"),
            visible_doc=None,
        )
        client = _client(service, _user())
        put = client.put("/knowledge/documents/doc1", json={"title": "X"})
        delete = client.delete("/knowledge/documents/doc1")

        absent = _write_service(trusted_doc=None, visible_doc=None)
        absent_client = _client(absent, _user())
        absent_put = absent_client.put("/knowledge/documents/doc1", json={"title": "X"})

        assert put.status_code == delete.status_code == 404
        # Byte-identical to the absent-id answer: no existence oracle.
        assert put.json() == absent_put.json() == {"detail": "Document not found"}
        service.update_document_metadata.assert_not_awaited()
        service.delete_document.assert_not_awaited()

    def test_refused_but_visible_target_stays_403(self, monkeypatch):
        monkeypatch.setattr(
            global_authoring, "requested_tenant_provider", lambda: BUILTIN_SINGLE
        )
        # A team-shared runbook authored by someone else: the caller can read
        # it (shares grant read, not write), so the refusal is honest.
        shared = _doc(scope="team", owner_id="someone_else")
        service = _write_service(trusted_doc=shared, visible_doc=shared)
        client = _client(service, _user())

        assert (
            client.put("/knowledge/documents/doc1", json={"title": "X"}).status_code
            == 403
        )
        assert client.delete("/knowledge/documents/doc1").status_code == 403
        service.update_document_metadata.assert_not_awaited()
        service.delete_document.assert_not_awaited()

    def test_permitted_write_never_runs_the_visibility_query(self, monkeypatch):
        # The extra query is on the refusal path only.
        monkeypatch.setattr(
            global_authoring, "requested_tenant_provider", lambda: BUILTIN_SINGLE
        )
        own = _doc(scope="personal", owner_id="u1")
        service = _write_service(trusted_doc=own, visible_doc=own)
        client = _client(service, _user(user_id="u1"))

        assert (
            client.put("/knowledge/documents/doc1", json={"title": "X"}).status_code
            == 200
        )
        service.get_document_visible.assert_not_awaited()
