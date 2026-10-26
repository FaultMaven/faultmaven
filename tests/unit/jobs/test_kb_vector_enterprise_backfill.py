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

COUNTS_BEFORE = {
    "items": 4,
    "chunks": 8,
    "already_correct": 2,
    "unstamped": 4,
    "mismatched": 2,
    "unattributable": 2,
}


@pytest.fixture
async def world(tmp_path: Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kb.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await sc.insert_enterprises(conn, ["ent-1", "ent-2"])
        await sc.insert_rows(conn, DOCS)
    client = chromadb.PersistentClient(
        path=str(tmp_path / "chroma"),
        settings=ChromaSettings(
            anonymized_telemetry=False,
            allow_reset=False,
            environment="",
            is_persistent=True,
        ),
    )
    sc.add_chunks(client, DOCS)
    # The job's row and enterprise reads go through the app's session factory.
    monkeypatch.setattr(
        database,
        "get_db_session",
        async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
    )
    yield SimpleNamespace(
        client=client,
        container=SimpleNamespace(knowledge_vector_store=KnowledgeVectorStore(client)),
    )
    await engine.dispose()


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


async def test_no_vector_store_skips():
    result = await job.run(
        settings=None, container=SimpleNamespace(knowledge_vector_store=None)
    )

    assert (result["status"], result["reason"]) == ("skipped", "no_vector_store")


async def test_no_enterprise_skips_before_any_listing(world, monkeypatch):
    """``$nin`` refuses an empty list, and no row exists without an enterprise."""

    async def none(_factory):
        return []

    monkeypatch.setattr(kb_orphan_sweep, "read_enterprise_ids", none)

    result = await job.run(settings=None, container=world.container, dry_run=False)

    assert (result["status"], result["reason"]) == ("skipped", "no_enterprises")
