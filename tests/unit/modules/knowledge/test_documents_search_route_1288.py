"""``POST /documents/search`` through the ROUTE, not the service (#1288).

The first cut of #1288's guards drove ``KnowledgeService`` directly. Two defects
survived that: the route resolves no ``team_ids``, so the "shared to my teams"
arm of the visibility rule the endpoint documents was dead; and the route is
optional-auth, so the newly-populated ``content`` field handed runbook body text
to unauthenticated callers. Neither is visible from below the route — a service
test supplies ``team_ids`` and a ``user`` itself, which is precisely the wiring
that was missing.

So these exercise the HTTP surface with a real relational read underneath.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
    KnowledgeScope,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
    DatabaseKnowledgeItemRepository,
)

ORG = "org-1"
#: The enterprise every row here is isolated to — the one the fixture seeds.
#: It is not optional for any tier: GLOBAL carries no ORGANIZATION (#770), but
#: it still carries an enterprise, and the column is NOT NULL with an FK.
ENTERPRISE = "ent-1"
USER_ID = "user-1"
TEAM_ID = "team-9"

GLOBAL_ID = "kb_aaaaaaaaaaaa"
TEAM_ID_DOC = "kb_bbbbbbbbbbbb"

# A distinctive string that appears ONLY in a body, never in a title. If it
# reaches a caller, it did so as document content.
BODY_SECRET = "ssh bastion.prod.internal as svc-oncall"

GLOBAL_BODY = f"# Runbook\nStep 1: {BODY_SECRET}\nStep 2: ENOSPC when /var fills.\n"
TEAM_BODY = "# Team runbook\nThe ENOSPC alert is routed to the platform team.\n"


def _item(item_id, title, content, scope, owner_id=None, organization=None):
    return KnowledgeItem(
        item_id=item_id,
        enterprise_id=ENTERPRISE,
        organization_id=organization,
        title=title,
        content=content,
        item_type=KnowledgeItemType.RUNBOOK,
        scope=scope,
        owner_id=owner_id,
        is_published=True,
        category="storage",
        tags=[],
    )


@pytest.fixture
async def wired():
    """A real service over FK-on SQLite, mounted behind the real router."""
    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from faultmaven.infrastructure.persistence.models import Base
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "INSERT INTO enterprises (enterprise_id, name, slug) "
                "VALUES ('ent-1', 'Default', 'default')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO organizations "
                "(organization_id, enterprise_id, name, slug) "
                f"VALUES ('{ORG}', 'ent-1', 'Org', 'org')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO users "
                "(user_id, enterprise_id, username, email, display_name) "
                f"VALUES ('{USER_ID}', 'ent-1', 'u', 'u@e', 'u')"
            )
        )

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    service = KnowledgeService.__new__(KnowledgeService)
    service._db_session_factory = factory
    service._vector_store = MagicMock()
    service._vector_store.delete_documents_by_parent_id = AsyncMock(return_value=0)
    service._tracer = MagicMock()
    service._share_repo = None

    async with factory() as session:
        repo = DatabaseKnowledgeItemRepository(session)
        await repo.create(
            _item(
                GLOBAL_ID, "Storage volume runbook", GLOBAL_BODY, KnowledgeScope.GLOBAL
            )
        )
        await repo.create(
            _item(
                TEAM_ID_DOC,
                "Team escalation runbook",
                TEAM_BODY,
                KnowledgeScope.TEAM,
                owner_id=None,
                organization=ORG,
            )
        )
        # The share row is the single source of truth for team visibility.
        await session.execute(
            text(
                "INSERT INTO resource_shares "
                "(share_id, resource_type, resource_id, scope_type, scope_id, "
                " enterprise_id, organization_id) "
                f"VALUES ('s1', 'knowledge_item', '{TEAM_ID_DOC}', 'team', "
                f"'{TEAM_ID}', '{ENTERPRISE}', '{ORG}')"
            )
        )
        await session.commit()

    from faultmaven.api.v1.auth_dependencies import get_current_user_optional
    from faultmaven.modules.knowledge.api.routes import get_knowledge_service, router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_knowledge_service] = lambda: service

    # The route reads team memberships off app.state, as GET /documents does.
    app.state.team_service = SimpleNamespace(
        list_all_user_team_ids=AsyncMock(return_value=[TEAM_ID])
    )

    def _as(user):
        app.dependency_overrides[get_current_user_optional] = lambda: user

    _as(None)
    client = TestClient(app, raise_server_exceptions=False)

    yield client, _as
    await engine.dispose()


#: The route reads the caller's ENTERPRISE to scope the share allowlist
#: (ADR-017 D1), so a principal carrying only a billing organization resolves no
#: team memberships and the "shared to my teams" arm goes dead again — the exact
#: defect this module was written for.
AUTHED = SimpleNamespace(user_id=USER_ID, enterprise_id=ENTERPRISE, organization_id=ORG)


def _search(client, **body):
    body.setdefault("limit", 50)
    return client.post("/knowledge/documents/search", json=body).json()


@pytest.mark.asyncio
class TestAnonymousCallersGetNoDocumentBodies:
    """``content`` is body text, so it goes only to callers who may read one.

    ``GET /documents/{id}`` — the canonical body read — requires authentication.
    A search that hands an anonymous caller an excerpt routes around that gate.
    Before the fix, ``POST {"query": "a"}`` returned a body excerpt of every
    global runbook to an unauthenticated caller.
    """

    async def test_anonymous_gets_titles_but_never_body_text(self, wired):
        client, _as = wired
        _as(None)

        body = _search(client, query="ENOSPC")

        assert body["total_results"] >= 1, (
            "anonymous title/keyword search must still work — the fix suppresses "
            "the body, it does not close the endpoint"
        )
        for hit in body["results"]:
            assert hit["content"] == "", hit
            assert BODY_SECRET not in str(hit)

    async def test_an_authenticated_caller_does_get_the_excerpt(self, wired):
        """The positive control: without it, an endpoint returning "" for every
        caller would pass the test above while being useless."""
        client, _as = wired
        _as(AUTHED)

        body = _search(client, query="ENOSPC")
        excerpts = [h["content"] for h in body["results"]]

        assert any(e for e in excerpts), "authenticated callers get body excerpts"

    async def test_a_short_query_no_longer_harvests_the_corpus(self, wired):
        """``{"query": "a"}`` matched every document by substring.

        It scored 1.0 on any title containing the letter ``a`` anywhere, so one
        request returned an excerpt of the whole visible knowledge base.
        """
        client, _as = wired
        _as(None)

        body = _search(client, query="a", limit=100)

        assert body["total_results"] == 0, (
            "a one-letter query matched documents by substring: "
            f"{[h['metadata']['title'] for h in body['results']]}"
        )


@pytest.mark.asyncio
class TestTeamSharedRunbooksAreFindable:
    """The endpoint documents "global ∪ own-org owned ∪ shared-to-my-teams".

    The route resolved no ``team_ids``, so the third arm was dead: a team-shared
    runbook was listed by ``GET /documents`` and unfindable here.
    """

    async def test_a_team_shared_runbook_is_returned(self, wired):
        client, _as = wired
        _as(AUTHED)

        body = _search(client, query="escalation")
        found = {h["document_id"] for h in body["results"]}

        assert TEAM_ID_DOC in found, (
            "a runbook shared with the caller's team was not findable; the "
            f"route resolved no team memberships. got {found}"
        )

    async def test_the_team_arm_is_the_reason_and_not_a_global_fallback(self, wired):
        """Guard the guard: the document above must be team-scoped, not global.

        If the fixture's row were global the assertion would pass with the team
        arm still dead, which is how this defect survived the first round.
        """
        client, _as = wired
        _as(None)

        body = _search(client, query="escalation")
        found = {h["document_id"] for h in body["results"]}

        assert TEAM_ID_DOC not in found, (
            "the team-shared runbook is visible anonymously, so the test above "
            "proves nothing about the team arm"
        )
