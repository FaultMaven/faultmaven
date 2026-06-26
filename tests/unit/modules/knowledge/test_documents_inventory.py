"""Inventory surface (`/documents`) backed by ``knowledge_items``.

PR-B re-routes the dashboard "Runbooks" inventory from ``conversion_drafts``
(the review queue) to ``knowledge_items`` (the published source of truth).
These tests pin:

- the built-in vs authored provenance discriminator,
- RBAC visibility enforced in ``list_for_inventory`` (InMemory matrix +
  a SQL personal-isolation check under foreign_keys=ON),
- the service surface: ``list_documents`` / ``get_document`` read
  knowledge_items, and ``delete_document`` unpublishes built-ins (keeps row,
  deletes vectors) vs hard-deletes authored items (drops row + vectors).

The FK-on SQLite fixture mirrors ``tests/unit/bootstrap/test_kb_init.py``'s
``fk_on_ingest_service`` so the inventory query is exercised against the same
constraint regime production runs under (#378).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
    KnowledgeScope,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
    DatabaseKnowledgeItemRepository,
    InMemoryKnowledgeItemRepository,
)
from faultmaven.utils.runbook_id import (
    authored_item_id,
    is_builtin_item_id,
    item_id_from_runbook_id,
)

BUILTIN_ID = item_id_from_runbook_id("openssh-auth-failures")  # kb_<12 hex>
AUTHORED_ID = "11111111-2222-3333-4444-555555555555"  # verify_draft uuid shape


def _mk_item(
    item_id,
    *,
    scope=KnowledgeScope.GLOBAL,
    owner_id=None,
    team_id=None,
    org="org-1",
    title="Runbook",
    is_published=True,
    item_type=KnowledgeItemType.RUNBOOK,
    metadata=None,
):
    return KnowledgeItem(
        item_id=item_id,
        organization_id=org,
        title=title,
        content="# body\nsteps",
        item_type=item_type,
        scope=scope,
        owner_id=owner_id,
        team_id=team_id,
        is_published=is_published,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Provenance discriminator
# ---------------------------------------------------------------------------


class TestIsBuiltinItemId:
    def test_deterministic_12_hex_is_builtin(self):
        assert is_builtin_item_id(BUILTIN_ID) is True
        assert is_builtin_item_id("kb_0123456789ab") is True

    def test_manual_create_16_hex_is_not_builtin(self):
        # KnowledgeService._generate_document_id emits kb_<16 hex>; must NOT
        # be misclassified as a bootstrap built-in.
        assert is_builtin_item_id("kb_0123456789abcdef") is False

    def test_uuid_authored_is_not_builtin(self):
        assert is_builtin_item_id(AUTHORED_ID) is False

    def test_empty_or_uppercase_is_not_builtin(self):
        assert is_builtin_item_id("") is False
        assert is_builtin_item_id("kb_0123456789AB") is False  # hex is lowercase

    def test_authored_mint_is_never_builtin(self):
        # Regression (data loss): authored ids must never match the 12-hex
        # built-in pattern, or the bootstrap orphan-prune deletes them on redeploy.
        # The property is structural (16 hex != 12 hex), so assert the shape.
        ident = authored_item_id()
        assert ident.startswith("kb_")
        assert len(ident) == len("kb_") + 16  # 16 hex, never the 12-hex built-in form
        assert is_builtin_item_id(ident) is False
        # a few draws to guard against any randomness-dependent edge
        assert all(not is_builtin_item_id(authored_item_id()) for _ in range(20))

    def test_no_authored_path_mints_12_hex_kb_id(self):
        # Regression: EVERY authored-runbook mint must route through
        # authored_item_id(); a raw `kb_<...hex[:12]>` collides with the built-in
        # prune pattern and gets the runbook deleted on redeploy. Scan the service
        # sources so a new colliding mint site fails CI.
        import pathlib
        import re as _re

        import faultmaven

        svc = pathlib.Path(faultmaven.__file__).parent / (
            "modules/knowledge/domain/services"
        )
        offenders = [
            f"{f.name}:{i}"
            for f in svc.glob("*.py")
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
            if _re.search(r"kb_\{[^}]*hex\[:12\]", line)
        ]
        assert (
            not offenders
        ), f"authored kb_<12-hex> mints collide with prune: {offenders}"


# ---------------------------------------------------------------------------
# RBAC visibility matrix (InMemory repo — pure semantics)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestListForInventoryRBAC:
    async def _repo_with(self, items):
        repo = InMemoryKnowledgeItemRepository()
        for it in items:
            await repo.create(it)
        return repo

    async def test_global_visible_to_everyone(self):
        repo = await self._repo_with([_mk_item(BUILTIN_ID)])
        # anonymous (no user_id), and an arbitrary user — both see global
        anon = await repo.list_for_inventory("org-1")
        someone = await repo.list_for_inventory("org-1", user_id="user-x")
        assert [i.item_id for i in anon] == [BUILTIN_ID]
        assert [i.item_id for i in someone] == [BUILTIN_ID]

    async def test_organization_scope_visible_to_everyone(self):
        repo = await self._repo_with(
            [_mk_item("org-item", scope=KnowledgeScope.ORGANIZATION)]
        )
        got = await repo.list_for_inventory("org-1", user_id="anyone")
        assert [i.item_id for i in got] == ["org-item"]

    async def test_personal_visible_to_owner_only(self):
        repo = await self._repo_with(
            [_mk_item("p1", scope=KnowledgeScope.PERSONAL, owner_id="user-1")]
        )
        owner = await repo.list_for_inventory("org-1", user_id="user-1")
        other = await repo.list_for_inventory("org-1", user_id="user-2")
        anon = await repo.list_for_inventory("org-1")
        assert [i.item_id for i in owner] == ["p1"]
        assert other == []
        assert anon == []

    async def test_team_visible_to_members_only(self):
        repo = await self._repo_with(
            [_mk_item("t1", scope=KnowledgeScope.TEAM, team_id="team-A")]
        )
        member = await repo.list_for_inventory(
            "org-1", user_id="u", team_ids=["team-A", "team-B"]
        )
        nonmember = await repo.list_for_inventory(
            "org-1", user_id="u", team_ids=["team-B"]
        )
        assert [i.item_id for i in member] == ["t1"]
        assert nonmember == []

    async def test_unpublished_excluded(self):
        repo = await self._repo_with(
            [
                _mk_item(BUILTIN_ID),
                _mk_item("hidden", is_published=False),
            ]
        )
        got = await repo.list_for_inventory("org-1", user_id="u")
        assert [i.item_id for i in got] == [BUILTIN_ID]

    async def test_item_type_filter(self):
        repo = await self._repo_with(
            [
                _mk_item(BUILTIN_ID, item_type=KnowledgeItemType.RUNBOOK),
                _mk_item("faq-1", item_type=KnowledgeItemType.FAQ),
            ]
        )
        runbooks = await repo.list_for_inventory(
            "org-1", item_type=KnowledgeItemType.RUNBOOK
        )
        assert [i.item_id for i in runbooks] == [BUILTIN_ID]
        all_items = await repo.list_for_inventory("org-1")
        assert {i.item_id for i in all_items} == {BUILTIN_ID, "faq-1"}

    async def test_org_isolation(self):
        repo = await self._repo_with(
            [_mk_item("mine", org="org-1"), _mk_item("theirs", org="org-2")]
        )
        got = await repo.list_for_inventory("org-1")
        assert [i.item_id for i in got] == ["mine"]


# ---------------------------------------------------------------------------
# Service surface over FK-on SQLite (the production constraint regime)
# ---------------------------------------------------------------------------


@pytest.fixture
async def inventory_service():
    """Real ``KnowledgeService`` inventory methods over FK-on SQLite.

    Seeds enterprise + org + two users so personal-scope FK INSERTs succeed.
    ChromaDB is stubbed; the relational read/write is real.
    """
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
        await conn.execute(
            text(
                "INSERT INTO organizations "
                "(organization_id, enterprise_id, name, slug) "
                "VALUES ('org-1', 'ent-1', 'Org', 'org')"
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

    svc = KnowledgeService.__new__(KnowledgeService)
    svc._db_session_factory = factory
    svc._vector_store = MagicMock()
    svc._vector_store.delete_documents_by_parent_id = AsyncMock(return_value=2)
    svc._tracer = MagicMock()

    async def _seed(item):
        async with factory() as session:
            repo = DatabaseKnowledgeItemRepository(session)
            await repo.create(item)

    svc._seed_item = _seed  # test helper

    yield svc, factory
    await engine.dispose()


@pytest.mark.asyncio
class TestInventoryServiceDB:
    async def test_list_documents_returns_published_builtins(self, inventory_service):
        svc, _ = inventory_service
        await svc._seed_item(_mk_item(BUILTIN_ID, title="OpenSSH Auth Failures"))

        result = await svc.list_documents(
            user=SimpleNamespace(user_id=None, organization_id="org-1"),
            team_ids=[],
        )

        assert result["total_count"] == 1
        doc = result["documents"][0]
        assert doc["document_id"] == BUILTIN_ID
        assert doc["title"] == "OpenSSH Auth Failures"
        assert doc["document_type"] == "runbook"
        # conversion-pipeline metadata is null for built-ins
        assert doc["metadata"] == {
            "domain": None,
            "service": None,
            "severity": None,
            "quality_score": None,
        }
        assert result["scope_counts"] == {"global": 1, "team": 0, "personal": 0}

    async def test_list_documents_personal_isolation_in_sql(self, inventory_service):
        svc, _ = inventory_service
        await svc._seed_item(_mk_item(BUILTIN_ID))
        await svc._seed_item(
            _mk_item("p-1", scope=KnowledgeScope.PERSONAL, owner_id="user-1")
        )

        owner = await svc.list_documents(
            user=SimpleNamespace(user_id="user-1", organization_id="org-1"),
            team_ids=[],
        )
        other = await svc.list_documents(
            user=SimpleNamespace(user_id="user-2", organization_id="org-1"),
            team_ids=[],
        )

        assert {d["document_id"] for d in owner["documents"]} == {BUILTIN_ID, "p-1"}
        # user-2 must not see user-1's personal runbook
        assert {d["document_id"] for d in other["documents"]} == {BUILTIN_ID}

    async def test_get_document_reads_row_content(self, inventory_service):
        svc, _ = inventory_service
        await svc._seed_item(_mk_item(BUILTIN_ID))

        doc = await svc.get_document(BUILTIN_ID)
        assert doc is not None
        assert doc["document_id"] == BUILTIN_ID
        assert doc["content"] == "# body\nsteps"

    async def test_get_document_missing_returns_none(self, inventory_service):
        svc, _ = inventory_service
        assert await svc.get_document("nope") is None

    async def test_delete_builtin_unpublishes_and_drops_vectors(
        self, inventory_service
    ):
        svc, factory = inventory_service
        await svc._seed_item(_mk_item(BUILTIN_ID))

        result = await svc.delete_document(BUILTIN_ID)
        assert result["success"] is True
        assert result["action"] == "unpublished"

        # Row kept, but unpublished.
        async with factory() as session:
            repo = DatabaseKnowledgeItemRepository(session)
            item = await repo.get_by_id(BUILTIN_ID)
        assert item is not None
        assert item.is_published is False

        # Gotcha lock: vectors MUST be deleted (retrieval ignores is_published).
        svc._vector_store.delete_documents_by_parent_id.assert_awaited_once_with(
            BUILTIN_ID
        )

    async def test_delete_authored_hard_deletes_and_drops_vectors(
        self, inventory_service
    ):
        svc, factory = inventory_service
        await svc._seed_item(_mk_item(AUTHORED_ID))

        result = await svc.delete_document(AUTHORED_ID)
        assert result["success"] is True
        assert result["action"] == "deleted"

        async with factory() as session:
            repo = DatabaseKnowledgeItemRepository(session)
            assert await repo.get_by_id(AUTHORED_ID) is None

        svc._vector_store.delete_documents_by_parent_id.assert_awaited_once_with(
            AUTHORED_ID
        )

    async def test_delete_missing_document_returns_failure(self, inventory_service):
        svc, _ = inventory_service
        result = await svc.delete_document("kb_ffffffffffff")
        assert result["success"] is False
        svc._vector_store.delete_documents_by_parent_id.assert_not_awaited()
