"""Searching a case creates no state (#1994).

``CaseVectorStore.search`` used to call ``get_or_create_collection``, so one
``case_evidence_search`` of a case that was never vectorized left an empty
``case_<id>`` collection behind, and a terminal case keeps its collections
until it is hard-deleted. A collection is born on the write path
(``add_documents``, reached by ``vectorize_file``) and nowhere else.

These tests run a REAL ChromaDB in-memory client, so the question "does a
collection exist afterwards" is answered by ChromaDB, not by a double that
records which method was called. Only the query embedding is stubbed.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings
from chromadb.errors import NotFoundError

from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore

pytestmark = [pytest.mark.unit]

_EMBEDDER = "faultmaven.infrastructure.model_cache.model_cache.aembed_query"
_DIM = 8
_VECTOR = [0.5] * _DIM


@pytest.fixture
def client():
    """A real in-process client. Its settings are pinned: chromadb caches one
    System per identifier and refuses a second client whose ``Settings``
    differ, and ``Settings.environment`` defaults to an ambient variable other
    tests set (#823). It is one shared store, so each test names its case
    uniquely and removes what it wrote."""
    return chromadb.EphemeralClient(
        settings=ChromaSettings(
            anonymized_telemetry=False,
            allow_reset=False,
            environment="",
            is_persistent=False,
        )
    )


@pytest.fixture
def case_id(client):
    case = f"case_1994{uuid.uuid4().hex[:12]}"
    yield case
    try:
        client.delete_collection(f"case_{case}")
    except Exception:  # noqa: BLE001 - nothing written: nothing to remove
        pass


def _collection_names(client) -> list[str]:
    return [c.name for c in client.list_collections()]


async def _search(store: CaseVectorStore, case_id: str, **kwargs):
    with patch(_EMBEDDER, new=AsyncMock(return_value=_VECTOR)):
        return await store.search(case_id=case_id, query="any errors?", **kwargs)


async def test_searching_a_never_vectorized_case_creates_no_collection(client, case_id):
    store = CaseVectorStore(client=client)
    assert f"case_{case_id}" not in _collection_names(client)

    results = await _search(store, case_id)

    assert results == []
    assert f"case_{case_id}" not in _collection_names(client)
    with pytest.raises(NotFoundError):
        client.get_collection(name=f"case_{case_id}")


async def test_the_collection_name_alias_reads_without_creating_too(client, case_id):
    """``DocumentQATool`` reaches the store by ``collection_name`` ("case_<id>"),
    the path ``case_evidence_search`` takes."""
    store = CaseVectorStore(client=client)

    with patch(_EMBEDDER, new=AsyncMock(return_value=_VECTOR)):
        results = await store.search(collection_name=f"case_{case_id}", query="errors")

    assert results == []
    assert f"case_{case_id}" not in _collection_names(client)


async def test_repeated_searches_still_create_nothing(client, case_id):
    store = CaseVectorStore(client=client)

    for _ in range(3):
        await _search(store, case_id)

    assert f"case_{case_id}" not in _collection_names(client)


async def test_the_write_path_creates_the_collection_and_a_search_then_reads_it(
    client, case_id
):
    """The positive control: reading a collection that exists still works, so
    the no-create assertions above are not passing on a store that cannot read."""
    store = CaseVectorStore(client=client)
    await store.add_documents(
        case_id,
        [
            {
                "id": "ev_1_chunk_0",
                "content": "ERROR upstream timeout",
                "metadata": {"filename": "app.log"},
            }
        ],
        embeddings=[_VECTOR],
    )
    assert f"case_{case_id}" in _collection_names(client)

    results = await _search(store, case_id)

    assert [r["id"] for r in results] == ["ev_1_chunk_0"]
    assert results[0]["content"] == "ERROR upstream timeout"


async def test_a_search_does_not_disturb_the_document_count(client, case_id):
    store = CaseVectorStore(client=client)

    await _search(store, case_id)

    assert await store.get_case_document_count(case_id) == 0
    assert f"case_{case_id}" not in _collection_names(client)


def test_only_an_absent_collection_reads_as_none(case_id):
    """A transport or corruption error is not "this case has no vectors": it
    propagates, as every failed search does (#943)."""
    absent = MagicMock()
    absent.get_collection.side_effect = NotFoundError("Collection does not exist")
    assert CaseVectorStore(client=absent)._get_existing_collection(case_id) is None

    broken = MagicMock()
    broken.get_collection.side_effect = ConnectionError("chroma is down")
    with pytest.raises(ConnectionError):
        CaseVectorStore(client=broken)._get_existing_collection(case_id)

    broken.get_or_create_collection.assert_not_called()
