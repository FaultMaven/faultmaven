"""The KB term index sees a same-size write made by another store (#2064).

The cached index was keyed on the collection's size, so a same-size replace,
or a restamp that moves a chunk out of the platform tier, made by another
replica or by the ``kb_seed`` process was never seen. Every write now moves a
write token in the collection's metadata, and the index is keyed on
``(size, token)``.

Two ``PersistentClient``s on one ``tmp_path`` stand in for two processes. In
one process they share one chromadb System, so what they stand in for is two
store caches over one collection, which is the state the defect lives in.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import chromadb
import pytest
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings as ChromaSettings

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
    KB_WRITE_TOKEN_KEY,
    KnowledgeVectorStore,
)
from tests.chroma_clients import closed_at_teardown

pytestmark = [pytest.mark.unit]


def _client(tmp_path: Path) -> Any:
    return closed_at_teardown(
        chromadb.PersistentClient(
            path=str(tmp_path / "chroma"),
            settings=ChromaSettings(
                anonymized_telemetry=False,
                allow_reset=False,
                environment="",
                is_persistent=True,
            ),
        )
    )


def _doc(parent: str, text: str, embedding: float) -> Dict[str, Any]:
    return {
        "id": f"{parent}_chunk_0",
        "content": text,
        "metadata": {
            "scope": "global",
            "enterprise_id": STANDALONE_ENTERPRISE_ID,
            "parent_document_id": parent,
        },
        "embedding": [embedding, 0.5, 0.25],
    }


async def _add(store: KnowledgeVectorStore, docs: List[Dict[str, Any]]) -> None:
    await store.add_documents(docs, embeddings=[d.pop("embedding") for d in docs])


async def _replace(store: KnowledgeVectorStore, parent: str, text: str) -> None:
    """A same-size replace: one chunk deleted, one added."""
    await store.delete_documents_by_parent_id(parent)
    await _add(store, [_doc(parent, text, 0.9)])


def _metadata(tmp_path: Path) -> Optional[Dict[str, Any]]:
    return _client(tmp_path).get_collection(KB_COLLECTION).metadata


def _token(tmp_path: Path) -> Any:
    return (_metadata(tmp_path) or {}).get(KB_WRITE_TOKEN_KEY)


async def test_a_same_size_replace_by_another_store_is_seen(tmp_path: Path) -> None:
    """The regression. The two clients share one System, standing in for the
    caches of two stores, another replica's or the ``kb_seed`` process's."""
    a = KnowledgeVectorStore(_client(tmp_path))
    b = KnowledgeVectorStore(_client(tmp_path))
    await _add(a, [_doc("p1", "alpha disk full", 0.1)])
    built = await a._term_stats_for(KB_COLLECTION)
    assert built is not None and built.document_frequency("alpha") == 1

    await _replace(b, "p1", "omega disk full")

    stats = await a._term_stats_for(KB_COLLECTION)
    assert stats is not None
    assert stats.document_frequency("omega") == 1
    assert stats.document_frequency("alpha") == 0


async def test_a_restamp_out_of_the_platform_tier_by_another_store_is_seen(
    tmp_path: Path,
) -> None:
    a = KnowledgeVectorStore(_client(tmp_path))
    b = KnowledgeVectorStore(_client(tmp_path))
    await _add(
        a, [_doc("p1", "alpha disk full", 0.1), _doc("p2", "beta disk full", 0.2)]
    )
    built = await a._term_stats_for(KB_COLLECTION)
    assert built is not None and built.document_frequency("alpha") == 1

    restamped = await b.restamp_chunk_ids(
        ["p1_chunk_0"], "ent-1", where={"enterprise_id": STANDALONE_ENTERPRISE_ID}
    )
    assert restamped == ["p1_chunk_0"]

    stats = await a._term_stats_for(KB_COLLECTION)
    assert stats is not None
    assert stats.n_chunks == 1
    assert stats.document_frequency("alpha") == 0


async def test_a_failed_marker_write_never_fails_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = KnowledgeVectorStore(_client(tmp_path))
    warnings = MagicMock(wraps=store.logger.warning)
    monkeypatch.setattr(store.logger, "warning", warnings)
    monkeypatch.setattr(
        Collection, "modify", MagicMock(side_effect=RuntimeError("modify refused"))
    )

    await _add(store, [_doc("p1", "alpha disk full", 0.1)])

    rows = _client(tmp_path).get_collection(KB_COLLECTION).get(include=[])
    assert rows["ids"] == ["p1_chunk_0"]
    assert warnings.call_count == 1
    assert KB_COLLECTION in str(warnings.call_args)
    assert _token(tmp_path) is None


async def test_a_write_keeps_the_collections_other_metadata(tmp_path: Path) -> None:
    store = KnowledgeVectorStore(_client(tmp_path))
    await _add(store, [_doc("p1", "alpha disk full", 0.1)])
    first = _metadata(tmp_path)
    await _replace(store, "p1", "omega disk full")

    metadata = _metadata(tmp_path)
    assert metadata is not None
    assert metadata["type"] == "knowledge_base"
    assert metadata["created_at"] == first["created_at"]
    assert metadata[KB_WRITE_TOKEN_KEY] not in (None, first[KB_WRITE_TOKEN_KEY])


class _Counting:
    """Records each ChromaDB call made through a client or a collection it returned."""

    def __init__(self, target: Any, calls: List[str]) -> None:
        self._target = target
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr

        def _call(*args: Any, **kwargs: Any) -> Any:
            self._calls.append(name)
            result = attr(*args, **kwargs)
            return (
                _Counting(result, self._calls)
                if name.endswith("collection")
                else result
            )

        return _call


async def test_an_unchanged_collection_costs_the_lookup_no_extra_call(
    tmp_path: Path,
) -> None:
    calls: List[str] = []
    store = KnowledgeVectorStore(_Counting(_client(tmp_path), calls))
    await _add(store, [_doc("p1", "alpha disk full", 0.1)])
    first = await store._term_stats_for(KB_COLLECTION)

    calls.clear()
    second = await store._term_stats_for(KB_COLLECTION)

    assert second is first
    assert calls == ["get_or_create_collection", "count"]


async def test_a_write_that_raises_part_way_still_moves_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = KnowledgeVectorStore(_client(tmp_path))
    await _add(store, [_doc("p1", "alpha disk full", 0.1)])
    before = _token(tmp_path)
    delete = Collection.delete

    def _delete_then_raise(self: Collection, *args: Any, **kwargs: Any) -> None:
        delete(self, *args, **kwargs)
        raise RuntimeError("connection dropped after the delete landed")

    monkeypatch.setattr(Collection, "delete", _delete_then_raise)

    with pytest.raises(RuntimeError):
        await store.delete_documents_by_parent_id("p1", retries=0)

    assert _token(tmp_path) not in (None, before)


async def test_a_collection_with_no_metadata_gets_a_token_on_its_first_write(
    tmp_path: Path,
) -> None:
    _client(tmp_path).get_or_create_collection(KB_COLLECTION)
    assert _metadata(tmp_path) is None
    store = KnowledgeVectorStore(_client(tmp_path))
    assert await store._term_stats_for(KB_COLLECTION) is None, "empty tier"

    await _add(store, [_doc("p1", "alpha disk full", 0.1)])

    assert _token(tmp_path) is not None
    stats = await store._term_stats_for(KB_COLLECTION)
    assert stats is not None and stats.document_frequency("alpha") == 1


async def test_an_index_read_under_the_pre_write_token_is_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lookup reads the token before the documents. Another store's write
    landing between the two leaves an index built from the write's documents
    under the token from before it, and the next lookup replaces it."""
    a = KnowledgeVectorStore(_client(tmp_path))
    b = KnowledgeVectorStore(_client(tmp_path))
    await _add(a, [_doc("p1", "alpha disk full", 0.1)])
    fetch = a._get_or_create_collection
    interleaved: List[bool] = []

    def _fetch_then_let_b_write(collection_name: str) -> Any:
        collection = fetch(collection_name)
        if not interleaved:
            interleaved.append(True)
            # B on its own thread and loop, as another process would be.
            writer = threading.Thread(
                target=asyncio.run, args=(_replace(b, "p1", "omega disk full"),)
            )
            writer.start()
            writer.join()
        return collection

    monkeypatch.setattr(a, "_get_or_create_collection", _fetch_then_let_b_write)

    first = await a._term_stats_for(KB_COLLECTION)
    assert interleaved and first is not None
    assert first.document_frequency("omega") == 1, "built from the write's documents"

    second = await a._term_stats_for(KB_COLLECTION)

    assert second is not first, "an index built under the pre-write token was kept"
    assert second is not None and second.document_frequency("omega") == 1
