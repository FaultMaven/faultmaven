"""Removing a document while something else writes it, on SQLite AND PostgreSQL (#1966).

The single-session tests (``test_delete_leaves_no_chunk_1966.py``) pin what a
delete does on its own. These pin what happens when another writer interleaves:
a repair pass re-embedding the same row (another replica's boot, or the kb_seed
Job), an operator's content edit, or an edit that commits between the delete's
read and its write. Concurrent sessions here are real connections — SQLite on
disk with the production PRAGMAs (WAL, busy_timeout), or a fresh PostgreSQL
database — because an in-memory engine shares one connection and would let
every interleaving through.

The delete's hold (row change uncommitted, vectors already deleted) is widened
by gating its ``commit`` on an event, standing in for the commit round trip or
another replica's timing. Built from the review round's probes on PR #1992.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    UserModel,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
    DatabaseKnowledgeItemRepository,
)
from faultmaven.modules.knowledge.taxonomy import KnowledgeScope
from faultmaven.utils.runbook_id import item_id_from_runbook_id

pytestmark = [pytest.mark.unit]

AUTHORED_ID = "aaaaaaaa-1111-2222-3333-444444444444"
BUILTIN_ID = item_id_from_runbook_id("race-builtin")

ARMS = [
    pytest.param(AUTHORED_ID, KnowledgeScope.PERSONAL, id="authored"),
    pytest.param(BUILTIN_ID, KnowledgeScope.GLOBAL, id="builtin"),
]


class _KbStore:
    """Chunk counts by parent id; the delete signature mirrors the real store's."""

    def __init__(self) -> None:
        self.parents: dict[str, int] = {}

    async def delete_documents_by_parent_id(
        self,
        parent_document_id: str,
        collection_name: Optional[str] = None,
        *,
        retries: int = 2,
    ) -> int:
        return self.parents.pop(parent_document_id, 0)

    async def list_parent_document_ids(self) -> set:
        return set(self.parents)

    async def delete_documents_by_parents(self, parent_ids, collection_name=None):
        return sum(self.parents.pop(p, 0) for p in list(parent_ids))


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgresql",
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(
                    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
                    reason="PostgreSQL-only; set DATABASE_URL to a PG instance.",
                ),
            ],
        ),
    ]
)
async def race(request, tmp_path):
    if request.param == "sqlite":
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/race.db")

        @event.listens_for(engine.sync_engine, "connect")
        def _pragmas(connection, _record):  # the production PRAGMAs
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        async for world in _world(engine, "sqlite"):
            yield world
        return

    from tests.integration.security.conftest import database_of_its_own

    async with database_of_its_own(os.environ["DATABASE_URL"], "fm_1966_race") as url:
        async for world in _world(create_async_engine(url), "postgresql"):
            yield world


async def _world(engine, dialect):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(enterprise_id=STANDALONE_ENTERPRISE_ID, name="E", slug="e")
        )
        await session.flush()
        session.add(
            UserModel(
                user_id="user-r",
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                username="ur",
                email="ur@example.com",
                display_name="U R",
            )
        )
        await session.commit()

    store = _KbStore()
    svc = KnowledgeService.__new__(KnowledgeService)
    svc._db_session_factory = factory
    svc._vector_store = store
    svc._tracer = MagicMock()
    svc._share_repo = None
    svc._sanitizer = MagicMock()

    async def _same(text):
        return text

    svc._sanitizer.asanitize = _same

    async def _index(document, prechunked=None, enterprise_id=None):
        await asyncio.sleep(0.05)  # the embed
        store.parents[document.document_id] = 3
        return 3

    svc._index_document_in_vector_store = _index

    async def seed(item_id, scope, *, chunks=3):
        async with factory() as session:
            await DatabaseKnowledgeItemRepository(session).create(
                KnowledgeItem(
                    item_id=item_id,
                    enterprise_id=STANDALONE_ENTERPRISE_ID,
                    organization_id=None,
                    title="t",
                    content="# c\nbody",
                    item_type=KnowledgeItemType.RUNBOOK,
                    scope=scope,
                    owner_id=None if scope == KnowledgeScope.GLOBAL else "user-r",
                )
            )
        if chunks:
            store.parents[item_id] = chunks

    async def row(item_id):
        async with factory() as session:
            return await DatabaseKnowledgeItemRepository(session).get_by_id(item_id)

    try:
        yield SimpleNamespace(
            dialect=dialect, svc=svc, store=store, seed=seed, row=row, factory=factory
        )
    finally:
        await engine.dispose()


class _HeldDelete:
    """``delete_document`` stopped at its commit: the row change is issued and
    the vectors are gone, but nothing is committed until ``release``."""

    def __init__(self, svc, item_id):
        self._svc, self._item_id = svc, item_id
        self.held, self.release = asyncio.Event(), asyncio.Event()
        self._real = DatabaseKnowledgeItemRepository.commit
        self.task: Optional[asyncio.Task] = None

    async def __aenter__(self):
        owner = {}
        real, held, release = self._real, self.held, self.release

        async def gated_commit(repo):
            if asyncio.current_task() is owner.get("task"):
                held.set()
                await release.wait()
            await real(repo)

        self._patch = patch.object(
            DatabaseKnowledgeItemRepository, "commit", gated_commit
        )
        self._patch.start()
        self.task = asyncio.create_task(self._svc.delete_document(self._item_id))
        owner["task"] = self.task
        await asyncio.wait_for(self.held.wait(), 10)
        return self

    async def __aexit__(self, *exc):
        self.release.set()
        try:
            if self.task is not None and not self.task.done():
                await asyncio.wait_for(self.task, 10)
        finally:
            self._patch.stop()


# ---------------------------------------------------------------------------
# Item 1: a repair racing a delete must not bring the chunks back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("item_id", "scope"), ARMS)
async def test_a_delete_during_the_repair_embed_leaves_no_chunk(race, item_id, scope):
    """The split state's repair is embedding when the user's retried delete
    lands and commits. The repair read the row before the embed; without the
    re-read after writing, its vectors outlive the row (authored: a chunk with
    no row, for good) or the unpublish (built-in: a retired runbook
    retrievable)."""
    await race.seed(item_id, scope, chunks=0)  # published, vectors missing
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated_index(document, prechunked=None, enterprise_id=None):
        entered.set()
        await release.wait()
        race.store.parents[document.document_id] = 3
        return 3

    race.svc._index_document_in_vector_store = gated_index
    repairing = asyncio.create_task(race.svc.reindex_missing_vectors(item_id))
    await asyncio.wait_for(entered.wait(), 10)

    removed = await race.svc.delete_document(item_id)
    release.set()
    chunks = await asyncio.wait_for(repairing, 10)

    assert removed["success"] is True
    assert chunks == 0
    assert item_id not in race.store.parents
    row = await race.row(item_id)
    assert row is None if scope == KnowledgeScope.PERSONAL else not row.is_published


@pytest.mark.parametrize(("item_id", "scope"), ARMS)
async def test_a_repair_during_the_delete_hold_waits_for_it(race, item_id, scope):
    """The other order: the delete has removed the vectors and holds its row
    change while a repair embeds and writes. The repair's locked re-read waits
    for the delete and then sees it — ``FOR SHARE`` on PostgreSQL; on SQLite,
    which ignores that clause and lets a reader see the last committed state,
    a no-op write that must first take the writer lock. A plain re-read kept
    the vectors on SQLite: an authored chunk with no row, for good (review
    probe R3).
    """
    await race.seed(item_id, scope)
    async with _HeldDelete(race.svc, item_id) as held:
        repairing = asyncio.create_task(race.svc.reindex_missing_vectors(item_id))
        await asyncio.sleep(0.5)
        assert not repairing.done(), "the re-read did not wait for the held delete"
    chunks = await asyncio.wait_for(repairing, 10)

    assert (await held.task)["success"] is True
    assert chunks == 0
    assert item_id not in race.store.parents


# ---------------------------------------------------------------------------
# Item 3: an unpublish racing an operator's content edit is not reverted
# ---------------------------------------------------------------------------


async def test_an_edit_racing_an_unpublish_neither_reverts_it_nor_keeps_vectors(race):
    """R5: the unpublish holds while a content edit re-indexes and writes. A
    full-row write from the edit's re-read put ``is_published`` back to true
    and kept the vectors it wrote. The targeted write waits for the unpublish
    (row lock / writer lock), leaves the flag alone, and the read after its
    commit drops the vectors."""
    await race.seed(BUILTIN_ID, KnowledgeScope.GLOBAL)
    async with _HeldDelete(race.svc, BUILTIN_ID) as held:
        editing = asyncio.create_task(
            race.svc.update_document_metadata(BUILTIN_ID, content="# edited\nbody2")
        )
        await asyncio.sleep(0.4)
    edited = await asyncio.wait_for(editing, 10)

    assert (await held.task)["action"] == "unpublished"
    assert edited is not None and edited["content"] == "# edited\nbody2"
    row = await race.row(BUILTIN_ID)
    assert row.is_published is False, "the edit republished the retired runbook"
    assert BUILTIN_ID not in race.store.parents


# ---------------------------------------------------------------------------
# Item 5: the unpublish writes only is_published
# ---------------------------------------------------------------------------


async def test_the_unpublish_does_not_revert_an_edit_committed_after_its_read(race):
    """The built-in arm reads the row, then writes. A full-row write from that
    read reverted any edit committed in between; the targeted
    ``UPDATE ... SET is_published = false`` keeps it."""
    await race.seed(BUILTIN_ID, KnowledgeScope.GLOBAL)
    real_get = DatabaseKnowledgeItemRepository.get_by_id
    edited = {"done": False}

    async def get_then_someone_edits(repo, item_id):
        item = await real_get(repo, item_id)
        if not edited["done"]:
            edited["done"] = True
            async with race.factory() as other:
                await DatabaseKnowledgeItemRepository(other).update_fields(
                    item_id, {"content": "# edited meanwhile\nbody"}
                )
        return item

    with patch.object(
        DatabaseKnowledgeItemRepository, "get_by_id", get_then_someone_edits
    ):
        result = await race.svc.delete_document(BUILTIN_ID)

    assert result["action"] == "unpublished"
    row = await race.row(BUILTIN_ID)
    assert row.is_published is False
    assert row.content == "# edited meanwhile\nbody", "the unpublish reverted an edit"
