"""The KB vector enterprise backfill job, end to end on SQLite and a real ChromaDB (#1777).

SQLite has no row-level security, so the row read is proven: these tests pin
what the job classifies, what a real run stores (read back from the
collection, not taken from the call's return), that a dry run writes nothing,
and that an unproven row read writes nothing and fails.

The corpus: enterprises ``ent-1`` and ``ent-2``; rows for A, B and C in ``ent-1``
and D in ``ent-2``. Chunks: A unstamped, B stamped ``ent-2`` (mismatched), C
stamped ``ent-1`` (correct), D stamped ``ent-ghost``, which no enterprise holds
(counted as unstamped), and Z unstamped with no row (unattributable).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import chromadb
import pytest
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings as ChromaSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.bootstrap import kb_orphan_sweep
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.persistence import database
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.jobs import kb_vector_enterprise_backfill as job
from tests import kb_orphan_sweep_scenario as sc

pytestmark = [pytest.mark.unit]

A = sc.Doc("item-A", "ent-1", None)
B = sc.Doc("item-B", "ent-1", "ent-2")
C = sc.Doc("item-C", "ent-1", "ent-1")
D = sc.Doc("item-D", "ent-2", "ent-ghost")
Z = sc.Doc("item-Z", None, None)
DOCS = (A, B, C, D, Z)

#: Every count the report carries.
COUNT_KEYS = (
    "items",
    "chunks",
    "already_correct",
    "unstamped",
    "mismatched",
    "stamped",
    "corrected",
    "unattributable",
    "failed",
)

COUNTS_BEFORE = {
    "items": 4,
    "chunks": 8,
    "already_correct": 2,
    "unstamped": 4,
    "mismatched": 2,
    "unattributable": 2,
}


@pytest.fixture
async def make_world(tmp_path: Path, monkeypatch):
    engines = []

    async def make(docs=DOCS):
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kb.sqlite'}")
        engines.append(engine)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await sc.insert_enterprises(conn, ["ent-1", "ent-2"])
            await sc.insert_rows(conn, docs)
        client = chromadb.PersistentClient(
            path=str(tmp_path / "chroma"),
            settings=ChromaSettings(
                anonymized_telemetry=False,
                allow_reset=False,
                environment="",
                is_persistent=True,
            ),
        )
        sc.add_chunks(client, docs)
        # The job's row and enterprise reads go through the app's session factory.
        monkeypatch.setattr(
            database,
            "get_db_session",
            async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
        )
        store = KnowledgeVectorStore(client)
        return SimpleNamespace(
            engine=engine,
            client=client,
            container=SimpleNamespace(knowledge_vector_store=store),
        )

    yield make
    for engine in engines:
        await engine.dispose()


@pytest.fixture
async def world(make_world):
    return await make_world()


def _stored(client: Any) -> Dict[str, Any]:
    """Every chunk's metadata, document and embedding, as the collection holds them."""
    got = client.get_collection(KB_COLLECTION).get(
        include=["metadatas", "documents", "embeddings"]
    )
    return {
        cid: (md, doc, [float(x) for x in emb])
        for cid, md, doc, emb in zip(
            got["ids"], got["metadatas"], got["documents"], got["embeddings"]
        )
    }


def _stamps(client: Any) -> Dict[str, Any]:
    return {cid: v[0].get("enterprise_id") for cid, v in _stored(client).items()}


def _subset(result: Dict[str, Any], keys) -> Dict[str, Any]:
    return {k: result[k] for k in keys}


async def test_a_dry_run_reports_the_classification_and_writes_nothing(world):
    before = _stored(world.client)

    result = await job.run(settings=None, container=world.container)  # flag omitted

    assert result["status"] == "completed"
    assert result["dry_run"] is True
    assert result["proven"] is True
    assert _subset(result, COUNTS_BEFORE) == COUNTS_BEFORE
    assert (result["stamped"], result["corrected"], result["failed"]) == (0, 0, 0)
    assert _stored(world.client) == before


async def test_a_real_run_stores_each_rows_enterprise_and_a_dry_run_then_finds_none(
    world,
):
    before = _stored(world.client)

    result = await job.run(settings=None, container=world.container, dry_run=False)

    assert result["status"] == "completed"
    assert result["dry_run"] is False
    assert _subset(result, COUNTS_BEFORE) == COUNTS_BEFORE
    assert (result["stamped"], result["corrected"], result["failed"]) == (4, 2, 0)
    stamps = _stamps(world.client)
    for doc, expected in ((A, "ent-1"), (B, "ent-1"), (C, "ent-1"), (D, "ent-2")):
        assert {stamps[cid] for cid in doc.chunk_ids()} == {expected}, doc.item_id
    # The orphan is never written.
    assert {stamps[cid] for cid in Z.chunk_ids()} == {None}
    after = _stored(world.client)
    for cid, (md, document, embedding) in before.items():
        old_rest = {k: v for k, v in md.items() if k != "enterprise_id"}
        new_rest = {k: v for k, v in after[cid][0].items() if k != "enterprise_id"}
        assert new_rest == old_rest, cid
        assert after[cid][1:] == (document, embedding), cid

    again = await job.run(settings=None, container=world.container, dry_run=True)

    assert again["status"] == "completed"
    assert (again["unstamped"], again["mismatched"]) == (0, 0)
    assert again["already_correct"] == 8
    assert again["unattributable"] == 2


async def test_an_unproven_row_read_writes_nothing_and_fails(world, monkeypatch):
    real_read = kb_orphan_sweep.read_rows_if_complete

    async def unproven(factory):
        # The rows themselves, so a job that ignored ``proven`` would write.
        _, rows, _ = await real_read(factory)
        return False, rows, kb_orphan_sweep.UNPROVEN_FILTERED

    monkeypatch.setattr(kb_orphan_sweep, "read_rows_if_complete", unproven)
    before = _stored(world.client)

    result = await job.run(settings=None, container=world.container, dry_run=False)

    assert result["status"] == "failed"
    assert result["proven"] is False
    assert result["reason"] == kb_orphan_sweep.UNPROVEN_FILTERED
    assert (result["stamped"], result["corrected"]) == (0, 0)
    assert _stored(world.client) == before


async def test_a_document_whose_write_raises_is_counted_and_the_rest_are_written(
    world, monkeypatch
):
    store = world.container.knowledge_vector_store
    real = store.restamp_chunk_ids

    async def restamp(ids, enterprise_id, where):
        if ids[0].startswith(B.item_id):
            raise RuntimeError("chroma unavailable")
        return await real(ids, enterprise_id, where)

    monkeypatch.setattr(store, "restamp_chunk_ids", restamp)

    result = await job.run(settings=None, container=world.container, dry_run=False)

    assert result["status"] == "failed"
    assert (result["stamped"], result["corrected"], result["failed"]) == (4, 0, 2)
    stamps = _stamps(world.client)
    assert {stamps[cid] for cid in B.chunk_ids()} == {"ent-2"}
    assert {stamps[cid] for cid in A.chunk_ids()} == {"ent-1"}
    assert {stamps[cid] for cid in D.chunk_ids()} == {"ent-2"}


async def test_no_vector_store_skips_and_reports_no_count():
    """A run that examined nothing must not read as a pass: no count is 0."""
    result = await job.run(
        settings=None, container=SimpleNamespace(knowledge_vector_store=None)
    )

    assert (result["status"], result["reason"]) == ("skipped", "no_vector_store")
    assert {key: result[key] for key in COUNT_KEYS} == dict.fromkeys(COUNT_KEYS)


async def test_no_enterprise_skips_before_any_listing_and_reports_no_count(
    world, monkeypatch
):
    """``$nin`` refuses an empty list, and no row exists without an enterprise."""

    async def none(_factory):
        return []

    monkeypatch.setattr(kb_orphan_sweep, "read_enterprise_ids", none)

    result = await job.run(settings=None, container=world.container, dry_run=False)

    assert (result["status"], result["reason"]) == ("skipped", "no_enterprises")
    assert {key: result[key] for key in COUNT_KEYS} == dict.fromkeys(COUNT_KEYS)


async def test_a_write_that_landed_and_then_raised_is_counted_as_stamped(
    world, monkeypatch
):
    """The retry's guarded get finds A's chunks already restamped and updates
    nothing; they still count, because they carry the row's enterprise."""
    real_update = Collection.update
    raised: list[bool] = []

    def land_then_raise(self, **kwargs):
        real_update(self, **kwargs)
        if kwargs["ids"][0].startswith(A.item_id) and not raised:
            raised.append(True)
            raise ConnectionError("the response was lost after the write landed")

    monkeypatch.setattr(Collection, "update", land_then_raise)

    result = await job.run(settings=None, container=world.container, dry_run=False)

    assert raised == [True]
    assert result["status"] == "completed"
    assert (result["stamped"], result["corrected"], result["failed"]) == (4, 2, 0)
    stamps = _stamps(world.client)
    assert {stamps[cid] for cid in A.chunk_ids()} == {"ent-1"}


async def test_a_parent_with_an_unstamped_and_a_mismatched_chunk_ends_on_its_row(
    make_world, monkeypatch
):
    """Two listings name the same parent, so it takes two writes, each under the
    ``where`` its chunk was listed under."""
    w = await make_world(docs=())
    async with w.engine.begin() as conn:
        await sc.insert_rows(conn, [sc.Doc("item-M", "ent-1", None)])
    w.client.get_or_create_collection(KB_COLLECTION).add(
        ids=["item-M_chunk_0", "item-M_chunk_1"],
        metadatas=[
            {"parent_document_id": "item-M", "scope": "personal"},
            {
                "parent_document_id": "item-M",
                "scope": "personal",
                "enterprise_id": "ent-2",
            },
        ],
        embeddings=[[0.1] * sc.DIM, [0.2] * sc.DIM],
        documents=["m0", "m1"],
    )
    store = w.container.knowledge_vector_store
    real = store.restamp_chunk_ids
    calls: list[tuple] = []

    async def spy(ids, enterprise_id, where):
        calls.append((tuple(ids), enterprise_id, where))
        return await real(ids, enterprise_id, where)

    monkeypatch.setattr(store, "restamp_chunk_ids", spy)

    result = await job.run(settings=None, container=w.container, dry_run=False)

    assert sorted(calls, key=str) == sorted(
        [
            (("item-M_chunk_1",), "ent-1", {"enterprise_id": "ent-2"}),
            (
                ("item-M_chunk_0",),
                "ent-1",
                {"enterprise_id": {"$nin": ["ent-1", "ent-2"]}},
            ),
        ],
        key=str,
    )
    assert _stamps(w.client) == {"item-M_chunk_0": "ent-1", "item-M_chunk_1": "ent-1"}
    assert (result["unstamped"], result["mismatched"]) == (1, 1)
    assert (result["stamped"], result["corrected"], result["status"]) == (
        1,
        1,
        "completed",
    )
