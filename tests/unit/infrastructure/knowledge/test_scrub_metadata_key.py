"""``KnowledgeVectorStore.scrub_metadata_key`` — strip a retired key from stored chunks.

fm#1295 removed ``cause_letters`` from ``VectorMetadata``. The allowlist is
enforced on write only, so chunks indexed before the removal keep the key in the
store; this is the one-shot that removes it, selected by the key so the cost is
the stale population, not the collection.
"""

from unittest.mock import MagicMock

import pytest

from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)

pytestmark = pytest.mark.unit


class _Collection:
    def __init__(self, chunks):
        # id -> metadata
        self.chunks = dict(chunks)
        self.updates = []

    def get(self, where=None, include=None):
        key = next(iter(where))
        ids = [i for i, m in self.chunks.items() if key in m]
        return {"ids": ids, "metadatas": [self.chunks[i] for i in ids]}

    def update(self, ids, metadatas):
        self.updates.append((list(ids), list(metadatas)))
        for i, m in zip(ids, metadatas):
            self.chunks[i] = m


def _store(collection):
    client = MagicMock()
    client.get_collection.return_value = collection
    return KnowledgeVectorStore(client)


@pytest.mark.asyncio
async def test_only_chunks_carrying_the_key_are_rewritten_and_only_that_key_goes():
    col = _Collection(
        {
            "kb_a_chunk_0": {"title": "A", "cause_letters": "A,B", "scope": "global"},
            "kb_a_chunk_1": {"title": "A", "cause_letters": "", "scope": "global"},
            "kb_b_chunk_0": {"title": "B", "scope": "global"},
        }
    )
    n = await _store(col).scrub_metadata_key("cause_letters")
    assert n == 2
    assert col.chunks["kb_a_chunk_0"] == {"title": "A", "scope": "global"}
    assert col.chunks["kb_a_chunk_1"] == {"title": "A", "scope": "global"}
    assert col.chunks["kb_b_chunk_0"] == {"title": "B", "scope": "global"}
    assert len(col.updates) == 1  # one batch for a small population


@pytest.mark.asyncio
async def test_steady_state_is_zero_without_a_write():
    col = _Collection({"kb_b_chunk_0": {"title": "B", "scope": "global"}})
    assert await _store(col).scrub_metadata_key("cause_letters") == 0
    assert col.updates == []


@pytest.mark.asyncio
async def test_batches_a_large_population():
    col = _Collection({f"kb_x_chunk_{i}": {"cause_letters": "A"} for i in range(1203)})
    n = await _store(col).scrub_metadata_key("cause_letters", batch_size=500)
    assert n == 1203
    assert [len(ids) for ids, _ in col.updates] == [500, 500, 203]
    assert all("cause_letters" not in m for m in col.chunks.values())


@pytest.mark.asyncio
async def test_an_absent_collection_is_zero_not_an_error():
    from chromadb.errors import NotFoundError

    client = MagicMock()
    client.get_collection.side_effect = NotFoundError("no such collection")
    assert await KnowledgeVectorStore(client).scrub_metadata_key("cause_letters") == 0
