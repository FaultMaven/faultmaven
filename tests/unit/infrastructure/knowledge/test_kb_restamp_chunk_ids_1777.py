"""``KnowledgeVectorStore.restamp_chunk_ids`` and the ChromaDB behaviour it rests on (#1777).

The enterprise-stamp backfill (``faultmaven/jobs/kb_vector_enterprise_backfill.py``)
lists chunks id-only per stamp, then rewrites each one's ``enterprise_id`` with
``collection.update``. Three behaviours of the pinned chromadb make that sound,
and each is pinned here as a dependency of the backfill:

- ``update`` MERGES metadata: the other keys, the document and the embedding
  survive a write of ``{"enterprise_id": ...}`` alone;
- ``$ne`` / ``$nin`` match a chunk MISSING the key, so the unattributed listing
  (``$nin`` every enterprise) finds the unstamped chunks;
- ``$eq`` / ``$in`` never match one, so a chunk listed under an enterprise's
  stamp really carries it.

All on an in-process ``PersistentClient`` under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import chromadb
import pytest
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings as ChromaSettings

from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
    KnowledgeVectorStore,
)

pytestmark = [pytest.mark.unit]

UNKNOWN = {"enterprise_id": {"$nin": ["ent-1", "ent-2"]}}


def _client(tmp_path: Path) -> Any:
    client = chromadb.PersistentClient(
        path=str(tmp_path / "chroma"),
        settings=ChromaSettings(
            anonymized_telemetry=False,
            allow_reset=False,
            environment="",
            is_persistent=True,
        ),
    )
    client.get_or_create_collection(KB_COLLECTION).add(
        ids=["A_chunk_0", "A_chunk_1", "B_chunk_0"],
        metadatas=[
            {"parent_document_id": "A", "scope": "personal"},
            {"parent_document_id": "A", "scope": "personal"},
            {"parent_document_id": "B", "scope": "personal", "enterprise_id": "ent-1"},
        ],
        embeddings=[[0.1] * 4, [0.2] * 4, [0.3] * 4],
        documents=["a0", "a1", "b0"],
    )
    return client


def _collection(tmp_path: Path) -> Any:
    return _client(tmp_path).get_collection(KB_COLLECTION)


def _ids(collection: Any, where: dict) -> list[str]:
    return sorted(collection.get(where=where, include=[])["ids"])


def _stamp(collection: Any, chunk_id: str) -> Any:
    got = collection.get(ids=[chunk_id], include=["metadatas"])
    return got["metadatas"][0].get("enterprise_id") if got["ids"] else "<absent>"


# ---------------------------------------------------------------------------
# The pins: what the backfill depends on in the pinned chromadb
# ---------------------------------------------------------------------------


def test_update_merges_metadata_so_the_backfill_keeps_every_other_key(tmp_path):
    collection = _collection(tmp_path)

    collection.update(ids=["A_chunk_0"], metadatas=[{"enterprise_id": "ent-1"}])

    got = collection.get(
        ids=["A_chunk_0"], include=["metadatas", "documents", "embeddings"]
    )
    assert got["metadatas"][0] == {
        "parent_document_id": "A",
        "scope": "personal",
        "enterprise_id": "ent-1",
    }
    assert got["documents"] == ["a0"]
    assert [round(float(x), 6) for x in got["embeddings"][0]] == [0.1] * 4


def test_ne_and_nin_match_a_chunk_missing_the_key_so_unstamped_chunks_are_listed(
    tmp_path,
):
    collection = _collection(tmp_path)

    assert _ids(collection, {"enterprise_id": {"$ne": "ent-1"}}) == [
        "A_chunk_0",
        "A_chunk_1",
    ]
    assert _ids(collection, {"enterprise_id": {"$nin": ["ent-1", "ent-2"]}}) == [
        "A_chunk_0",
        "A_chunk_1",
    ]


def test_eq_and_in_never_match_a_chunk_missing_the_key_so_a_stamp_listing_is_exact(
    tmp_path,
):
    collection = _collection(tmp_path)

    assert _ids(collection, {"enterprise_id": "ent-1"}) == ["B_chunk_0"]
    assert _ids(collection, {"enterprise_id": {"$in": ["ent-1"]}}) == ["B_chunk_0"]


# ---------------------------------------------------------------------------
# restamp_chunk_ids
# ---------------------------------------------------------------------------


async def test_restamp_writes_only_the_observed_ids_still_matching_where(tmp_path):
    client = _client(tmp_path)
    collection = client.get_collection(KB_COLLECTION)
    store = KnowledgeVectorStore(client)
    listed = await store.list_chunk_ids_by_parent(UNKNOWN)
    # After the listing, someone else stamps one chunk with another enterprise.
    collection.update(ids=["A_chunk_1"], metadatas=[{"enterprise_id": "ent-2"}])

    written = await store.restamp_chunk_ids(listed["A"], "ent-1", UNKNOWN)

    assert written == ["A_chunk_0"]
    assert _stamp(collection, "A_chunk_0") == "ent-1"
    assert _stamp(collection, "A_chunk_1") == "ent-2"
    # A chunk the caller did not name is not written, whatever it matches.
    assert _stamp(collection, "B_chunk_0") == "ent-1"


async def test_restamp_skips_a_vanished_id_and_does_not_create_it(tmp_path):
    client = _client(tmp_path)
    collection = client.get_collection(KB_COLLECTION)
    store = KnowledgeVectorStore(client)
    listed = await store.list_chunk_ids_by_parent(UNKNOWN)
    collection.delete(ids=["A_chunk_0"])

    written = await store.restamp_chunk_ids(listed["A"], "ent-1", UNKNOWN)

    assert written == ["A_chunk_1"]
    assert _stamp(collection, "A_chunk_0") == "<absent>"
    assert _stamp(collection, "A_chunk_1") == "ent-1"


async def test_a_retry_after_a_write_that_landed_returns_the_ids_now_stamped(
    tmp_path, monkeypatch
):
    """The write lands and its response is lost; ``call_external`` retries,
    and the retry's guarded get finds nothing still matching ``where``. The
    ids carry the enterprise, so they are returned."""
    client = _client(tmp_path)
    real_update = Collection.update
    raised: list[bool] = []

    def land_then_raise(self, **kwargs):
        real_update(self, **kwargs)
        if not raised:
            raised.append(True)
            raise ConnectionError("the response was lost after the write landed")

    monkeypatch.setattr(Collection, "update", land_then_raise)
    store = KnowledgeVectorStore(client)

    written = await store.restamp_chunk_ids(
        ["A_chunk_0", "A_chunk_1"], "ent-1", UNKNOWN
    )

    assert raised == [True]
    assert sorted(written) == ["A_chunk_0", "A_chunk_1"]


async def test_an_id_deleted_between_the_get_and_the_update_is_not_returned(
    tmp_path, monkeypatch
):
    client = _client(tmp_path)
    real_update = Collection.update

    def delete_then_update(self, **kwargs):
        self.delete(ids=["A_chunk_0"])
        real_update(self, **kwargs)

    monkeypatch.setattr(Collection, "update", delete_then_update)
    store = KnowledgeVectorStore(client)

    written = await store.restamp_chunk_ids(
        ["A_chunk_0", "A_chunk_1"], "ent-1", UNKNOWN
    )

    assert written == ["A_chunk_1"]
    collection = client.get_collection(KB_COLLECTION)
    assert _stamp(collection, "A_chunk_0") == "<absent>"
    assert _stamp(collection, "A_chunk_1") == "ent-1"


@pytest.mark.parametrize(
    "enterprise_id,where",
    [("ent-1", {}), ("", UNKNOWN), ("   ", UNKNOWN)],
    ids=["empty-where", "empty-enterprise", "blank-enterprise"],
)
async def test_restamp_refuses_an_empty_where_or_a_blank_enterprise(
    tmp_path, enterprise_id, where
):
    client = _client(tmp_path)
    collection = client.get_collection(KB_COLLECTION)
    store = KnowledgeVectorStore(client)

    with pytest.raises(ValueError):
        await store.restamp_chunk_ids(["A_chunk_0"], enterprise_id, where)

    assert _stamp(collection, "A_chunk_0") is None
