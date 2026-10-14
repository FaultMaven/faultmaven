"""The authored-orphan sweep on SQLite, over a real ChromaDB (#1982).

SQLite has no row-level security, so its row read is always complete: these
tests pin what the sweep decides once the proof holds — per-enterprise scoping,
the listing-before-read order, the reference-set check, the cap, and the
unattributed bucket. What a PostgreSQL role's posture does to the proof is
``tests/integration/modules/knowledge/test_kb_orphan_sweep_postgres_1982.py``.

The corpus is ``tests/kb_orphan_sweep_scenario.py``: three enterprises, each
with live authored documents and one authored orphan, plus unstamped legacy
chunks, a live built-in and a built-in orphan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from faultmaven.bootstrap import kb_init, kb_orphan_sweep
from faultmaven.bootstrap.kb_orphan_sweep import sweep_authored_orphans
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.persistence.models import Base
from tests import kb_orphan_sweep_scenario as sc

pytestmark = [pytest.mark.unit]


@dataclass
class World:
    engine: AsyncEngine
    factory: Any
    client: Any
    store: KnowledgeVectorStore
    svc: Any

    async def sweep(self, **kwargs) -> kb_orphan_sweep.AuthoredSweepResult:
        kwargs.setdefault("max_parents", 25)
        return await sweep_authored_orphans(self.svc, self.factory, **kwargs)

    def present(self) -> set[str]:
        return sc.chunk_ids_present(self.client)


def chroma_client(path: Path) -> Any:
    """A ChromaDB of this test's own (a path no other test uses)."""
    return chromadb.PersistentClient(
        path=str(path),
        settings=ChromaSettings(
            anonymized_telemetry=False,
            allow_reset=False,
            environment="",
            is_persistent=True,
        ),
    )


async def make_world(tmp_path: Path, docs=sc.DOCS) -> World:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kb.sqlite'}")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await sc.seed_database(conn, docs)
    client = chroma_client(tmp_path / "chroma")
    sc.add_chunks(client, docs)
    store = KnowledgeVectorStore(client)
    return World(
        engine=engine,
        factory=async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
        client=client,
        store=store,
        svc=SimpleNamespace(_vector_store=store),
    )


@pytest.fixture
async def world(tmp_path):
    w = await make_world(tmp_path)
    yield w
    await w.engine.dispose()


def _orphan_chunks(*docs: sc.Doc) -> set[str]:
    return {cid for doc in docs for cid in doc.chunk_ids()}


ALL_AUTHORED_ORPHANS = tuple(sc.AUTHORED_ORPHANS.values()) + (sc.UNSTAMPED_ORPHAN,)


# ---------------------------------------------------------------------------
# What a proven sweep deletes
# ---------------------------------------------------------------------------


async def test_each_enterprises_orphans_go_and_every_live_chunk_stays(world):
    result = await world.sweep()

    assert result.proven is True
    assert result.skipped is None
    for enterprise_id, orphan in sc.AUTHORED_ORPHANS.items():
        assert result.enterprises[enterprise_id].deleted == [orphan.item_id]
    assert result.unattributed.deleted == [sc.UNSTAMPED_ORPHAN.item_id]
    present = world.present()
    assert sc.live_chunk_ids() <= present, sc.live_chunk_ids() - present
    assert not _orphan_chunks(*ALL_AUTHORED_ORPHANS) & present
    # Built-ins are the built-in reconcile's: the sweep never deletes one,
    # orphaned or not.
    assert set(sc.BUILTIN_ORPHAN.chunk_ids()) <= present
    # Every chunk no listed stamp attributes is counted, built-ins included:
    # two unstamped live authored, one unstamped orphan, one unstamped built-in.
    assert result.unattributed_chunks == 8
    assert result.summary()["deleted"] == 4


async def test_a_second_pass_finds_nothing(world):
    await world.sweep()
    again = await world.sweep()

    assert again.deleted == []
    assert sc.live_chunk_ids() <= world.present()


async def test_a_chunk_written_between_the_listing_and_the_row_read_survives(
    world, monkeypatch
):
    """Every create path commits the row before it writes vectors, so a chunk
    the listing saw has a row the later read sees. The document created here
    lands between the two steps — whichever runs first — and must survive.
    Reading the rows first would see the chunk and miss the row."""
    new = sc.Doc("1c000000-0000-4000-8000-000000000001", sc.ST, sc.ST)
    created: list[bool] = []

    async def create_document():
        if created:
            return
        created.append(True)
        async with world.engine.begin() as conn:
            await sc.insert_rows(conn, [new])  # the row commits first
        sc.add_chunks(world.client, [new])

    real_list = world.store.list_chunk_ids_by_parent

    async def list_then_create(where, *args, **kwargs):
        found = await real_list(where, *args, **kwargs)
        await create_document()
        return found

    real_read = kb_orphan_sweep._read_rows_if_complete

    async def read_then_create(factory):
        rows = await real_read(factory)
        await create_document()
        return rows

    monkeypatch.setattr(world.store, "list_chunk_ids_by_parent", list_then_create)
    monkeypatch.setattr(kb_orphan_sweep, "_read_rows_if_complete", read_then_create)

    result = await world.sweep()

    assert created == [True]
    assert new.item_id not in result.deleted
    assert set(new.chunk_ids()) <= world.present()
    assert result.enterprises[sc.ST].deleted == [sc.ST_ORPHAN.item_id]


async def test_a_parent_under_two_stamps_loses_only_the_orphaned_stamps_chunks(
    tmp_path,
):
    """A live document of E2 with one stray chunk stamped E1: absent from E1's
    rows, so E1's sweep deletes the E1-stamped chunk — and only that one. A
    delete by parent would take E2's live chunk with it."""
    mixed = sc.Doc("3c000000-0000-4000-8000-0000000000e2", sc.E2, sc.E2)
    world = await make_world(tmp_path, sc.DOCS + (mixed,))
    try:
        collection = world.client.get_or_create_collection(sc.COLLECTION)
        collection.update(
            ids=[f"{mixed.item_id}_chunk_0"],
            metadatas=[
                {
                    "parent_document_id": mixed.item_id,
                    "scope": "personal",
                    "enterprise_id": sc.E1,
                }
            ],
        )

        result = await world.sweep()

        present = world.present()
        assert f"{mixed.item_id}_chunk_0" not in present
        assert f"{mixed.item_id}_chunk_1" in present
        assert sorted(result.enterprises[sc.E1].deleted) == sorted(
            [sc.E1_ORPHAN.item_id, mixed.item_id]
        )
        assert result.enterprises[sc.E2].deleted == [sc.E2_ORPHAN.item_id]
    finally:
        await world.engine.dispose()


async def test_a_chunk_restamped_after_the_listing_is_not_deleted(world, monkeypatch):
    """The delete is the observed ids AND the stamp they were observed under.
    A chunk rewritten under another enterprise's stamp after the listing (its
    document re-indexed by its owner) no longer matches, and stays."""
    restamped = f"{sc.E1_ORPHAN.item_id}_chunk_0"
    real_read = kb_orphan_sweep._read_rows_if_complete

    async def read_then_restamp(factory):
        rows = await real_read(factory)
        world.client.get_or_create_collection(sc.COLLECTION).update(
            ids=[restamped],
            metadatas=[
                {
                    "parent_document_id": sc.E1_ORPHAN.item_id,
                    "scope": "personal",
                    "enterprise_id": sc.E2,
                }
            ],
        )
        return rows

    monkeypatch.setattr(kb_orphan_sweep, "_read_rows_if_complete", read_then_restamp)

    result = await world.sweep()

    present = world.present()
    assert restamped in present
    assert f"{sc.E1_ORPHAN.item_id}_chunk_1" not in present
    assert result.enterprises[sc.E1].deleted == [sc.E1_ORPHAN.item_id]


# ---------------------------------------------------------------------------
# The guards: reference set, cap, empty table
# ---------------------------------------------------------------------------


E3 = "e3000000-0000-0000-0000-00000000e003"
E3_ORPHANS = (
    sc.Doc("0f000000-0000-4000-8000-0000000000f1", None, E3),
    sc.Doc("0f000000-0000-4000-8000-0000000000f2", None, E3),
)


async def _with_disjoint_enterprise(tmp_path) -> World:
    world = await make_world(tmp_path, sc.DOCS + E3_ORPHANS)
    async with world.engine.begin() as conn:
        await sc.insert_enterprises(conn, [E3])
    return world


async def test_an_enterprise_none_of_whose_vectors_has_a_row_is_skipped(
    tmp_path, caplog
):
    """Every authored chunk stamped E3 is an orphan by the diff — the shape a
    partial row read, a keyspace change and a genuinely emptied enterprise all
    take. Not deleted without the operator's acknowledgment."""
    world = await _with_disjoint_enterprise(tmp_path)
    try:
        with caplog.at_level(logging.WARNING, logger=kb_orphan_sweep.__name__):
            result = await world.sweep()

        assert result.enterprises[E3].disjoint == 2
        assert result.enterprises[E3].deleted == []
        assert _orphan_chunks(*E3_ORPHANS) <= world.present()
        # The other enterprises are swept as usual.
        assert result.enterprises[sc.E1].deleted == [sc.E1_ORPHAN.item_id]
        assert any("NONE is referenced" in r.getMessage() for r in caplog.records)
    finally:
        await world.engine.dispose()


async def test_the_acknowledgment_lets_a_disjoint_enterprise_be_swept(tmp_path):
    world = await _with_disjoint_enterprise(tmp_path)
    try:
        result = await world.sweep(allow_disjoint_reference_set=True)

        assert sorted(result.enterprises[E3].deleted) == sorted(
            d.item_id for d in E3_ORPHANS
        )
        assert not _orphan_chunks(*E3_ORPHANS) & world.present()
        assert sc.live_chunk_ids() <= world.present()
    finally:
        await world.engine.dispose()


async def test_more_orphans_than_the_cap_deletes_none(world, caplog):
    """Four orphan documents, a cap of three: all or nothing, so nothing."""
    with caplog.at_level(logging.ERROR, logger=kb_orphan_sweep.__name__):
        result = await world.sweep(max_parents=3)

    assert result.deleted == []
    for enterprise_id in sc.AUTHORED_ORPHANS:
        assert result.enterprises[enterprise_id].capped == 1
    assert result.unattributed.capped == 1
    assert result.summary()["capped"] == 4
    assert _orphan_chunks(*ALL_AUTHORED_ORPHANS) <= world.present()
    assert any("deleting NONE" in r.getMessage() for r in caplog.records)


async def test_orphans_up_to_the_cap_are_deleted(world):
    result = await world.sweep(max_parents=4)

    assert len(result.deleted) == 4
    assert result.summary()["capped"] == 0


async def test_an_empty_table_deletes_nothing(tmp_path):
    """Chunks with no rows at all is an anomaly (an emptied or not-yet-seeded
    database beside an older index), never a signal to delete them all."""
    world = await make_world(tmp_path, docs=())
    try:
        sc.add_chunks(world.client, sc.DOCS)
        result = await world.sweep(allow_disjoint_reference_set=True)

        assert result.skipped == "no_rows"
        assert result.deleted == []
        assert _orphan_chunks(*sc.DOCS) <= world.present()
    finally:
        await world.engine.dispose()


# ---------------------------------------------------------------------------
# Unproven, degraded and failed passes
# ---------------------------------------------------------------------------


async def test_an_unproven_read_sweeps_nothing_and_says_what_it_left(
    world, monkeypatch
):
    """The branch the RLS-scoped app role takes on PostgreSQL (proved there by
    the PostgreSQL module): every enterprise reported unproven, the
    unattributed bucket too, and nothing deleted."""

    async def unproven(_factory):
        return False, {}

    monkeypatch.setattr(kb_orphan_sweep, "_read_rows_if_complete", unproven)

    result = await world.sweep()

    assert result.proven is False
    assert result.deleted == []
    assert {e: c.unproven for e, c in result.enterprises.items()} == {
        sc.ST: 3,
        sc.E1: 3,
        sc.E2: 2,
    }
    assert result.unattributed.unproven == 3
    assert _orphan_chunks(*ALL_AUTHORED_ORPHANS) <= world.present()


async def test_a_failed_delete_is_counted_and_the_rest_proceed(world, monkeypatch):
    real_delete = world.store.delete_chunk_ids

    async def delete(chunk_ids, where, *args, **kwargs):
        if where == {"enterprise_id": sc.E1}:
            raise RuntimeError("chroma unavailable")
        return await real_delete(chunk_ids, where, *args, **kwargs)

    monkeypatch.setattr(world.store, "delete_chunk_ids", delete)

    result = await world.sweep()

    assert result.enterprises[sc.E1].failed == 1
    assert result.enterprises[sc.E1].deleted == []
    assert set(sc.E1_ORPHAN.chunk_ids()) <= world.present()
    assert result.enterprises[sc.E2].deleted == [sc.E2_ORPHAN.item_id]


async def test_a_store_that_cannot_list_by_stamp_is_skipped(world):
    result = await sweep_authored_orphans(
        SimpleNamespace(_vector_store=object()), world.factory, max_parents=25
    )
    assert result.skipped == "store_cannot_enumerate"


async def test_a_listing_that_raises_leaves_everything(world, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("chroma unavailable")

    monkeypatch.setattr(world.store, "list_chunk_ids_by_parent", boom)

    result = await world.sweep()

    assert result.skipped == "error"
    assert result.deleted == []
    assert _orphan_chunks(*sc.DOCS) <= world.present()


# ---------------------------------------------------------------------------
# The store's two new calls
# ---------------------------------------------------------------------------


async def test_the_store_lists_and_deletes_only_with_a_where_clause(world):
    with pytest.raises(ValueError, match="requires a where clause"):
        await world.store.list_chunk_ids_by_parent({})
    with pytest.raises(ValueError, match="requires a where clause"):
        await world.store.delete_chunk_ids(["x_chunk_0"], {})


async def test_the_store_deletes_the_intersection_of_ids_and_stamp(world):
    """``E1_LIVE_1``'s ids under E2's stamp match nothing: nothing goes."""
    deleted = await world.store.delete_chunk_ids(
        sc.E1_LIVE_1.chunk_ids(), {"enterprise_id": sc.E2}
    )
    assert deleted == []
    assert set(sc.E1_LIVE_1.chunk_ids()) <= world.present()

    listed = await world.store.list_chunk_ids_by_parent({"enterprise_id": sc.E2})
    assert listed == {
        sc.E2_LIVE.item_id: sc.E2_LIVE.chunk_ids(),
        sc.E2_ORPHAN.item_id: sc.E2_ORPHAN.chunk_ids(),
    }


# ---------------------------------------------------------------------------
# Through the bootstrap: reported, and the built-in path as it was
# ---------------------------------------------------------------------------


async def test_the_bootstrap_sweeps_authored_orphans_beside_the_builtin_reconcile(
    tmp_path, caplog
):
    """The whole pass over SQLite and a real ChromaDB: the pack runbook is
    ingested, the built-in orphan is the reconcile's (``orphaned_vectors_
    cleaned``, unchanged), the authored orphans are the sweep's, and the
    completion line reports them."""
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )
    from tests.unit.bootstrap.test_kb_init import _write_pack

    docs = tuple(
        d for d in sc.DOCS if d not in (sc.BUILTIN_LIVE, sc.BUILTIN_UNSTAMPED_LIVE)
    )
    world = await make_world(tmp_path, docs)
    try:
        svc = KnowledgeService.__new__(KnowledgeService)
        svc._db_session_factory = world.factory
        svc._vector_store = world.store
        svc._share_repo = None
        svc._tracer = MagicMock()

        with caplog.at_level(logging.INFO, logger=kb_init.__name__):
            result = await kb_init.bootstrap_kb(
                knowledge_service=svc,
                db_session_factory=world.factory,
                enterprise_id=sc.ST,
                project_root=tmp_path,
                pack_dir=_write_pack(tmp_path),
            )

        assert result.failed == []
        assert result.ingested == ["global/example.md"]
        assert result.orphaned_vectors_cleaned == [sc.BUILTIN_ORPHAN.item_id]
        assert result.authored_orphans.deleted == sorted(
            d.item_id for d in ALL_AUTHORED_ORPHANS
        )
        assert result.orphaned_rows == []
        present = world.present()
        assert sc.live_chunk_ids(d for d in sc.LIVE if d in docs) <= present
        assert not _orphan_chunks(*ALL_AUTHORED_ORPHANS, sc.BUILTIN_ORPHAN) & present
        assert "authored_orphans_swept=4" in repr(result)
        assert any("4 authored orphans swept" in r.getMessage() for r in caplog.records)
    finally:
        await world.engine.dispose()
