"""The built-in reconcile deletes the chunk ids it observed (#2017).

``_reconcile_vectors`` lists built-in parents from chunk IDS and used to delete
them by METADATA (``where parent_document_id $in``). A chunk with no
``parent_document_id`` key was listed, judged orphaned, never matched by the
delete, and reported cleaned on every boot. These tests run it over a real
ChromaDB: a parent is reported only if none of its chunks remain.
"""

from __future__ import annotations

import pytest

from faultmaven.bootstrap import kb_init
from tests import kb_orphan_sweep_scenario as sc
from tests.unit.bootstrap.test_kb_orphan_sweep import make_world

pytestmark = [pytest.mark.unit]

LEGACY = "kb_dddddddddddd"  # built-in id, no row
WITH_META = "kb_eeeeeeeeeeee"  # built-in id, no row


def _add(client, chunk_ids, *, parent_key: bool) -> None:
    collection = client.get_or_create_collection(sc.COLLECTION)
    metadatas = [
        (
            {"parent_document_id": c.rsplit("_chunk_", 1)[0], "scope": "global"}
            if parent_key
            else {"scope": "global"}
        )
        for c in chunk_ids
    ]
    collection.add(
        ids=chunk_ids,
        metadatas=metadatas,
        embeddings=[[0.2] * sc.DIM for _ in chunk_ids],
        documents=["x"] * len(chunk_ids),
    )


@pytest.fixture
async def world(tmp_path):
    w = await make_world(tmp_path, docs=(sc.BUILTIN_LIVE,))
    yield w
    await w.engine.dispose()


async def test_an_orphan_chunk_without_parent_metadata_is_deleted_and_reported(world):
    chunk = f"{LEGACY}_chunk_0"
    _add(world.client, [chunk], parent_key=False)
    assert chunk in world.present()

    result = await kb_init._reconcile_vectors(world.svc, world.factory)

    assert result.orphaned_vectors_cleaned == [LEGACY]
    assert chunk not in world.present()  # reported cleaned => actually gone


async def test_an_orphan_chunk_with_parent_metadata_is_deleted_and_reported(world):
    chunks = [f"{WITH_META}_chunk_0", f"{WITH_META}_chunk_1"]
    _add(world.client, chunks, parent_key=True)

    result = await kb_init._reconcile_vectors(world.svc, world.factory)

    assert result.orphaned_vectors_cleaned == [WITH_META]
    assert not set(chunks) & world.present()


async def test_a_live_built_ins_chunks_are_untouched(world):
    _add(world.client, [f"{LEGACY}_chunk_0"], parent_key=False)
    live = set(sc.BUILTIN_LIVE.chunk_ids())

    result = await kb_init._reconcile_vectors(world.svc, world.factory)

    assert result.orphaned_vectors_cleaned == [LEGACY]
    assert live <= world.present()


async def test_a_failed_delete_reports_nothing_cleaned(world, monkeypatch):
    chunk = f"{LEGACY}_chunk_0"
    _add(world.client, [chunk], parent_key=False)

    async def boom(*_a, **_k):
        raise RuntimeError("chroma down")

    monkeypatch.setattr(world.store, "delete_observed_chunk_ids", boom)

    result = await kb_init._reconcile_vectors(world.svc, world.factory)

    assert result.orphaned_vectors_cleaned == []
    assert chunk in world.present()


async def test_a_chunk_written_after_the_listing_keeps_its_parent_unreported(
    world, monkeypatch, caplog
):
    """The invariant is "none of its chunks remain after the pass": a chunk
    written between the listing and the delete survives an id delete, so the
    parent is not reported and the warning names it."""
    import logging

    _add(world.client, [f"{LEGACY}_chunk_0"], parent_key=False)
    real_delete = world.store.delete_observed_chunk_ids

    async def delete_after_a_write(chunk_ids, collection_name=None):
        _add(world.client, [f"{LEGACY}_chunk_9"], parent_key=False)
        return await real_delete(chunk_ids)

    monkeypatch.setattr(world.store, "delete_observed_chunk_ids", delete_after_a_write)
    with caplog.at_level(logging.WARNING, logger=kb_init.__name__):
        result = await kb_init._reconcile_vectors(world.svc, world.factory)

    assert result.orphaned_vectors_cleaned == []
    assert f"{LEGACY}_chunk_9" in world.present()
    assert any(LEGACY in r.getMessage() for r in caplog.records)
