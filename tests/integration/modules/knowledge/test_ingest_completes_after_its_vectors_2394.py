"""An authored runbook row is incomplete until its vectors exist (#2394).

``ingest_runbook`` used to insert a published row before writing its vectors,
and the startup repair re-embeds every published row with no vectors, so a row
any failed ingest left behind went live at the next boot. One such row is
beyond every cleanup: the INSERT's COMMIT completing on the server after the
caller gave up on it. An authored row now carries ``ingest_complete`` false from
its INSERT until one guarded UPDATE, with its team share, after its vectors are
written: until then it is listed to no one, never re-embedded and no KB door
admits its chunks (#2397), and the KB bootstrap sweeps it with its vectors once
stale. A pack row is complete from its INSERT. A failed ingest's cleanup removes
its row and never its vectors: they are left under an id no row admits. Driven
through the real service on SQLite with foreign keys enforced, reusing the
#2365 / #2378 helpers.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from faultmaven.bootstrap import kb_init
from faultmaven.bootstrap.kb_incomplete_sweep import sweep_incomplete_ingests
from faultmaven.bootstrap.kb_orphan_sweep import (
    SWEEP_ARMED,
    SWEEP_OFF,
    SWEEP_REPORT_ONLY,
)
from faultmaven.bootstrap.kb_pack import PackChunk, PackRunbook
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    KnowledgeItemModel,
    UserModel,
)
from faultmaven.infrastructure.persistence.share_repository import (
    PostgreSQLShareRepository,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
)
from faultmaven.modules.knowledge.domain.services import knowledge_service
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    DatabaseKnowledgeItemRepository,
    KnowledgeItemRepositoryException,
)
from faultmaven.modules.knowledge.taxonomy import KnowledgeScope
from tests.integration.modules.knowledge.test_ingest_cleans_up_its_own_row_2378 import (
    DOC_ID,
    ONE_TEAM_ITEM,
    PEER_CREATED_AT,
    _a_peer_reingests,
    _Chunks,
    _created_at,
    _delete_the_row,
    _ingest,
)
from tests.integration.modules.knowledge.test_upload_writes_rows_after_ingest_2365 import (  # noqa: F401
    NOTHING,
    OWNER,
    TEAM_ID,
    _knowledge,
    _rows,
    session_factory,
)
from tests.runbook_samples import valid_runbook
from tests.utils import seed_enterprises, seed_users

pytestmark = [pytest.mark.integration, pytest.mark.knowledge_base]

THIS_CHUNKS = ["this-0", "this-1", "this-2"]


class _Index(_Chunks):
    """``_Chunks`` with the listing and observed-id delete the reconcile uses."""

    async def list_all_chunk_ids_by_parent(self):
        by_parent: dict[str, list[str]] = {}
        for chunk_id, parent in self.chunks.items():
            by_parent.setdefault(parent, []).append(chunk_id)
        return by_parent

    async def delete_observed_chunk_ids(self, chunk_ids):
        return [c for c in chunk_ids if self.chunks.pop(c, None) is not None]


def _service(factory, store, *, before=None, then=None):
    """The service indexing into ``store``. Its first indexing is this call's:
    ``before`` runs while it embeds, then it writes ``this`` chunks, then
    ``then`` runs, before the ingest completes. Every later one (a repair's)
    writes ``repair`` chunks."""
    service = _knowledge(factory)
    service._vector_store = store
    calls: list[str] = []

    async def index(document, prechunked=None, enterprise_id=None, generation=None):
        writer = "repair" if calls else "this"
        calls.append(writer)
        if writer == "this" and before:
            await before()
        store.index(document.document_id, writer)
        if writer == "this" and then:
            await then()
        return 3

    service._index_document_in_vector_store = index
    return service


async def _complete(factory, item_id=DOC_ID):
    async with factory() as session:
        return await session.scalar(
            select(KnowledgeItemModel.ingest_complete).where(
                KnowledgeItemModel.item_id == item_id
            )
        )


async def _visible(factory) -> dict[str, bool]:
    """Whether each listing and visibility read shows the row to its owner, a
    member of its team."""
    who = dict(enterprise_id=STANDALONE_ENTERPRISE_ID, user_id=OWNER)
    who["team_ids"] = [TEAM_ID]
    async with factory() as session:
        repo = DatabaseKnowledgeItemRepository(session)
        listed = await repo.list_for_inventory(**who)
        return {
            "listed": DOC_ID in [i.item_id for i in listed],
            "by_id": await repo.get_visible_by_id(DOC_ID, **who) is not None,
            "ids": DOC_ID in await repo.visible_item_ids([DOC_ID], **who),
            # Whether a KB door serves its chunks (#2397).
            "admitted": DOC_ID in await repo.admitted_generations([DOC_ID]),
        }


SHOWN = dict.fromkeys(("listed", "by_id", "ids", "admitted"), True)
HIDDEN = dict.fromkeys(SHOWN, False)


async def _repair(service, factory):
    """The startup reconcile and repair, in ``bootstrap_kb``'s order."""
    _, _, orphaned = await kb_init._reconcile_vectors(service, factory)
    repaired, _ = await kb_init._repair_unserved_rows(orphaned, service)
    return orphaned, repaired


def _incomplete_row(item_id=DOC_ID, *, created_at=None) -> KnowledgeItem:
    stamp = created_at or datetime.now(timezone.utc)
    return KnowledgeItem(
        item_id=item_id,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        title="Stray",
        content=valid_runbook("Stray Runbook Left By A Failed Ingest"),
        item_type=KnowledgeItemType.RUNBOOK,
        scope=KnowledgeScope.PERSONAL,
        owner_id=OWNER,
        created_at=stamp,
        updated_at=stamp,
        ingest_complete=False,
    )


async def _insert(factory, item: KnowledgeItem) -> None:
    async with factory() as session:
        await DatabaseKnowledgeItemRepository(session).create(item)


async def test_an_authored_row_is_incomplete_unlisted_and_unrepaired_until_its_vectors_exist(
    session_factory,
):
    """Mid-ingest, before and after its chunks are written, a concurrent
    listing does not show the row, and a concurrent reconcile neither repairs
    it nor deletes its chunks; it has no share. Then it completes, with its
    share, and is listed. On ``main`` the row is published from its INSERT:
    listed, and re-embedded by the repair while the embed runs."""
    store = _Index()
    seen: dict = {}

    async def while_it_embeds():
        seen["visible"] = await _visible(session_factory)
        seen["before_vectors"] = await _repair(service, session_factory)
        seen["rows"] = await _rows(session_factory)
        seen["complete"] = await _complete(session_factory)

    async def once_its_vectors_exist():
        seen["after_vectors"] = await _repair(service, session_factory)

    service = _service(
        session_factory, store, before=while_it_embeds, then=once_its_vectors_exist
    )

    assert await _ingest(service) == 3

    assert seen["visible"] == HIDDEN
    assert seen["before_vectors"] == ([], [])
    assert seen["after_vectors"] == ([], [])
    assert seen["rows"] == {**NOTHING, "knowledge_items": 1}
    assert seen["complete"] is False
    assert sorted(store.chunks) == THIS_CHUNKS
    assert await _complete(session_factory) is True
    assert await _rows(session_factory) == ONE_TEAM_ITEM
    assert await _visible(session_factory) == SHOWN


async def test_a_pack_row_is_complete_from_its_insert_and_issues_no_update(
    session_factory,
):
    """Under ``kb_seed`` the maintenance role may not UPDATE ``knowledge_items``
    (``role_may_update=False``): a pack ingest writes its vectors before any
    row exists (#2388), then inserts its row complete, with its share, and
    issues no UPDATE."""
    statements: list[str] = []
    engine = session_factory.kw["bind"].sync_engine

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement.lstrip().upper())

    event.listen(engine, "before_cursor_execute", record)
    store = _Index()
    seen = {}

    async def mid_ingest():
        seen["complete"] = await _complete(session_factory, "kb_aaaaaaaaaaaa")

    service = _knowledge(session_factory)
    service._vector_store = store

    async def index(document, prechunked=None, enterprise_id=None, generation=None):
        store.index(document.document_id, "pack")
        await mid_ingest()
        return len(prechunked)

    service._index_document_in_vector_store = index
    runbook = PackRunbook(
        item_id="kb_aaaaaaaaaaaa",
        content_hash="h1",
        title="Shipped Runbook",
        scope="team",
        relpath="team/shipped.md",
        content=valid_runbook("Shipped Runbook For The Pack"),
        tags=[],
        source_url=None,
        owner_id=OWNER,
        team_id=TEAM_ID,
        chunks=[PackChunk(chunk_index=0, text="t", embedding=[0.0] * 4)],
    )
    try:
        outcome = await kb_init._ingest_pack_runbook(
            runbook=runbook,
            knowledge_service=service,
            db_session_factory=session_factory,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            role_may_update=False,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert outcome == "ingested"
    assert seen["complete"] is None, "a row existed while the vectors were written"
    assert await _complete(session_factory, "kb_aaaaaaaaaaaa") is True
    assert await _rows(session_factory) == ONE_TEAM_ITEM
    assert [s for s in statements if s.startswith("UPDATE")] == []
    assert any(s.startswith("INSERT INTO KNOWLEDGE_ITEMS") for s in statements)


REAL_CREATE = DatabaseKnowledgeItemRepository.create
REAL_DELETE = DatabaseKnowledgeItemRepository.delete


@pytest.mark.parametrize("lands", ["between_delete_and_read", "after_read"])
@pytest.mark.parametrize("failure", ["cancel", "command_timeout"])
async def test_an_insert_that_commits_after_its_cleanup_is_never_listed_or_repaired(
    session_factory, monkeypatch, lands, failure
):
    """#2381's probe P7. Under ``AbortOnCancelConnection`` (#2280) a cancelled
    or timed-out command aborts the transport and sends no CancelRequest, so a
    COMMIT the server already received completes after the caller's cleanup
    ran on another connection: modelled by withholding the row and writing it,
    with this call's stamp, at a chosen point of the cleanup. The row stands
    with no vectors and no share. It is listed to no one, the startup repair
    does not re-embed it, and the sweep removes it once stale. On ``main`` it
    stood published, and the repair re-embedded it."""
    store = _Index()
    service = _service(session_factory, store)
    held = {}

    async def land():
        item = held.pop("item", None)
        if item is not None:
            async with session_factory() as s:
                await REAL_CREATE(DatabaseKnowledgeItemRepository(s), item)

    async def create(self, item):
        held["item"] = item
        if failure == "cancel":
            raise asyncio.CancelledError()
        raise KnowledgeItemRepositoryException("Failed to create: aborted")

    async def cleanup_delete(self, item_id, **kwargs):
        matched = await REAL_DELETE(self, item_id, **kwargs)
        if lands == "between_delete_and_read":
            await land()
            return matched
        # The cleanup's read of the row, after its delete, is the session's.
        real_read = self.db.scalar

        async def read_then_land(*args, **read_kwargs):
            found = await real_read(*args, **read_kwargs)
            await land()
            return found

        self.db.scalar = read_then_land
        return matched

    monkeypatch.setattr(DatabaseKnowledgeItemRepository, "create", create)
    monkeypatch.setattr(DatabaseKnowledgeItemRepository, "delete", cleanup_delete)
    expected = (
        asyncio.CancelledError
        if failure == "cancel"
        else KnowledgeItemRepositoryException
    )
    with pytest.raises(expected):
        await _ingest(service)
    monkeypatch.undo()

    assert await _rows(session_factory) == {**NOTHING, "knowledge_items": 1}
    assert await _visible(session_factory) == HIDDEN
    assert await _repair(service, session_factory) == ([], [])
    assert await service.reindex_missing_vectors(DOC_ID) == 0
    assert store.chunks == {}
    assert await _complete(session_factory) is False

    (stamp,) = await _created_at(session_factory)
    stale = stamp.replace(tzinfo=timezone.utc) + timedelta(hours=25)
    swept = await sweep_incomplete_ingests(
        service,
        session_factory,
        mode=SWEEP_ARMED,
        max_age=timedelta(hours=24),
        now=stale,
    )
    assert swept.deleted == [DOC_ID]
    assert await _rows(session_factory) == NOTHING


async def test_a_completed_row_that_loses_its_vectors_is_still_repaired(
    session_factory,
):
    """What the repair exists for: crash recovery, a wiped collection. A pin:
    ``main`` repairs it too."""
    store = _Index()
    service = _service(session_factory, store)
    await _ingest(service)
    store.chunks.clear()

    assert await _repair(service, session_factory) == ([DOC_ID], [DOC_ID])
    assert sorted(store.chunks) == ["repair-0", "repair-1", "repair-2"]


async def test_the_flip_leaves_a_peer_row_alone(session_factory):
    """Another call re-created the id, incomplete, with its own stamp, while
    this call embedded: this call's flip matches no row, so it raises, and the
    peer's row stays incomplete and standing, with the chunks under its id. On
    ``main`` this call returns success over the peer's row."""
    store = _Index()

    async def a_peer_starts_its_ingest():
        await _delete_the_row(session_factory)
        await _insert(session_factory, _incomplete_row(created_at=PEER_CREATED_AT))

    service = _service(session_factory, store, then=a_peer_starts_its_ingest)

    with pytest.raises(RuntimeError, match="not marked complete"):
        await _ingest(service)

    (created_at,) = await _created_at(session_factory)
    assert created_at.replace(tzinfo=None) == PEER_CREATED_AT.replace(tzinfo=None)
    assert await _complete(session_factory) is False
    assert sorted(store.chunks) == THIS_CHUNKS


async def test_a_flip_that_matches_no_row_raises_into_the_cleanup(session_factory):
    """This call's row was removed while it embedded (a delete, or the sweep of
    a slow ingest): the flip matches no row and raises. The cleanup deletes no
    vector (#2397): this call's chunks stay under an id with no row, which no
    KB door admits. Before #2394 the call reported success with no row."""
    store = _Index()
    service = _service(
        session_factory, store, then=lambda: _delete_the_row(session_factory)
    )

    with pytest.raises(RuntimeError, match="not marked complete"):
        await _ingest(service)

    assert await _rows(session_factory) == NOTHING
    assert sorted(store.chunks) == THIS_CHUNKS
    assert await service.admitted_generations([DOC_ID]) == {}


async def test_an_incomplete_row_is_read_and_deleted_but_never_edited_or_reindexed(
    session_factory,
):
    """The trusted read still returns the row, and ``delete_document`` removes
    it; ``update_document_metadata`` and ``reindex_missing_vectors`` refuse it
    before embedding anything."""
    await _insert(session_factory, _incomplete_row())
    store = _Index()
    service = _service(session_factory, store)
    service._index_document_in_vector_store = AsyncMock(return_value=3)

    assert (
        await service.update_document_metadata(
            DOC_ID, content=valid_runbook("An Edit Of The Stray Runbook")
        )
        is None
    )
    assert await service.reindex_missing_vectors(DOC_ID) == 0
    service._index_document_in_vector_store.assert_not_awaited()
    assert (await service.get_document(DOC_ID))["title"] == "Stray"

    outcome = await service.delete_document(DOC_ID)

    assert outcome["success"] is True
    assert await _rows(session_factory) == NOTHING


# ---------------------------------------------------------------------------
# The stray sweep
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
STALE, YOUNG, DONE = (f"kb_{c * 16}" for c in "abc")


async def _sweep_fixture(factory) -> _Index:
    """A stale and a young incomplete row and an old complete one, each with
    chunks."""
    store = _Index()
    for item_id, age, complete in (
        (STALE, 25, False),
        (YOUNG, 23, False),
        (DONE, 48, True),
    ):
        row = _incomplete_row(item_id, created_at=NOW - timedelta(hours=age))
        row.ingest_complete = complete
        await _insert(factory, row)
        store.index(item_id, item_id)
    return store


async def _ids(factory) -> set[str]:
    async with factory() as session:
        return set(await session.scalars(select(KnowledgeItemModel.item_id)))


@pytest.mark.parametrize("mode", [SWEEP_ARMED, SWEEP_REPORT_ONLY, SWEEP_OFF])
async def test_the_sweep_removes_only_stale_incomplete_rows(session_factory, mode):
    store = await _sweep_fixture(session_factory)
    service = _knowledge(session_factory)
    service._vector_store = store

    result = await sweep_incomplete_ingests(
        service, session_factory, mode=mode, max_age=timedelta(hours=24), now=NOW
    )

    if mode == SWEEP_OFF:
        assert (result.skipped, result.would_delete) == ("disabled", [])
    else:
        assert result.would_delete == [STALE]
    if mode == SWEEP_ARMED:
        assert (result.deleted, result.spared) == ([STALE], 0)
        assert await _ids(session_factory) == {YOUNG, DONE}
        assert set(store.chunks.values()) == {YOUNG, DONE}
    else:
        assert result.deleted == []
        assert await _ids(session_factory) == {STALE, YOUNG, DONE}
        assert set(store.chunks.values()) == {STALE, YOUNG, DONE}


async def test_a_failed_vector_delete_keeps_the_row_for_the_next_run(
    session_factory,
):
    store = await _sweep_fixture(session_factory)
    store.delete_documents_by_parent_id = AsyncMock(side_effect=OSError("down"))
    service = _knowledge(session_factory)
    service._vector_store = store

    result = await sweep_incomplete_ingests(
        service, session_factory, mode=SWEEP_ARMED, max_age=timedelta(hours=24), now=NOW
    )

    assert (result.deleted, result.unremoved) == ([], 1)
    assert STALE in await _ids(session_factory)


async def test_a_row_that_completed_since_the_read_is_not_swept(
    session_factory, monkeypatch
):
    """The delete is guarded on the row still being incomplete."""
    from faultmaven.bootstrap import kb_incomplete_sweep

    store = await _sweep_fixture(session_factory)
    service = _knowledge(session_factory)
    service._vector_store = store
    done_at = NOW - timedelta(hours=48)
    monkeypatch.setattr(
        kb_incomplete_sweep, "_read_stale", AsyncMock(return_value=[(DONE, done_at)])
    )

    result = await sweep_incomplete_ingests(
        service, session_factory, mode=SWEEP_ARMED, max_age=timedelta(hours=24), now=NOW
    )

    assert (result.would_delete, result.deleted, result.spared) == ([DONE], [], 1)
    assert DONE in await _ids(session_factory)
    assert DONE in store.chunks.values()


@pytest.fixture
def india_clock(monkeypatch):
    """The process's local zone at +05:30, where reading a naive value as
    local time and reading it as UTC part by 5.5 h."""
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is POSIX-only")
async def test_a_naive_clock_is_read_as_utc_as_the_stamp_guard_reads_it(
    session_factory, india_clock
):
    """The cutoff takes the ``created_at`` guard's rule (``_as_utc``): a naive
    ``now`` is UTC, not local time."""
    store = await _sweep_fixture(session_factory)
    service = _knowledge(session_factory)
    service._vector_store = store

    result = await sweep_incomplete_ingests(
        service,
        session_factory,
        mode=SWEEP_REPORT_ONLY,
        max_age=timedelta(hours=24),
        now=NOW.replace(tzinfo=None),
    )

    assert result.would_delete == [STALE]


# ---------------------------------------------------------------------------
# The pack path (``shipped=True``): its vectors first, then one transaction
# that inserts its row complete with its share (#2388). A failure writes no
# row, and its chunks stay unadmitted.
# ---------------------------------------------------------------------------


async def _ingest_shipped(service):
    return await service.ingest_runbook(
        document_id=DOC_ID,
        title="Shipped Runbook",
        content=valid_runbook("Shipped Runbook For The Pack"),
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        scope="team",
        owner_id=OWNER,
        team_id=TEAM_ID,
        shipped=True,
    )


async def test_a_pack_ingest_whose_share_fails_leaves_no_row(
    session_factory, monkeypatch
):
    """The share is written in the row's transaction, so its failure rolls the
    INSERT back: no row, no share, and the chunks unadmitted."""
    store = _Index()
    service = _service(session_factory, store)
    monkeypatch.setattr(
        PostgreSQLShareRepository,
        "share",
        AsyncMock(side_effect=RuntimeError("share failed")),
    )

    with pytest.raises(RuntimeError, match="share failed"):
        await _ingest_shipped(service)

    assert await _rows(session_factory) == NOTHING
    assert sorted(store.chunks) == THIS_CHUNKS
    assert await service.admitted_generations([DOC_ID]) == {}


async def test_a_pack_ingest_whose_vectors_fail_leaves_no_admitted_chunk(
    session_factory,
):
    """No row is written and the chunks the store wrote stay, unadmitted
    (#2397), for the next pack-loading bootstrap to collect."""
    store = _Index()

    async def the_store_fails_after_the_write():
        raise RuntimeError("store failed after the write")

    service = _service(session_factory, store, then=the_store_fails_after_the_write)

    with pytest.raises(RuntimeError, match="after the write"):
        await _ingest_shipped(service)

    assert await _rows(session_factory) == NOTHING
    assert sorted(store.chunks) == THIS_CHUNKS
    assert await service.admitted_generations([DOC_ID]) == {}


@pytest.mark.parametrize("peer", ["live", "gone"])
async def test_a_failed_pack_ingest_leaves_a_live_peer_whole(session_factory, peer):
    """This pack ingest's store raised after writing its chunks, once the id's
    row was deleted (``gone``) or deleted and re-created by a peer with its
    share and its chunks (``live``). It wrote no row, so the peer's row, share
    and chunks stay; its own chunks stay under an id no row admits (#2397)."""
    store = _Index()

    async def a_peer_then_the_store_fails():
        if peer == "live":
            await _a_peer_reingests(session_factory, store)
        else:
            await _delete_the_row(session_factory)
        raise RuntimeError("store failed after the write")

    service = _service(session_factory, store, then=a_peer_then_the_store_fails)

    with pytest.raises(RuntimeError, match="after the write"):
        await _ingest_shipped(service)

    if peer == "live":
        (created_at,) = await _created_at(session_factory)
        assert created_at.replace(tzinfo=None) == PEER_CREATED_AT.replace(tzinfo=None)
        assert await _rows(session_factory) == ONE_TEAM_ITEM
        assert sorted(store.chunks) == ["peer-0", "peer-1", "peer-2", *THIS_CHUNKS]
    else:
        assert await _rows(session_factory) == NOTHING
        assert sorted(store.chunks) == THIS_CHUNKS
        assert await service.admitted_generations([DOC_ID]) == {}


# ---------------------------------------------------------------------------
# PostgreSQL: the cleanup's DELETE waits for the flip
# ---------------------------------------------------------------------------


@pytest.mark.postgres
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
)
@pytest.mark.parametrize("flip", ["commits", "rolls_back"])
async def test_a_cleanup_waits_on_the_flip_and_never_deletes_a_completed_row(flip):
    """``mark_ingest_complete`` holds the row's lock until its transaction
    ends, and the incomplete-only delete of ``_delete_knowledge_item_row``
    waits on it: this engine sets no ``statement_timeout``, so the wait is
    unbounded here. Then the delete re-checks the row (EvalPlanQual): a
    committed flip's row is complete and stands, and the cleanup reports that
    its completion landed (#2396); a rolled-back one's is deleted. No vector is
    deleted. Before #2396 the committed row was deleted too."""
    enterprise, owner = f"ent_2394_{uuid4().hex[:8]}", f"user-2394-{uuid4().hex[:8]}"
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    stamp = datetime.now(timezone.utc)
    item = _incomplete_row(f"kb_{uuid4().hex[:16]}", created_at=stamp)
    item.enterprise_id, item.owner_id = enterprise, owner
    service = _knowledge(factory)
    try:
        async with factory() as session:
            await seed_enterprises(session, [enterprise])
            await seed_users(session, [owner], enterprise_id=enterprise)
        await _insert(factory, item)

        async with factory() as flipping:
            repo = DatabaseKnowledgeItemRepository(flipping)
            assert await repo.mark_ingest_complete(item.item_id, created_at=stamp)
            backend = await flipping.scalar(select(func.pg_backend_pid()))
            cleanup = asyncio.create_task(
                service._delete_knowledge_item_row(item.item_id, created_at=stamp)
            )
            await _until_blocked_by(factory, backend)
            assert not cleanup.done()
            if flip == "commits":
                await repo.commit()
            else:
                await repo.rollback()
        found = await cleanup

        async with factory() as session:
            row = await session.get(KnowledgeItemModel, item.item_id)
        if flip == "commits":
            assert found == knowledge_service._COMPLETED
            assert row is not None and row.ingest_complete
        else:
            assert found == knowledge_service._DELETED
            assert row is None
        service._vector_store.delete_documents_by_parent_id.assert_not_awaited()
    finally:
        async with factory() as session:
            for model in (KnowledgeItemModel, UserModel, EnterpriseModel):
                await session.execute(
                    delete(model).where(model.enterprise_id == enterprise)
                )
            await session.commit()
        await engine.dispose()


@pytest.fixture
async def wal_factory(tmp_path):
    """SQLite as the app's engine opens it (``database.get_engine``): WAL,
    ``busy_timeout=5000``, foreign keys on. Concurrent sessions are separate
    connections."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'wal.db'}", poolclass=NullPool
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        for pragma in ("journal_mode=WAL", "busy_timeout=5000", "foreign_keys=ON"):
            cursor.execute(f"PRAGMA {pragma}")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        await seed_enterprises(session, [STANDALONE_ENTERPRISE_ID])
        await seed_users(session, [OWNER], enterprise_id=STANDALONE_ENTERPRISE_ID)
    yield factory
    await engine.dispose()


@pytest.mark.parametrize("flip", ["commits", "rolls_back"])
async def test_on_sqlite_the_cleanup_deletes_first_and_reads_after(wal_factory, flip):
    """The SQLite half of the same rule (#2396, the design critique's probe
    S2). A flip holds the writer lock. A read of the row does not wait for it
    and sees the row incomplete, so it cannot tell whether the flip lands. The
    cleanup's delete, its session's first statement, waits on the lock
    (``busy_timeout``), and only then is the row read: a committed flip's row
    is complete and stands; a rolled-back one's is deleted."""
    stamp = datetime.now(timezone.utc)
    await _insert(wal_factory, _incomplete_row(created_at=stamp))
    service = _knowledge(wal_factory)
    engine = wal_factory.kw["bind"].sync_engine
    deleting = asyncio.Event()

    def on_statement(_conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("DELETE FROM KNOWLEDGE_ITEMS"):
            deleting.set()

    event.listen(engine, "before_cursor_execute", on_statement)
    try:
        async with wal_factory() as flipping:
            repo = DatabaseKnowledgeItemRepository(flipping)
            assert await repo.mark_ingest_complete(DOC_ID, created_at=stamp)
            # A read while the flip is open: answered at once, pre-flip.
            assert await _complete(wal_factory) is False
            cleanup = asyncio.create_task(
                service._delete_knowledge_item_row(DOC_ID, created_at=stamp)
            )
            # The delete is issued, or the cleanup ended without one (a defect
            # the assertions below then report, rather than a hang).
            waiting = asyncio.ensure_future(deleting.wait())
            await asyncio.wait({waiting, cleanup}, return_when=asyncio.FIRST_COMPLETED)
            waiting.cancel()
            if flip == "commits":
                await repo.commit()
            else:
                await repo.rollback()
        found = await cleanup
    finally:
        event.remove(engine, "before_cursor_execute", on_statement)

    if flip == "commits":
        assert found == knowledge_service._COMPLETED
        assert await _complete(wal_factory) is True
    else:
        assert found == knowledge_service._DELETED
        assert await _rows(wal_factory) == NOTHING


async def test_the_cleanup_reads_its_stamp_as_an_instant(session_factory):
    """The cleanup's read matches the stamp as its delete and the flip do
    (``_as_utc``): the same instant in another zone still finds this call's
    row complete, so a landed completion is never reported absent (#2396)."""
    stamp = datetime.now(timezone.utc)
    row = _incomplete_row(created_at=stamp)
    row.ingest_complete = True
    await _insert(session_factory, row)
    ist = timezone(timedelta(hours=5, minutes=30))

    found = await _knowledge(session_factory)._delete_knowledge_item_row(
        DOC_ID, created_at=stamp.astimezone(ist)
    )

    assert found == knowledge_service._COMPLETED
    assert await _complete(session_factory) is True


async def _until_blocked_by(factory, backend: int) -> None:
    """Poll ``pg_blocking_pids`` until a session waits on ``backend``'s lock
    (a condition, not a timing: bounded only so a defect fails, not hangs)."""
    for _ in range(500):
        async with factory() as session:
            waiting = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE :pid = ANY(pg_blocking_pids(pid))"
                ),
                {"pid": backend},
            )
        if waiting:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the cleanup's DELETE never waited on the flip's lock")
