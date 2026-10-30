"""Removing a document while something else writes it, on SQLite AND PostgreSQL (#1966).

The single-session tests (``test_delete_leaves_no_chunk_1966.py``) pin what a
delete does on its own. These pin what happens when another writer interleaves:
a repair pass re-embedding the same row (another replica's boot, or the kb_seed
Job), an operator's content edit, or an edit that commits between the delete's
read and its write. Concurrent sessions here are real connections — SQLite on
disk with the production PRAGMAs (WAL, busy_timeout), or a fresh PostgreSQL
database — because an in-memory engine shares one connection and would let
every interleaving through.

The writer that loses such a race writes vectors under a row that is gone or
unpublished. No writer deletes them in-process any more (#2397): no KB door
admits a chunk whose row is not complete and published, so the outcome each
test pins is that the document is not admitted
(``KnowledgeService.admitted_document_ids``, the real query), whatever the
store holds. The delete's hold (row change issued, not committed) is widened by
gating its ``commit`` on an event, standing in for the commit round trip or
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
        self.deleted: list[str] = []

    async def delete_documents_by_parent_id(
        self,
        parent_document_id: str,
        collection_name: Optional[str] = None,
        *,
        retries: int = 2,
    ) -> int:
        self.deleted.append(parent_document_id)
        return self.parents.pop(parent_document_id, 0)

    async def list_all_chunk_ids_by_parent(self) -> dict:
        return {
            p: [f"{p}_chunk_{n}" for n in range(c)] for p, c in self.parents.items()
        }

    async def delete_observed_chunk_ids(self, chunk_ids, collection_name=None):
        ids = list(chunk_ids)
        for parent in {c.rsplit("_chunk_", 1)[0] for c in ids}:
            self.parents.pop(parent, None)
        return ids


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

    async def admitted(item_id):
        return item_id in await svc.admitted_document_ids([item_id])

    try:
        yield SimpleNamespace(
            dialect=dialect,
            svc=svc,
            store=store,
            seed=seed,
            row=row,
            admitted=admitted,
            factory=factory,
        )
    finally:
        await engine.dispose()


class _HeldDelete:
    """``delete_document`` stopped at its commit: the row change is issued,
    holding the row (PostgreSQL) or the writer lock (SQLite), but nothing is
    committed until ``release``."""

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
# Item 1: a repair racing a delete never makes the document servable again
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("item_id", "scope"), ARMS)
async def test_a_delete_during_the_repair_embed_leaves_nothing_admitted(
    race, item_id, scope
):
    """The repair is embedding when the user's delete lands and commits. Its
    vectors land under a row that is gone (authored) or unpublished
    (built-in); the repair makes no vector delete for them, and no door admits
    them (#2397)."""
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
    await asyncio.wait_for(repairing, 10)

    assert removed["success"] is True
    assert race.store.parents[item_id] == 3  # left for collection
    assert not await race.admitted(item_id)
    # The authored delete's own best-effort vector delete ran before the
    # repair's late write; nothing deleted vectors after it.
    expected = [item_id] if scope == KnowledgeScope.PERSONAL else []
    assert race.store.deleted == expected
    row = await race.row(item_id)
    assert row is None if scope == KnowledgeScope.PERSONAL else not row.is_published


@pytest.mark.parametrize(("item_id", "scope"), ARMS)
async def test_a_repair_during_the_delete_hold_leaves_nothing_admitted(
    race, item_id, scope
):
    """The other order: the delete holds its row change while a repair embeds
    and writes. Whichever order they finish in, once the delete commits the
    document is not admitted, and the repair never deleted a vector."""
    await race.seed(item_id, scope)
    async with _HeldDelete(race.svc, item_id) as held:
        repairing = asyncio.create_task(race.svc.reindex_missing_vectors(item_id))
        await asyncio.sleep(0.2)
    await asyncio.wait_for(repairing, 10)

    assert (await held.task)["success"] is True
    assert not await race.admitted(item_id)
    assert race.store.deleted == ([item_id] if scope == KnowledgeScope.PERSONAL else [])


# ---------------------------------------------------------------------------
# Item 3: an unpublish racing an operator's content edit is not reverted
# ---------------------------------------------------------------------------


async def test_an_edit_racing_an_unpublish_neither_reverts_it_nor_serves_it(race):
    """R5: the unpublish holds while a content edit re-indexes and writes. A
    full-row write from the edit's re-read put ``is_published`` back to true.
    The targeted write waits for the unpublish (row lock / writer lock) and
    leaves the flag alone, so the vectors the edit wrote are never served."""
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
    assert not await race.admitted(BUILTIN_ID)
    assert race.store.deleted == []  # the edit's discard is gone (#2397)


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
